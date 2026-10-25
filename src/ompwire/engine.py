"""The platform-neutral agent engine: one ChatAgent per conversation.

This module owns everything a turn needs that is not platform I/O: the ACP subprocess and its
session, the drain of updates into traces, the pacing of outgoing messages, permission asking,
schedules, files and usage. A platform implements `Surface` and gets a working agent; nothing
here knows Telegram.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
import os
import re
import shlex
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

from . import allowlist, gateways, omp_sessions
from . import schedules as schedule_lib
from .acp import AcpClient, AcpError, AcpTimeout, pick_permission_option
from .acp import Update as AcpUpdate
from .attachments import extract as extract_files
from .attachments import hide_partial as hide_file_partial
from .config import Config, approval_policy
from .format import humanize_token_report, tokens, wib_time
from .mcp_hub import hub_servers, normalize
from .mcp_hub import merge_entry as merge_mcp_entry
from .mdrender import strip_agent_noise
from .omp_sessions import plausible_context
from .questions import extract as extract_options
from .questions import hide_partial
from .store import SessionStore

log = logging.getLogger("ompwire.engine")

REASONING_CHARS = 1200  # the tail matters; the beginning usually restates the question
PERMISSION_TIMEOUT = 120.0
# One more try after a restart is worth it; a turn that keeps dying with the gateway is the cause.
RESUME_LIMIT = 2
RESUME_PROMPT = (
    "The chat gateway restarted while you were working on the request below, and your previous "
    "turn was cut off. Everything you did before the cut is in this session's history. Check "
    "what is already done, do not repeat finished steps, and finish the request.\n\n"
    "Original request:\n{prompt}"
)
HARNESS_SECRETS = ("TELEGRAM_BOT_TOKEN", "HONCHO_API_KEY", "OMPWIRE_HONCHO_API_KEY")


def child_env() -> dict[str, str]:
    """The agent inherits the environment without the credentials this harness runs on."""
    env = dict(os.environ)
    for name in HARNESS_SECRETS:
        env.pop(name, None)
    return env


def _acp_args(config: Config) -> list[str]:
    """`[acp] args`, accepting the one-string form the environment override produces."""
    raw = config.get("acp", "args") or ["--mode", "acp"]
    if isinstance(raw, str):
        return shlex.split(raw)
    return [str(part) for part in raw]


def resume_plan(store_path: str, prefix: str) -> tuple[list[tuple[str, str, int]],
                                                       list[tuple[str, str, int]]]:
    """Interrupted turns of one transport, split into (resume, drop).

    A transport owns its keys by prefix, so a gateway never picks up another one's turn. A
    turn already resumed RESUME_LIMIT times died with the gateway that many times: it is
    dropped instead of looping.
    """
    store = SessionStore(store_path)
    try:
        rows = store.interrupted(prefix)
    finally:
        store.close()
    resume = [row for row in rows if row[2] < RESUME_LIMIT]
    drop = [row for row in rows if row[2] >= RESUME_LIMIT]
    return resume, drop


WAITING_RE = re.compile(r"^(waiting for|menunggu)\b", re.IGNORECASE)


def _task_brief(text: str) -> str:
    """The first meaningful line of a subagent brief.

    omp sends the brief as markdown that opens with a heading ("# Target"), which says nothing on
    its own. Drop the headings and keep the first line that carries the assignment.
    """
    for line in str(text or "").splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#") or clean.startswith("```"):
            continue  # headings and fences label the brief, they are not the brief
        return " ".join(clean.split())[:120]
    return ""


def _is_waiting(title: str) -> bool:
    """A call that only says it is waiting adds a line and no information."""
    return bool(WAITING_RE.match(title.strip()))

def _chunks(text: str, limit: int) -> list[str]:
    out: list[str] = []
    while text:
        out.append(text[:limit])
        text = text[limit:]
    return out or [""]


def resolve_mcp_servers(config: Any) -> list[dict[str, Any]]:
    """The servers a session is opened with.

    One resolver, because the banner lives on a different class than the session: a copy of this
    logic in the banner reported "none" while the hub was supplying twenty-four servers.
    """
    source = str(config.get("mcp", "source", "inline") or "inline").lower()
    if source == "none":
        return []
    if source == "mcp-hub":
        entries = hub_servers(
            config.get("mcp", "hub_path", "~/mcp-hub/servers.yaml"),
            include=config.get("mcp", "include", []) or [],
            exclude=config.get("mcp", "exclude", []) or [],
        )
        overrides = config.get("mcp", "overrides", {}) or {}
        for entry in entries:
            patch = overrides.get(entry.get("name")) or {}
            merge_mcp_entry(entry, patch)
        return normalize(entries)
    return normalize(config.get("mcp", "servers", []) or [])


MCP_LABEL_MAX = 6


def mcp_label(names: Iterable[str]) -> str:
    """How the banner names the MCP servers: the first few, then a count of the rest."""
    names = [str(name) for name in names]
    if not names:
        return "none"
    shown = ", ".join(names[:MCP_LABEL_MAX])
    if len(names) > MCP_LABEL_MAX:
        shown += f" +{len(names) - MCP_LABEL_MAX}"
    return shown


def context_line(used: int, size: int) -> str:
    """One readable row for /status and /usage, e.g. "124k / 1M (12%)"."""
    if size <= 0:
        return "unknown"
    return f"{tokens(used)} / {tokens(size)} ({used * 100 // size}%)"


# Choices that belong to the chat, not to one ACP session: /new must not reset them.
PREF_KEYS = ("model", "mode", "thinking")


@dataclass
class ToolTrace:
    """One tool call as the turn saw it, ready for the surface to render."""

    kind: str
    title: str = ""
    command: str = ""
    output: str = ""
    status: str = ""
    subagent: str = ""
    steps: list[str] = field(default_factory=list)
    brief: str = ""


class FloodWait(Exception):
    """The platform is rate limiting; `window` is how long to wait before sending again."""

    def __init__(self, window: float) -> None:
        super().__init__(f"flood control, wait {window:.0f}s")
        self.window = window


class Surface(abc.ABC):
    """What a conversation platform implements for the engine.

    `post` sends one message: kind "text" carries raw agent markdown the surface converts,
    the other kinds arrive already rendered by this surface. `trace`, `reasoning`, `dropped`
    and `join` are the renderers the engine calls before posting.
    """

    label: str
    limit: int
    # A surface without a rate limit (a terminal) can take the answer as it is written instead
    # of one message per segment. The engine then calls stream() with each piece of text and
    # does not post the segment again. A surface that sets it must implement both stream() and
    # end_stream().
    streams: bool = False

    async def stream(self, delta: str) -> None:
        """Show a piece of answer text as it arrives; only called when `streams` is set."""
        raise NotImplementedError

    async def end_stream(self) -> None:
        """The streamed text stops here: a tool call or the end of the turn follows."""
        raise NotImplementedError

    @abc.abstractmethod
    async def post(self, body: str, *, kind: str) -> Any | None:
        """Send one message; raises FloodWait on rate limit, returns None on other failures."""

    @abc.abstractmethod
    def trace(self, t: ToolTrace) -> str: ...

    @abc.abstractmethod
    def reasoning(self, text: str) -> str: ...

    @abc.abstractmethod
    def dropped(self, count: int) -> str: ...

    @abc.abstractmethod
    def join(self, blocks: list[str]) -> str: ...

    @abc.abstractmethod
    def typing(self): ...  # async context manager

    @abc.abstractmethod
    async def offer_options(self, options: list[str]) -> None: ...

    async def retire_options(self, note: str) -> None:
        """The open question was answered; surfaces with buttons take them away."""
        return None

    @abc.abstractmethod
    async def ask_permission(self, token: str, kind: str, detail: str,
                             labels: list[str]) -> Any: ...

    @abc.abstractmethod
    async def settle_permission(self, handle: Any, kind: str, detail: str, outcome: str,
                                expired: bool) -> None: ...

    @abc.abstractmethod
    async def send_file(self, path, as_photo: bool) -> None: ...

    @abc.abstractmethod
    async def offer_schedule(self, name: str, expression: str, gateway: str,
                             prompt: str) -> None: ...

    @abc.abstractmethod
    async def notice(self, text: str) -> Any | None: ...

class ChatAgent:
    """One chat's ACP agent: subprocess, session id, queue and counters."""

    def __init__(self, config: Config, key: str, surface: Surface) -> None:
        self.config = config
        self.key = key
        self.surface = surface
        self.label = surface.label
        self.store = SessionStore(config.get("session", "store"))
        self.client: AcpClient | None = None
        self.session_id = ""
        # /cwd is stored with the session; without reading it back a restart silently moved the
        # chat to the configured directory while still loading the session from the other one
        stored = self.store.get(key)
        configured = str(config.get("agent", "cwd") or "~")
        kept = stored.cwd if stored is not None and stored.cwd else ""
        # a directory removed since then would make every turn fail to start; fall back
        self.cwd = kept if kept and os.path.isdir(os.path.expanduser(kept)) else configured
        self.reasoning = bool(config.get("stream", "show_reasoning", False))
        # the bot that receives a scheduled result defaults to the bot being talked to
        self.gateway_name = str(config.get("_gateway", "name", "telegram") or "telegram")
        self.pending_schedules: dict[str, schedule_lib.Proposal] = {}
        self.started_at = time.time()
        self.pending_options: list[str] = []
        # when this chat last got a message; the pacing gap is measured from it across turns
        self._last_post = 0.0
        self.last_used = time.time()
        self.busy = False
        self._user_cancel = False
        self.verbose = bool(config.get("stream", "show_tool_calls", True))
        # yolo survives /new: it is stored per chat, like model/mode/thinking.
        # With no stored choice the config decides, so setup can ship yolo on.
        stored_yolo = self.store.prefs(key).get("yolo")
        if stored_yolo is None:
            self.yolo = bool(config.get("agent", "yolo", False))
        else:
            self.yolo = stored_yolo == "on"
        self.turns = 0
        self.tools_used = 0
        self.options: dict[str, dict[str, Any]] = {}
        self.info: dict[str, Any] = {}
        self.agent_version = ""
        self.advertised_commands: list[str] = []
        # context window from usage_update; the size is remembered per chat, because after a
        # restart /usage is asked before omp has sent its first report
        self.context = {"used": 0, "size": int(self.store.prefs(key).get("context_size") or 0)}
        # compactions already in the transcript; a higher count after a turn is a new one
        self._compactions_seen: int | None = None
        # warning fires once per crossing and re-arms when the context drops back below it
        self._context_warned = False
        # a /compact prompt reports its own before/after; the notice would repeat it
        self._compact_turn = False
        # last context report that looked real; outlives a process restart, not a session change
        self._context_good: int | None = None
        self._updates: asyncio.Queue[AcpUpdate | None] = asyncio.Queue()
        self._pending_perm: dict[str, dict[str, Any]] = {}
        self._loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup
    def _build_client(self, yolo: bool = False) -> AcpClient:
        args = _acp_args(self.config)
        approval = "yolo" if (yolo or self.yolo) else str(self.config.get("agent", "approval_mode", ""))
        hint = str(self.config.get("agent", "append_system_prompt", "") or "").strip()
        from .config import PROTOCOL_HINT

        # always: the markers are how a reply carries a file or a question, not a preference
        hint = f"{hint}\n\n{PROTOCOL_HINT}".strip()
        if approval:
            args.append(f"--approval-mode={approval}")
        if hint:
            args += ["--append-system-prompt", hint]
        return AcpClient(
            command=str(self.config.get("acp", "command", "omp")),
            args=args,
            cwd=self.cwd,
            env=child_env(),
            on_update=self._on_update,
            on_permission=self._on_permission,
            startup_timeout=float(self.config.get("acp", "startup_timeout", 60.0)),
        )

    async def ensure(self, yolo: bool = False) -> None:
        if self.client is not None and self.client.alive:
            return
        self.client = self._build_client(yolo=yolo)
        info = await asyncio.to_thread(self.client.initialize)
        agent = info.get("agentInfo") or {}
        self.agent_version = str(agent.get("version") or "")
        self.info = info
        log.info("agent up for %s: %s %s (approval=%s, mcp=%d)", self.label,
                 agent.get("name"), agent.get("version"),
                 "yolo" if self.yolo else self.config.get("agent", "approval_mode", "write"),
                 len(self._mcp_servers()))
        record = self.store.get(self.key)
        previous = self.client.on_update
        self.client.on_update = None  # session/load replays the transcript
        try:
            if record is not None:
                try:
                    result = await asyncio.to_thread(
                        self.client.load_session, record.session_id, self.cwd, self._mcp_servers()
                    )
                    self.session_id = record.session_id
                except AcpError:
                    result = await asyncio.to_thread(self.client.new_session, self.cwd, self._mcp_servers())
                    self.session_id = str(result.get("sessionId") or "")
            else:
                result = await asyncio.to_thread(self.client.new_session, self.cwd, self._mcp_servers())
                self.session_id = str(result.get("sessionId") or "")
        except AcpError as exc:
            if not self._mcp_servers():
                raise
            log.warning("session/new with mcp servers failed (%s); retrying without them", exc)
            result = await asyncio.to_thread(self.client.new_session, self.cwd, [])
            self.session_id = str(result.get("sessionId") or "")
        finally:
            self.client.on_update = previous
        self.options = {opt.get("id"): opt for opt in (result.get("configOptions") or [])}
        self.store.put(self.key, self.session_id, self.cwd)
        self._seed_context()
        await self._apply_preferences()

    def _seed_context(self) -> None:
        """The first usage_update after a restart can be an unanchored sliver; the transcript
        knows what the last real call carried, so that is the reference to hold it against."""
        seen = self._transcript()
        if seen is None:
            return
        if self._compactions_seen is None:
            self._compactions_seen = len(seen.compactions)
        if not self._context_good and seen.context:
            self._context_good = seen.context
            self.context["used"] = seen.context

    def _transcript(self) -> omp_sessions.SessionUsage | None:
        try:
            return omp_sessions.usage(self.session_id)
        except OSError:
            return None

    async def _report_compaction(self) -> None:
        """omp logs each compaction in its transcript, and that log is the only place it shows:
        no event crosses ACP, and the context number alone cannot tell a compaction apart from a
        report omp could not anchor. A new entry since the last turn gets one notice."""
        seen = self._transcript()
        if seen is None:
            return
        known, self._compactions_seen = self._compactions_seen, len(seen.compactions)
        if known is None or len(seen.compactions) <= known or self._compact_turn:
            return
        latest = seen.compactions[-1]
        if latest.after:
            # the size right after compaction is the honest number until the next call reports
            self._context_good = latest.after
        await self._context_notice(f"🗜️ omp compacted the context at {wib_time(latest.at)} · "
                                   f"{tokens(latest.before)} → {tokens(latest.after)}")

    def _mcp_servers(self) -> list[dict[str, Any]]:
        return resolve_mcp_servers(self.config)

    def prefs(self) -> dict[str, str]:
        """Per-chat choices that must outlive a session (/new used to reset them)."""
        return self.store.prefs(self.key)

    async def _apply_preferences(self) -> None:
        stored = self.prefs()
        for config_id in PREF_KEYS:
            value = stored.get(config_id) or str(self.config.get("agent", config_id, "") or "")
            if not value or self.client is None:
                continue
            try:
                result = await asyncio.to_thread(
                    self.client.set_config_option, self.session_id, config_id, value
                )
                # the reply carries the authoritative option set; without this the
                # pickers keep showing the previous value after a restart or /new
                updated = {opt.get("id"): opt for opt in (result.get("configOptions") or [])}
                if updated:
                    self.options = updated
                log.info("preference applied %s=%s (session=%s, source=%s, effective=%s)",
                         config_id, value, self.session_id[:8],
                         "store" if stored.get(config_id) else "config",
                         (self.options.get(config_id) or {}).get("currentValue"))
            except AcpError as exc:
                log.warning("set %s=%s failed: %s", config_id, value, exc)

    # ------------------------------------------------------- thread -> loop
    def _on_update(self, update: AcpUpdate) -> None:
        if update.kind == "available_commands_update":
            names = [f"/{c.get('name')}" for c in (update.raw.get("availableCommands") or [])]
            self._loop.call_soon_threadsafe(self.advertised_commands.__setitem__, slice(None), names)
            return
        if update.kind == "session_info_update":
            return
        if update.kind == "usage_update":
            raw = update.raw
            try:
                used = int(raw.get("used") or 0)
                size = int(raw.get("size") or 0)
            except (TypeError, ValueError):
                return
            self._loop.call_soon_threadsafe(self._on_usage, used, size)
            return
        self._loop.call_soon_threadsafe(self._updates.put_nowait, update)

    def _on_usage(self, used: int, size: int) -> None:
        """Runs on the loop thread. Tracks the context window, the warning crossing and
        compaction drops; usage_update never enters the turn queue."""
        good = self._context_good
        if good and not self._compact_turn and not plausible_context(used, good):
            # omp anchors the number on the last assistant message; a turn resumed after a
            # restart can anchor on ~400 tokens against a 700k context. Keep the last good value.
            log.info("context report %s ignored (last good %s) chat=%s", used, good, self.label)
            return
        self._context_good = used
        if size > 0 and size != self.context["size"]:
            self.store.set_pref(self.key, "context_size", str(size))
        self.context["used"], self.context["size"] = used, size
        if size > 0:
            warn_at = float(self.config.get("stream", "compaction_warn", 0.6))
            if used / size >= warn_at:
                if not self._context_warned:
                    self._context_warned = True
                    self._loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(self._context_notice(self._warning_text(used, size))))
            else:
                self._context_warned = False

    def _warning_text(self, used: int, size: int) -> str:
        from .omp_config import read_compaction
        threshold = read_compaction().get("thresholdPercent")
        line = context_line(used, size)
        pct = used * 100 // size
        if isinstance(threshold, int):
            return f"⚠️ context at {pct}% · omp compacts at {threshold}%"
        return f"⚠️ context at {pct}% ({line})"

    async def _context_notice(self, text: str) -> None:
        """A notice outside a turn: surface.notice goes straight to the platform, so it is
        safe to call while ask() is not running."""
        try:
            await self.surface.notice(text)
        except Exception:
            log.warning("context notice failed chat=%s", self.label, exc_info=True)

    def _on_permission(self, params: dict[str, Any]) -> str | None:
        policy = approval_policy(self.config)
        title = params.get("title") or (params.get("toolCall") or {}).get("title") or "tool call"
        if policy in {"allow", "deny"}:
            log.info("permission auto-%s chat=%s tool=%s", policy, self.label, title)
            return pick_permission_option(params, policy)
        # A read-only command is not worth a button. Asking for `git log` teaches the habit of
        # tapping Allow without reading, which is exactly what the gate exists to prevent.
        command = str(((params.get("toolCall") or {}).get("rawInput") or {}).get("command") or "")
        if command and allowlist.is_read_only(command):
            log.info("permission auto-allow chat=%s (read-only): %s", self.label, command[:120])
            return pick_permission_option(params, "allow")
        token = f"perm-{uuid.uuid4().hex[:12]}"  # two requests can land in the same millisecond
        slot: dict[str, Any] = {"optionId": None, "params": params}
        self._pending_perm[token] = slot
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._ask_permission(token, params)))
        log.info("permission requested chat=%s tool=%s options=%s", self.label, title,
                 [o.get("kind") for o in (params.get("options") or [])])
        deadline = time.time() + float(self.config.get("approval", "timeout", PERMISSION_TIMEOUT))
        while time.time() < deadline:
            if slot.get("optionId") is not None:
                self._pending_perm.pop(token, None)
                return slot["optionId"]
            time.sleep(0.2)
        log.warning("permission timed out chat=%s tool=%s (denied)", self.label, title)
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._expire_permission(token)))
        return None

    OPTION_LABELS: ClassVar[dict[str, str]] = {
        "allow_once": "✅ Allow once",
        "allow_always": "✅ Always allow",
        "reject_once": "⛔ Deny",
        "reject_always": "⛔ Always deny",
    }

    @staticmethod
    def _permission_detail(params: dict[str, Any]) -> tuple[str, str]:
        """Split a permission request into (headline, monospace body)."""
        tool_call = params.get("toolCall") or {}
        raw_input = tool_call.get("rawInput") or {}
        kind = str(tool_call.get("kind") or params.get("kind") or "tool")
        command = str(raw_input.get("command") or "").strip()
        title = str(params.get("title") or tool_call.get("title") or "").strip()
        if command:
            return kind, command
        if title:
            return kind, title
        return kind, ""

    def _option_label(self, option: dict[str, Any]) -> str:
        kind = str(option.get("kind") or "")
        known = self.OPTION_LABELS.get(kind)
        if known:
            return known
        return str(option.get("name") or kind or "option")

    async def _ask_permission(self, token: str, params: dict[str, Any]) -> None:
        options = params.get("options") or []
        kind, detail = self._permission_detail(params)
        labels = [self._option_label(option) for option in options]
        slot = self._pending_perm.get(token)
        if slot is None:
            return
        try:
            slot["handle"] = await self.surface.ask_permission(token, kind, detail, labels)
        except Exception as exc:
            log.warning("could not send approval prompt: %s", exc)

    async def deliver_files(self, raw: str) -> None:
        """Upload the files the agent marked, and say what was dropped.

        The marker never reaches the chat, so a path that does not work would otherwise disappear
        without a trace. One short line names what was skipped and why.
        """
        max_mb = float(self.config.get("files", "max_mb", 20.0))
        _, files, rejected = extract_files(raw, root=self.cwd, max_mb=max_mb)
        for item in files:
            try:
                await self.surface.send_file(item.path, item.as_photo)
                log.info("file sent %s path=%s bytes=%s", self.label, item.path, item.size)
            except Exception as exc:  # a send failure must not end the turn
                log.warning("file send failed %s path=%s: %s", self.label, item.path, exc)
                rejected.append(f"{item.path.name}: {exc}")
        if rejected:
            log.info("files rejected %s: %s", self.label, "; ".join(rejected))
            shown = rejected[:4]
            more = "" if len(rejected) <= 4 else f" (+{len(rejected) - 4} more)"
            await self.surface.notice("skipped a file · " + " · ".join(shown) + more)

    # ------------------------------------------------------------- schedules
    def _schedule_name(self, proposal: schedule_lib.Proposal) -> str:
        """A readable name from the prompt, unique among what already exists."""
        words = re.findall(r"[a-z0-9]+", proposal.prompt.lower())[:3]
        base = "-".join(words)[:24] or "scheduled"
        existing = set(schedule_lib.load())
        if base not in existing:
            return base
        for index in range(2, 100):
            candidate = f"{base}-{index}"
            if candidate not in existing:
                return candidate
        return f"{base}-{len(existing) + 1}"

    async def offer_schedules(self, proposals: list[schedule_lib.Proposal]) -> None:
        """A marker is a proposal: nothing is installed until a button is pressed."""
        known = {item.name for item in gateways.load()}
        for proposal in proposals:
            gateway = proposal.gateway or self.gateway_name
            problem = ""
            if known and gateway not in known:
                problem = f"unknown gateway '{gateway}'"
            try:
                expression = proposal.expression
                schedule_lib.validate_calendar(f"{expression} {schedule_lib.DEFAULT_TIMEZONE}")
            except schedule_lib.ScheduleError as exc:
                problem = str(exc)
            if problem:
                await self.surface.notice(f"schedule rejected · {problem}")
                continue
            name = self._schedule_name(proposal)
            self.pending_schedules[name] = proposal
            await self.surface.offer_schedule(name, expression, gateway, proposal.prompt)
    async def _schedule_run(self, name: str) -> None:
        from . import schedule_run

        await self.surface.notice(f"running {name} now…")
        try:
            code = await asyncio.to_thread(schedule_run.run, name)
        except Exception as exc:
            return await self.surface.notice(f"could not run {name} · {exc}")
        if code != 0:
            await self.surface.notice(f"{name} exited with code {code}; /schedule shows the status")

    async def _schedule_remove(self, name: str) -> None:
        try:
            schedule_lib.uninstall(name)
            item = schedule_lib.remove(name)
        except schedule_lib.ScheduleError as exc:
            return await self.surface.notice(f"could not remove · {exc}")
        await self.surface.notice(f"schedule {item.name} removed, timer included")

    async def _schedule_create(self, name: str) -> None:
        proposal = self.pending_schedules.pop(name, None)
        if proposal is None:
            return await self.surface.notice("that proposal is gone; ask again")
        gateway = proposal.gateway or self.gateway_name
        try:
            item = schedule_lib.Schedule(name=name, cron=proposal.cron, prompt=proposal.prompt,
                                         gateway=gateway, calendar=proposal.calendar)
            schedule_lib.add(name=name, cron=proposal.cron, calendar=proposal.calendar,
                             prompt=proposal.prompt, gateway=gateway)
            schedule_lib.install(item)
            schedule_lib.enable(name)
        except schedule_lib.ScheduleError as exc:
            return await self.surface.notice(f"could not create the schedule · {exc}")
        nxt = schedule_lib.next_elapse(name) or "see systemctl --user list-timers"
        await self.surface.notice(f"schedule {name} created · {item.on_calendar} → {gateway}\nnext: {nxt}")

    async def offer_options(self, options: list[str]) -> None:
        """The agent offered answers; the surface turns them into buttons."""
        self.pending_options = options
        log.info("question offered %s options=%s", self.label, options)
        await self.surface.offer_options(options)

    async def answer_permission(self, token: str, index: int) -> str | None:
        """Record the user's choice, then let the surface rewrite the prompt into the decision."""
        slot = self._pending_perm.get(token)
        if slot is None:
            return None
        options = slot["params"].get("options") or []
        if not (0 <= index < len(options)):
            return None
        option = options[index]
        slot["optionId"] = option.get("optionId")
        label = self._option_label(option)
        log.info("permission answered %s choice=%s", self.label, label)
        kind, detail = self._permission_detail(slot["params"])
        handle = slot.get("handle")
        if handle is not None:
            try:
                await self.surface.settle_permission(handle, kind, detail, label, expired=False)
            except Exception as exc:
                log.debug("approval settle failed: %s", exc)
        return label

    async def _expire_permission(self, token: str) -> None:
        slot = self._pending_perm.get(token)
        if slot is None:
            return
        kind, detail = self._permission_detail(slot["params"])
        handle = slot.get("handle")
        if handle is not None:
            try:
                await self.surface.settle_permission(handle, kind, detail, "", expired=True)
            except Exception as exc:
                log.debug("could not rewrite the expired approval prompt: %s", exc)

    # ------------------------------------------------------------------ turn
    async def ask(self, text: str, images: list[dict[str, Any]] | None = None,
                  resumes: int = 0, record: str | None = None) -> str:
        await self.ensure()
        self.busy = True
        self._compact_turn = text.strip() == "/compact"
        self._user_cancel = False
        self.last_used = time.time()
        started = time.time()
        self.turns += 1
        # written before the prompt goes out, deleted only when the turn really ends: a gateway
        # restart in between leaves the row, and the next start picks the turn back up. A resume
        # records the person's words, not the resume wrapper, so a second restart does not nest it.
        self.store.begin_turn(self.key, record or text, resumes)
        log.info("turn start chat=%s session=%s input_chars=%s", self.label,
                 self.session_id[:8], len(text))
        # Every message is posted once and never edited, so its timestamp is when that step
        # happened. Answer text is cut into segments at each tool call; each tool call is its own
        # message, posted when it finishes so it carries its output.
        #
        # Telegram allows roughly one message a second per chat. drain() never waits on the
        # network: it appends to an outbox, and one sender posts it in order, at least
        # stream.min_gap apart. Tool traces that pile up while the sender waits (pacing or a
        # flood window) go out together as one message, and a trace that is dropped after flood
        # control is counted and named in the chat instead of vanishing.
        segment: list[str] = []    # answer text since the last tool call
        answer: list[str] = []     # the whole answer, for the markers read at the end
        thinking: list[str] = []
        trace: dict[str, ToolTrace] = {}
        posted: set[str] = set()
        state: dict[str, Any] = {"text_msg": None, "tools": 0, "dropped": 0, "closed": False}
        outbox: list[dict[str, Any]] = []
        wake = asyncio.Event()
        min_gap = float(self.config.get("stream", "min_gap", 1.0))
        streams = bool(getattr(self.surface, "streams", False))
        # streaming: how much of the current segment has already been shown, measured on the
        # cleaned text, so a marker is held back until it closes and then never shown
        shown_len = [0]
        # omp tags every chunk with the assistant message it belongs to. A steered turn holds
        # several messages; without a cut at the boundary the first ended in `…>>` and the next
        # began on the same line, and both landed in one Telegram message.
        message_id = [""]

        def enqueue(kind: str, body: str, *, essential: bool) -> None:
            outbox.append({"kind": kind, "body": body, "essential": essential})
            wake.set()

        def clean(text: str, *, final: bool) -> str:
            text = humanize_token_report(strip_agent_noise(text))
            text = hide_file_partial(hide_partial(text, final=final), final=final)
            return schedule_lib.hide_partial(text, final=final)

        def stream_delta(*, final: bool) -> None:
            """Queue the part of the segment that is safe to show and has not been shown yet."""
            visible = clean("".join(segment), final=final)
            # a marker may still be open: hold back from its `<<` until it is complete
            cut = visible.rfind("<<")
            if not final and cut != -1 and ">>" not in visible[cut:]:
                visible = visible[:cut]
            if len(visible) > shown_len[0]:
                enqueue("delta", visible[shown_len[0]:], essential=True)
                shown_len[0] = len(visible)

        async def post(body: str, *, kind: str) -> Any | None:
            """One new message. A flood window is waited out for the answer, not for a trace."""
            essential = kind in {"text", "notice"}
            for attempt in range(2):
                wait = self._last_post + min_gap - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    sent_msg = await self.surface.post(body, kind=kind)
                    self._last_post = time.monotonic()
                    if sent_msg is None:
                        log.warning("message not delivered %s", self.label)
                    return sent_msg
                except FloodWait as exc:
                    window = exc.window
                    self._last_post = time.monotonic() + window  # nothing goes out before it ends
                    if attempt or window > (60 if essential else 10):
                        log.warning("message dropped %s after flood control (%.0fs)",
                                    self.label, window)
                        return None
            return None

        def dropped_line() -> str:
            return self.surface.dropped(state["dropped"])

        async def sender() -> None:
            while True:
                if not outbox:
                    if state["closed"]:
                        break
                    wake.clear()
                    await wake.wait()
                    continue
                if outbox[0]["kind"] == "delta":
                    # a terminal has no rate limit and no message boundary to wait for
                    await self.surface.stream(outbox.pop(0)["body"])
                    continue
                if outbox[0]["kind"] == "end_stream":
                    outbox.pop(0)
                    await self.surface.end_stream()
                    continue
                if outbox[0]["kind"] != "trace":
                    item = outbox.pop(0)
                    sent_msg = await post(item["body"], kind=item["kind"])
                    if sent_msg is not None and item["kind"] == "text":
                        state["text_msg"] = sent_msg
                    continue
                # every trace waiting at the head of the outbox goes out as one message
                blocks: list[str] = [dropped_line()] if state["dropped"] else []
                taken = 0
                while taken < len(outbox) and outbox[taken]["kind"] == "trace":
                    candidate = self.surface.join([*blocks, outbox[taken]["body"]])
                    if taken and len(candidate) > self.surface.limit:
                        break
                    blocks.append(outbox[taken]["body"])
                    taken += 1
                del outbox[:taken]
                if await post(self.surface.join(blocks), kind="trace") is None:
                    state["dropped"] += taken
                else:
                    state["dropped"] = 0
            if state["dropped"]:
                await post(dropped_line(), kind="notice")

        def flush_thinking() -> None:
            shown = "".join(thinking).strip()
            thinking.clear()
            if not self.reasoning or not shown:
                return
            if len(shown) > REASONING_CHARS:
                shown = "…\n" + shown[-REASONING_CHARS:]
            # prose, so not a code block: monospace reads badly on a phone and a <pre> cannot
            # collapse. An expandable quote stays one line until tapped.
            enqueue("reasoning", self.surface.reasoning(shown), essential=False)

        def flush_segment() -> None:
            if streams:
                if segment:
                    stream_delta(final=True)
                    enqueue("end_stream", "", essential=True)
                segment.clear()
                shown_len[0] = 0
                return
            text = clean("".join(segment), final=True)
            segment.clear()
            if not text.strip():
                return
            for part in _chunks(text, 3600):
                enqueue("text", part, essential=True)

        def post_tool(call_id: str) -> None:
            entry = trace[call_id]
            posted.add(call_id)
            if not self.verbose:
                return
            body = self.surface.trace(entry)
            if len(body) > self.surface.limit:
                body = self.surface.trace(replace(entry, output="(output too long for one message)"))
            enqueue("trace", body, essential=False)

        async def finish() -> None:
            """End of turn: whatever is still buffered, then the markers in the whole answer."""
            flush_thinking()
            flush_segment()
            for call_id in trace:
                if call_id not in posted:
                    post_tool(call_id)
            state["closed"] = True
            wake.set()
            await sender_task  # the buttons and files come after the last message, in order
            raw = "".join(answer)
            _, options = extract_options(raw)
            if options:
                await self.offer_options(options)
            await self.deliver_files(raw)
            _clean, proposals = schedule_lib.extract(raw)
            if proposals:
                await self.offer_schedules(proposals)

        async def drain() -> None:
            while True:
                update = await self._updates.get()
                if update is None:
                    return
                if update.kind == "agent_thought_chunk" and update.text:
                    thinking.append(update.text)
                elif update.kind == "agent_message_chunk" and update.text:
                    flush_thinking()
                    current = str(update.raw.get("messageId") or "")
                    if current and message_id[0] and current != message_id[0]:
                        flush_segment()
                        answer.append("\n\n")  # the markers read the whole answer at the end
                    if current:
                        message_id[0] = current
                    segment.append(update.text)
                    answer.append(update.text)
                    if streams:
                        stream_delta(final=False)
                elif update.kind in {"tool_call", "tool_call_update"}:
                    raw = update.raw
                    call_id = str(raw.get("toolCallId") or f"anon{len(trace)}")
                    entry = trace.setdefault(call_id, ToolTrace(kind="tool"))
                    if update.kind == "tool_call":
                        # the text before a tool call is a finished thought: post it now
                        flush_thinking()
                        flush_segment()
                        if _is_waiting(str(raw.get("title") or "")):
                            posted.add(call_id)
                            continue
                        state["tools"] += 1
                        self.tools_used += 1
                        entry.kind = str(raw.get("kind") or "tool")
                        entry.title = str(raw.get("title") or "")
                        raw_input = raw.get("rawInput") or {}
                        entry.command = str(raw_input.get("command") or "")
                        tasks = raw_input.get("tasks")
                        if isinstance(tasks, list) and tasks:
                            entry.kind = "subagent"
                            # omp runs subagents through the `task` tool; ACP has no event of
                            # its own for them, so the task list is the only place they appear
                            entry.subagent = ", ".join(
                                f"{item.get('name', '?')} ({item.get('agent', 'task')})"
                                for item in tasks if isinstance(item, dict))[:80]
                            first = next((item for item in tasks if isinstance(item, dict)), {})
                            entry.brief = _task_brief(first.get("task", ""))
                            tools = first.get("tools")
                            if isinstance(tools, list) and tools:
                                entry.brief = (entry.brief + " · tools: "
                                               + ", ".join(str(x) for x in tools))[:160]
                        log.info("tool call %s kind=%s command=%r", self.label,
                                 entry.kind, entry.command[:120] or entry.title[:120])
                    if call_id in posted:
                        continue
                    entry.status = str(raw.get("status") or entry.status or "")
                    steps = [item.get("text", "") for item in (raw.get("content") or [])
                             if isinstance(item, dict) and item.get("text")]
                    if steps:
                        for step in steps:
                            clean = " ".join(str(step).split())[:110]
                            if clean and clean not in entry.steps:
                                entry.steps.append(clean)
                        del entry.steps[:-4]
                    output = raw.get("rawOutput") or {}
                    chunks = [item.get("text", "") for item in (output.get("content") or [])
                              if isinstance(item, dict)]
                    if any(chunks):
                        entry.output = "\n".join(chunks)
                    if entry.status in {"completed", "failed"}:
                        post_tool(call_id)

        sender_task = asyncio.create_task(sender())
        drain_task = asyncio.create_task(drain())
        try:
            async with self.surface.typing():
                result = await asyncio.to_thread(
                    self.client.prompt, self.session_id, text,
                    images=images or None,
                    timeout=float(self.config.get("acp", "turn_timeout", 900.0)),
                )
        except AcpError as exc:
            # A silent turn is abandoned here, so stop it in omp too: left running, its late
            # chunks land in the next turn's stream and the chat sees the answer twice.
            if self.client is not None and self.client.alive:
                with contextlib.suppress(AcpError):
                    self.client.cancel(self.session_id)
            # scheduled, not immediate: the sentinel must queue behind updates the reader thread
            # has already handed to the loop, or the last of a turn is dropped
            self._loop.call_soon_threadsafe(self._updates.put_nowait, None)
            await drain_task
            await finish()  # what did arrive is still worth showing
            self.busy = False
            # A crash or shutdown cut keeps the row, so the next start resumes it. A deliberate
            # stop and a silence timeout do not: the person has already been told it failed.
            if self._user_cancel or isinstance(exc, AcpTimeout):
                self.store.end_turn(self.key)
            raise
        except BaseException:
            # a cancelled turn (gateway shutdown) never reaches finish(); do not leave the sender
            sender_task.cancel()
            raise
        self._loop.call_soon_threadsafe(self._updates.put_nowait, None)
        await drain_task
        await finish()
        self.busy = False
        await self._report_compaction()
        self._compact_turn = False
        self.store.touch(self.key)
        # A cancel nobody asked for is the gateway going down (systemd signals the agent too).
        # Keep the row so the next start resumes; /stop, /kill and /new clear it. A try/finally
        # here would delete it on exactly the path this table exists for.
        if result.stop_reason != "cancelled" or self._user_cancel:
            self.store.end_turn(self.key)

        usage = result.usage or {}
        log.info("turn end chat=%s stop=%s tools=%s in=%s out=%s cached=%s duration=%.1fs",
                 self.label, result.stop_reason, state["tools"],
                 usage.get("inputTokens"), usage.get("outputTokens"),
                 usage.get("cachedReadTokens"), time.time() - started)
        if self.config.get("stream", "show_usage", True):
            try:
                await self.surface.notice(
                    f"{result.stop_reason} · in {tokens(usage.get('inputTokens'))} "
                    f"out {tokens(usage.get('outputTokens'))} "
                    f"cached {tokens(usage.get('cachedReadTokens', 0))}"
                )
            except Exception:
                pass
        return result.text

    async def cancel(self) -> bool:
        if self.client is None or not self.session_id:
            return False
        self.forget_turn()
        self.client.cancel(self.session_id)
        return True

    async def steer(self, text: str, images: list[dict[str, Any]] | None = None) -> tuple[str, str]:
        """Hand a message to the running turn: (outcome, reason). `promptRequired` = idle."""
        if self.client is None or not self.client.alive or not self.session_id:
            return "promptRequired", ""
        try:
            outcome, reason = await asyncio.to_thread(
                self.client.steer, self.session_id, text, images,
                float(self.config.get("acp", "steer_timeout", 15.0)))
        except AcpError as exc:
            log.warning("steer failed chat=%s: %s", self.label, exc)
            return "failed", str(exc)
        log.info("steer chat=%s outcome=%s chars=%s reason=%s", self.label, outcome, len(text),
                 reason or "-")
        return outcome, reason

    def forget_turn(self) -> None:
        """The person ended this turn on purpose; a restart must not bring it back."""
        self._user_cancel = True
        self.store.end_turn(self.key)

    async def respawn(self, yolo: bool) -> None:
        self.yolo = yolo
        self.store.set_pref(self.key, "yolo", "on" if yolo else "off")
        self.forget_turn()
        if self.client is not None:
            await asyncio.to_thread(self.client.close, self.session_id)
        self.client = None
        await self.ensure(yolo=yolo)

    async def stop_process(self) -> None:
        """Close the agent process; the chat keeps its store and comes back on the next message."""
        if self.client is not None:
            await asyncio.to_thread(self.client.close, self.session_id)
            self.client = None

    async def shutdown(self) -> None:
        await self.stop_process()
        self.store.close()

    async def new_session(self) -> str:
        self.forget_turn()
        # the old context numbers belong to the old session; the next usage_update re-reads them
        self._compactions_seen = None
        self._context_good = None
        self._context_warned = False
        if self.client is not None and self.client.alive and self.session_id:
            await asyncio.to_thread(self.client.close, self.session_id)
        self.client = None
        self.store.clear(self.key)
        await self.ensure()
        return self.session_id

    async def load(self, session_id: str) -> None:
        if self.client is not None:
            await asyncio.to_thread(self.client.close, self.session_id)
        self.client = None
        self._compactions_seen = None
        self._context_good = None
        self._context_warned = False
        self.store.put(self.key, session_id, self.cwd)
        await self.ensure()

    async def fork(self) -> str:
        await self.ensure()
        self._compactions_seen = None
        self._context_warned = False
        result = await asyncio.to_thread(self.client.request, "session/fork",
                                          {"sessionId": self.session_id, "cwd": self.cwd})
        new_id = str(result.get("sessionId") or "")
        if new_id:
            self.session_id = new_id
            self.store.put(self.key, new_id, self.cwd)
        return new_id

