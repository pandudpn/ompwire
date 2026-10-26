"""Telegram transport for ompwire.

Design rule: this module owns transport concerns only (polling, posting each step of a turn,
approvals, steering and queueing, session routing). Anything the agent can do itself is
forwarded as a prompt.

Threading model: the ACP client is synchronous and talks on its own reader thread;
Telegram is asyncio. The bridge is one asyncio.Queue fed by `loop.call_soon_threadsafe`.
An approval is answered on its own thread, which waits for the button while the reader
keeps delivering updates.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import logging.handlers
import os
import pathlib
import re
import signal
import time
from pathlib import Path
from typing import Any

# PTB 22 warns on every RetryAfter.retry_after read until a process opts into the timedelta it
# returns from v23. _retry_window already takes either, so opt in rather than log noise.
os.environ.setdefault("PTB_TIMEDELTA", "true")

from telegram import (
    BotCommand,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import __version__, engine, omp_config, omp_sessions
from . import schedules as schedule_lib
from .acp import AcpError
from .attachments import incoming_summary, safe_name
from .attachments import save as save_incoming
from .audience import CHANNEL, GROUP, PRIVATE, Audience
from .config import Config, approval_policy, save_override
from .engine import (
    PERMISSION_TIMEOUT,
    RESUME_PROMPT,
    mcp_label,
    resolve_mcp_servers,
)
from .format import now_wib, tokens, wib_time
from .format import uptime as fmt_uptime
from .gateways import remember_chat
from .tg_html import card, change, esc, help_sections
from .tg_login import LoginMixin
from .tg_pickers import PickerMixin
from .tg_skills import SkillsMixin
from .tg_surface import (
    STEERED,
    TelegramSurface,
    app_send,
    held_text,
    queued_text,
    send_html,
    send_md,
)

log = logging.getLogger("ompwire.telegram")

IDLE_CHECK_INTERVAL = 60.0
# This transport owns the store keys under this prefix: sessions, prefs and inflight rows.
KEY_PREFIX = "tg:"

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
    ("verbose", "toggle activity in this chat: trace, todo, notices"),
    ("commands", "search the agent's own slash commands"),
    ("queue", "queue a prompt behind the running turn"),
    ("compact", "run omp's context compaction now"),
    ("compaction", "show or set omp's auto-compaction threshold"),
    ("steer", "add an instruction to the running turn"),
    ("schedule", "scheduled turns: list, run now, remove"),
    ("reasoning", "show or hide the agent's thinking"),
    ("yolo", "respawn the agent with approvals off, for this chat only"),
    ("kill", "stop the agent process for this chat"),
    ("whoami", "ids and transport info"),
    ("help", "this list"),
]


FORWARD_HINT = (
    "Everything else is sent to the agent as a prompt. Its own commands work here, "
    "including /skill:<name> and /compact."
)


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


class Transport(SkillsMixin, LoginMixin, PickerMixin):
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
        self.token = str(config.get("gateway", "credential", "") or "")
        # Telegram names a person by a numeric id; the gateway keeps ids as text for every platform
        self.audience = Audience.from_config(config)
        # (chat, title, kind) already written to seen_chats.json by this process
        self._noted: set[tuple[str, str, str]] = set()
        self.allowed = {int(user) for user in self.audience.allowed}
        self.username = ""   # the bot's own @name, set at startup; a group mention is checked against it
        self.bot_id = 0
        self.agents: dict[int, engine.ChatAgent] = {}
        self.queues: dict[int, asyncio.Queue[tuple[str, Any]]] = {}
        self.workers: dict[int, asyncio.Task] = {}
        # messages typed while a question is open, released once it is answered or skipped
        self.held: dict[int, list[tuple[Any, Any]]] = {}
        # chats where "Other answer" was tapped: the next typed message is the answer
        self.answer_armed: set[int] = set()
        # set on SIGTERM: the worker stops taking turns, so the interrupted one stays on record
        self.stopping = False

    # ---------------------------------------------------------------- helpers
    async def send_html(self, chat_id: int, body: str):
        return await send_html(self.app.bot, chat_id, body)

    async def send_md(self, chat_id: int, text: str):
        return await send_md(self.app.bot, chat_id, text)

    def agent_for(self, chat_id: int) -> engine.ChatAgent:
        agent = self.agents.get(chat_id)
        if agent is None:
            # a negative id is a group or a channel: other people read it
            agent = chat_agent(self.config, chat_id, f"{KEY_PREFIX}{chat_id}", self.app,
                               shared=chat_id < 0)
            self.agents[chat_id] = agent
        return agent

    def _authorized(self, update: Update) -> bool:
        return not self._refusal(update)

    def _refusal(self, update: Update) -> str:
        """Why this update is not taken ("" when it is): the gateway's audience decides."""
        chat = update.effective_chat
        if chat is None:
            return "no chat"
        if update.callback_query is not None:
            # a button press: the person who pressed it, wherever the buttons were posted
            user = update.effective_user
            allowed = user is not None and str(user.id) in self.audience.allowed
            return "" if allowed else "sender not allowed"
        kind = {Chat.PRIVATE: PRIVATE, Chat.CHANNEL: CHANNEL}.get(chat.type, GROUP)
        user = update.effective_user
        message = update.effective_message
        return self.audience.admit(chat=str(chat.id), kind=kind,
                                   sender=str(user.id) if user else "",
                                   addressed=kind == GROUP and self._addressed(message))

    def _addressed(self, message: Any) -> bool:
        """A group message meant for the bot: an @mention of it, a reply to one of its messages,
        or a command written for it (`/status@this_bot`)."""
        if message is None:
            return False
        reply = message.reply_to_message
        if reply is not None and reply.from_user is not None and reply.from_user.id == self.bot_id:
            return True
        text = message.text or message.caption or ""
        entities = message.entities or message.caption_entities or ()
        name = self.username.lower()
        for entity in entities:
            piece = text[entity.offset:entity.offset + entity.length].lower()
            if entity.type == MessageEntity.MENTION and name and piece == f"@{name}":
                return True
            text_mention = entity.type == MessageEntity.TEXT_MENTION and entity.user is not None
            if text_mention and entity.user.id == self.bot_id:
                return True
            if entity.type == MessageEntity.BOT_COMMAND and entity.offset == 0:
                # a bare /command in a group reaches every bot; only /command@this_bot is ours
                return name != "" and piece.endswith(f"@{name}")
        return False

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
                chat_id,
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
            chat_id, card("schedules", f"{len(items)}", [("scheduled", "\n".join(rows))],
                               "run one now or remove it with the buttons"),
            parse_mode=ParseMode.HTML, reply_markup=keyboard)

    async def _deny(self, update: Update) -> None:
        """Say nothing. A refusal tells a stranger, or a whole group, that a bot answers here;
        the log line is where an owner finds out why a message was not taken."""
        user = update.effective_user
        log.info("update dropped chat=%s user=%s: %s",
                 update.effective_chat.id if update.effective_chat else "?",
                 user.id if user else "-", self._refusal(update) or "not allowed")

    def _note_chat(self, chat: Any) -> None:
        """Remember a group or channel by id and title, for `gateway update` to offer later.
        Telegram has no call that lists a bot's chats, so the bot keeps its own list."""
        if chat is None or chat.type == Chat.PRIVATE:
            return
        kind = "channel" if chat.type == Chat.CHANNEL else "group"
        entry = (str(chat.id), str(chat.title or ""), kind)
        if entry in self._noted:
            return
        self._noted.add(entry)
        try:
            remember_chat(str(self.config.get("gateway", "name", "telegram") or "telegram"),
                          *entry)
        except OSError as exc:
            log.warning("could not record chat %s: %s", chat.id, exc)

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
                    await agent.stop_process()
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
                        await agent.ask(message.text,
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

    async def on_command(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """A slash command this transport does not own goes to the agent as a prompt.

        FORWARD_HINT promises /skill:<name> and /compact work; without this they died in a
        Telegram "unknown command" void. Registered commands never reach here: their
        CommandHandler runs first and stops the update.
        """
        if not self._authorized(update):
            return await self._deny(update)
        message = update.effective_message
        if message is None or not message.text:
            return
        name = message.text.split()[0].split("@")[0][1:].lower()
        if name in {cmd for cmd, _ in COMMANDS} | {"start"}:
            return
        log.info("forwarding /%s to the agent chat=%s", name, update.effective_chat.id)
        await self.submit(update.effective_chat.id, message, message)

    async def submit(self, chat_id: int, payload: Any, message: Any) -> None:
        """Route one incoming message: into the running turn, or into the queue.

        By default a message sent while the agent works steers that turn, the way a person
        interrupts a colleague. `[telegram] busy_message = "queue"` restores the old FIFO, and
        `/queue` always queues. Anything omp declines to inject still lands in the queue.
        """
        if chat_id < 0:
            payload = self._framed(payload, message)
        agent = self.agent_for(chat_id)
        if chat_id in self.answer_armed:
            # "Other answer" was tapped: this message is the answer, whatever else is waiting
            self.answer_armed.discard(chat_id)
            await self._route(chat_id, agent, payload, message)
            await self._release_held(chat_id)
            return
        if agent.pending_options:
            # the agent is waiting on its question; a message about something else must not
            # jump ahead of the answer, so it waits until the question is answered or skipped
            held = self.held.setdefault(chat_id, [])
            held.append((payload, message))
            log.info("held while a question is open chat=%s position=%s", chat_id, len(held))
            await self._event(agent, message, held_text(len(held)))
            return
        await self._route(chat_id, agent, payload, message)

    def _framed(self, payload: Any, message: Any) -> Any:
        """A group or channel prompt as the agent should read it: the bot's own @mention taken
        out, and the sender named, since one session serves everyone in that chat."""
        text = str(getattr(payload, "text", "") or "")
        if self.username:
            text = re.sub(rf"@{re.escape(self.username)}\b", "", text, flags=re.IGNORECASE)
            text = " ".join(text.split()) if "\n" not in text else text.strip()
        user = getattr(message, "from_user", None)
        chat = getattr(message, "sender_chat", None) or getattr(message, "chat", None)
        who = (getattr(user, "full_name", "") or getattr(user, "username", "") if user is not None
               else getattr(chat, "title", "") or "channel")
        framed = _TextOnly(f"[{who}] {text}" if who else text, getattr(payload, "_message", message),
                           images=list(getattr(payload, "images", None) or []))
        framed.original = getattr(payload, "original", None)
        framed.resumes = int(getattr(payload, "resumes", 0))
        return framed

    async def _release_held(self, chat_id: int) -> None:
        """The question is settled: what was typed meanwhile runs next, in order."""
        held = self.held.pop(chat_id, [])
        for payload, _message in held:
            self.enqueue(chat_id, "prompt", payload)
        if held:
            log.info("released %s held message(s) chat=%s", len(held), chat_id)

    def _settle_question(self, chat_id: int, agent: engine.ChatAgent) -> None:
        agent.pending_options = []
        self.answer_armed.discard(chat_id)

    async def _route(self, chat_id: int, agent: engine.ChatAgent, payload: Any, message: Any) -> None:
        if not agent.busy:
            self.enqueue(chat_id, "prompt", payload)
            return
        if str(self.config.get("telegram", "busy_message", "steer")) == "steer":
            outcome, reason = await agent.steer(payload.text, getattr(payload, "images", None) or None)
            if outcome == "injected":
                await self._event(agent, message, STEERED)
                return
            if outcome == "promptRequired":
                # the turn ended between the busy check and the steer: this is a fresh prompt
                self.enqueue(chat_id, "prompt", payload)
                return
            log.warning("steer not accepted chat=%s outcome=%s reason=%s; queueing", chat_id,
                        outcome, reason or "-")
        depth = self.enqueue(chat_id, "prompt", payload)
        await self._event(agent, message, queued_text(depth))

    async def _event(self, agent: engine.ChatAgent, message: Any, text: str) -> None:
        """Where an ordinary message went (steered, queued, held). A quiet chat only ever sees
        the answer, so there it is not said."""
        if agent.verbose:
            await self._ack(message, text)

    async def _ack(self, message: Any, text: str) -> None:
        """A reply quoting the message itself, so it is clear which message went where.

        python-telegram-bot only quotes by default in groups; in a private chat reply_text
        posts a plain new message, so the quote is asked for explicitly."""
        try:
            await message.reply_text(text, do_quote=True)
        except TelegramError as exc:
            log.warning("ack failed: %s", exc)

    def _agent_gate(self, agent: engine.ChatAgent) -> str:
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

    def session_card(self, agent: engine.ChatAgent, title: str, note: str = "") -> str:
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

    async def _busy(self, update: Update, agent: engine.ChatAgent, command: str) -> bool:
        """These commands replace the agent process; doing that mid-turn cut the turn off with
        an "agent error". They wait for the turn instead of killing it behind the person's back."""
        if not agent.busy:
            return False
        await update.effective_message.reply_text(
            f"a turn is still running; {command} waits until it ends, or /stop it first")
        return True

    async def cmd_new(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        if await self._busy(update, agent, "/new"):
            return
        await agent.new_session()
        log.info("new session chat=%s session=%s", update.effective_chat.id, agent.session_id[:8])
        if agent.pending_options or update.effective_chat.id in self.answer_armed:
            # the question belonged to the old session; what waited on it goes to the new one
            self._settle_question(update.effective_chat.id, agent)
            await agent.surface.retire_options("closed by /new")
        await self._release_held(update.effective_chat.id)
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
            *self._context_rows(agent, self._session_usage(agent)),
            ("clock", now_wib()),
        ]
        await update.effective_message.reply_text(
            card("📊 status", "", rows), parse_mode=ParseMode.HTML
        )

    async def cmd_usage(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Session totals from omp's transcript, so a gateway restart or /resume keeps them."""
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        seen = self._session_usage(agent)
        if seen is None:
            rows = [("tokens", "unknown (no transcript for this session yet)")]
        else:
            rows = [
                ("input", tokens(seen.input)),
                ("output", tokens(seen.output)),
                ("cache", f"{tokens(seen.cache_read)} read · {tokens(seen.cache_write)} written"),
                ("calls", f"{seen.calls:,}"),
            ]
        rows += self._context_rows(agent, seen)
        await update.effective_message.reply_text(
            card("🧮 usage", f"session {agent.session_id[:8] or '-'}", rows),
            parse_mode=ParseMode.HTML,
        )

    @staticmethod
    def _session_usage(agent: engine.ChatAgent) -> omp_sessions.SessionUsage | None:
        if not agent.session_id:
            return None
        try:
            return omp_sessions.usage(agent.session_id)
        except OSError:
            return None

    @staticmethod
    def _context_rows(agent: engine.ChatAgent,
                      seen: omp_sessions.SessionUsage | None = None) -> list[tuple[str, str]]:
        """Where the context is, where omp will compact it, and when it last did."""
        used, size = agent.context["used"], agent.context["size"]
        if not used and seen is not None:
            used = seen.context
        compaction = omp_config.read_compaction()
        threshold = compaction.get("thresholdPercent")
        if size > 0:
            rows = [("context", engine.context_line(used, size))]
        else:
            rows = [("context", f"{tokens(used)} / limit not reported yet" if used else "unknown")]
        if compaction.get("enabled") is False:
            rows.append(("compacts", "off"))
        elif isinstance(threshold, int):
            at = f" ({tokens(size * threshold // 100)})" if size > 0 else ""
            rows.append(("compacts", f"at {threshold}%{at}"))
        if seen is not None and seen.compactions:
            last = seen.compactions[-1]
            times = f" · {len(seen.compactions)}× this session" if len(seen.compactions) > 1 else ""
            rows.append(("last", f"{wib_time(last.at)} · {tokens(last.before)} → "
                                 f"{tokens(last.after)}{times}"))
        return rows

    async def cmd_compact(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Run omp's own /compact. While busy it queues: steering a compaction makes no sense."""
        if not self._authorized(update):
            return await self._deny(update)
        chat_id = update.effective_chat.id
        agent = self.agent_for(chat_id)
        payload = _TextOnly("/compact", update.effective_message)
        if agent.busy:
            depth = self.enqueue(chat_id, "prompt", payload)
            await self._ack(update.effective_message, queued_text(depth))
            return
        log.info("compact requested chat=%s", chat_id)
        await self.submit(chat_id, payload, update.effective_message)

    async def cmd_compaction(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show or change omp's auto-compaction threshold. Applies to every omp client."""
        if not self._authorized(update):
            return await self._deny(update)
        agent = self.agent_for(update.effective_chat.id)
        arg = (ctx.args or [""])[0].strip().lower().rstrip("%")
        if not arg:
            current = omp_config.read_compaction()
            threshold = current.get("thresholdPercent", "?")
            line = engine.context_line(agent.context["used"], agent.context["size"])
            state = "off" if current.get("enabled") is False else "on"
            await update.effective_message.reply_text(
                card("🗜️ compaction", f"{state} at {threshold}%",
                     [("context", line if agent.context["size"] > 0 else "unknown")],
                     "/compaction 10-95 · /compaction off · /compaction on · applies to every "
                     "omp client, new agent processes; /kill restarts this chat's agent"),
                parse_mode=ParseMode.HTML,
            )
            return
        if arg == "off":
            new = omp_config.set_compaction(enabled=False)
        elif arg == "on":
            new = omp_config.set_compaction(enabled=True)
        else:
            try:
                percent = int(arg)
            except ValueError:
                return await update.effective_message.reply_text("usage: /compaction [10-95|off|on]")
            try:
                new = omp_config.set_compaction(thresholdPercent=percent)
            except ValueError as exc:
                return await update.effective_message.reply_text(str(exc))
        if await self._busy(update, agent, "/compaction"):
            return
        target = "off" if new.get("enabled") is False else "on"
        await self.announce(update.effective_chat.id,
                            change("compaction", target, f"{new.get('thresholdPercent', '?')}%",
                                   "applies to every omp client, new agent processes"))

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
            await self._ack(update.effective_message, queued_text(depth))

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
        if await self._busy(update, agent, "/fork"):
            return
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
        if await self._busy(update, agent, "/cwd"):
            return
        agent.cwd = target
        await agent.new_session()
        log.info("cwd change chat=%s cwd=%s session=%s", update.effective_chat.id, target,
                 agent.session_id[:8])
        await update.effective_message.reply_text(
            self.session_card(agent, "📁 cwd changed", "a new session starts in that directory"),
            parse_mode=ParseMode.HTML,
        )

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
        path = save_override("approval", "policy", value, self.config.overrides_path)
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
        agent.set_display("verbose", not agent.verbose)
        await self.announce(update.effective_chat.id,
                           f"activity (trace, todo list, notices): {'on' if agent.verbose else 'off'} · this chat")

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
            agent.set_display("reasoning", value)
            return await self.announce(update.effective_chat.id,
                                       f"reasoning: {'on' if value else 'off'} · this chat")
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
        await self._ack(update.effective_message,
                        queued_text(depth) if agent.busy else "▶️ starting now")

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
            return await self._ack(update.effective_message, STEERED)
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
        if await self._busy(update, agent, "/yolo"):
            return
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

    async def on_added(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """The bot was added to a group or a channel, or removed from one.

        A chat that is not listed yet cannot ask the bot anything, /whoami included, so its id
        reaches the owner here instead: in a private message, never in the chat itself.
        """
        change = update.my_chat_member
        if change is None or change.chat.type == Chat.PRIVATE:
            return
        chat = change.chat
        joined = change.new_chat_member.status in {"member", "administrator"}
        listed = str(chat.id) in self.audience.chats
        if joined:
            self._note_chat(chat)
        log.info("bot %s %s %s %r (listed: %s)", "added to" if joined else "removed from",
                 chat.type, chat.id, chat.title, listed)
        home = self._home_chat()
        if home is None or home < 0 or not joined:
            return
        mode = self.audience.mode(str(chat.id))
        note = (f"listed · mode {mode}" if listed else
                f"not listed, so it is ignored · add it: ompwire gateway update "
                f"{self.config.get('gateway', 'name', 'telegram')} (it is offered by name)")
        await self.announce(home, card(f"➕ added to a {chat.type}", chat.title or "",
                                       [("id", chat.id)], note))

    async def cmd_whoami(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        line = f"user {user.id if user else '-'} · chat {chat.id} ({chat.type})"
        if chat.type != Chat.PRIVATE:
            line += f" · mode {self.audience.mode(str(chat.id))}"
        await update.effective_message.reply_text(line)

    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        names = dict(COMMANDS)
        groups = [
            ("session", ["new", "sessions", "resume", "continue", "fork", "cwd", "kill"]),
            ("agent", ["model", "mode", "thinking", "approve", "trace", "verbose", "yolo"]),
            ("catalog", ["skills", "login", "commands"]),
            ("running", ["stop", "queue", "steer", "status", "usage", "context",
                         "compact", "compaction"]),
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
                if not agent.pending_options:
                    await query.answer("that question is closed")
                    return
                await query.answer("type your answer")
                agent.pending_options = []
                self.answer_armed.add(chat_id)
                # the buttons go with the choice: a later tap would send an answer nobody chose
                await agent.surface.retire_options("type your answer; your next message is it")
                return
            if choice == "skip":
                self._settle_question(chat_id, agent)
                await query.answer("skipped")
                await agent.surface.retire_options("skipped")
                await self._release_held(chat_id)
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
            agent.surface.options_message = None
            self._settle_question(chat_id, agent)
            await self._route(chat_id, agent, _TextOnly(answer, query.message), query.message)
            await self._release_held(chat_id)
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

        if data in {"sk:noop", "lg:noop"}:  # the page counter between the arrows
            return await query.answer()
        try:
            token, index = data.rsplit(":", 1)
            label = await agent.answer_permission(token, int(index))
            await query.answer(label or "expired")
        except (ValueError, AttributeError):
            await query.answer("bad callback")

    async def log_update(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        self._note_chat(update.effective_chat)
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
        app.add_handler(CommandHandler("compact", self.cmd_compact))
        app.add_handler(CommandHandler("compaction", self.cmd_compaction))
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
        app.add_handler(ChatMemberHandler(self.on_added, ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_error_handler(self.on_error)
        # Registered commands stop at their CommandHandler; what is left reaches on_command,
        # which forwards it to the agent, and on_text takes plain prose.
        # a new message or a new channel post; an edit would run the same prompt a second time
        new = filters.UpdateType.MESSAGE | filters.UpdateType.CHANNEL_POST
        app.add_handler(MessageHandler(new & filters.TEXT & filters.COMMAND, self.on_command))
        app.add_handler(MessageHandler(new & filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(new & (filters.PHOTO | filters.Document.IMAGE),
                                       self.on_photo))
        # any other document: PDF, zip, csv, text. The image filter above owns the rest.
        app.add_handler(MessageHandler(new & filters.Document.ALL & ~filters.Document.IMAGE,
                                       self.on_document))
        app.add_handler(MessageHandler(filters.ALL, self.log_update), group=-1)

        await app.initialize()
        await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in _unique_commands()])
        me = await app.bot.get_me()
        self.username, self.bot_id = str(me.username or ""), int(me.id)
        log.info("ompwire online as @%s (allowed users: %s, chats: %s)", me.username,
                 sorted(self.allowed), sorted(self.audience.chats) or "none")
        await self.startup_banner(str(me.username))
        await app.start()
        # Kept, not dropped: a message sent during the few seconds of a restart is still a
        # message the person expects an answer to.
        await app.updater.start_polling(drop_pending_updates=False,
                                        allowed_updates=Update.ALL_TYPES)
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
        enabled = bool(self.config.get("telegram", "resume_interrupted", True))
        resumable, exhausted = engine.resume_plan(str(self.config.get("session", "store")),
                                                 KEY_PREFIX)
        exhausted_keys = {key for key, _prompt, _resumes in exhausted}
        for key, prompt, resumes in [*resumable, *exhausted]:
            try:
                chat_id = int(key[len(KEY_PREFIX):])
            except ValueError:
                continue
            agent = self.agent_for(chat_id)
            # a person on the allow list, or a group or channel the gateway still listens in
            known = chat_id in self.allowed or str(chat_id) in self.audience.chats
            if not enabled or key in exhausted_keys or not known:
                agent.store.end_turn(key)
                log.warning("interrupted turn dropped chat=%s resumes=%s", chat_id, resumes)
                if known:
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
        home = str(self.config.get("gateway", "home_channel", "") or "")
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


def chat_agent(config: Config, chat_id: int, key: str, app: Application, *,
               shared: bool = False) -> engine.ChatAgent:
    return engine.ChatAgent(config, key, TelegramSurface(app, chat_id), shared=shared)


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
    name = config.get("gateway", "name", "?")
    if not transport.token:
        print(f"error: gateway {name!r} has no credential; run `ompwire gateway start {name}`")
        return 2
    if not transport.allowed:
        print(f"error: gateway {name!r} has no allowed users, nobody could talk to the bot")
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
              "       ompwire gateway start   (choose this gateway, then a new token)\n"
              "     `ompwire doctor` shows the same check without starting anything.")
        return 2
    except Conflict:
        print("\nFAIL another process is polling this bot (409 Conflict). Telegram delivers\n"
              "     each update to one poller, so updates are being dropped. Stop the other\n"
              "     one: `ompwire gateway list` shows what is running.")
        return 2
    return 0

