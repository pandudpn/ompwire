"""Telegram transport for ompwire.

Design rule: this module owns transport concerns only (polling, streaming edits,
approvals, queueing, session routing). Anything the agent can do itself is
forwarded as a prompt.

Threading model: the ACP client is synchronous and talks on its own reader
thread; Telegram is asyncio. The bridge is one asyncio.Queue fed by
`loop.call_soon_threadsafe`, plus a threading.Event for approvals, which must
block the reader thread until the user taps a button.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import logging.handlers
import pathlib
import re
import signal
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, ClassVar

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import __version__, allowlist, gateways
from . import schedules as schedule_lib
from .acp import AcpClient, AcpError, pick_permission_option
from .acp import Update as AcpUpdate
from .attachments import extract as extract_files
from .attachments import hide_partial as hide_file_partial
from .attachments import incoming_summary, safe_name
from .attachments import save as save_incoming
from .catalog import credential_snapshot, list_providers, list_skills, skill_body
from .config import Config, approval_policy, save_override
from .format import card, change, esc, help_sections, now_wib, tool_block
from .format import uptime as fmt_uptime
from .login import LoginSession
from .mcp_hub import hub_servers, normalize
from .mcp_hub import merge_entry as merge_mcp_entry
from .mdrender import split_blocks, strip_agent_noise, strip_html, to_html, to_plain
from .questions import extract as extract_options
from .questions import hide_partial
from .store import SessionStore

log = logging.getLogger("ompwire.telegram")

TELEGRAM_LIMIT = 4000  # leave headroom under the 4096 hard cap
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
IDLE_CHECK_INTERVAL = 60.0

COMMANDS: list[tuple[str, str]] = [
    ("new", "start a fresh session"),
    ("stop", "cancel the running turn"),
    ("status", "session, model, mode and process state"),
    ("usage", "token usage for this session"),
    ("context", "context window breakdown from the agent"),
    ("skills", "browse skills and read their SKILL.md"),
    ("commands", "search the agent's advertised slash commands"),
    ("login", "sign in to a model provider"),
    ("sessions", "list recorded sessions"),
    ("resume", "resume a session by id"),
    ("continue", "resume this chat's last session"),
    ("fork", "branch this conversation into a new session"),
    ("cwd", "change working directory (new session)"),
    ("model", "show or set the model"),
    ("mode", "show or set default|plan"),
    ("thinking", "show or set the thinking level"),
    ("approve", "show or set the approval policy"),
    ("verbose", "toggle tool-call trace"),
    ("commands", "search the agent's own slash commands"),
    ("queue", "queue a prompt behind the running turn"),
    ("steer", "add an instruction to the running turn"),
    ("schedule", "scheduled turns: list, run now, remove"),
    ("reasoning", "show or hide the agent's thinking"),
    ("yolo", "respawn the agent with approvals off, for this chat only"),
    ("kill", "stop the agent process for this chat"),
    ("whoami", "ids and transport info"),
    ("help", "this list"),
]

def paginate(items: list, page: int, per_page: int) -> tuple[list, int, int]:
    """Clamp a page index and slice it; page is zero-based here."""
    pages = max(1, -(-len(items) // per_page))
    page = max(0, min(page, pages - 1))
    return items[page * per_page:(page + 1) * per_page], page, pages


def two_columns(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


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


def normalize_mcp_servers(servers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backwards-compatible alias for `mcp_hub.normalize`."""
    return normalize(list(servers or []))


# Choices that belong to the chat, not to one ACP session: /new must not reset them.
PREF_KEYS = ("model", "mode", "thinking")

FORWARD_HINT = (
    "Everything else is sent to the agent as a prompt. Its own commands work here, "
    "including /skill:<name> and /compact."
)


async def send_html(bot, chat_id: int, body: str):
    """Send pre-rendered Telegram HTML, falling back to plain text."""
    try:
        return await bot.send_message(chat_id, body, parse_mode=ParseMode.HTML)
    except BadRequest as exc:
        log.warning("html send rejected (%s); falling back to plain text", exc)
        return await bot.send_message(chat_id, strip_html(body))


async def send_md(bot, chat_id: int, text: str):
    """Send agent markdown as Telegram HTML, degrading to plain text on refusal."""
    try:
        return await bot.send_message(chat_id, to_html(text), parse_mode=ParseMode.HTML)
    except BadRequest as exc:
        log.warning("html send rejected (%s); falling back to plain text", exc)
        return await bot.send_message(chat_id, to_plain(text))


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


