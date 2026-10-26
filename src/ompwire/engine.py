"""The platform-neutral agent engine: one ChatAgent per conversation.

This module owns everything a turn needs that is not platform I/O: the omp RPC subprocess and its
session, the drain of updates into traces, the pacing of outgoing messages, permission asking,
schedules, files and usage. A platform implements `Surface` and gets a working agent; nothing
here knows Telegram.
"""

from __future__ import annotations

import abc
import asyncio
import inspect
import logging
import os
import re
import shlex
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar

from . import allowlist, gateways, omp_sessions
from . import schedules as schedule_lib
from .attachments import extract as extract_files
from .attachments import hide_partial as hide_file_partial
from .config import Config, approval_policy
from .format import TOOL_LINE_WIDTH, _clip, humanize_token_report, tokens, wib_time
from .mdrender import strip_agent_noise
from .omp_sessions import plausible_context
from .questions import extract as extract_options
from .questions import hide_partial
from .rpc import APPROVE, DENY, AgentClient, AgentError, AgentTimeout, approval_detail, is_approval
from .rpc import Update as AgentUpdate
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
    """The agent inherits the environment without the credentials this harness runs on, every
    gateway's included: a unit loads its own bot token, and an agent that can run a shell must
    not be able to print it."""
    env = dict(os.environ)
    for name in HARNESS_SECRETS:
        env.pop(name, None)
    for name in [key for key in env if key.startswith("OMPWIRE_GATEWAY_") and key.endswith("_TOKEN")]:
        env.pop(name)
    return env


def agent_args(config: Config) -> list[str]:
    """`[acp] args` as omp RPC-UI arguments. The section keeps its name so existing configs load;
    `--mode acp` and plain `--mode rpc` are read as `--mode rpc-ui`, the only mode whose `ask`
    tool works over the wire."""
    raw = config.get("acp", "args") or ["--mode", "rpc-ui"]
    args = shlex.split(raw) if isinstance(raw, str) else [str(part) for part in raw]
    if args[:1] == ["acp"]:
        args = args[1:]  # the `omp acp` subcommand form
    for index, part in enumerate(args):
        if part == "--mode" and index + 1 < len(args) and args[index + 1] in {"acp", "rpc"}:
            args[index + 1] = "rpc-ui"
        elif part == "--mode=acp" or part == "--mode=rpc":
            args[index] = "--mode=rpc-ui"
    if not any(part == "--mode" or part.startswith("--mode=") for part in args):
        args = ["--mode", "rpc-ui", *args]
    return args


_VERSIONS: dict[str, str] = {}


