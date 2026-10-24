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
import contextlib
import logging
import time
from collections.abc import Iterable
from typing import Any

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .acp import AcpClient, AcpError, Update as AcpUpdate, pick_permission_option
from .config import Config
from .store import SessionStore

log = logging.getLogger("ompwire.telegram")

TELEGRAM_LIMIT = 4000  # leave headroom under the 4096 hard cap
PERMISSION_TIMEOUT = 120.0
IDLE_CHECK_INTERVAL = 60.0

COMMANDS: list[tuple[str, str]] = [
    ("new", "start a fresh session"),
    ("stop", "cancel the running turn"),
    ("status", "session, model, mode and process state"),
    ("usage", "token usage for this session"),
    ("sessions", "list recorded sessions"),
    ("resume", "resume a session by id"),
    ("continue", "resume this chat's last session"),
    ("fork", "branch this conversation into a new session"),
    ("cwd", "change working directory (new session)"),
    ("model", "show or set the model"),
    ("mode", "show or set default|plan"),
    ("thinking", "show or set the thinking level"),
    ("verbose", "toggle tool-call output"),
    ("skills", "search the agent's own commands and skills"),
    ("queue", "queue a prompt for the next turn"),
    ("steer", "steer the running turn (experimental)"),
    ("yolo", "respawn the agent with approvals off"),
    ("kill", "stop the agent process for this chat"),
    ("whoami", "ids and transport info"),
    ("help", "this list"),
]