def _unique_commands() -> list[tuple[str, str]]:
    """Telegram rejects a duplicate command name, so the menu is deduped."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name, description in COMMANDS:
        if name in seen:
            continue
        seen.add(name)
        out.append((name, description))
    return out


def _chunks(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
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


class ChatAgent:
    """One chat's ACP agent: subprocess, session id, queue and counters."""

    def __init__(self, config: Config, chat_id: int, key: str, app: Application) -> None:
        self.config = config
        self.chat_id = chat_id
        self.key = key
        self.app = app
        self.store = SessionStore(config.get("session", "store"))
        self.client: AcpClient | None = None
        self.session_id = ""
        self.cwd = str(config.get("agent", "cwd") or "~")
        self.reasoning = bool(config.get("stream", "show_reasoning", False))
        # Telegram's flood control counts edits too. When it answers with RetryAfter, everything
        # that edits a live message goes quiet until the window passes, instead of turning a
        # pretty trace into "Turn failed: Flood control exceeded".
        self._quiet_until = 0.0
        # the bot that receives a scheduled result defaults to the bot being talked to
        self.gateway_name = str(config.get("_gateway", "name", "telegram") or "telegram")
        self.pending_schedules: dict[str, schedule_lib.Proposal] = {}
        self.started_at = time.time()
        self.pending_options: list[str] = []
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
        self.usage = {"input": 0, "output": 0, "cached": 0, "turns": 0}
        self.turns = 0
        self.tools_used = 0
        self.options: dict[str, dict[str, Any]] = {}
        self.mcp_warning = ""
        self.info: dict[str, Any] = {}
        self.agent_version = ""
        self.picker: dict[str, Any] = {}
        self.advertised_commands: list[str] = []
        self._updates: asyncio.Queue[AcpUpdate | None] = asyncio.Queue()
        self._pending_perm: dict[str, dict[str, Any]] = {}
        self._loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup
    def _build_client(self, yolo: bool = False) -> AcpClient:
        args = list(self.config.get("acp", "args") or ["--mode", "acp"])
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
        log.info("agent up for %s: %s %s (approval=%s, mcp=%d)", self.chat_id,
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
            self.mcp_warning = str(exc)
            result = await asyncio.to_thread(self.client.new_session, self.cwd, [])
            self.session_id = str(result.get("sessionId") or "")
        finally:
            self.client.on_update = previous
        self.options = {opt.get("id"): opt for opt in (result.get("configOptions") or [])}
        self.store.put(self.key, self.session_id, self.cwd)
        await self._apply_preferences()

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
        if update.kind in {"usage_update", "session_info_update"}:
            return
        self._loop.call_soon_threadsafe(self._updates.put_nowait, update)

    def _on_permission(self, params: dict[str, Any]) -> str | None:
        policy = approval_policy(self.config)
        title = params.get("title") or (params.get("toolCall") or {}).get("title") or "tool call"
        if policy in {"allow", "deny"}:
            log.info("permission auto-%s chat=%s tool=%s", policy, self.chat_id, title)
            return pick_permission_option(params, policy)
        # A read-only command is not worth a button. Asking for `git log` teaches the habit of
        # tapping Allow without reading, which is exactly what the gate exists to prevent.
        command = str(((params.get("toolCall") or {}).get("rawInput") or {}).get("command") or "")
        if command and allowlist.is_read_only(command):
            log.info("permission auto-allow chat=%s (read-only): %s", self.chat_id, command[:120])
            return pick_permission_option(params, "allow")
        token = f"perm-{int(time.time() * 1000)}"
        slot: dict[str, Any] = {"optionId": None, "params": params}
        self._pending_perm[token] = slot
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._ask_permission(token, params)))
        log.info("permission requested chat=%s tool=%s options=%s", self.chat_id, title,
                 [o.get("kind") for o in (params.get("options") or [])])
        deadline = time.time() + float(self.config.get("approval", "timeout", PERMISSION_TIMEOUT))
        while time.time() < deadline:
            if slot.get("optionId") is not None:
                self._pending_perm.pop(token, None)
                return slot["optionId"]
            time.sleep(0.2)
        log.warning("permission timed out chat=%s tool=%s (denied)", self.chat_id, title)
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
        rows = [[InlineKeyboardButton(self._option_label(option), callback_data=f"{token}:{index}")]
                for index, option in enumerate(options)]
        head = card("🛡️ approval needed", kind, [],
                    "pick one · the run is paused until you answer")
        body = head + (f"\n<pre>{esc(detail[:1200])}</pre>" if detail else "")
        slot = self._pending_perm.get(token)
        try:
            sent = await self.app.bot.send_message(self.chat_id, body,
                                                   reply_markup=InlineKeyboardMarkup(rows),
                                                   parse_mode=ParseMode.HTML)
            if slot is not None:
                slot["message"] = sent
        except TelegramError as exc:
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
                with item.path.open("rb") as handle:
                    if item.as_photo:
                        await self.app.bot.send_photo(self.chat_id, photo=handle)
                    else:
                        await self.app.bot.send_document(self.chat_id, document=handle)
                log.info("file sent chat=%s path=%s bytes=%s", self.chat_id, item.path, item.size)
            except Exception as exc:  # a send failure must not end the turn
                log.warning("file send failed chat=%s path=%s: %s", self.chat_id, item.path, exc)
                rejected.append(f"{item.path.name}: {exc}")
        if rejected:
            log.info("files rejected chat=%s: %s", self.chat_id, "; ".join(rejected))
            shown = rejected[:4]
            more = "" if len(rejected) <= 4 else f" (+{len(rejected) - 4} more)"
            await self.send_md("skipped a file · " + " · ".join(shown) + more)

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
                await self.send_md(f"schedule rejected · {problem}")
                continue
            name = self._schedule_name(proposal)
            self.pending_schedules[name] = proposal
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Create schedule", callback_data=f"sched:new:{name}"),
                InlineKeyboardButton("Cancel", callback_data=f"sched:cancel:{name}"),
            ]])
            body = card("schedule", expression, [
                ("delivers to", gateway),
                ("prompt", proposal.prompt[:200]),
            ], "nothing runs until you press Create")
            await self.app.bot.send_message(self.chat_id, body, parse_mode=ParseMode.HTML,
                                            reply_markup=keyboard)

    async def _schedule_run(self, name: str) -> None:
        from . import schedule_run

        await self.send_md(f"running {name} now…")
        try:
            code = await asyncio.to_thread(schedule_run.run, name)
        except Exception as exc:
            return await self.send_md(f"could not run {name} · {exc}")
        if code != 0:
            await self.send_md(f"{name} exited with code {code}; /schedule shows the status")

    async def _schedule_remove(self, name: str) -> None:
        try:
            schedule_lib.uninstall(name)
            item = schedule_lib.remove(name)
        except schedule_lib.ScheduleError as exc:
            return await self.send_md(f"could not remove · {exc}")
        await self.send_md(f"schedule {item.name} removed, timer included")

    async def _schedule_create(self, name: str) -> None:
        proposal = self.pending_schedules.pop(name, None)
        if proposal is None:
            return await self.send_md("that proposal is gone; ask again")
        gateway = proposal.gateway or self.gateway_name
        try:
            item = schedule_lib.Schedule(name=name, cron=proposal.cron, prompt=proposal.prompt,
                                         gateway=gateway, calendar=proposal.calendar)
            schedule_lib.add(name=name, cron=proposal.cron, calendar=proposal.calendar,
                             prompt=proposal.prompt, gateway=gateway)
            schedule_lib.install(item)
            schedule_lib.enable(name)
        except schedule_lib.ScheduleError as exc:
            return await self.send_md(f"could not create the schedule · {exc}")
        nxt = schedule_lib.next_elapse(name) or "see systemctl --user list-timers"
        await self.send_md(f"schedule {name} created · {item.on_calendar} → {gateway}\nnext: {nxt}")

    async def offer_options(self, options: list[str], chat_id: int | None = None) -> None:
        """Buttons for the answers the agent offered, plus a free-text escape hatch."""
        target = chat_id or self.chat_id
        self.pending_options = options
        rows = [[InlineKeyboardButton(option[:60], callback_data=f"qo:{index}")]
                for index, option in enumerate(options)]
        rows.append([InlineKeyboardButton("✏️ Other answer", callback_data="qo:other")])
        log.info("question offered chat=%s options=%s", target, options)
        await self.app.bot.send_message(
            target,
            card("❓ pick an answer", "tap one, or choose Other and just type it", [],
                 "the chosen text is sent as your next message"),
            reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
        )

    async def handle_permission_callback(self, token: str, index: int) -> str | None:
        """Record the user's choice, then rewrite the prompt into the decision."""
        slot = self._pending_perm.get(token)
        if slot is None:
            return None
        options = slot["params"].get("options") or []
        if not (0 <= index < len(options)):
            return None
        option = options[index]
        slot["optionId"] = option.get("optionId")
        label = self._option_label(option)
        log.info("permission answered chat=%s choice=%s", self.chat_id, label)
        kind, detail = self._permission_detail(slot["params"])
        body = card("🛡️ approval", f"{label} · {now_wib()}", [("tool", kind)],
                    "decision recorded")
        if detail:
            body += f"\n<pre>{esc(detail[:800])}</pre>"
        message = slot.get("message")
        if message is not None:
            try:
                await message.edit_text(body, parse_mode=ParseMode.HTML)
            except BadRequest as exc:
                log.debug("approval edit failed: %s", exc)
        return label

    async def _expire_permission(self, token: str) -> None:
        slot = self._pending_perm.get(token)
        if slot is None:
            return
        kind, detail = self._permission_detail(slot["params"])
        body = card("🛡️ approval", f"expired · {now_wib()}", [("tool", kind)],
                    "no answer in time, the tool call was denied")
        if detail:
            body += f"\n<pre>{esc(detail[:800])}</pre>"
        message = slot.get("message")
        if message is not None:
            try:
                await message.edit_text(body, parse_mode=ParseMode.HTML)
            except BadRequest:
                pass
            except Exception as exc:
                log.debug("could not rewrite the expired approval prompt: %s", exc)

    async def _send_answer(self, text: str):
        """The answer is the one message worth waiting for, briefly."""
        try:
            return await self.send_md(text)
        except RetryAfter as exc:
            self._note_rate_limit(exc)
            window = self._retry_window(exc)
            if window > 20:
                log.error("rate limited for %.0fs, the answer was not delivered", window)
                return None
            await asyncio.sleep(window + 0.5)
            try:
                self._quiet_until = 0.0
                return await self.send_md(text)
            except TelegramError as retry:
                log.error("answer still not delivered after waiting: %s", retry)
                return None

    async def _edit_answer(self, message, text: str) -> None:
        try:
            await self.edit_md(message, text)
        except RetryAfter as exc:
            self._note_rate_limit(exc)
        except TelegramError as exc:
            log.debug("answer edit rejected: %s", exc)

    def _rate_limited(self) -> bool:
        return time.monotonic() < self._quiet_until

    @staticmethod
    def _retry_window(exc: Exception) -> float:
        """PTB gives seconds today and a timedelta from v23; both mean the same wait."""
        value = getattr(exc, "retry_after", 5)
        if hasattr(value, "total_seconds"):
            return float(value.total_seconds())
        return float(value or 5)

    def _note_rate_limit(self, exc: Exception) -> None:
        window = self._retry_window(exc)
        # a long window is not worth stalling the turn for; stop editing and let the answer land
        self._quiet_until = time.monotonic() + window
        log.warning("telegram rate limit: no edits for %.0fs (%s)", window, exc)

    # ------------------------------------------------------------------ turn
    async def ask(self, text: str, message=None, images: list[dict[str, Any]] | None = None,
                  resumes: int = 0, record: str | None = None) -> str:
        await self.ensure()
        self.busy = True
        self._user_cancel = False
        self.last_used = time.time()
        started = time.time()
        self.turns += 1
        # written before the prompt goes out, deleted only when the turn really ends: a gateway
        # restart in between leaves the row, and the next start picks the turn back up. A resume
        # records the person's words, not the resume wrapper, so a second restart does not nest it.
        self.store.begin_turn(self.key, record or text, resumes)
        log.info("turn start chat=%s session=%s input_chars=%s", self.chat_id,
                 self.session_id[:8], len(text))
        text_buffer: list[str] = []
        trace: dict[str, dict[str, str]] = {}
        sent: list[Any] = []
        state = {"text_msg": None, "tool_msg": None, "reason_msg": None, "reasoning": "",
                 "last_edit": 0.0, "text": "", "tools": 0}

        async def flush_text(force: bool = False) -> None:
            joined = strip_agent_noise("".join(text_buffer))
            if not joined.strip():
                return
            now = time.time()
            interval = float(self.config.get("stream", "edit_interval", 1.0))
            if not force and now - state["last_edit"] < interval:
                return
            state["last_edit"] = now
            joined = hide_file_partial(hide_partial(joined, final=force), final=force)
            joined = schedule_lib.hide_partial(joined, final=force)
            if not joined.strip():
                return
            parts = _chunks(joined, 3600)
            # one Telegram message per chunk, edited in place; sending parts[1:] on every flush
            # posted the tail of a long answer again each second
            chunk_msgs: list[Any] = state.setdefault("chunk_msgs", [])
            if joined != state["text"]:
                for index, part in enumerate(parts):
                    if index < len(chunk_msgs):
                        if index == len(chunk_msgs) - 1 or index == len(parts) - 1 or force:
                            await self._edit_answer(chunk_msgs[index], part)
                        continue
                    sent_msg = await self._send_answer(part)
                    if sent_msg is None:
                        break
                    chunk_msgs.append(sent_msg)
                    sent.append(sent_msg)
                state["text_msg"] = chunk_msgs[0] if chunk_msgs else None
            state["text"] = joined
            if force:
                raw = "".join(text_buffer)
                _, options = extract_options(raw)
                if options:
                    await self.offer_options(options)
                await self.deliver_files(raw)
                _clean, proposals = schedule_lib.extract(raw)
                if proposals:
                    await self.offer_schedules(proposals)

        async def flush_tools(force: bool = False) -> None:
            if not trace or self._rate_limited():
                return
            if state["tool_msg"] is not None and not force:
                interval = float(self.config.get("stream", "tool_edit_interval", 2.0))
                if time.time() - state.get("last_tool_edit", 0.0) < interval:
                    return
            state["last_tool_edit"] = time.time()
            blocks = [
                tool_block(entry.get("kind", "tool"), entry.get("title", ""),
                           entry.get("command", ""), entry.get("output", ""),
                           entry.get("status", ""), entry.get("subagent", ""),
                           entry.get("steps") or [], entry.get("brief", ""))
                for entry in trace.values()
            ]
            body = ""
            dropped = 0
            for block in reversed(blocks):
                candidate = block if not body else f"{block}\n{body}"
                if len(candidate) > TELEGRAM_LIMIT - 40:
                    dropped += 1
                    continue
                body = candidate
            if dropped:
                body = f"… {dropped} earlier call(s)\n{body}" if body else f"… {dropped} call(s)"
            if state["tool_msg"] is None:
                try:
                    state["tool_msg"] = await self.send_html(body)
                    sent.append(state["tool_msg"])
                except RetryAfter as exc:
                    self._note_rate_limit(exc)
                return
            try:
                await state["tool_msg"].edit_text(body, parse_mode=ParseMode.HTML)
            except RetryAfter as exc:
                self._note_rate_limit(exc)
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return
                log.debug("tool trace edit rejected (%s)", exc)

        async def flush_reasoning(force: bool = False) -> None:
            if not self.reasoning or not state["reasoning"].strip() or self._rate_limited():
                return
            now = time.time()
            interval = float(self.config.get("stream", "edit_interval", 1.0))
            if not force and now - state["last_edit"] < interval:
                return
            state["last_edit"] = now
            shown = state["reasoning"].strip()
            if len(shown) > REASONING_CHARS:
                shown = "…\n" + shown[-REASONING_CHARS:]
            body = f"💭 <i>thinking</i>\n<blockquote expandable>{esc(shown)}</blockquote>"
            if state["reason_msg"] is None:
                try:
                    state["reason_msg"] = await self.send_html(body)
                    sent.append(state["reason_msg"])
                except RetryAfter as exc:
                    self._note_rate_limit(exc)
                return
            try:
                await state["reason_msg"].edit_text(body, parse_mode=ParseMode.HTML)
            except RetryAfter as exc:
                self._note_rate_limit(exc)
            except BadRequest as exc:
                if "not modified" not in str(exc).lower():
                    log.debug("reasoning edit rejected: %s", exc)

        async def drain() -> None:
            while True:
                update = await self._updates.get()
                if update is None:
                    return
                if update.kind == "agent_message_chunk" and update.text:
                    text_buffer.append(update.text)
                    try:
                        await flush_text()
                    except RetryAfter as exc:
                        self._note_rate_limit(exc)
                elif update.kind == "agent_thought_chunk" and update.text and self.reasoning:
                    state["reasoning"] += update.text
                    await flush_reasoning()
                elif update.kind in {"tool_call", "tool_call_update"} and self.verbose:
                    raw = update.raw
                    call_id = str(raw.get("toolCallId") or f"anon{len(trace)}")
                    entry = trace.setdefault(call_id, {"kind": "tool"})
                    if update.kind == "tool_call" and _is_waiting(str(raw.get("title") or "")):
                        continue
                    if update.kind == "tool_call":
                        state["tools"] += 1
                        self.tools_used += 1
                        entry["kind"] = str(raw.get("kind") or "tool")
                        entry["title"] = str(raw.get("title") or "")
                        raw_input = raw.get("rawInput") or {}
                        entry["command"] = str(raw_input.get("command") or "")
                        tasks = raw_input.get("tasks")
                        if isinstance(tasks, list) and tasks:
                            entry["kind"] = "subagent"
                            # omp runs subagents through the `task` tool; ACP has no event of
                            # its own for them, so the task list is the only place they appear
                            entry["subagent"] = ", ".join(
                                f"{item.get('name', '?')} ({item.get('agent', 'task')})"
                                for item in tasks if isinstance(item, dict))[:80]
                            first = next((item for item in tasks if isinstance(item, dict)), {})
                            entry["brief"] = _task_brief(first.get("task", ""))
                            tools = first.get("tools")
                            if isinstance(tools, list) and tools:
                                entry["brief"] = (entry["brief"] + " · tools: "
                                                  + ", ".join(str(t) for t in tools))[:160]
                        log.info("tool call chat=%s kind=%s command=%r", self.chat_id,
                                 entry["kind"], entry["command"][:120] or entry["title"][:120])
                    entry["status"] = str(raw.get("status") or entry.get("status", ""))
                    steps = [item.get("text", "") for item in (raw.get("content") or [])
                             if isinstance(item, dict) and item.get("text")]
                    if steps:
                        kept = entry.setdefault("steps", [])
                        for step in steps:
                            clean = " ".join(str(step).split())[:110]
                            if clean and clean not in kept:
                                kept.append(clean)
                        del kept[:-4]
                    output = raw.get("rawOutput") or {}
                    chunks = [item.get("text", "") for item in (output.get("content") or [])
                              if isinstance(item, dict)]
                    if any(chunks):
                        entry["output"] = "\n".join(chunks)
                    await flush_tools()

        drain_task = asyncio.create_task(drain())
        try:
            async with self.typing():
                result = await asyncio.to_thread(
                    self.client.prompt, self.session_id, text,
                    images=images or None,
                    timeout=float(self.config.get("acp", "turn_timeout", 900.0)),
                )
        except AcpError:
            # A silent turn is abandoned here, so stop it in omp too: left running, its late
            # chunks land in the next turn's stream and the chat sees the answer twice.
            if self.client is not None and self.client.alive:
                with contextlib.suppress(AcpError):
                    self.client.cancel(self.session_id)
            # scheduled, not immediate: the sentinel must queue behind updates the reader thread
            # has already handed to the loop, or the last of a turn is dropped
            self._loop.call_soon_threadsafe(self._updates.put_nowait, None)
            await drain_task
            await flush_text(force=True)  # what did arrive is still worth showing
            self.busy = False
            # the agent died under the turn; only a deliberate stop/kill means it is finished
            if self._user_cancel:
                self.store.end_turn(self.key)
            raise
        self._loop.call_soon_threadsafe(self._updates.put_nowait, None)
        await drain_task
        await flush_reasoning(force=True)
        await flush_text(force=True)
        await flush_tools(force=True)
        self.busy = False
        self.store.touch(self.key)
        # A cancel nobody asked for is the gateway going down (systemd signals the agent too).
        # Keep the row so the next start resumes; /stop, /kill and /new clear it. A try/finally
        # here would delete it on exactly the path this table exists for.
        if result.stop_reason != "cancelled" or self._user_cancel:
            self.store.end_turn(self.key)

        usage = result.usage or {}
        self.usage["input"] += int(usage.get("inputTokens") or 0)
        self.usage["output"] += int(usage.get("outputTokens") or 0)
        self.usage["cached"] += int(usage.get("cachedReadTokens") or 0)
        self.usage["turns"] += 1
        log.info("turn end chat=%s stop=%s tools=%s in=%s out=%s cached=%s duration=%.1fs",
                 self.chat_id, result.stop_reason, state["tools"],
                 usage.get("inputTokens"), usage.get("outputTokens"),
                 usage.get("cachedReadTokens"), time.time() - started)
        anchor = state["text_msg"] or message
        if self.config.get("stream", "show_usage", True) and anchor is not None:
            try:
                await anchor.reply_text(
                    f"{result.stop_reason} · in {usage.get('inputTokens', '?')} "
                    f"out {usage.get('outputTokens', '?')} cached {usage.get('cachedReadTokens', 0)}"
                )
            except TelegramError:
                pass
        return result.text

    async def send_html(self, body: str):
        return await send_html(self.app.bot, self.chat_id, body)

    async def send_md(self, text: str):
        return await send_md(self.app.bot, self.chat_id, text)

    async def edit_md(self, message: Any, text: str) -> None:
        try:
            await message.edit_text(to_html(text), parse_mode=ParseMode.HTML)
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.warning("html edit rejected (%s); falling back to plain text", exc)
            await message.edit_text(to_plain(text))

    @contextlib.asynccontextmanager
    async def typing(self):
        """Keep the Telegram 'typing…' indicator alive for the duration of a turn."""
        stop = asyncio.Event()

        async def loop() -> None:
            while not stop.is_set():
                try:
                    await self.app.bot.send_chat_action(self.chat_id, ChatAction.TYPING)
                except TelegramError:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=4.0)
                except TimeoutError:
                    continue

        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            stop.set()
            task.cancel()

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
            log.warning("steer failed chat=%s: %s", self.chat_id, exc)
            return "failed", str(exc)
        log.info("steer chat=%s outcome=%s chars=%s reason=%s", self.chat_id, outcome, len(text),
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

    async def shutdown(self) -> None:
        if self.client is not None:
            await asyncio.to_thread(self.client.close, self.session_id)
            self.client = None
        self.store.close()

    async def new_session(self) -> str:
        self.forget_turn()
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
        self.store.put(self.key, session_id, self.cwd)
        await self.ensure()

    async def fork(self) -> str:
        await self.ensure()
        result = await asyncio.to_thread(self.client.request, "session/fork",
                                          {"sessionId": self.session_id, "cwd": self.cwd})
        new_id = str(result.get("sessionId") or "")
        if new_id:
            self.session_id = new_id
            self.store.put(self.key, new_id, self.cwd)
        return new_id


class Transport:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.started_at = time.time()
        # per-chat catalogs and the live `omp login` processes
        self._login_cache: list[Any] = []
        self._login_cache_at = 0.0
        self._skills: dict[int, list[Any]] = {}
        self.logins: dict[int, Any] = {}
        self._login_seen: dict[int, list[str]] = {}
        self._login_before: dict[int, dict[str, tuple[str, str]]] = {}
        self.credential_db = str(config.get("agent", "credential_db", "~/.omp/agent/agent.db"))
        self.token = str(config.get("telegram", "bot_token", "") or "")
        self.allowed = {int(str(u)) for u in (config.get("telegram", "allowed_users") or []) if str(u).strip()}
        self.agents: dict[int, ChatAgent] = {}
        self.queues: dict[int, asyncio.Queue[tuple[str, Any]]] = {}
        self.workers: dict[int, asyncio.Task] = {}
        # set on SIGTERM: the worker stops taking turns, so the interrupted one stays on record
        self.stopping = False

    # ---------------------------------------------------------------- helpers
    async def send_html(self, chat_id: int, body: str):
        return await send_html(self.app.bot, chat_id, body)

    async def send_md(self, chat_id: int, text: str):
        return await send_md(self.app.bot, chat_id, text)

    def agent_for(self, chat_id: int) -> ChatAgent:
        agent = self.agents.get(chat_id)
        if agent is None:
            agent = ChatAgent(self.config, chat_id, f"tg:{chat_id}", self.app)
            self.agents[chat_id] = agent
        return agent

    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        if not self.allowed:
            return False
        return user.id in self.allowed

    async def cmd_schedule(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        chat_id = update.effective_chat.id
        agent = self.agent_for(chat_id)
        args = list(ctx.args or [])
        action = args[0].lower() if args else "list"
        rest = args[1:]
        if action in ("remove", "rm") and rest:
            return await agent._schedule_remove(rest[0])
        if action == "run" and rest:
            return await agent._schedule_run(rest[0])
        items = schedule_lib.load()
        if not items:
            return await self.send_md(
                "no schedules yet. Just ask, for example: \"every morning at 7, summarise the repo\".")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"▶ {name}", callback_data=f"sched:run:{name}"),
             InlineKeyboardButton(f"remove {name}", callback_data=f"sched:rm:{name}")]
            for name in sorted(items)
        ])
        rows = []
        for name in sorted(items):
            item = items[name]
            rows.append(f"{name} · {item.cron or item.calendar} · {item.timezone} → {item.gateway}"
                        f" · {item.last_status or 'never ran'}")
        await self.app.bot.send_message(
            self.chat_id, card("schedules", f"{len(items)}", [("scheduled", "\n".join(rows))],
                               "run one now or remove it with the buttons"),
            parse_mode=ParseMode.HTML, reply_markup=keyboard)

    async def _deny(self, update: Update) -> None:
        user = update.effective_user
        if update.effective_message is not None:
            await update.effective_message.reply_text(
                f"not authorized. your user id: {user.id if user else 'unknown'}"
            )

    # ----------------------------------------------------------------- queue
    def enqueue(self, chat_id: int, kind: str, payload: Any) -> int:
        queue = self.queues.setdefault(chat_id, asyncio.Queue())
        queue.put_nowait((kind, payload))
        if chat_id not in self.workers or self.workers[chat_id].done():
            self.workers[chat_id] = asyncio.create_task(self._worker(chat_id))
        return queue.qsize()

    async def _worker(self, chat_id: int) -> None:
        queue = self.queues[chat_id]
        agent = self.agent_for(chat_id)
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=IDLE_CHECK_INTERVAL)
            except TimeoutError:
                idle = float(self.config.get("session", "idle_timeout", 1800))
                if not agent.busy and time.time() - agent.last_used > idle and agent.client is not None:
                    log.info("idle eviction for chat %s", chat_id)
                    await agent.shutdown()
                if queue.empty():
                    return
                continue
            try:
                if self.stopping:
                    # a turn started now would be cut by the shutdown and overwrite the record
                    # of the one that was really interrupted
                    continue
                if kind == "prompt":
                    message = payload
                    try:
                        await agent.ask(message.text, message=message,
                                        images=getattr(payload, "images", None),
                                        resumes=int(getattr(payload, "resumes", 0)),
                                        record=getattr(payload, "original", None))
                    except AcpError as exc:
                        if not self.stopping:  # the shutdown killed it; the next start resumes
                            await message.reply_text(f"agent error: {exc}")
                    except Exception as exc:
                        log.exception("turn failed")
                        await message.reply_text(f"turn failed: {exc}")
                elif kind == "resume":
                    message, session_id = payload
                    try:
                        await agent.load(session_id)
                        log.info("resume chat=%s session=%s", chat_id, session_id[:8])
                        await message.reply_text(
                            self.session_card(agent, "⏪ resumed", "history lives in the agent session"),
                            parse_mode=ParseMode.HTML,
                        )
                    except AcpError as exc:
                        await message.reply_text(f"resume failed: {exc}")
            finally:
                queue.task_done()

    # -------------------------------------------------------------- handlers
    async def on_text(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        message = update.effective_message
        session = self.logins.get(update.effective_chat.id)
        if session is not None and session.alive and session.awaiting_code and message and message.text:
            session.send(message.text)
            log.info("login code relayed chat=%s chars=%s", update.effective_chat.id,
                     len(message.text.strip()))
            await message.reply_text("🔑 code sent to omp, waiting for the result…")
            return
        if message is None or not message.text:
            return
        await self.submit(update.effective_chat.id, message, message)

    async def submit(self, chat_id: int, payload: Any, message: Any) -> None:
        """Route one incoming message: into the running turn, or into the queue.

        By default a message sent while the agent works steers that turn, the way a person
        interrupts a colleague. `[telegram] busy_message = "queue"` restores the old FIFO, and
        `/queue` always queues. Anything omp declines to inject still lands in the queue.
        """
        agent = self.agent_for(chat_id)
        if not agent.busy:
            self.enqueue(chat_id, "prompt", payload)
            return
        if str(self.config.get("telegram", "busy_message", "steer")) == "steer":
            outcome, reason = await agent.steer(payload.text, getattr(payload, "images", None) or None)
            if outcome == "injected":
                await self._react(message, "👀", "steering the running turn")
                return
            if outcome == "promptRequired":
                # the turn ended between the busy check and the steer: this is a fresh prompt
                self.enqueue(chat_id, "prompt", payload)
                return
            log.warning("steer not accepted chat=%s outcome=%s reason=%s; queueing", chat_id,
                        outcome, reason or "-")
        depth = self.enqueue(chat_id, "prompt", payload)
        await message.reply_text(f"queued (position {depth}). the running turn finishes first.")

    async def _react(self, message: Any, emoji: str, fallback: str) -> None:
        """Acknowledge without adding a message to the chat; a reply if reactions are refused."""
        try:
            await message.set_reaction(emoji)
        except (TelegramError, AttributeError):
            try:
                await message.reply_text(fallback)
            except TelegramError:
                pass

    def _agent_gate(self, agent: ChatAgent) -> str:
        """What the agent itself was started with, which is what decides whether anything asks."""
        if getattr(agent, "yolo", False):
            return "yolo · no tool asks"
        mode = str(self.config.get("agent", "approval_mode", "write") or "write")
        if approval_policy(self.config) == "allow":
            return f"{mode} · auto-allow · nothing asks"
        if approval_policy(self.config) == "deny":
            return f"{mode} · auto-deny"
        timeout = float(self.config.get("approval", "timeout", PERMISSION_TIMEOUT))
        return f"{mode} · asks · {timeout:.0f}s"

    def session_card(self, agent: ChatAgent, title: str, note: str = "") -> str:
        options = getattr(agent, "options", {}) or {}
        rows = [
            ("session", agent.session_id[:8]),
            ("model", (options.get("model") or {}).get("currentValue", "?")),
            ("mode", (options.get("mode") or {}).get("currentValue", "?")),
            ("thinking", (options.get("thinking") or {}).get("currentValue", "-")),
            ("approval", self._agent_gate(agent)),
            ("yolo", "on · nothing asks" if agent.yolo else "off"),
            ("cwd", agent.cwd),
        ]
        return card(title, now_wib(), rows, note)

    async def cmd_new(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        await agent.new_session()
        log.info("new session chat=%s session=%s", update.effective_chat.id, agent.session_id[:8])
        await update.effective_message.reply_text(
            self.session_card(agent, "🆕 new session", "old sessions stay resumable with /sessions"),
            parse_mode=ParseMode.HTML,
        )

    async def cmd_stop(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        ok = await agent.cancel()
        await update.effective_message.reply_text("cancel sent" if ok else "nothing running")

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        queue = self.queues.get(update.effective_chat.id)
        alive = agent.client is not None and agent.client.alive
        options = getattr(agent, "options", {}) or {}
        rows = [
            ("session", agent.session_id[:8] or "-"),
            ("process", f"{'alive' if alive else 'stopped'} · {fmt_uptime(time.time() - agent.started_at)}"),
            ("state", "busy" if agent.busy else "idle"),
            ("queue", queue.qsize() if queue else 0),
            ("model", (options.get("model") or {}).get("currentValue", "?")),
            ("mode", (options.get("mode") or {}).get("currentValue", "?")),
            ("thinking", (options.get("thinking") or {}).get("currentValue", "-")),
            ("cwd", agent.cwd),
            ("approval", approval_policy(self.config)),
            ("yolo", "on" if agent.yolo else "off"),
            ("trace", "verbose" if agent.verbose else "quiet"),
            ("work", f"{agent.turns} turns · {agent.tools_used} tools"),
            ("clock", now_wib()),
        ]
        await update.effective_message.reply_text(
            card("📊 status", "", rows), parse_mode=ParseMode.HTML
        )

    async def cmd_usage(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        usage = agent.usage
        rows = [
            ("turns", usage["turns"]),
            ("input", f"{usage['input']:,}"),
            ("output", f"{usage['output']:,}"),
            ("cached", f"{usage['cached']:,}"),
            ("tools", agent.tools_used),
        ]
        await update.effective_message.reply_text(
            card("🧮 usage", f"session {agent.session_id[:8]}", rows,
                 "counters reset when the agent process restarts"),
            parse_mode=ParseMode.HTML,
        )

    async def on_photo(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward a Telegram photo to the agent as an ACP image block."""
        if not self._authorized(update):
            return await self._deny(update)
        message = update.effective_message
        if message is None:
            return
        chat_id = update.effective_chat.id
        source = None
        mime = "image/jpeg"
        if message.photo:
            source = message.photo[-1]
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            source = message.document
            mime = message.document.mime_type or mime
        if source is None:
            return
        try:
            telegram_file = await source.get_file()
            raw = bytes(await telegram_file.download_as_bytearray())
        except TelegramError as exc:
            log.warning("photo download failed chat=%s: %s", chat_id, exc)
            return await message.reply_text(f"could not fetch the image: {exc}")
        block = {"type": "image", "mimeType": mime, "data": base64.b64encode(raw).decode("ascii")}
        caption = (message.caption or "").strip() or "Describe this image."
        log.info("photo chat=%s bytes=%s caption=%r", chat_id, len(raw), caption[:120])
        payload = _TextOnly(caption, message, images=[block])
        await self.submit(chat_id, payload, message)

    async def on_document(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Save a document from the chat and hand the agent its path.

        ACP has no general attachment block, and the agent has a shell: a readable path is more
        useful than a content type the transport would have to guess at.
        """
        if not self._authorized(update):
            return await self._deny(update)
        message = update.effective_message
        if message is None or message.document is None:
            return
        chat_id = update.effective_chat.id
        document = message.document
        max_mb = float(self.config.get("files", "max_mb", 20.0))
        if document.file_size and max_mb and document.file_size > max_mb * 1024 * 1024:
            return await message.reply_text(
                f"too large: {document.file_size / (1024 * 1024):.1f} MB, the limit is {max_mb:g} MB")
        try:
            telegram_file = await document.get_file()
            raw = bytes(await telegram_file.download_as_bytearray())
        except TelegramError as exc:
            log.warning("document download failed chat=%s: %s", chat_id, exc)
            return await message.reply_text(f"could not fetch the file: {exc}")
        base = Path(str(self.config.get("files", "incoming_dir", "~/.local/state/ompwire/files")))
        directory = base.expanduser() / str(chat_id)
        try:
            target = save_incoming(raw, safe_name(document.file_name or "file"), directory, max_mb)
        except (OSError, ValueError) as exc:
            log.warning("document save failed chat=%s: %s", chat_id, exc)
            return await message.reply_text(f"could not save the file: {exc}")
        log.info("document chat=%s saved=%s bytes=%s", chat_id, target, len(raw))
        payload = _TextOnly(
            incoming_summary(target, document.mime_type or "", message.caption or ""), message)
        await self.submit(chat_id, payload, message)

    SKILL_PAGE = 10   # two columns x five rows, same shape as the model picker
    LOGIN_PAGE = 10

    async def cmd_skills(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        chat_id = update.effective_chat.id
        agent = self.agent_for(chat_id)
        skills = await asyncio.to_thread(list_skills, cwd=agent.cwd)
        self._skills[chat_id] = skills
        if not skills:
            return await update.effective_message.reply_text("no skills found")
        page = 1
        if ctx.args and ctx.args[0].isdigit():
            page = int(ctx.args[0])
        window, index, pages = paginate(skills, page - 1, self.SKILL_PAGE)
        buttons = [
            InlineKeyboardButton(skill.name[:28], callback_data=f"skv:{index * self.SKILL_PAGE + offset}")
            for offset, skill in enumerate(window)
        ]
        rows = two_columns(buttons)
        nav: list[InlineKeyboardButton] = []
        if pages > 1:
            if index > 0:
                nav.append(InlineKeyboardButton("◀", callback_data=f"skl:{index}"))
            nav.append(InlineKeyboardButton(f"{index + 1}/{pages}", callback_data="sk:noop"))
            if index < pages - 1:
                nav.append(InlineKeyboardButton("▶", callback_data=f"skl:{index + 2}"))
        if nav:
            rows.append(nav)
        log.info("skills listed chat=%s count=%s page=%s/%s", chat_id, len(skills), index + 1, pages)
        await update.effective_message.reply_text(
            card("🧩 skills", f"{len(skills)} available · page {index + 1}/{pages}", [],
                 "tap one to read its SKILL.md"),
            reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
        )

    async def show_skill(self, chat_id: int, index: int) -> None:
        skills = self._skills.get(chat_id) or []
        if not (0 <= index < len(skills)):
            return await self.app.bot.send_message(chat_id, "unknown skill, open /skills again")
        skill = skills[index]
        body = await asyncio.to_thread(skill_body, skill)
        log.info("skill opened chat=%s name=%s chars=%s", chat_id, skill.name, len(body))
        await self.send_html(chat_id, card("🧩 " + skill.name, skill.description[:160] or str(skill.path),
                                  [("file", str(skill.path)), ("size", f"{len(body)} chars")]))
        chunks = split_blocks(body, 3200)
        for chunk in chunks[:4]:
            await self.send_md(chat_id, chunk)
        if len(chunks) > 4:
            await self.app.bot.send_message(
                chat_id, f"<i>truncated · full text in {esc(str(skill.path))}</i>",
                parse_mode=ParseMode.HTML,
            )

    async def _render_skills(self, chat_id: int, page: int, message: Any) -> None:
        skills = self._skills.get(chat_id) or []
        if not skills:
            return
        window, index, pages = paginate(skills, max(0, page - 1), self.SKILL_PAGE)
        buttons = [InlineKeyboardButton(skill.name[:28], callback_data=f"skv:{skills.index(skill)}")
                   for skill in window]
        rows = two_columns(buttons)
        nav = []
        if pages > 1:
            if index > 0:
                nav.append(InlineKeyboardButton("◀", callback_data=f"skl:{index}"))
            nav.append(InlineKeyboardButton(f"{index + 1}/{pages}", callback_data="sk:noop"))
            if index < pages - 1:
                nav.append(InlineKeyboardButton("▶", callback_data=f"skl:{index + 2}"))
        if nav:
            rows.append(nav)
        try:
            await message.edit_text(
                card("🧩 skills", f"{len(skills)} available · page {index + 1}/{pages}", [],
                     "tap one to read its SKILL.md"),
                reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            pass

    async def _render_login(self, chat_id: int, page: int, message: Any) -> None:
        providers = self._login_cache
        if not providers:
            return
        window, index, pages = paginate(providers, max(0, page - 1), self.LOGIN_PAGE)
        buttons = []
        for provider in window:
            mark = "✅ " if provider.logged_in else ("⚪ " if provider.provider_id else "❔ ")
            buttons.append(InlineKeyboardButton(f"{mark}{provider.label[:24]}",
                                                callback_data=f"lgv:{provider.index}"))
        rows = two_columns(buttons)
        nav = []
        if pages > 1:
            if index > 0:
                nav.append(InlineKeyboardButton("◀", callback_data=f"lgl:{index}"))
            nav.append(InlineKeyboardButton(f"{index + 1}/{pages}", callback_data="lg:noop"))
            if index < pages - 1:
                nav.append(InlineKeyboardButton("▶", callback_data=f"lgl:{index + 2}"))
        if nav:
            rows.append(nav)
        signed = sum(1 for provider in providers if provider.logged_in)
        try:
            await message.edit_text(
                card("🔐 login", f"{signed} signed in · {len(providers)} providers · page {index + 1}/{pages}",
                     [], "✅ signed in · ⚪ not signed in · ❔ id unknown, opened via menu"),
                reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            pass

    async def _provider_list(self, force: bool = False) -> list[Any]:
        fresh = time.time() - self._login_cache_at < 600
        if self._login_cache and fresh and not force:
            return self._login_cache
        providers = await asyncio.to_thread(list_providers, str(self.config.get("acp", "command", "omp")))
        self._login_cache = providers
        self._login_cache_at = time.time()
        log.info("providers loaded: %s entries, %s signed in", len(providers),
                 sum(1 for p in providers if p.logged_in))
        return providers

    async def cmd_login(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        message = await update.effective_message.reply_text("reading the provider list from omp…")
        providers = await self._provider_list(force=True)
        if not providers:
            return await message.edit_text("omp returned an empty provider list")
        page = 1
        if ctx.args and ctx.args[0].isdigit():
            page = int(ctx.args[0])
        window, index, pages = paginate(providers, page - 1, self.LOGIN_PAGE)
        buttons = []
        for provider in window:
            mark = "✅ " if provider.logged_in else ("⚪ " if provider.provider_id else "❔ ")
            buttons.append(InlineKeyboardButton(
                f"{mark}{provider.label[:24]}", callback_data=f"lgv:{provider.index}"))
        rows = two_columns(buttons)
        nav: list[InlineKeyboardButton] = []
        if pages > 1:
            if index > 0:
                nav.append(InlineKeyboardButton("◀", callback_data=f"lgl:{index}"))
            nav.append(InlineKeyboardButton(f"{index + 1}/{pages}", callback_data="lg:noop"))
            if index < pages - 1:
                nav.append(InlineKeyboardButton("▶", callback_data=f"lgl:{index + 2}"))
        if nav:
            rows.append(nav)
        signed = sum(1 for provider in providers if provider.logged_in)
        await message.edit_text(
            card("🔐 login", f"{signed} signed in · {len(providers)} providers · page {index + 1}/{pages}",
                 [], "✅ signed in · ⚪ not signed in · ❔ id unknown, opened via menu"),
            reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
        )

    async def start_login(self, chat_id: int, index: int) -> None:
        providers = self._login_cache or await self._provider_list()
        provider = next((item for item in providers if item.index == index), None)
        if provider is None:
            return await self.app.bot.send_message(chat_id, "unknown provider")
        target = provider.provider_id or ""   # empty -> choose from omp's menu
        self.close_login(chat_id)
        loop = asyncio.get_running_loop()

        def emit(line: str) -> None:
            asyncio.run_coroutine_threadsafe(self._login_line(chat_id, provider.label, line), loop)

        def done(ok: bool, output: str) -> None:
            asyncio.run_coroutine_threadsafe(self._login_done(chat_id, provider.label, ok, output), loop)

        session = LoginSession(
            target, emit, done,
            command=str(self.config.get("acp", "command", "omp")),
            menu_index=None if target else provider.index,
        )
        self.logins[chat_id] = session
        self._login_seen[chat_id] = []
        self._login_before[chat_id] = await asyncio.to_thread(credential_snapshot, self.credential_db)
        log.info("login started chat=%s provider=%s target=%s (credentials before: %s)",
                 chat_id, provider.label, target, len(self._login_before[chat_id]))
        await self.app.bot.send_message(
            chat_id,
            card("🔐 login started", provider.label,
                 [("target", target or f"menu #{provider.index}")],
                 "follow the steps below, then paste the authorization code into this chat"),
            parse_mode=ParseMode.HTML,
        )

    # omp's own console chatter; the chat only needs the actionable lines
    LOGIN_NOISE = ("Local shortcut", "Preferred port", "Waiting for browser authentication",
                   "Complete login in your browser")

    async def _login_line(self, chat_id: int, label: str, line: str) -> None:
        text = line.strip()
        if not text or any(noise in text for noise in self.LOGIN_NOISE):
            return
        seen = self._login_seen.setdefault(chat_id, [])
        if text in seen:
            return  # omp redraws the same prompt on every step; the chat does not need it twice
        seen.append(text)
        del seen[:-40]
        session = self.logins.get(chat_id)
        if session is not None and session.url and session.url in text:
            rows = [[InlineKeyboardButton("open login page", url=session.url)]]
            await self.app.bot.send_message(
                chat_id,
                card("🔗 open this URL", label, [], "after approving, paste the code or redirect URL here"),
                reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
            )
            return
        if session is not None and session.awaiting_code:
            await self.app.bot.send_message(
                chat_id, "🔑 omp is waiting for the authorization code — paste it here.")
            return
        if len(text) <= 200:
            safe = esc(text)
            await self.app.bot.send_message(chat_id, f"<code>{safe}</code>", parse_mode=ParseMode.HTML)

    async def _login_done(self, chat_id: int, label: str, ok: bool, output: str) -> None:
        self.logins.pop(chat_id, None)
        self._login_cache_at = 0.0  # force a status refresh on the next /login
        before = self._login_before.pop(chat_id, {})
        after = await asyncio.to_thread(credential_snapshot, self.credential_db)
        added = sorted(set(after) - set(before))
        refreshed = sorted(p for p in set(after) & set(before) if after[p] != before[p])
        if added:
            stored = f"new credential: {', '.join(added)}"
        elif refreshed:
            stored = f"credential refreshed: {', '.join(refreshed)}"
        else:
            stored = f"no change detected ({len(after)} in store)"
        log.info("login finished chat=%s provider=%s ok=%s added=%s refreshed=%s total=%s",
                 chat_id, label, ok, added or "-", refreshed or "-", len(after))
        rows = [
            ("provider", label),
            ("result", "ok" if ok else "failed"),
            ("store", stored),
            ("db", self.credential_db),
        ]
        note = ("credential is in omp's store; the agent restarts so the new provider "
                "appears in /model" if ok else "see the output below")
        await self.app.bot.send_message(
            chat_id, card("✅ login complete" if ok else "⛔ login failed", now_wib(), rows, note),
            parse_mode=ParseMode.HTML,
        )
        if output and not ok:
            await self.app.bot.send_message(
                chat_id, f"<pre>{esc(output[-400:])}</pre>", parse_mode=ParseMode.HTML)
        if ok:
            agent = self.agent_for(chat_id)
            try:
                await agent.respawn(yolo=agent.yolo)
                log.info("agent respawned after login chat=%s", chat_id)
            except Exception as exc:
                log.warning("respawn after login failed chat=%s: %s", chat_id, exc)

    def close_login(self, chat_id: int) -> None:
        session = self.logins.pop(chat_id, None)
        if session is not None:
            session.close()
            log.info("login session closed chat=%s", chat_id)

    async def cmd_context(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """The agent renders a better context report than we could, so ask it."""
        if not self._authorized(update):
            return await self._deny(update)
        chat_id = update.effective_chat.id
        agent = self.agent_for(chat_id)
        payload = _TextOnly("/context", update.effective_message)
        depth = self.enqueue(chat_id, "prompt", payload)
        log.info("context report requested chat=%s busy=%s queue=%s", chat_id, agent.busy, depth)
        if agent.busy:
            await update.effective_message.reply_text(f"queued (position {depth})")

    async def cmd_sessions(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        await agent.ensure()
        data = await asyncio.to_thread(agent.client.list_sessions, agent.cwd)
        rows = (data.get("sessions") or [])[:10]
        if not rows:
            return await update.effective_message.reply_text("no sessions recorded")
        table = "\n".join(
            f"{str(row.get('sessionId') or '')[:8]}  {str(row.get('updatedAt') or '')[:16]}  "
            f"{((row.get('_meta') or {}).get('size') or '?')}B"
            for row in rows
        )
        body = card("🗂 sessions", f"{len(rows)} of {len(data.get('sessions') or [])} · {agent.cwd}",
                    [], "/resume <id> to continue one")
        body = body.replace("</i>", "</i>\n<pre>" + esc(table) + "</pre>")
        await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML)

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        if not ctx.args:
            return await update.effective_message.reply_text("usage: /resume <session-id>")
        prefix = ctx.args[0]
        agent = self.agent_for(update.effective_chat.id)
        await agent.ensure()
        data = await asyncio.to_thread(agent.client.list_sessions, agent.cwd)
        match = next((str(r.get("sessionId")) for r in (data.get("sessions") or [])
                      if str(r.get("sessionId", "")).startswith(prefix)), None)
        if match is None:
            return await update.effective_message.reply_text(f"no session starting with {prefix}")
        self.enqueue(update.effective_chat.id, "resume", (update.effective_message, match))

    async def cmd_continue(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        record = agent.store.get(agent.key)
        if record is None:
            return await update.effective_message.reply_text("nothing to continue, use /new")
        self.enqueue(update.effective_chat.id, "resume", (update.effective_message, record.session_id))

    async def cmd_fork(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        try:
            await agent.fork()
            log.info("fork chat=%s session=%s", update.effective_chat.id, agent.session_id[:8])
            await update.effective_message.reply_text(
                self.session_card(agent, "🌿 forked", "context copied into a new session id"),
                parse_mode=ParseMode.HTML,
            )
        except AcpError as exc:
            await update.effective_message.reply_text(f"fork failed: {exc}")

    async def cmd_cwd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        if not ctx.args:
            agent = self.agent_for(update.effective_chat.id)
            return await update.effective_message.reply_text(f"cwd {agent.cwd}")
        import os
        target = os.path.expanduser(ctx.args[0])
        if not os.path.isdir(target):
            return await update.effective_message.reply_text(f"not a directory: {target}")
        agent = self.agent_for(update.effective_chat.id)
        agent.cwd = target
        await agent.new_session()
        log.info("cwd change chat=%s cwd=%s session=%s", update.effective_chat.id, target,
                 agent.session_id[:8])
        await update.effective_message.reply_text(
            self.session_card(agent, "📁 cwd changed", "a new session starts in that directory"),
            parse_mode=ParseMode.HTML,
        )

    # -------------------------------------------------- config pickers (UI)
    PAGE_SIZE = 8          # single-column configs (mode, thinking)
    PAGE_SIZE_TWO_COL = 10  # two columns x five rows

    def _values(self, agent: ChatAgent, config_id: str) -> list[str]:
        option = (getattr(agent, "options", {}) or {}).get(config_id) or {}
        return [str(o.get("value")) for o in (option.get("options") or [])]

    def _current(self, agent: ChatAgent, config_id: str) -> str:
        option = (getattr(agent, "options", {}) or {}).get(config_id) or {}
        return str(option.get("currentValue") or "?")

    def _providers(self, agent: ChatAgent, config_id: str) -> list[tuple[str, list[int]]]:
        """Group option values by the provider prefix of their id."""
        values = self._values(agent, config_id)
        grouped: dict[str, list[int]] = {}
        for index, value in enumerate(values):
            provider = value.split("/", 1)[0] if "/" in value else "other"
            grouped.setdefault(provider, []).append(index)
        return sorted(grouped.items())

    @staticmethod
    def _page_slice(values: list[str], page: int, size: int) -> tuple[list[str], int, int]:
        """Return (window, clamped_page, total_pages) for a fixed-size page."""
        pages = max(1, (len(values) + size - 1) // size)
        page = min(max(0, page), pages - 1)
        return values[page * size:(page + 1) * size], page, pages

    async def _show_config(
        self,
        agent: ChatAgent,
        chat_id: int,
        config_id: str,
        screen: str = "l",
        provider: int = 0,
        page: int = 0,
        edit: Any = None,
    ) -> None:
        current = self._current(agent, config_id)
        providers = self._providers(agent, config_id)
        multi_provider = len(providers) > 1
        all_values = self._values(agent, config_id)

        if screen == "p" and multi_provider:
            rows: list[list[InlineKeyboardButton]] = []
            row: list[InlineKeyboardButton] = []
            for index, (name, indices) in enumerate(providers):
                label = f"{name} ({len(indices)})"
                if name == (current.split("/", 1)[0] if "/" in current else "other"):
                    label = f"• {label}"
                row.append(InlineKeyboardButton(label[:60], callback_data=f"o:{config_id}:l:{index}:0"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            text = f"{config_id}: {current}\n{len(providers)} providers · pick one"
        else:
            if multi_provider:
                provider = provider if 0 <= provider < len(providers) else 0
                provider_name, indices = providers[provider]
                subset = [all_values[i] for i in indices]
            else:
                provider, provider_name, indices = 0, config_id, list(range(len(all_values)))
                subset = all_values
            columns = 2 if config_id == "model" else 1
            per_page = self.PAGE_SIZE_TWO_COL if columns == 2 else self.PAGE_SIZE
            window, page, pages = self._page_slice(subset, page, per_page)
            start = page * per_page
            rows = []
            pending: list[InlineKeyboardButton] = []
            for offset, value in enumerate(window):
                global_index = indices[start + offset]
                label = value.split("/", 1)[1] if (columns == 2 and "/" in value) else value
                mark = "• " if value == current else ""
                button = InlineKeyboardButton(f"{mark}{label}"[:64],
                                              callback_data=f"o:{config_id}:s:{global_index}:0")
                if columns == 2:
                    pending.append(button)
                    if len(pending) == 2:
                        rows.append(pending)
                        pending = []
                else:
                    rows.append([button])
            if pending:
                rows.append(pending)
            nav: list[InlineKeyboardButton] = []
            if multi_provider:
                nav.append(InlineKeyboardButton("◀ providers", callback_data=f"o:{config_id}:p:0:0"))
            if pages > 1:
                if page > 0:
                    nav.append(InlineKeyboardButton("◀", callback_data=f"o:{config_id}:l:{provider}:{page - 1}"))
                nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="o:noop"))
                if page < pages - 1:
                    nav.append(InlineKeyboardButton("▶", callback_data=f"o:{config_id}:l:{provider}:{page + 1}"))
            if nav:
                rows.append(nav)
            text = (
                f"{config_id}: {current}\n{provider_name} · {len(subset)} option(s)"
                + (f" · page {page + 1}/{pages}" if pages > 1 else "")
            )

        markup = InlineKeyboardMarkup(rows)
        if edit is not None:
            try:
                await edit.edit_text(text, reply_markup=markup)
                return
            except BadRequest:
                pass
        await self.app.bot.send_message(chat_id, text, reply_markup=markup)

    async def _apply_pick(self, agent: ChatAgent, config_id: str, value: str) -> tuple[str, str]:
        """Set a config option; returns (previous, current) so the caller can render once."""
        previous = self._current(agent, config_id)
        result = await asyncio.to_thread(agent.client.set_config_option, agent.session_id, config_id, value)
        agent.options = {opt.get("id"): opt for opt in (result.get("configOptions") or [])}
        current = self._current(agent, config_id)
        if config_id in PREF_KEYS and current:
            agent.store.set_pref(agent.key, config_id, current)
        log.info("config change %s: %s -> %s (session=%s, persisted=%s)", config_id, previous,
                 current, agent.session_id[:8], config_id in PREF_KEYS)
        return previous, current

    async def cmd_model(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        await agent.ensure()
        await self._show_config(agent, update.effective_chat.id, "model", screen="p")

    async def cmd_mode(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        await agent.ensure()
        await self._show_config(agent, update.effective_chat.id, "mode")

    async def cmd_thinking(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        await agent.ensure()
        await self._show_config(agent, update.effective_chat.id, "thinking")

    async def announce(self, chat_id: int, line: str) -> None:
        """Post a one-line status/config notice and mirror it into the log."""
        log.info("notice chat=%s %s", chat_id, line.replace("\n", " | "))
        try:
            await self.app.bot.send_message(chat_id, line, parse_mode=ParseMode.HTML)
        except TelegramError as exc:
            log.warning("notice send failed chat=%s: %s", chat_id, exc)

    def approval_card(self, policy: str, footer: str = "") -> str:
        timeout = float(self.config.get("approval", "timeout", 120))
        rows = [
            ("ask", "buttons in chat, then allow or reject"),
            ("allow", "every tool call auto-approved"),
            ("deny", "every tool call auto-rejected"),
            ("timeout", f"{timeout:.0f}s, then deny"),
        ]
        return card("🛡️ approval", f"{policy} · {now_wib()}", rows,
                    footer or "pick a policy below")

    def approval_keyboard(self, policy: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(("• " if value == policy else "") + value, callback_data=f"ap:{value}")]
            for value in ("ask", "allow", "deny")
        ])

    async def cmd_approve(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        chat_id = update.effective_chat.id
        policy = approval_policy(self.config)
        if not ctx.args:
            return await update.effective_message.reply_text(
                self.approval_card(policy), reply_markup=self.approval_keyboard(policy),
                parse_mode=ParseMode.HTML,
            )
        value = ctx.args[0].lower()
        if value not in {"ask", "allow", "deny"}:
            return await update.effective_message.reply_text("usage: /approve [ask|allow|deny]")
        await self.set_policy(chat_id, value)

    async def set_policy(self, chat_id: int, value: str, anchor: Any = None) -> str:
        """Persist a new approval policy. Returns changed | unchanged."""
        previous = approval_policy(self.config)
        if value == previous:
            log.info("approval.policy unchanged (%s) chat=%s", value, chat_id)
            return "unchanged"
        self.config.data.setdefault("approval", {})["policy"] = value
        path = save_override("approval", "policy", value)
        log.info("config change approval.policy %s -> %s (persisted to %s)", previous, value, path)
        body = self.approval_card(value, f"saved · {previous} → {value} · persisted")
        if anchor is not None:
            try:
                await anchor.edit_text(body, parse_mode=ParseMode.HTML)
                return "changed"
            except BadRequest as exc:
                log.debug("approval anchor edit failed: %s", exc)
        await self.announce(chat_id, body)
        return "changed"

    async def cmd_verbose(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        agent.verbose = not agent.verbose
        await self.announce(update.effective_chat.id,
                           f"tool trace: {'on' if agent.verbose else 'off'}")

    async def cmd_reasoning(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show or hide the agent's thinking.

        ACP sends it as agent_thought_chunk and the transport used to drop it. Off by default:
        at thinking=xhigh the thought stream is longer than the answer, and reading it every turn
        is noise. On demand, it is the most direct way to see what the agent is actually weighing.
        """
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        wanted = (ctx.args[0].lower() if ctx.args else "").strip()
        if wanted in {"on", "off", "1", "0", "true", "false", "yes", "no"}:
            value = wanted in {"on", "1", "true", "yes"}
            from .config import save_override

            save_override("stream", "show_reasoning", value)
            agent.reasoning = value
            self.config.data.setdefault("stream", {})["show_reasoning"] = value
            return await self.announce(update.effective_chat.id,
                                       f"reasoning: {'on' if value else 'off'}")
        await self.announce(
            update.effective_chat.id,
            f"reasoning: {'on' if agent.reasoning else 'off'} · `/reasoning on` or `off`")

    async def cmd_commands(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Search the slash commands the agent advertises (about a thousand)."""
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        await agent.ensure()
        query = " ".join(ctx.args).lower()
        commands = getattr(agent, "advertised_commands", [])
        if not commands:
            return await update.effective_message.reply_text(
                "the agent has not advertised its commands yet; send a prompt first"
            )
        hits = [c for c in commands if query in c.lower()] if query else commands
        if not hits:
            return await update.effective_message.reply_text(f"no match for {query}")
        sample = ", ".join(hits[:60])
        await update.effective_message.reply_text(f"{len(hits)} match(es):\n{sample}")

    async def cmd_queue(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        if not ctx.args:
            return await update.effective_message.reply_text("usage: /queue <prompt>")
        text = " ".join(ctx.args)
        agent = self.agent_for(update.effective_chat.id)
        depth = self.enqueue(update.effective_chat.id, "prompt", _TextOnly(text, update.effective_message))
        await update.effective_message.reply_text(
            f"queued (position {depth})" + ("" if agent.busy else "; it will start now")
        )

    async def cmd_steer(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        if not ctx.args:
            return await update.effective_message.reply_text("usage: /steer <text>")
        chat_id = update.effective_chat.id
        text = " ".join(ctx.args)
        agent = self.agent_for(chat_id)
        outcome, reason = await agent.steer(text)
        if outcome == "injected":
            return await self._react(update.effective_message, "👀", "steering the running turn")
        if outcome == "promptRequired":
            # nothing to steer: the text is still what the person wants the agent to read
            self.enqueue(chat_id, "prompt", _TextOnly(text, update.effective_message))
            return await update.effective_message.reply_text("nothing running; sent as a new prompt")
        await update.effective_message.reply_text(
            f"steer was not accepted ({reason or outcome}); /queue it, or /stop and resend")

    async def cmd_yolo(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        target = not agent.yolo
        log.warning("yolo toggle chat=%s -> %s", update.effective_chat.id, target)
        await agent.respawn(yolo=target)
        await self.announce(
            update.effective_chat.id,
            change("yolo", "off" if target else "on", "on" if target else "off",
                   "agent respawned with --approval-mode=yolo" if target
                   else "agent respawned with the configured approval mode"),
        )

    async def cmd_kill(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        if agent.client is not None:
            agent.forget_turn()
            await asyncio.to_thread(agent.client.close, agent.session_id)
            agent.client = None
            log.info("agent killed by user chat=%s", update.effective_chat.id)
            await self.announce(update.effective_chat.id, "agent process stopped")
        else:
            await update.effective_message.reply_text("no process running")

    async def cmd_whoami(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        await update.effective_message.reply_text(
            f"user {user.id if user else '?'} · chat {chat.id} · allowed={sorted(self.allowed)}"
        )

    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        names = dict(COMMANDS)
        groups = [
            ("session", ["new", "sessions", "resume", "continue", "fork", "cwd", "kill"]),
            ("agent", ["model", "mode", "thinking", "approve", "trace", "verbose", "yolo"]),
            ("catalog", ["skills", "login", "commands"]),
            ("running", ["stop", "queue", "steer", "status", "usage", "context"]),
            ("info", ["whoami", "help"]),
        ]
        sections = [(heading, [(name, names[name]) for name in wanted if name in names])
                    for heading, wanted in groups]
        body = help_sections(sections) + "\n\n<i>" + esc(FORWARD_HINT) + "</i>"
        await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML)

    async def on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not self._authorized(update):
            return
        data = str(query.data or "")
        chat_id = update.effective_chat.id
        agent = self.agent_for(chat_id)

        if data.startswith("sched:"):
            _, what, name = (data.split(":", 2) + ["", ""])[:3]
            if what == "new":
                await query.answer("creating…")
                await agent._schedule_create(name)
            elif what == "cancel":
                agent.pending_schedules.pop(name, None)
                await query.answer("cancelled")
                try:
                    await query.edit_message_text("schedule proposal cancelled")
                except Exception as exc:
                    log.debug("could not rewrite the schedule prompt: %s", exc)
            elif what == "run":
                await query.answer("running…")
                await agent._schedule_run(name)
            elif what == "rm":
                await query.answer("removing…")
                await agent._schedule_remove(name)
            return

        if data.startswith("qo:"):
            agent = self.agent_for(chat_id)
            choice = data.split(":", 1)[1]
            if choice == "other":
                await query.answer("type your answer")
                await self.app.bot.send_message(
                    chat_id, "✏️ go ahead, type your answer — it goes straight to the agent.")
                return
            try:
                answer = agent.pending_options[int(choice)]
            except (ValueError, IndexError):
                await query.answer("that option expired")
                return
            await query.answer("sent")
            agent.pending_options = []
            log.info("question answered chat=%s answer=%r", chat_id, answer)
            try:
                await query.edit_message_text(
                    card("❓ answer sent", answer, [], "waiting for the agent"), parse_mode=ParseMode.HTML)
            except BadRequest:
                pass
            if agent.busy:
                depth = self.enqueue(chat_id, "prompt", _TextOnly(answer, query.message))
                await self.app.bot.send_message(chat_id, f"queued (position {depth})")
            else:
                self.enqueue(chat_id, "prompt", _TextOnly(answer, query.message))
            return

        if data.startswith("skv:"):
            await query.answer()
            try:
                await self.show_skill(chat_id, int(data.split(":", 1)[1]))
            except ValueError:
                pass
            return

        if data.startswith("skl:"):
            await query.answer()
            page = int(data.split(":", 1)[1])
            await self._render_skills(chat_id, page, query.message)
            return

        if data.startswith("lgv:"):
            await query.answer("memulai login…")
            try:
                await self.start_login(chat_id, int(data.split(":", 1)[1]))
            except ValueError:
                pass
            return

        if data.startswith("lgl:"):
            await query.answer()
            page = int(data.split(":", 1)[1])
            await self._render_login(chat_id, page, query.message)
            return

        if data.startswith("ap:"):
            value = data.split(":", 1)[1]
            outcome = await self.set_policy(chat_id, value, anchor=query.message)
            await query.answer("already " + value if outcome == "unchanged" else f"policy: {value}")
            return

        if data.startswith("o:"):
            parts = data.split(":")
            if len(parts) < 3 or parts[1] == "noop":
                return await query.answer()
            config_id, screen = parts[1], parts[2]
            a = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            b = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            try:
                await agent.ensure()
                if screen == "s":
                    value = self._values(agent, config_id)[a]
                    previous, current = await self._apply_pick(agent, config_id, value)
                    await query.answer(f"{config_id}: {value}")
                    body = card(
                        f"⚙️ {config_id}", f"{current} · {now_wib()}",
                        [("previous", previous), ("session", agent.session_id[:8])],
                        "selection applied" if current != previous else "already selected",
                    )
                    try:
                        await query.edit_message_text(body, parse_mode=ParseMode.HTML)
                    except BadRequest as exc:
                        log.debug("picker edit failed: %s", exc)
                    return
                if screen == "p":
                    await query.answer()
                    await self._show_config(agent, chat_id, config_id, screen="p", edit=query.message)
                    return
                if screen == "l":
                    await query.answer()
                    await self._show_config(agent, chat_id, config_id, screen="l",
                                            provider=a, page=b, edit=query.message)
                    return
            except (AcpError, IndexError) as exc:
                await query.answer("failed", show_alert=False)
                await self.app.bot.send_message(chat_id, f"{config_id} failed: {exc}")
                return
            await query.answer()
            return

        try:
            token, index = data.rsplit(":", 1)
            label = await agent.handle_permission_callback(token, int(index))
            await query.answer(label or "expired")
        except (ValueError, AttributeError):
            await query.answer("bad callback")

    async def log_update(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.effective_message
        body = (message.text if message and message.text else "")
        log.info("update chat=%s user=%s authorized=%s text=%r",
                 update.effective_chat.id if update.effective_chat else "?",
                 user.id if user else "?", self._authorized(update), body[:200])

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("handler failed", exc_info=context.error)
        try:
            if isinstance(update, Update) and update.effective_message is not None:
                await update.effective_message.reply_text(f"error: {context.error}")
        except TelegramError:
            pass

    # ------------------------------------------------------------------ serve
    async def run(self) -> None:
        builder = Application.builder().token(self.token)
        self.app = builder.build()
        app = self.app
        app.add_handler(CommandHandler("new", self.cmd_new))
        app.add_handler(CommandHandler("stop", self.cmd_stop))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("usage", self.cmd_usage))
        app.add_handler(CommandHandler("context", self.cmd_context))
        app.add_handler(CommandHandler("skills", self.cmd_skills))
        app.add_handler(CommandHandler("login", self.cmd_login))
        app.add_handler(CommandHandler("commands", self.cmd_commands))
        app.add_handler(CommandHandler("sessions", self.cmd_sessions))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CommandHandler("continue", self.cmd_continue))
        app.add_handler(CommandHandler("fork", self.cmd_fork))
        app.add_handler(CommandHandler("cwd", self.cmd_cwd))
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("thinking", self.cmd_thinking))
        app.add_handler(CommandHandler("approve", self.cmd_approve))
        app.add_handler(CommandHandler("verbose", self.cmd_verbose))
        app.add_handler(CommandHandler("schedule", self.cmd_schedule))
        app.add_handler(CommandHandler("reasoning", self.cmd_reasoning))
        app.add_handler(CommandHandler("queue", self.cmd_queue))
        app.add_handler(CommandHandler("steer", self.cmd_steer))
        app.add_handler(CommandHandler("yolo", self.cmd_yolo))
        app.add_handler(CommandHandler("kill", self.cmd_kill))
        app.add_handler(CommandHandler("whoami", self.cmd_whoami))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("start", self.cmd_help))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_error_handler(self.on_error)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.on_photo))
        # any other document: PDF, zip, csv, text. The image filter above owns the rest.
        app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.IMAGE,
                                       self.on_document))
        app.add_handler(MessageHandler(filters.ALL, self.log_update), group=-1)

        await app.initialize()
        await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in _unique_commands()])
        me = await app.bot.get_me()
        log.info("ompwire online as @%s (allowed users: %s)", me.username, sorted(self.allowed))
        await self.startup_banner(str(me.username))
        await app.start()
        # Kept, not dropped: a message sent during the few seconds of a restart is still a
        # message the person expects an answer to.
        await app.updater.start_polling(drop_pending_updates=False)
        await self.resume_interrupted()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signal_name, stop.set)
            except (NotImplementedError, RuntimeError):
                pass

        try:
            await stop.wait()
        finally:
            self.stopping = True
            await self.shutdown_notice()
            await app.updater.stop()
            await app.stop()
            self.close_logins()
            for agent in self.agents.values():
                await agent.shutdown()

    async def resume_interrupted(self) -> None:
        """Pick up every turn the previous process was cut off in the middle of.

        The session already holds what the agent did before the restart (omp writes it as it
        goes), so the agent is told to check that and finish, not to start over. A turn that was
        interrupted again after RESUME_LIMIT tries is dropped with a note instead of looping.
        """
        store = SessionStore(str(self.config.get("session", "store")))
        try:
            pending = store.interrupted()
        finally:
            store.close()
        enabled = bool(self.config.get("telegram", "resume_interrupted", True))
        for key, prompt, resumes in pending:
            if not key.startswith("tg:"):
                continue
            try:
                chat_id = int(key[3:])
            except ValueError:
                continue
            agent = self.agent_for(chat_id)
            if not enabled or resumes >= RESUME_LIMIT or chat_id not in self.allowed:
                agent.store.end_turn(key)
                log.warning("interrupted turn dropped chat=%s resumes=%s", chat_id, resumes)
                if chat_id in self.allowed:
                    await self.send_md(chat_id, "a turn was interrupted by a gateway restart and "
                                                "was not resumed; send it again if you still need it")
                continue
            log.info("resuming interrupted turn chat=%s attempt=%s", chat_id, resumes + 1)
            notice = await self.send_md(chat_id, "🔁 gateway restarted mid-turn, picking it back up")
            payload = _TextOnly(RESUME_PROMPT.format(prompt=prompt), notice)
            payload.original = prompt
            payload.resumes = resumes + 1
            self.enqueue(chat_id, "prompt", payload)

    def _home_chat(self) -> int | None:
        home = str(self.config.get("telegram", "home_channel", "") or "")
        if home.strip():
            try:
                return int(home)
            except ValueError:
                return None
        return next(iter(sorted(self.allowed)), None)

    async def startup_banner(self, username: str) -> None:
        """Say what actually came up, not just that the process started."""
        chat_id = self._home_chat()
        if chat_id is None:
            return
        rows: list[tuple[str, object]] = []
        try:
            agent = self.agent_for(chat_id)
            await agent.ensure()
            options = getattr(agent, "options", {}) or {}
            rows += [
                ("agent", f"omp {agent.agent_version or '?'}"),
                ("session", agent.session_id[:8]),
                ("model", (options.get("model") or {}).get("currentValue", "?")),
                ("mode", f"{(options.get('mode') or {}).get('currentValue', '?')}"
                         f" · thinking {(options.get('thinking') or {}).get('currentValue', '-')}"),
                ("cwd", agent.cwd),
            ]
        except Exception as exc:
            rows.append(("agent", f"failed: {exc}"))
            log.warning("startup banner agent probe failed: %s", exc)
        # The resolved list, not config["mcp"]["servers"]: with source = "mcp-hub" that inline
        # list is empty while the hub supplies every server, and the banner said "none".
        try:
            servers = [str(server.get("name")) for server in resolve_mcp_servers(self.config)]
        except Exception as exc:
            servers = []
            log.warning("mcp resolution failed: %s", exc)
        rows += [
            ("approval", self._agent_gate(agent)),
            ("yolo", "on" if agent.yolo else "off"),
            ("reasoning", "on" if agent.reasoning else "off"),
            ("mcp", mcp_label(servers)),
            ("users", ", ".join(str(u) for u in sorted(self.allowed)) or "none"),
            ("store", self.config.get("session", "store", "")),
            ("clock", now_wib()),
        ]
        body = card("🟢 ompwire", f"{__version__} · @{username}", rows,
                    "send /help for commands · /status for live state")
        log.info("startup banner: %s", " | ".join(f"{k}={v}" for k, v in rows))
        try:
            await app_send(self.app, chat_id, body)
        except TelegramError as exc:
            log.warning("startup banner send failed: %s", exc)

    def close_logins(self) -> None:
        for chat_id in list(self.logins):
            self.close_login(chat_id)

    async def shutdown_notice(self) -> None:
        chat_id = self._home_chat()
        if chat_id is None:
            return
        active = [cid for cid, agent in self.agents.items()
                  if agent.client is not None and agent.client.alive]
        rows = [
            ("uptime", fmt_uptime(time.time() - self.started_at)),
            ("agents", f"{len(active)} process(es) closed"),
            ("turns", sum(agent.turns for agent in self.agents.values())),
            ("tools", sum(agent.tools_used for agent in self.agents.values())),
        ]
        body = card("🔴 ompwire stopping", now_wib(), rows,
                    "state is persisted, the next start resumes the same sessions")
        log.info("shutdown notice: %s", " | ".join(f"{k}={v}" for k, v in rows))
        try:
            await app_send(self.app, chat_id, body)
        except TelegramError as exc:
            log.warning("shutdown notice send failed: %s", exc)


async def app_send(app: Application, chat_id: int, text: str) -> None:
    await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


class _TextOnly:
    """Payload wrapper for queued prompts: text, plus optional image blocks."""

    def __init__(self, text: str, message: Any, images: list[dict[str, Any]] | None = None) -> None:
        self.text = text
        self.images = images or []
        self._message = message
        # set on a resumed turn: the person's own words, and how many restarts it has survived
        self.original: str | None = None
        self.resumes = 0

    async def reply_text(self, text: str) -> Any:
        if self._message is None:  # a resumed turn whose notice could not be sent
            return None
        return await self._message.reply_text(text)


def configure_logging(config: Config) -> None:
    level = getattr(logging, str(config.get("logging", "level", "INFO")).upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    target = str(config.get("logging", "file", "") or "")
    if target:
        path = pathlib.Path(target).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    # the Telegram API puts the token in every request path, and httpx logs those
    # paths at INFO: without this the token ends up in the log file on every poll
    from .logs import install as install_redaction

    install_redaction(level)


def serve(config: Config) -> int:
    configure_logging(config)
    transport = Transport(config)
    if not transport.token:
        print("error: TELEGRAM_BOT_TOKEN is not set")
        return 2
    if not transport.allowed:
        print("error: telegram.allowed_users is empty, nobody could talk to the bot")
        return 2
    from telegram.error import Conflict, InvalidToken

    try:
        asyncio.run(transport.run())
    except KeyboardInterrupt:
        pass
    except InvalidToken:
        # a revoked token is the most common way a bot stops answering; say which one
        print("\nFAIL Telegram rejected the token for this gateway. It was probably revoked\n"
              "     in @BotFather. Replace it:\n"
              "       ompwire gateway add telegram --force\n"
              "     `ompwire doctor` shows the same check without starting anything.")
        return 2
    except Conflict:
        print("\nFAIL another process is polling this bot (409 Conflict). Telegram delivers\n"
              "     each update to one poller, so updates are being dropped. Stop the other\n"
              "     one: `ompwire gateway list` shows what is running.")
        return 2
    return 0