def _omp_version(command: str) -> str:
    """`omp --version` once per process ("omp/18.12.6" -> "18.12.6"); RPC has no handshake that
    reports it."""
    if command not in _VERSIONS:
        import subprocess

        try:
            out = subprocess.run([command, "--version"], capture_output=True, text=True,
                                 timeout=10, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        _VERSIONS[command] = out.split("/", 1)[-1].split()[0] if out else ""
    return _VERSIONS[command]


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


# omp names the tool on every RPC event. A call without a name falls back to its kind, and only
# the kinds that stand for one tool are named.
_KIND_TOOL = {"think": "todo", "fetch": "web_search"}


def tool_name(kind: str, raw_input: dict[str, Any], named: str = "") -> str:
    """The tool behind a call: omp's own name when it sent one, else a safe guess."""
    path = str(raw_input.get("path") or "")
    if path.startswith("xd://"):
        # a device runs through read or write; the device says what happened, "write" does not
        return path[len("xd://"):].split("/", 1)[0] or "device"
    if named:
        return named
    if raw_input.get("tasks"):
        return "task"
    return _KIND_TOOL.get(kind, kind or "tool")


def _option_name(options: dict[str, dict[str, Any]], config_id: str, value: str) -> str:
    """The readable name of one option value, with its id when the two differ."""
    for entry in (options.get(config_id) or {}).get("options") or []:
        if isinstance(entry, dict) and str(entry.get("value")) == value:
            name = str(entry.get("name") or "")
            if name and name != value:
                return f"{name} (`{value}`)"
    return f"`{value}`"


def _pref(prefs: dict[str, str], name: str, default: bool) -> bool:
    """A per-chat on/off choice; the default applies until the chat sets it."""
    value = prefs.get(name)
    return default if value is None else value == "on"


def _chunks(text: str, limit: int) -> list[str]:
    out: list[str] = []
    while text:
        out.append(text[:limit])
        text = text[limit:]
    return out or [""]


def mcp_server_names() -> list[str]:
    """The MCP servers omp loads on its own, from its `mcp.json`. In RPC mode the agent reads that
    file itself, so this only names them for the banner; nothing is passed to the session."""
    from .setup import read_mcp_servers

    return sorted(read_mcp_servers())



def _event_text(update: AgentUpdate) -> str:
    """The one notice line for a retry/notice/compaction event, or "" to say nothing."""
    raw = update.raw
    kind = str(raw.get("type") or update.kind)
    if kind == "auto_retry_start":
        delay = int(raw.get("delayMs") or 0) / 1000
        return (f"🔁 retry {raw.get('attempt')}/{raw.get('maxAttempts')} in {delay:.0f}s"
                f" · {raw.get('errorMessage')}")
    if kind == "auto_retry_end":
        return "🔁 retry ok" if raw.get("success") else f"🔁 retry gave up: {raw.get('finalError')}"
    if kind == "retry_fallback_applied":
        return f"↪️ fallback {raw.get('from')} → {raw.get('to')}" + (
            f" ({raw.get('reason')})" if raw.get("reason") else "")
    if kind == "retry_fallback_succeeded":
        return f"↪️ fallback {raw.get('model')} works"
    if kind == "notice":
        return f"⚠️ {raw.get('message')}"
    if kind == "extension_error":
        return f"⚠️ extension error: {raw.get('error') or raw.get('message')}"
    if kind == "auto_compaction_start":
        return f"🗜️ compacting ({raw.get('reason')})…"
    if kind == "auto_compaction_end":
        # the transcript notice already reports before→after; only a failure is news here
        if raw.get("aborted") or raw.get("errorMessage"):
            return f"🗜️ compaction failed: {raw.get('errorMessage') or 'aborted'}"
        return ""
    return ""


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


def approval_gate(agent: ChatAgent) -> str:
    """What the agent was started with, which is what decides whether anything asks."""
    if agent.yolo:
        return "yolo · nothing asks"
    approval_mode = str(agent.config.get("agent", "approval_mode", "write") or "write")
    if agent.mode == "plan":
        return "always-ask · reads only (plan mode)"
    policy = approval_policy(agent.config)
    if policy == "allow":
        return f"{approval_mode} · auto-allow · nothing asks"
    if policy == "deny":
        return f"{approval_mode} · auto-deny"
    timeout = float(agent.config.get("approval", "timeout", PERMISSION_TIMEOUT))
    return f"{approval_mode} · asks · {timeout:.0f}s"


def agent_rows(agent: ChatAgent) -> list[tuple[str, str]]:
    """The agent's state as every card shows it: the start banner, /new and /status read the same
    rows, so the three never disagree on what the chat is running."""
    options = agent.options or {}
    try:
        servers = mcp_server_names()
    except Exception:  # an unreadable mcp.json must not take a status card down with it
        servers = []
    return [
        ("agent", f"omp {agent.agent_version or '?'}"),
        ("session", agent.session_id[:8] or "-"),
        ("model", str((options.get("model") or {}).get("currentValue") or "?")),
        ("mode", agent.mode),
        ("thinking", str((options.get("thinking") or {}).get("currentValue") or "-")),
        ("approval", approval_gate(agent)),
        ("trace", ("verbose" if agent.verbose else "quiet")
                  + (" · reasoning" if agent.reasoning else "")),
        ("busy", agent.busy_mode),
        ("mcp", mcp_label(servers)),
        ("cwd", agent.cwd),
    ]


def context_line(used: int, size: int) -> str:
    """One readable row for /status and /usage, e.g. "124k / 1M (12%)"."""
    if size <= 0:
        return "unknown"
    return f"{tokens(used)} / {tokens(size)} ({used * 100 // size}%)"


# Choices that belong to the chat, not to one agent session: /new must not reset them.
PREF_KEYS = ("model", "mode", "thinking")

# Tools plan mode lets through without a button: they only look. Everything else is refused.
PLAN_READ_TOOLS = frozenset({"read", "grep", "glob", "find", "lsp", "web_search", "todo"})

# Plan mode is ompwire's: omp's own plan review lives in its terminal UI, not over RPC. The
# turn is told to plan; the answer ends with Approve/Deny buttons; Deny takes feedback next.
PLAN_PROMPT = (
    "Plan mode is on. Investigate read-only: you may read, search and run read-only commands; "
    "any write, edit or other side effect will be refused. Do not implement anything. Reply with "
    "a concise numbered plan for the request below: the files you would change, how, and how you "
    "would verify it. Do not ask whether to proceed; the person approves or rejects the plan with "
    "buttons.\n\nRequest:\n{prompt}"
)
PLAN_APPROVED = (
    "The plan above is approved. Plan mode is off: implement it now, then verify the result."
)
PLAN_FEEDBACK = (
    "The plan above was rejected with this feedback. Revise the plan accordingly; stay in plan "
    "mode and do not implement anything yet.\n\nFeedback:\n{feedback}"
)
PLAN_OPTIONS = ("✅ Approve plan", "✏️ Deny & revise")


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
    tool: str = ""  # omp's name for it (bash, wait, todo, …); kind is the fallback class


@dataclass(frozen=True)
class PlanItem:
    """One entry of the agent's todo list; status is pending, in_progress or completed."""

    content: str
    status: str


JOB_FINAL = frozenset({"completed", "failed", "aborted"})
JOB_EDIT_GAP = 60.0  # a running job's card moves at most this often; its last state always does
JOB_OPEN_DELAY = 1.5  # the summary waits this long for the rest of a call's start frames


@dataclass
class JobCard:
    """One subagent the `task` tool spawned, as the chat's own card shows it."""

    name: str
    agent: str = "task"
    model: str = ""
    status: str = ""
    last_intent: str = ""
    last_tool: str = ""
    tool_count: int = 0
    duration_ms: int = 0
    # the call in flight and when it began: omp's durationMs stops while one long call runs
    current_tool: str = ""
    current_since_ms: int = 0
    parent: str = ""  # the `task` call that spawned it: one summary card per call
    detached: bool = False  # a background agent, which can outlive the turn that spawned it


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def job_from_frame(raw: dict[str, Any]) -> JobCard | None:
    """The job one `subagent_lifecycle` or `subagent_progress` frame reports on.

    omp splits a job over two frame types: lifecycle says it started or ended, progress says
    what it is doing. Each frame carries the job id and the call that spawned it; the fields a
    frame does not carry stay empty and the engine keeps what the earlier frames said.
    """
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return None
    frame = raw.get("type")
    parent = str(payload.get("parentToolCallId") or "")
    if frame == "subagent_lifecycle":
        return JobCard(name=str(payload.get("id") or ""),
                       agent=str(payload.get("agent") or ""),
                       status=str(payload.get("status") or ""),
                       parent=parent,
                       detached=bool(payload.get("detached")))
    if frame != "subagent_progress":
        return None
    progress = payload.get("progress")
    if not isinstance(progress, dict):
        return None
    recent = [tool for tool in (progress.get("recentTools") or []) if isinstance(tool, dict)]
    last = recent[-1] if recent else {}
    last_tool = ""
    if last.get("tool"):
        args = _clip(" ".join(str(last.get("args") or "").split()), lines=1,
                     width=TOOL_LINE_WIDTH - 20)
        last_tool = f"{last['tool']}: {args}" if args else str(last["tool"])
    current_tool = ""
    if progress.get("currentTool"):
        args = _clip(" ".join(str(progress.get("currentToolArgs") or "").split()), lines=1,
                     width=TOOL_LINE_WIDTH - 20)
        current_tool = f"{progress['currentTool']}: {args}" if args else str(progress["currentTool"])
    intent = progress.get("currentToolIntent") if current_tool else None
    return JobCard(
        name=str(progress.get("id") or ""),
        agent=str(progress.get("agent") or payload.get("agent") or ""),
        model=str(progress.get("resolvedModel") or progress.get("resolvedModelIdentity") or ""),
        status=str(progress.get("status") or ""),
        last_intent=_clip(" ".join(str(intent or progress.get("lastIntent") or "").split()),
                          lines=1, width=TOOL_LINE_WIDTH),
        last_tool=current_tool or last_tool,
        tool_count=_as_int(progress.get("toolCount")),
        duration_ms=_as_int(progress.get("durationMs")),
        current_tool=current_tool,
        current_since_ms=_as_int(progress.get("currentToolStartMs")) if current_tool else 0,
        parent=parent,
    )


def job_status(status: str) -> str:
    """The word a card shows for a job's state."""
    if status in {"", "started"}:
        return "running"
    return {"completed": "✅ done", "failed": "⛔ failed", "aborted": "⛔ aborted"}.get(
        status, status)


def job_duration(ms: int) -> str:
    """How long a job has run, in the shape a card shows it: 42s, 1m20s, 1h05m."""
    elapsed = max(0, int(ms))
    if elapsed < 1000:
        # a job that has just started reports a few hundred ms; "0s" reads like a bug
        return "<1s" if elapsed else "0s"
    total = elapsed // 1000
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def job_line(card: JobCard) -> str:
    """The status line of a job card: running · 1m20s · 3 tools, or how it ended."""
    if card.status in JOB_FINAL:
        return job_status(card.status)
    parts = [job_status(card.status)]
    if card.duration_ms:
        parts.append(job_duration(card.duration_ms))
    if card.tool_count:
        parts.append(f"{card.tool_count} tool{'s' if card.tool_count > 1 else ''}")
    return " · ".join(parts)


def job_action(card: JobCard) -> str:
    """What the job last did: what it was for, then the call behind it. A job that has not
    called a tool yet says so, so every card has the line the person looks for."""
    done = " · ".join(part for part in (card.last_intent, card.last_tool) if part)
    if done:
        return done
    return "none" if card.status in JOB_FINAL else "starting…"


def job_row(card: JobCard) -> str:
    """One line of the summary card: name · agent type · status."""
    return f"{card.name} · {card.agent or 'task'} · {job_status(card.status)}"


class FloodWait(Exception):
    """The platform is rate limiting; `window` is how long to wait before sending again."""

    def __init__(self, window: float) -> None:
        super().__init__(f"flood control, wait {window:.0f}s")
        self.window = window


class Surface(abc.ABC):
    """What a conversation platform implements for the engine.

    `post` sends one message: kind "text" carries raw agent markdown the surface converts,
    the other kinds arrive already rendered by this surface. `trace`, `reasoning`, `dropped`
    and `join` are the renderers the engine calls before posting; `job` and `jobs` render the
    subagent cards, which a surface with editable messages rewrites instead of reposting.
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
    def plan(self, items: list[PlanItem]) -> str:
        """The agent's todo list, posted again whenever it changes."""

    @abc.abstractmethod
    def dropped(self, count: int) -> str: ...

    @abc.abstractmethod
    def join(self, blocks: list[str]) -> str: ...

    @abc.abstractmethod
    def typing(self): ...  # async context manager

    @abc.abstractmethod
    async def offer_options(self, options: list[str], question: str = "") -> None: ...

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

    # The cards for the subagents a `task` call spawned. The engine keeps their order and their
    # pacing; a surface only has to show one card per key, editing the first message it sent.
    @abc.abstractmethod
    def job(self, card: JobCard) -> str:
        """One subagent as a card of its own: what it is, what it last did, how it is doing."""

    @abc.abstractmethod
    def jobs(self, group: list[JobCard], spawned: int) -> str:
        """The card that opens a `task` call: one line per job it spawned."""

    async def job_card(self, key: str, body: str) -> Any | None:
        """Show one job card under `key`: a surface that can edit keeps the message it posted
        first and rewrites it; one that cannot (a terminal) posts every update as a new one."""
        return await self.post(body, kind="job")

class ChatAgent:
    """One chat's omp agent: subprocess, session id, queue and counters."""

    def __init__(self, config: Config, key: str, surface: Surface, *, shared: bool = False) -> None:
        self.config = config
        self.key = key
        self.surface = surface
        # a group or a channel: other people read it, so the trace and the reasoning stay out of
        # it unless that chat asks for them with /verbose and /reasoning
        self.shared = shared
        self.label = surface.label
        self.store = SessionStore(config.get("session", "store"))
        self.client: AgentClient | None = None
        self.session_id = ""
        # /cwd is stored with the session; without reading it back a restart silently moved the
        # chat to the configured directory while still loading the session from the other one
        stored = self.store.get(key)
        configured = str(config.get("agent", "cwd") or "~")
        kept = stored.cwd if stored is not None and stored.cwd else ""
        # a directory removed since then would make every turn fail to start; fall back
        self.cwd = kept if kept and os.path.isdir(os.path.expanduser(kept)) else configured
        prefs = self.store.prefs(key)
        self.reasoning = _pref(prefs, "reasoning",
                               not shared and bool(config.get("stream", "show_reasoning", False)))
        # the todo list last shown; omp repeats it as a reminder, and a repeat is not news
        self._last_plan: list[PlanItem] = []
        # the bot that receives a scheduled result defaults to the bot being talked to
        self.gateway_name = str(config.get("gateway", "name", "telegram") or "telegram")
        self.pending_schedules: dict[str, schedule_lib.Proposal] = {}
        self.started_at = time.time()
        self.pending_options: list[str] = []
        # an ask question the person owes an answer to: {"questions", "index", "answers"}
        self.pending_ask: dict[str, Any] | None = None
        # plan mode: a plan is waiting for Approve/Deny; "revise" means the next message is feedback
        self.plan_review: str = ""
        # when this chat last got a message; the pacing gap is measured from it across turns
        self._last_post = 0.0
        self.last_used = time.time()
        self.busy = False
        self._user_cancel = False
        self.verbose = _pref(prefs, "verbose",
                             not shared and bool(config.get("stream", "show_tool_calls", True)))
        # yolo survives /new: it is stored per chat, like model/mode/thinking.
        # With no stored choice the config decides, so setup can ship yolo on.
        stored_yolo = self.store.prefs(key).get("yolo")
        if stored_yolo is None:
            self.yolo = bool(config.get("agent", "yolo", False))
        else:
            self.yolo = stored_yolo == "on"
        self.turns = 0
        self.tools_used = 0
        # the agent's choices in one shape for every picker: {"model": {"currentValue",
        # "options": [{"value", "name"}]}, "thinking": …}, rebuilt from get_state after each change
        self.options: dict[str, dict[str, Any]] = {}
        # values ompwire itself asked for; omp reports them back as model_changed and
        # thinking_level_changed, which must not be announced as a change made from omp's side
        self._asked: set[tuple[str, str]] = set()
        self._models: list[dict[str, Any]] = []
        # set while a turn runs: posts a notice in order with the turn's own messages
        self._turn_notice: Callable[[str], None] | None = None
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
        self._updates: asyncio.Queue[AgentUpdate | None] = asyncio.Queue()
        self._pending_perm: dict[str, dict[str, Any]] = {}
        self._loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        self._btw_waiters: dict[str, asyncio.Future] = {}
        self._ask_slots: dict[str, dict[str, Any]] = {}
        self._btw_records: dict[str, dict[str, Any]] = {}
        # the subagents the `task` tool spawned, by (call that spawned it, job name): one card
        # each, plus one summary card per call. A detached job outlives the turn that made it,
        # so its last frame still lands here after the turn is over.
        self._jobs: dict[tuple[str, str], JobCard] = {}
        self._job_groups: dict[str, list[str]] = {}
        # how many jobs each `task` call asked for, by its tool call id, read from its arguments
        self._job_spawned: dict[str, int] = {}
        self._job_began: dict[tuple[str, str], float] = {}  # per job, on the engine's clock
        self._job_written: dict[str, float] = {}
        self._job_body: dict[str, str] = {}  # what each card says, so a repeat is not sent
        self._job_queue: list[list[str]] = []
        self._job_flush: asyncio.Task | None = None
        # one poll per chat while any job runs: omp's registry, read once a minute
        self._job_poll: asyncio.Task | None = None
        self._job_opening: set[str] = set()  # calls whose summary is about to open
        self._job_open_delay = JOB_OPEN_DELAY
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], Any] = asyncio.sleep

    # ------------------------------------------------------------------ setup
    def _build_client(self, yolo: bool = False, resume: str = "") -> AgentClient:
        args = agent_args(self.config)
        approval = "yolo" if (yolo or self.yolo) else str(self.config.get("agent", "approval_mode", ""))
        if self.mode == "plan":
            # every tool call asks, and _on_permission refuses each write: plan mode only reads
            approval = "always-ask"
        hint = str(self.config.get("agent", "append_system_prompt", "") or "").strip()
        from .config import PROTOCOL_HINT

        # always: the markers are how a reply carries a file or a question, not a preference
        hint = f"{hint}\n\n{PROTOCOL_HINT}".strip()
        if approval:
            args.append(f"--approval-mode={approval}")
        if hint:
            args += ["--append-system-prompt", hint]
        if resume:
            args += ["--resume", resume]
        return AgentClient(
            command=str(self.config.get("acp", "command", "omp")),
            args=args,
            cwd=self.cwd,
            env=child_env(),
            on_update=self._on_update,
            on_dialog=self._on_dialog,
            startup_timeout=float(self.config.get("acp", "startup_timeout", 60.0)),
        )

    async def ensure(self, yolo: bool = False) -> None:
        if self.client is not None and self.client.alive:
            return
        record = self.store.get(self.key)
        resume = record.session_id if record is not None else ""
        self.client = self._build_client(yolo=yolo, resume=resume)
        try:
            state = await asyncio.to_thread(self.client.state)
        except AgentError as exc:
            if not resume:
                raise
            # the transcript is gone (deleted, or another machine's): start fresh, say so
            log.warning("resume %s failed for %s (%s); starting a new session", resume[:8],
                        self.label, exc)
            self.client = self._build_client(yolo=yolo)
            state = await asyncio.to_thread(self.client.state)
        self.session_id = str(state.get("sessionId") or "")
        self.agent_version = self.agent_version or _omp_version(
            str(self.config.get("acp", "command", "omp")))
        self.info = {"agentInfo": {"name": "omp", "version": self.agent_version}}
        log.info("agent up for %s: omp %s (approval=%s, session=%s%s)", self.label,
                 self.agent_version or "?",
                 "yolo" if self.yolo else self.config.get("agent", "approval_mode", "write"),
                 self.session_id[:8], ", resumed" if resume == self.session_id else "")
        self.store.put(self.key, self.session_id, self.cwd)
        await self._refresh_options(state)
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
        """omp logs each compaction in its transcript, and that log is the source used here: it
        survives restarts, and the context number alone cannot tell a compaction apart from a
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

    async def _refresh_options(self, state: dict[str, Any] | None = None) -> None:
        """Rebuild `options` from what the agent reports: model and thinking from get_state, the
        choices from the model catalog. ompwire's own `mode` (default | plan) rides along."""
        client = self.client
        if client is None:
            return
        if state is None:
            state = await asyncio.to_thread(client.state)
        if not self._models:
            try:
                self._models = await asyncio.to_thread(client.models)
            except AgentError as exc:
                log.warning("model list unavailable for %s: %s", self.label, exc)
        try:
            levels = await asyncio.to_thread(client.thinking_levels)
        except AgentError:
            levels = []
        model = state.get("model") or {}
        current_model = f"{model.get('provider')}/{model.get('id')}" if model.get("id") else ""
        model_values = [{"value": f"{entry.get('provider')}/{entry.get('id')}",
                         "name": str(entry.get("name") or entry.get("id") or "")}
                        for entry in self._models if entry.get("id")]
        mode = str((self.options.get("mode") or {}).get("currentValue")
                   or self.prefs().get("mode") or "default")
        self.options = {
            "model": {"id": "model", "currentValue": current_model, "options": model_values},
            "thinking": {"id": "thinking", "currentValue": str(state.get("thinkingLevel") or ""),
                         "options": [{"value": level, "name": level} for level in levels]},
            "mode": {"id": "mode", "currentValue": mode,
                     "options": [{"value": "default", "name": "default"},
                                 {"value": "plan", "name": "plan"}]},
        }
        usage = state.get("contextUsage") or {}
        if usage.get("contextWindow"):
            self._on_usage(int(usage.get("tokens") or 0), int(usage.get("contextWindow") or 0))

    async def _read_context(self) -> None:
        """RPC sends no usage event: the context meter and its warning read get_state per turn."""
        client = self.client
        if client is None or not client.alive:
            return
        try:
            state = await asyncio.to_thread(client.state)
        except AgentError as exc:
            log.info("context read failed chat=%s: %s", self.label, exc)
            return
        usage = state.get("contextUsage") or {}
        if usage.get("contextWindow"):
            self._on_usage(int(usage.get("tokens") or 0), int(usage.get("contextWindow") or 0))

    def prefs(self) -> dict[str, str]:
        """Per-chat choices that must outlive a session (/new used to reset them)."""
        return self.store.prefs(self.key)

    async def set_option(self, config_id: str, value: str) -> dict[str, Any]:
        """Set one option and keep `options` current; every setter goes through here.

        model and thinking go to the agent; `mode` is ompwire's own (plan mode, see ask()).
        """
        if config_id == "mode":
            if value not in {"default", "plan"}:
                raise AgentError(f"unknown mode {value!r}; choose default or plan")
            changed = value != self.mode
            self.options.setdefault("mode", {"id": "mode", "options": []})["currentValue"] = value
            self.store.set_pref(self.key, "mode", value)
            if changed and self.client is not None and not self.busy:
                # the approval mode is a process argument: the same session resumes under it
                await self.stop_process()
                await self.ensure()
            return self.options
        if self.client is None:
            raise AgentError("the agent is not running")
        self._asked.add((config_id, value))
        try:
            if config_id == "model":
                provider, _, model_id = value.partition("/")
                if not model_id:
                    raise AgentError(f"model {value!r} is not provider/id")
                await asyncio.to_thread(self.client.set_model, provider, model_id)
            elif config_id == "thinking":
                await asyncio.to_thread(self.client.set_thinking, value)
            else:
                raise AgentError(f"unknown option {config_id!r}")
        finally:
            # the echo arrives before the reply; once the reply is in, a later report carrying
            # the same value is someone else's change
            self._loop.call_soon(self._asked.discard, (config_id, value))
        await self._refresh_options()
        return self.options

    async def _apply_preferences(self) -> None:
        stored = self.prefs()
        for config_id in PREF_KEYS:
            value = stored.get(config_id) or str(self.config.get("agent", config_id, "") or "")
            if not value or self.client is None:
                continue
            if config_id == "model" and "/" not in value:
                continue  # an ACP-era id without its provider; the agent's default stands
            if value == (self.options.get(config_id) or {}).get("currentValue"):
                continue
            try:
                await self.set_option(config_id, value)
                log.info("preference applied %s=%s (session=%s, source=%s, effective=%s)",
                         config_id, value, self.session_id[:8],
                         "store" if stored.get(config_id) else "config",
                         (self.options.get(config_id) or {}).get("currentValue"))
            except AgentError as exc:
                log.warning("set %s=%s failed: %s", config_id, value, exc)

    # ------------------------------------------------------- thread -> loop
    def _on_update(self, update: AgentUpdate) -> None:
        if update.kind == "available_commands_update":
            names = [f"/{c.get('name')}" for c in (update.raw.get("commands") or [])]
            self._loop.call_soon_threadsafe(self.advertised_commands.__setitem__, slice(None), names)
            return
        if update.kind == "btw":
            # a side question runs next to the turn; its frames must not touch the turn queue
            self._loop.call_soon_threadsafe(self._on_btw, update)
            return
        if update.kind == "subagent":
            # a subagent runs next to the turn and can outlive it; its frames are cards of their
            # own, never part of the turn's outbox
            self._loop.call_soon_threadsafe(self._on_subagent, update)
            return
        if update.kind == "tool_call":
            tasks = (update.raw.get("rawInput") or {}).get("tasks")
            if isinstance(tasks, list) and tasks:
                # the jobs' own frames overtake the turn's drain, so how many the call spawns
                # is noted here, in arrival order, before the first job reports
                call_id = str(update.raw.get("toolCallId") or "")
                spawned = sum(isinstance(task, dict) for task in tasks)
                self._loop.call_soon_threadsafe(self._job_spawned.__setitem__, call_id, spawned)
        if update.kind in {"retry", "notice", "compaction"} and not self.busy:
            # outside a turn nothing reads the turn queue; a straight notice keeps its place.
            # Mid-turn they fall through to _updates, so drain() renders them in order.
            if self.verbose:
                text = _event_text(update)
                if text:
                    self._loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(self._context_notice(text)))
            return
        if update.kind in {"queue_update", "session_settled", "agent_end"}:
            return
        if update.kind == "config_option_update" and not self.busy:
            # between turns nothing reads the turn queue; mid-turn they take it, so a notice
            # lands after the messages that came before it
            self._loop.call_soon_threadsafe(self._on_option_update, update)
            return
        self._loop.call_soon_threadsafe(self._updates.put_nowait, update)

    BTW_FINAL = frozenset({"complete", "error", "cancelled"})

    def _on_btw(self, update: AgentUpdate) -> None:
        """Runs on the loop thread. Every btw_record is kept by record id; a waiter is settled
        only by a final one, since omp announces the record as `running` before it answers.
        btw_delta streams text nobody renders, so it is dropped."""
        if update.raw.get("type") != "btw_record":
            return
        record = update.raw.get("record") or {}
        record_id = str(record.get("id") or "")
        if not record_id:
            return
        self._btw_records[record_id] = record
        if str(record.get("status") or "") not in self.BTW_FINAL:
            return
        waiter = self._btw_waiters.pop(record_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(record)

    async def btw(self, question: str) -> str:
        """Ask omp's side question: it does not enter the session context and runs while the
        main turn runs. Returns the answer, or a short failure line."""
        client = self.client
        if client is None or not client.alive:
            await self.ensure()
            client = self.client
        assert client is not None
        record = await asyncio.to_thread(client.btw, question)
        record_id = str((record.get("record") or record).get("id") or "")
        waiter: asyncio.Future = self._loop.create_future()
        landed = self._btw_records.get(record_id) if record_id else None
        if landed is not None and str(landed.get("status") or "") in self.BTW_FINAL:
            # a fast answer can land before the waiter exists; its record is already kept
            waiter.set_result(landed)
        else:
            self._btw_waiters[record_id] = waiter
        try:
            done = await asyncio.wait_for(waiter, 120.0)
        except TimeoutError:
            self._btw_waiters.pop(record_id, None)
            return "the side question timed out"
        finally:
            self._btw_records.pop(record_id, None)
        status = str(done.get("status") or "")
        if status == "error":
            return "the side question failed"
        return str(done.get("answer") or "").strip() or "no answer came back"

    # ------------------------------------------------------------ job cards
    def _on_subagent(self, update: AgentUpdate, *, polled: bool = False) -> None:
        """Runs on the loop thread. omp reports every subagent the `task` tool spawns: a
        lifecycle frame when it starts and one when it ends, and progress frames in between.
        Each job becomes a card of its own under one summary card per `task` call; a chat that
        shows no tool calls shows none of this. `polled` marks the minute's registry read,
        which is already paced, so it always moves the card."""
        if not self.verbose:
            return
        raw = update.raw or {}
        fresh = job_from_frame(raw)
        if fresh is None or not fresh.name:
            return
        key = (fresh.parent, fresh.name)
        job = self._jobs.get(key)
        previous = job.status if job is not None else None
        if job is None:
            job = self._jobs[key] = fresh
            self._job_groups.setdefault(fresh.parent, []).append(fresh.name)
            # when this chat first heard of the job, minus what omp says it already ran:
            # the card's age keeps counting while omp's durationMs stands still
            self._job_began[key] = self._clock() - fresh.duration_ms / 1000
        else:
            job.agent = fresh.agent or job.agent
            job.model = fresh.model or job.model
            job.status = fresh.status or job.status
            job.last_intent = fresh.last_intent or job.last_intent
            job.last_tool = fresh.last_tool or job.last_tool
            job.tool_count = max(job.tool_count, fresh.tool_count)
            job.detached = job.detached or fresh.detached
            if raw.get("type") == "subagent_progress":
                job.current_tool = fresh.current_tool
                job.current_since_ms = fresh.current_since_ms
        if job.status not in JOB_FINAL:
            elapsed = int((self._clock() - self._job_began[key]) * 1000)
            job.duration_ms = max(job.duration_ms, fresh.duration_ms, elapsed)
        if previous != job.status:
            log.info("subagent %s job=%s status=%s tools=%s", self.label, job.name,
                     job.status or "-", job.tool_count)
            if f"group:{job.parent}" in self._job_body or job.status in JOB_FINAL:
                self._write_group(job.parent)
            elif job.parent not in self._job_opening:
                # omp starts a call's jobs one frame apart; the summary waits a moment so its
                # first post already counts all of them instead of "spawned 1"
                self._job_opening.add(job.parent)
                self._loop.call_later(self._job_open_delay, self._open_group, job.parent)
        # A lifecycle start frame only says the job exists. Its card opens on the first frame
        # that shows the work (or on the end of a job that never reported any), so nobody is
        # left looking at a card whose last action is nothing.
        reported = previous is not None or raw.get("type") == "subagent_progress"
        if job.status not in JOB_FINAL and (self._job_poll is None or self._job_poll.done()):
            self._job_poll = self._loop.create_task(self._poll_jobs())
        if job.parent in self._job_opening:
            # the summary goes first; this card follows when it opens (_open_group)
            return
        if reported or job.status in JOB_FINAL:
            # the end of a job goes out at once: the minute between edits is for the steps in
            # between, and the frame that ends a job is the one a person is waiting for
            self._job_write(f"job:{job.parent}:{job.name}", self.surface.job(job),
                            force=polled or job.status in JOB_FINAL)

    def _has_job_cards(self, call_id: str) -> bool:
        """Whether job cards account for this `task` call. A call's trace can finish before
        its jobs' first frame arrives, so an omp that took the subagent subscription counts as
        covered; one that refused it (or a call with no jobs reported, in a test) keeps the
        trace. parentToolCallId carries the call id before a `|`."""
        if not self.verbose:
            return False
        if getattr(self.client, "subagent_frames", False):
            return True
        return any(parent.split("|", 1)[0] == call_id for parent in self._job_groups)

    def _open_group(self, parent: str) -> None:
        self._job_opening.discard(parent)
        self._write_group(parent)
        # the cards held while the summary waited, in the order their jobs started
        for name in self._job_groups.get(parent, []):
            job = self._jobs[(parent, name)]
            if job.model or job.last_intent or job.status in JOB_FINAL:
                self._job_write(f"job:{parent}:{name}", self.surface.job(job), force=True)

    def _write_group(self, parent: str) -> None:
        group = [self._jobs[(parent, name)] for name in self._job_groups.get(parent, [])]
        if not group:
            return
        # omp's parentToolCallId is "<call id>|fc_<call id>"; the trace knows the first part
        spawned = self._job_spawned.get(parent.split("|", 1)[0], 0)
        self._job_write(f"group:{parent}", self.surface.jobs(group, max(spawned, len(group))),
                        force=True)

    async def _poll_jobs(self) -> None:
        """Until every job has ended, ask omp for its subagents once a minute and refresh their
        cards. Progress frames stop while a job waits on a long tool call; the card still has to
        show that it is alive and what it is on, so the poll does not wait for omp to speak."""
        while any(job.status not in JOB_FINAL for job in self._jobs.values()):
            await self._sleep(JOB_EDIT_GAP)
            client = self.client
            if client is None or not client.alive:
                return
            try:
                rows = await asyncio.to_thread(client.subagents)
            except AgentError as exc:
                log.info("subagent poll failed chat=%s: %s", self.label, exc)
                continue
            listed = set()
            for row in rows:
                progress = dict(row.get("progress") or {})
                progress.setdefault("id", row.get("id"))
                progress.setdefault("agent", row.get("agent"))
                progress.setdefault("status", row.get("status"))
                frame = {"type": "subagent_progress",
                         "payload": {"parentToolCallId": row.get("parentToolCallId"),
                                     "agent": row.get("agent"), "progress": progress}}
                job = job_from_frame(frame)
                if job is not None and job.name:
                    listed.add((job.parent, job.name))
                    self._on_subagent(AgentUpdate(kind="subagent", session_id=self.session_id,
                                                  raw=frame), polled=True)
            # omp lists only running subagents; one that left the list without an end frame
            # (the process restarted, a frame was lost) ended without saying how
            for key, job in list(self._jobs.items()):
                if job.status not in JOB_FINAL and key not in listed:
                    log.info("subagent %s job=%s gone from omp's list", self.label, job.name)
                    self._on_subagent(AgentUpdate(kind="subagent", session_id=self.session_id,
                                                  raw={"type": "subagent_lifecycle", "payload": {
                                                      "id": job.name, "parentToolCallId": job.parent,
                                                      "agent": job.agent, "status": "completed"}}))

    def _job_write(self, key: str, body: str, *, force: bool) -> None:
        """Queue one card under `key`, at most once a minute unless `force`. A card that would
        say exactly what it already says is not sent again: Telegram answers an unchanged edit
        with an error, and a terminal would print the same block twice. Cards go out in the
        order their frames arrived: the summary stays above the jobs it names, and an edit
        never overtakes the send of the message it means to change."""
        if self._job_body.get(key) == body:
            return
        now = self._clock()
        last = self._job_written.get(key)
        if last is not None and not force and now - last < JOB_EDIT_GAP:
            return
        self._job_written[key] = now
        self._job_body[key] = body
        for queued in self._job_queue:
            if queued[0] == key:
                queued[1] = body  # the newest state wins, and still goes out in its own place
                break
        else:
            self._job_queue.append([key, body])
        if self._job_flush is None or self._job_flush.done():
            self._job_flush = self._loop.create_task(self._flush_jobs())

    async def _flush_jobs(self) -> None:
        while self._job_queue:
            key, body = self._job_queue.pop(0)
            try:
                await self.surface.job_card(key, body)
            except Exception:
                # a card is a progress report; a platform refusing one must not stop the work
                log.warning("job card failed chat=%s key=%s", self.label, key, exc_info=True)

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
        safe to call while ask() is not running. A quiet chat gets the answer and nothing else."""
        if not self.verbose:
            return
        try:
            await self.surface.notice(text)
        except Exception:
            log.warning("context notice failed chat=%s", self.label, exc_info=True)

    # Option changes the chat should hear about when omp makes them on its own: a model falling
    # back to another. Thinking is left out; it moves with the model.
    WATCHED_OPTIONS = ("model",)

    def _on_options(self, updated: dict[str, dict[str, Any]]) -> None:
        """Runs on the loop thread. Keeps `options` current and names a change ompwire did not ask for."""
        if not updated:
            return
        changes = []
        for config_id in self.WATCHED_OPTIONS:
            old = str((self.options.get(config_id) or {}).get("currentValue") or "")
            new = str((updated.get(config_id) or {}).get("currentValue") or "")
            # an empty `old` is the first report of a session, not a change
            if old and new and old != new and (config_id, new) not in self._asked:
                changes.append((config_id, old, new))
        previous, self.options = self.options, updated
        for config_id, old, new in changes:
            log.info("option changed by omp %s %s: %s -> %s", self.label, config_id, old, new)
            text = (f"⚙️ omp switched {config_id}: {_option_name(previous, config_id, old)}"
                    f" → {_option_name(updated, config_id, new)}")
            if not self.verbose:
                continue
            if self._turn_notice is not None:
                # mid-turn the chat is still receiving this turn's messages; a notice sent
                # straight to the platform would land above traces still waiting to go out
                self._turn_notice(text)
            else:
                self._loop.create_task(self._context_notice(text))

    def _on_option_update(self, update: AgentUpdate) -> None:
        """model_changed or thinking_level_changed: fold the reported value into `options`."""
        raw = update.raw
        updated = {key: dict(value) for key, value in self.options.items()}
        if raw.get("event") == "thinking_level_changed" and raw.get("thinkingLevel"):
            updated.setdefault("thinking", {"id": "thinking", "options": []})[
                "currentValue"] = str(raw["thinkingLevel"])
        model = raw.get("model") if isinstance(raw.get("model"), dict) else {}
        if raw.get("event") == "model_changed" and model.get("id"):
            updated.setdefault("model", {"id": "model", "options": []})[
                "currentValue"] = f"{model.get('provider')}/{model.get('id')}"
        self._on_options(updated)

    def _on_dialog(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """A dialog the agent opened (reader thread). Tool approvals become buttons, an `ask`
        becomes a question card; any other dialog is cancelled and the agent carries on."""
        if frame.get("method") == "ask":
            return self._on_ask(frame)
        if not is_approval(frame):
            log.info("dialog cancelled chat=%s method=%s title=%r", self.label, frame.get("method"),
                     str(frame.get("title") or "")[:120])
            return None
        option = self._on_permission(frame)
        return {"value": option} if option else {"cancelled": True}

    def _on_ask(self, frame: dict[str, Any]) -> dict[str, Any]:
        """The ask tool's questions (reader thread, blocking like _on_permission). Questions go
        to the chat one at a time through the options card. An ask is a question, not an
        approval: it always reaches a person, whatever the approval policy says."""
        questions = [q for q in (frame.get("questions") or []) if isinstance(q, dict)]
        if not questions:
            return {"cancelled": True}
        slot: dict[str, Any] = {"answer": None}
        token = f"ask-{uuid.uuid4().hex[:12]}"
        state = {"questions": questions, "index": 0, "answers": [], "slot": slot,
                 "token": token}
        self._ask_slots[token] = state
        self.pending_ask = {"token": token, "questions": questions, "index": 0}
        timeout = float(self.config.get("approval", "timeout", PERMISSION_TIMEOUT))
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._show_ask_question(token)))
        deadline = time.time() + timeout
        note = "answered"
        try:
            while time.time() < deadline:
                if slot["answer"] is not None:
                    return slot["answer"]
                time.sleep(0.2)
            log.warning("ask timed out chat=%s (%.0fs)", self.label, timeout)
            note = "timed out"
            return {"cancelled": True, "timedOut": True}
        finally:
            self._ask_slots.pop(token, None)
            self.pending_ask = None
            self.pending_options = []
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.surface.retire_options(note)))

    async def _show_ask_question(self, token: str) -> None:
        """Runs on the loop thread: the open question becomes the options card."""
        state = self._ask_slots.get(token)
        if state is None:
            return
        question = state["questions"][state["index"]]
        labels = [str(o.get("label") or "") for o in (question.get("options") or [])]
        text = str(question.get("question") or question.get("header") or "")
        self.pending_ask = {"token": token, "questions": state["questions"],
                            "index": state["index"]}
        self.pending_options = labels
        await self.offer_options(labels, question=text)

    async def answer_ask(self, text: str | None, *, option_index: int | None = None,
                         skip: bool = False) -> bool:
        """Consume a chat answer for the open ask question. True when it was ours to take."""
        state = next(iter(self._ask_slots.values()), None)
        if state is None:
            return False
        question = state["questions"][state["index"]]
        options = [str(o.get("label") or "") for o in (question.get("options") or [])]
        if skip:
            # cancel the whole ask: the agent continues without any answer
            state["slot"]["answer"] = {"cancelled": True}
            return True
        answer: dict[str, Any] | None = None
        if option_index is not None and 0 <= option_index < len(options):
            answer = {"answers": [{"id": question.get("id"),
                                   "selectedOptions": [options[option_index]]}]}
        elif text:
            answer = {"answers": [{"id": question.get("id"), "selectedOptions": [],
                                   "customInput": text}]}
        if answer is None:
            return False
        state["answers"].append(answer)
        if state["index"] + 1 < len(state["questions"]):
            state["index"] += 1
            await self.surface.retire_options("answered")
            self._loop.create_task(self._show_ask_question(state["token"]))
            return True
        state["slot"]["answer"] = (answer if len(state["questions"]) == 1
                                   else {"answers": [a["answers"][0] for a in state["answers"]]})
        return True

    def _on_permission(self, frame: dict[str, Any]) -> str | None:
        """The option to answer an approval with: "Approve", "Deny", or None to cancel."""
        tool, command = approval_detail(frame)
        policy = approval_policy(self.config)
        if policy in {"allow", "deny"}:
            log.info("permission auto-%s chat=%s tool=%s", policy, self.label, tool)
            return APPROVE if policy == "allow" else DENY
        if self.mode == "plan":
            # plan mode explores and proposes; it runs with always-ask, so reads come here too
            reads = tool in PLAN_READ_TOOLS or (tool == "bash" and allowlist.is_read_only(command))
            log.info("permission %s in plan mode chat=%s tool=%s: %s",
                     "allowed" if reads else "denied", self.label, tool, command[:120])
            return APPROVE if reads else DENY
        # A read-only command is not worth a button. Asking for `git log` teaches the habit of
        # tapping Allow without reading, which is exactly what the gate exists to prevent.
        if tool == "bash" and command and allowlist.is_read_only(command):
            log.info("permission auto-allow chat=%s (read-only): %s", self.label, command[:120])
            return APPROVE
        token = f"perm-{uuid.uuid4().hex[:12]}"  # two requests can land in the same millisecond
        params = {"tool": tool, "command": command, "options": list(frame.get("options") or [])}
        slot: dict[str, Any] = {"optionId": None, "params": params}
        self._pending_perm[token] = slot
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._ask_permission(token, params)))
        log.info("permission requested chat=%s tool=%s: %s", self.label, tool, command[:120])
        deadline = time.time() + float(self.config.get("approval", "timeout", PERMISSION_TIMEOUT))
        while time.time() < deadline:
            if slot.get("optionId") is not None:
                self._pending_perm.pop(token, None)
                return slot["optionId"]
            time.sleep(0.2)
        log.warning("permission timed out chat=%s tool=%s (denied)", self.label, tool)
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._expire_permission(token)))
        return None

    OPTION_LABELS: ClassVar[dict[str, str]] = {APPROVE: "✅ Allow", DENY: "⛔ Deny"}

    @staticmethod
    def _permission_detail(params: dict[str, Any]) -> tuple[str, str]:
        """(headline, monospace body): the tool and what it will run."""
        return str(params.get("tool") or "tool"), str(params.get("command") or "")

    def _option_label(self, option: str) -> str:
        if option.startswith(f"{APPROVE} ") and "session" in option:
            return "✅ Allow for this session"  # omp's "Approve write for this session"
        return self.OPTION_LABELS.get(option, option)

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

    async def offer_options(self, options: list[str], question: str = "") -> None:
        """The agent offered answers; the surface turns them into buttons."""
        self.pending_options = options
        log.info("question offered %s options=%s question=%r", self.label, options, question[:80])
        params = inspect.signature(self.surface.offer_options).parameters.values()
        if any(p.name == "question" or p.kind is p.VAR_KEYWORD for p in params):
            await self.surface.offer_options(options, question=question)
        else:
            # a surface not yet updated for the ask heading; the options still work
            await self.surface.offer_options(options)

    async def answer_permission(self, token: str, index: int) -> str | None:
        """Record the user's choice, then let the surface rewrite the prompt into the decision."""
        slot = self._pending_perm.get(token)
        if slot is None:
            return None
        options = slot["params"].get("options") or []
        if not (0 <= index < len(options)):
            return None
        option = str(options[index])
        slot["optionId"] = option
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
        text = await self._plan_turn(text)
        if not text:
            return ""
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
        inputs: dict[str, dict[str, Any]] = {}  # each call's rawInput, to name its tool
        held_todo: list[ToolTrace] = []  # finished todo calls waiting to see if a list follows
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
            if state["dropped"] and self.verbose:
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
            if entry.kind != "subagent":
                # the device path beats omp's name: "read" says less than "github"
                entry.tool = tool_name(entry.kind, inputs.get(call_id, {}), entry.tool)
            # the start line logs the guess; this one logs the name the chat shows
            log.info("tool done %s tool=%s status=%s %r", self.label, entry.tool,
                     entry.status or "-", entry.title[:80])
            if entry.tool == "todo" and entry.status == "completed":
                # omp sends the list a todo call produced right after the call ends, and that
                # checklist says more than the trace. Hold the trace until the next update shows
                # whether the list came; an omp that sends none still gets the trace.
                held_todo.append(entry)
                return
            if entry.kind == "subagent" and self._has_job_cards(call_id):
                # the job cards already show this call: a summary with every job, then a card
                # each; the trace would be a second "spawned" message saying less
                return
            show_tool(entry)

        def show_tool(entry: ToolTrace) -> None:
            if not self.verbose:
                return
            body = self.surface.trace(entry)
            if len(body) > self.surface.limit:
                body = self.surface.trace(replace(entry, output="(output too long for one message)"))
            enqueue("trace", body, essential=False)

        def release_todo() -> None:
            while held_todo:
                show_tool(held_todo.pop(0))

        def post_plan(raw_entries: Any) -> None:
            items = [PlanItem(content=" ".join(str(item.get("content") or "").split()),
                              status=str(item.get("status") or "pending"))
                     for item in (raw_entries or []) if isinstance(item, dict)]
            items = [item for item in items if item.content]
            # an emptied list (omp clears it once all is done) and a reminder repeating the
            # last one carry nothing new; a todo call behind them keeps its own trace
            if not items or items == self._last_plan:
                release_todo()
                return
            held_todo.clear()  # the checklist stands in for the todo call that changed it
            self._last_plan = items
            if self.verbose:
                enqueue("plan", self.surface.plan(items), essential=True)

        async def finish() -> None:
            """End of turn: whatever is still buffered, then the markers in the whole answer."""
            flush_thinking()
            flush_segment()
            for call_id in trace:
                if call_id not in posted:
                    post_tool(call_id)
            release_todo()
            # from here a notice goes straight to the platform: this outbox is no longer read
            self._turn_notice = None
            state["closed"] = True
            wake.set()
            await sender_task  # the buttons and files come after the last message, in order
            raw = "".join(answer)
            _, options = extract_options(raw)
            if self.mode == "plan" and raw.strip() and not self._user_cancel:
                # the plan's own question buttons would bypass the review; the review replaces them
                options = list(PLAN_OPTIONS)
                self.plan_review = "pending"
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
                if update.kind != "plan":
                    release_todo()  # no list followed the todo call, so its trace is the news
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
                elif update.kind == "config_option_update":
                    flush_thinking()
                    flush_segment()
                    self._on_option_update(update)
                elif update.kind in {"retry", "notice", "compaction"}:
                    if not self.verbose:
                        continue  # a quiet chat hears the answer and nothing else
                    flush_thinking()
                    flush_segment()
                    if self._turn_notice is not None:
                        text = _event_text(update)
                        if text:
                            self._turn_notice(text)
                    continue
                elif update.kind == "plan":
                    flush_thinking()
                    flush_segment()
                    post_plan(update.raw.get("entries"))
                    continue
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
                        named = str(raw.get("toolName") or "")
                        entry.title = str(raw.get("title") or "")
                        raw_input = raw.get("rawInput") or {}
                        if not isinstance(raw_input, dict):
                            raw_input = {}
                        inputs[call_id] = raw_input
                        entry.tool = tool_name(entry.kind, raw_input, named)
                        entry.command = str(raw_input.get("command") or "")
                        tasks = raw_input.get("tasks")
                        if isinstance(tasks, list) and tasks:
                            entry.kind = entry.tool = "subagent"
                            # the trace names the jobs this call spawned; their progress has
                            # cards of its own (see _on_subagent)
                            entry.subagent = ", ".join(
                                f"{item.get('name', '?')} ({item.get('agent', 'task')})"
                                for item in tasks if isinstance(item, dict))[:80]
                            first = next((item for item in tasks if isinstance(item, dict)), {})
                            entry.brief = _task_brief(first.get("task", ""))
                            tools = first.get("tools")
                            if isinstance(tools, list) and tools:
                                entry.brief = (entry.brief + " · tools: "
                                               + ", ".join(str(x) for x in tools))[:160]
                        log.info("tool call %s tool=%s kind=%s command=%r", self.label, entry.tool,
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
        # markdown, like surface.notice between turns
        self._turn_notice = lambda body: enqueue("text", body, essential=True)
        try:
            async with self.surface.typing():
                result = await asyncio.to_thread(
                    self.client.prompt, text,
                    images=images or None,
                    timeout=float(self.config.get("acp", "turn_timeout", 900.0)),
                )
        except AgentError as exc:
            # A silent turn is abandoned here, so stop it in omp too: left running, its late
            # chunks land in the next turn's stream and the chat sees the answer twice.
            if self.client is not None and self.client.alive:
                self.client.abort()
            # scheduled, not immediate: the sentinel must queue behind updates the reader thread
            # has already handed to the loop, or the last of a turn is dropped
            self._loop.call_soon_threadsafe(self._updates.put_nowait, None)
            await drain_task
            await finish()  # what did arrive is still worth showing
            self.busy = False
            # A crash or shutdown cut keeps the row, so the next start resumes it. A deliberate
            # stop and a silence timeout do not: the person has already been told it failed.
            if self._user_cancel or isinstance(exc, AgentTimeout):
                self.store.end_turn(self.key)
            raise
        except BaseException:
            # a cancelled turn (gateway shutdown) never reaches finish(); do not leave the sender
            sender_task.cancel()
            self._turn_notice = None
            raise
        self._loop.call_soon_threadsafe(self._updates.put_nowait, None)
        await drain_task
        await finish()
        self.busy = False
        await self._report_compaction()
        await self._read_context()
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
        if self.verbose and self.config.get("stream", "show_usage", True):
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
        self.client.abort()
        return True

    async def steer(self, text: str, images: list[dict[str, Any]] | None = None) -> tuple[str, str]:
        """Hand a message to the running turn: (outcome, reason). `promptRequired` = idle."""
        if self.client is None or not self.client.alive or not self.session_id:
            return "promptRequired", ""
        try:
            outcome, reason = await asyncio.to_thread(
                self.client.steer, text, images,
                float(self.config.get("acp", "steer_timeout", 15.0)))
        except AgentError as exc:
            log.warning("steer failed chat=%s: %s", self.label, exc)
            return "failed", str(exc)
        log.info("steer chat=%s outcome=%s chars=%s reason=%s", self.label, outcome, len(text),
                 reason or "-")
        return outcome, reason

    async def follow_up(self, text: str, images: list[dict[str, Any]] | None = None) -> tuple[str, str]:
        """Queue a message to run after the current turn: same outcomes as steer."""
        if self.client is None or not self.client.alive or not self.session_id:
            return "promptRequired", ""
        try:
            outcome, reason = await asyncio.to_thread(
                self.client.follow_up, text, images,
                float(self.config.get("acp", "steer_timeout", 15.0)))
        except AgentError as exc:
            log.warning("follow_up failed chat=%s: %s", self.label, exc)
            return "failed", str(exc)
        log.info("follow_up chat=%s outcome=%s chars=%s", self.label, outcome, len(text))
        return outcome, reason

    @property
    def busy_mode(self) -> str:
        """What a message means while a turn runs: steer | followup | queue."""
        value = self.store.prefs(self.key).get("busy")
        if value in {"steer", "followup", "queue"}:
            return value
        legacy = str(self.config.get("telegram", "busy_message", "") or "") or \
            str(self.config.get("whatsapp", "busy_message", "") or "")
        return legacy if legacy in {"steer", "followup", "queue"} else "steer"

    def set_busy_mode(self, value: str) -> str:
        """Store this chat's busy mode; returns the value set."""
        if value not in {"steer", "followup", "queue"}:
            raise AgentError(f"unknown busy mode {value!r}; choose steer, followup or queue")
        self.store.set_pref(self.key, "busy", value)
        return value

    async def session_stats(self) -> dict[str, Any]:
        """omp's counters for this session: messages, tool calls, tokens, cost."""
        await self.ensure()
        return await asyncio.to_thread(self.client.session_stats)

    async def rename(self, name: str) -> None:
        await self.ensure()
        await asyncio.to_thread(self.client.set_session_name, name)

    async def export_html(self) -> Path:
        await self.ensure()
        base = Path(str(self.config.get("files", "incoming_dir",
                                        "~/.local/state/ompwire/files"))).expanduser()
        target = base.parent / "exports" / f"{self.session_id or 'session'}.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        return Path(await asyncio.to_thread(self.client.export_html, str(target)))

    async def handoff(self, instructions: str = "") -> dict[str, Any]:
        await self.ensure()
        result = await asyncio.to_thread(self.client.handoff, instructions)
        # the session id stays the same, but its title and options may have moved
        state = await asyncio.to_thread(self.client.state)
        self.session_id = str(state.get("sessionId") or self.session_id)
        self.store.put(self.key, self.session_id, self.cwd)
        await self._refresh_options(state)
        return result

    async def _plan_turn(self, text: str) -> str:
        """What a message means in plan mode; "" when it was handled without a turn.

        Approve leaves plan mode and implements the plan. Deny asks for feedback, and the message
        after it revises the plan. Anything else typed while a plan waits is feedback too, so a
        review never blocks the chat.
        """
        if self.mode != "plan":
            self.plan_review = ""
            return text
        if text == PLAN_OPTIONS[0] and self.plan_review == "pending":
            self.plan_review = ""
            await self.set_option("mode", "default")
            await self.surface.notice("✅ plan approved · plan mode off, implementing")
            log.info("plan approved chat=%s", self.label)
            return PLAN_APPROVED
        if text == PLAN_OPTIONS[1] and self.plan_review == "pending":
            self.plan_review = "revise"
            await self.surface.notice("✏️ what should change? Your next message revises the plan.")
            return ""
        if self.plan_review in {"pending", "revise"}:
            self.plan_review = ""
            log.info("plan feedback chat=%s chars=%s", self.label, len(text))
            return PLAN_FEEDBACK.format(feedback=text)
        return PLAN_PROMPT.format(prompt=text)

    def forget_turn(self) -> None:
        """The person ended this turn on purpose; a restart must not bring it back."""
        self._user_cancel = True
        self.store.end_turn(self.key)

    async def respawn(self, yolo: bool) -> None:
        self.yolo = yolo
        self.store.set_pref(self.key, "yolo", "on" if yolo else "off")
        self.forget_turn()
        if self.client is not None:
            await asyncio.to_thread(self.client.close)
        self.client = None
        await self.ensure(yolo=yolo)

    async def stop_process(self) -> None:
        """Close the agent process; the chat keeps its store and comes back on the next message."""
        if self.client is not None:
            await asyncio.to_thread(self.client.close)
            self.client = None

    async def shutdown(self) -> None:
        await self.stop_process()
        self.store.close()

    def set_display(self, name: str, on: bool) -> None:
        """Turn the tool trace (`verbose`) or the reasoning on or off for this chat only.

        Stored per chat key, so it survives /new and a restart, and one group asking for the
        trace does not turn it on in every other chat.
        """
        setattr(self, name, on)
        self.store.set_pref(self.key, name, "on" if on else "off")

    async def new_session(self) -> str:
        self.forget_turn()
        # the old context numbers belong to the old session; the next usage_update re-reads them
        self._compactions_seen = None
        self._last_plan = []
        self._context_good = None
        self._context_warned = False
        if self.client is not None and self.client.alive and not self.busy:
            # the same process starts the new session; a closed one would resume the old one
            self.session_id = await asyncio.to_thread(self.client.new_session)
            self.store.put(self.key, self.session_id, self.cwd)
            await self._refresh_options()
            return self.session_id
        if self.client is not None:
            await asyncio.to_thread(self.client.close)
        self.client = None
        self.store.clear(self.key)
        await self.ensure()
        return self.session_id

    async def load(self, session_id: str) -> None:
        if self.client is not None:
            await asyncio.to_thread(self.client.close)
        self.client = None
        self._compactions_seen = None
        self._last_plan = []
        self._context_good = None
        self._context_warned = False
        self.store.put(self.key, session_id, self.cwd)
        await self.ensure()

    async def fork(self) -> str:
        await self.ensure()
        self._compactions_seen = None
        self._context_warned = False
        new_id = await asyncio.to_thread(self.client.fork)
        if new_id:
            self.session_id = new_id
            self.store.put(self.key, new_id, self.cwd)
        return new_id

    async def compact(self) -> dict[str, Any]:
        """omp's `compact` command; returns its summary. The context numbers re-read after."""
        await self.ensure()
        result = await asyncio.to_thread(self.client.compact)
        await self._refresh_options()
        return result

    async def list_sessions(self) -> list[dict[str, Any]]:
        """This cwd's omp transcripts, newest first: sessionId, updatedAt, size."""
        return omp_sessions.listing(self.cwd)

    @property
    def mode(self) -> str:
        """ompwire's own mode; before omp first answers (a restart), the chat's stored choice."""
        current = (self.options.get("mode") or {}).get("currentValue")
        if current:
            return str(current)
        store = getattr(self, "store", None)
        return str((store.prefs(self.key).get("mode") if store is not None else "") or "default")