def normalize_mcp_servers(servers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape MCP entries the way omp 18.12.6 accepts them.

    Measured: `env` must be a list. An object, or a missing env, makes
    `session/new` fail with "Internal error / undefined is not an object".
    """
    out: list[dict[str, Any]] = []
    for server in servers or []:
        env = server.get("env", [])
        if isinstance(env, dict):
            env = [{"name": str(k), "value": str(v)} for k, v in env.items()]
        elif env is None:
            env = []
        entry = {
            "name": str(server.get("name", "mcp")),
            "type": str(server.get("type", "stdio")),
            "command": str(server.get("command", "")),
            "args": [str(a) for a in (server.get("args") or [])],
            "env": list(env),
        }
        out.append(entry)
    return out


FORWARD_HINT = (
    "Everything else is sent to the agent as a prompt. Its own commands work here, "
    "including /skill:<name> and /compact."
)


def _chunks(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    out: list[str] = []
    while text:
        out.append(text[:limit])
        text = text[limit:]
    return out or [""]


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
        self.started_at = time.time()
        self.last_used = time.time()
        self.busy = False
        self.verbose = bool(config.get("stream", "show_tool_calls", True))
        self.usage = {"input": 0, "output": 0, "cached": 0, "turns": 0}
        self.options: dict[str, dict[str, Any]] = {}
        self.mcp_warning = ""
        self.picker: dict[str, Any] = {}
        self.advertised_commands: list[str] = []
        self._updates: asyncio.Queue[AcpUpdate | None] = asyncio.Queue()
        self._pending_perm: dict[str, dict[str, Any]] = {}
        self._loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup
    def _build_client(self, yolo: bool = False) -> AcpClient:
        args = list(self.config.get("acp", "args") or ["--mode", "acp"])
        approval = "yolo" if yolo else str(self.config.get("agent", "approval_mode", ""))
        if approval:
            args.append(f"--approval-mode={approval}")
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
        log.info("agent up for %s: %s %s", self.chat_id, agent.get("name"), agent.get("version"))
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
        return normalize_mcp_servers(self.config.get("mcp", "servers", []) or [])

    async def _apply_preferences(self) -> None:
        for config_id in ("model", "mode"):
            value = str(self.config.get("agent", config_id, "") or "")
            if not value or self.client is None:
                continue
            try:
                await asyncio.to_thread(self.client.set_config_option, self.session_id, config_id, value)
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
        policy = str(self.config.get("agent", "permission_policy", "ask"))
        if policy in {"allow", "deny"}:
            return pick_permission_option(params, policy)
        token = f"perm-{int(time.time() * 1000)}"
        slot: dict[str, Any] = {"optionId": None, "params": params}
        self._pending_perm[token] = slot
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._ask_permission(token, params)))
        deadline = time.time() + PERMISSION_TIMEOUT
        while time.time() < deadline:
            if slot.get("optionId") is not None:
                self._pending_perm.pop(token, None)
                return slot["optionId"]
            time.sleep(0.2)
        self._pending_perm.pop(token, None)
        return None

    async def _ask_permission(self, token: str, params: dict[str, Any]) -> None:
        options = params.get("options") or []
        title = params.get("title") or (params.get("toolCall") or {}).get("title") or "tool call"
        rows = [
            [InlineKeyboardButton(str(option.get("name") or option.get("kind") or "option"),
                                  callback_data=f"{token}:{index}")]
            for index, option in enumerate(options)
        ]
        text = f"*Approval needed*\n{title}"
        if params.get("toolCall", {}).get("input"):
            text += f"\n```\n{str(params['toolCall']['input'])[:800]}\n```"
        try:
            await self.app.bot.send_message(
                self.chat_id, text, reply_markup=InlineKeyboardMarkup(rows),
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError as exc:
            log.warning("could not send approval prompt: %s", exc)

    async def handle_permission_callback(self, token: str, index: int) -> bool:
        slot = self._pending_perm.get(token)
        if slot is None:
            return False
        options = slot["params"].get("options") or []
        if 0 <= index < len(options):
            slot["optionId"] = options[index].get("optionId")
            return True
        return False

    # ------------------------------------------------------------------ turn
    async def ask(self, text: str, message=None) -> str:
        await self.ensure()
        await self.app.bot.send_chat_action(self.chat_id, ChatAction.TYPING)
        self.busy = True
        self.last_used = time.time()
        buffer: list[str] = []
        sent: list[Any] = []
        state = {"msg": None, "last_edit": 0.0, "text": ""}

        async def flush(force: bool = False) -> None:
            now = time.time()
            interval = float(self.config.get("stream", "edit_interval", 1.0))
            joined = "".join(buffer)
            if not joined.strip():
                return
            if not force and now - state["last_edit"] < interval:
                return
            state["last_edit"] = now
            if state["msg"] is None:
                state["msg"] = await self.app.bot.send_message(self.chat_id, _chunks(joined)[0])
                sent.append(state["msg"])
                return
            if joined == state["text"]:
                return
            state["text"] = joined
            parts = _chunks(joined)
            try:
                await state["msg"].edit_text(parts[0])
                for extra in parts[1:]:
                    sent.append(await self.app.bot.send_message(self.chat_id, extra))
            except BadRequest as exc:
                if "not modified" not in str(exc).lower():
                    log.debug("edit failed: %s", exc)

        async def drain() -> None:
            while True:
                update = await self._updates.get()
                if update is None:
                    return
                if update.kind == "agent_message_chunk" and update.text:
                    buffer.append(update.text)
                    await flush()
                elif update.kind == "tool_call" and self.verbose:
                    buffer.append(f"\n_{update.tool_title or 'tool'}_\n")
                    await flush(force=True)

        drain_task = asyncio.create_task(drain())
        try:
            async with self.typing():
                result = await asyncio.to_thread(
                    self.client.prompt, self.session_id, text,
                    timeout=float(self.config.get("acp", "turn_timeout", 900.0)),
                )
        except AcpError as exc:
            self._updates.put_nowait(None)
            await drain_task
            self.busy = False
            raise
        self._updates.put_nowait(None)
        await drain_task
        await flush(force=True)
        self.busy = False
        self.store.touch(self.key)

        usage = result.usage or {}
        self.usage["input"] += int(usage.get("inputTokens") or 0)
        self.usage["output"] += int(usage.get("outputTokens") or 0)
        self.usage["cached"] += int(usage.get("cachedReadTokens") or 0)
        self.usage["turns"] += 1
        anchor = state["msg"] or message
        if self.config.get("stream", "show_usage", True) and anchor is not None:
            try:
                await anchor.reply_text(
                    f"{result.stop_reason} · in {usage.get('inputTokens', '?')} "
                    f"out {usage.get('outputTokens', '?')} cached {usage.get('cachedReadTokens', 0)}"
                )
            except TelegramError:
                pass
        return result.text

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
                except asyncio.TimeoutError:
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
        self.client.cancel(self.session_id)
        return True

    async def respawn(self, yolo: bool) -> None:
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
        self.token = str(config.get("telegram", "bot_token", "") or "")
        self.allowed = {int(str(u)) for u in (config.get("telegram", "allowed_users") or []) if str(u).strip()}
        self.agents: dict[int, ChatAgent] = {}
        self.queues: dict[int, asyncio.Queue[tuple[str, Any]]] = {}
        self.workers: dict[int, asyncio.Task] = {}

    # ---------------------------------------------------------------- helpers
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
            except asyncio.TimeoutError:
                idle = float(self.config.get("session", "idle_timeout", 1800))
                if not agent.busy and time.time() - agent.last_used > idle and agent.client is not None:
                    log.info("idle eviction for chat %s", chat_id)
                    await agent.shutdown()
                if queue.empty():
                    return
                continue
            try:
                if kind == "prompt":
                    message = payload
                    try:
                        await agent.ask(message.text, message=message)
                    except AcpError as exc:
                        await message.reply_text(f"agent error: {exc}")
                    except Exception as exc:  # noqa: BLE001 - a turn must never kill the worker
                        log.exception("turn failed")
                        await message.reply_text(f"turn failed: {exc}")
                elif kind == "resume":
                    message, session_id = payload
                    try:
                        await agent.load(session_id)
                        await message.reply_text(f"resumed {session_id[:8]}")
                    except AcpError as exc:
                        await message.reply_text(f"resume failed: {exc}")
            finally:
                queue.task_done()

    # -------------------------------------------------------------- handlers
    async def on_text(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        message = update.effective_message
        if message is None or not message.text:
            return
        agent = self.agent_for(update.effective_chat.id)
        if agent.busy:
            depth = self.enqueue(update.effective_chat.id, "prompt", message)
            await message.reply_text(f"queued (position {depth}). the running turn finishes first.")
            return
        self.enqueue(update.effective_chat.id, "prompt", message)

    async def cmd_new(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        session_id = await agent.new_session()
        await update.effective_message.reply_text(f"new session {session_id[:8]} in {agent.cwd}")

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
        model = (options.get("model") or {}).get("currentValue", "?")
        mode = (options.get("mode") or {}).get("currentValue", "?")
        thinking = (options.get("thinking") or {}).get("currentValue", "-")
        uptime = int(time.time() - agent.started_at) if alive else 0
        await update.effective_message.reply_text(
            f"session {agent.session_id[:8] or '-'}\n"
            f"cwd {agent.cwd}\n"
            f"model {model} · mode {mode} · thinking {thinking}\n"
            f"process {'alive' if alive else 'stopped'} ({uptime}s) · busy={agent.busy}\n"
            f"queue {queue.qsize() if queue else 0} · verbose={agent.verbose}"
        )

    async def cmd_usage(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        u = self.agent_for(update.effective_chat.id).usage
        await update.effective_message.reply_text(
            f"turns {u['turns']} · input {u['input']} · output {u['output']} · cached {u['cached']}"
        )

    async def cmd_sessions(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        await agent.ensure()
        data = await asyncio.to_thread(agent.client.list_sessions, agent.cwd)
        rows = (data.get("sessions") or [])[:10]
        if not rows:
            return await update.effective_message.reply_text("no sessions recorded")
        lines = []
        for row in rows:
            sid = str(row.get("sessionId") or "")
            meta = row.get("_meta") or {}
            lines.append(f"{sid[:8]}  {row.get('updatedAt', '')[:16]}  {meta.get('size', '?')}B")
        lines.append("\n/resume <id> to continue one")
        await update.effective_message.reply_text("\n".join(lines))

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
            new_id = await agent.fork()
            await update.effective_message.reply_text(f"forked into {new_id[:8]}")
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
        session_id = await agent.new_session()
        await update.effective_message.reply_text(f"cwd {target}, new session {session_id[:8]}")

    # -------------------------------------------------- config pickers (UI)
    PAGE_SIZE = 8

    def _values(self, agent: ChatAgent, config_id: str) -> list[str]:
        option = (getattr(agent, "options", {}) or {}).get(config_id) or {}
        return [str(o.get("value")) for o in (option.get("options") or [])]

    def _current(self, agent: ChatAgent, config_id: str) -> str:
        option = (getattr(agent, "options", {}) or {}).get(config_id) or {}
        return str(option.get("currentValue") or "?")

    def _providers(self, agent: ChatAgent, config_id: str) -> list[tuple[str, list[int]]]:
        values = self._values(agent, config_id)
        grouped: dict[str, list[int]] = {}
        for index, value in enumerate(values):
            provider = value.split("/", 1)[0] if "/" in value else "other"
            grouped.setdefault(provider, []).append(index)
        return sorted(grouped.items())

    async def _show_config(
        self,
        agent: ChatAgent,
        chat_id: int,
        config_id: str,
        screen: str = "l",
        arg: int = 0,
        edit: Any = None,
    ) -> None:
        current = self._current(agent, config_id)
        providers = self._providers(agent, config_id)
        multi_provider = len(providers) > 1

        if screen == "p" and multi_provider:
            rows = []
            row: list[InlineKeyboardButton] = []
            for index, (provider, indices) in enumerate(providers):
                label = f"{provider} ({len(indices)})"
                if provider == (current.split("/", 1)[0] if "/" in current else "other"):
                    label = f"• {label}"
                row.append(InlineKeyboardButton(label[:60], callback_data=f"o:{config_id}:l:{index}:0"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            text = f"{config_id}: {current}\n{len(providers)} providers · pick one"
        else:
            page = max(0, arg)
            if multi_provider:
                provider_name, indices = providers[page] if page < len(providers) else providers[0]
                page = 0
                values = [self._values(agent, config_id)[i] for i in indices]
                start, pages = 0, (len(values) + self.PAGE_SIZE - 1) // self.PAGE_SIZE
                label_prefix = provider_name
            else:
                values = self._values(agent, config_id)
                pages = max(1, (len(values) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
                page = min(page, pages - 1)
                start = page * self.PAGE_SIZE
                label_prefix = config_id
            window = values[start:start + self.PAGE_SIZE]
            rows = []
            for offset, value in enumerate(window):
                mark = "• " if value == current else ""
                rows.append([InlineKeyboardButton(f"{mark}{value}"[:60],
                                                  callback_data=f"o:{config_id}:s:{start + offset}:0")])
            nav: list[InlineKeyboardButton] = []
            if multi_provider:
                nav.append(InlineKeyboardButton("◀ providers", callback_data=f"o:{config_id}:p:0:0"))
            if pages > 1:
                if page > 0:
                    nav.append(InlineKeyboardButton("◀", callback_data=f"o:{config_id}:l:{arg}:{page - 1}"))
                nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="o:noop"))
                if page < pages - 1:
                    nav.append(InlineKeyboardButton("▶", callback_data=f"o:{config_id}:l:{arg}:{page + 1}"))
            if nav:
                rows.append(nav)
            text = (
                f"{config_id}: {current}\n{label_prefix} · {len(values)} option(s)"
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

    async def _apply_pick(self, agent: ChatAgent, config_id: str, value: str) -> None:
        result = await asyncio.to_thread(agent.client.set_config_option, agent.session_id, config_id, value)
        agent.options = {opt.get("id"): opt for opt in (result.get("configOptions") or [])}

    async def cmd_model(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        await agent.ensure()
        await self._show_config(agent, update.effective_chat.id, "model", "p", 0)

    async def cmd_mode(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        await agent.ensure()
        await self._show_config(agent, update.effective_chat.id, "mode", "l", 0)

    async def cmd_thinking(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        await agent.ensure()
        await self._show_config(agent, update.effective_chat.id, "thinking", "l", 0)

    async def cmd_verbose(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        agent.verbose = not agent.verbose
        await update.effective_message.reply_text(f"tool output {'on' if agent.verbose else 'off'}")

    async def cmd_skills(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
        agent = self.agent_for(update.effective_chat.id)
        if agent.client is None or not agent.session_id:
            return await update.effective_message.reply_text("no session to steer")
        if not agent.busy:
            return await update.effective_message.reply_text("nothing running; send it as a normal message")
        agent.client.notify("_session/steering", {"sessionId": agent.session_id, "message": " ".join(ctx.args)})
        await update.effective_message.reply_text(
            "steer sent (experimental: it will not cancel the turn, but this agent version may ignore it)"
        )

    async def cmd_yolo(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        await agent.respawn(yolo=True)
        await update.effective_message.reply_text("agent respawned with approvals off. /new to reset, /kill to undo.")

    async def cmd_kill(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        if agent.client is not None:
            await asyncio.to_thread(agent.client.close, agent.session_id)
            agent.client = None
            await update.effective_message.reply_text("agent process stopped")
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
        lines = [f"/{name} — {desc}" for name, desc in COMMANDS]
        await update.effective_message.reply_text("\n".join(lines) + "\n\n" + FORWARD_HINT)

    async def on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not self._authorized(update):
            return
        data = str(query.data or "")
        chat_id = update.effective_chat.id
        agent = self.agent_for(chat_id)

        if data.startswith("o:"):
            parts = data.split(":")
            if len(parts) < 3 or parts[1] == "noop":
                return await query.answer()
            _, config_id, screen = parts[0], parts[1], parts[2]
            a = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            b = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            try:
                await agent.ensure()
                if screen == "s":
                    value = self._values(agent, config_id)[a]
                    await self._apply_pick(agent, config_id, value)
                    await query.answer("model set" if config_id == "model" else f"{config_id} set")
                    await query.edit_message_text(f"{config_id} = {value}")
                    return
                if screen == "p":
                    await query.answer()
                    await self._show_config(agent, chat_id, config_id, "p", 0, edit=query.message)
                    return
                if screen == "l":
                    await query.answer()
                    await self._show_config(agent, chat_id, config_id, "l", a, b, edit=query.message)
                    return
            except (AcpError, IndexError) as exc:
                await query.answer("failed", show_alert=False)
                await self.app.bot.send_message(chat_id, f"{config_id} failed: {exc}")
                return
            await query.answer()
            return

        try:
            token, index = data.rsplit(":", 1)
            ok = await agent.handle_permission_callback(token, int(index))
            await query.answer("recorded" if ok else "expired")
            if ok:
                await query.edit_message_text(f"{query.message.text}\n\n→ selected")
        except (ValueError, AttributeError):
            await query.answer("bad callback")

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
        app.add_handler(CommandHandler("sessions", self.cmd_sessions))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CommandHandler("continue", self.cmd_continue))
        app.add_handler(CommandHandler("fork", self.cmd_fork))
        app.add_handler(CommandHandler("cwd", self.cmd_cwd))
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("thinking", self.cmd_thinking))
        app.add_handler(CommandHandler("verbose", self.cmd_verbose))
        app.add_handler(CommandHandler("skills", self.cmd_skills))
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

        await app.initialize()
        await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in COMMANDS])
        me = await app.bot.get_me()
        log.info("ompwire online as @%s (allowed users: %s)", me.username, sorted(self.allowed))
        home = str(self.config.get("telegram", "home_channel", "") or "")
        if home:
            try:
                await app.bot.send_message(int(home), f"ompwire online as @{me.username}")
            except TelegramError as exc:
                log.warning("home channel ping failed: %s", exc)
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            for agent in self.agents.values():
                await agent.shutdown()


class _TextOnly:
    """Payload wrapper for /queue, which has text but no message object."""

    def __init__(self, text: str, message: Any) -> None:
        self.text = text
        self._message = message

    async def reply_text(self, text: str) -> Any:
        return await self._message.reply_text(text)


def serve(config: Config) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    transport = Transport(config)
    if not transport.token:
        print("error: TELEGRAM_BOT_TOKEN is not set")
        return 2
    if not transport.allowed:
        print("error: telegram.allowed_users is empty, nobody could talk to the bot")
        return 2
    try:
        asyncio.run(transport.run())
    except KeyboardInterrupt:
        pass
    return 0
