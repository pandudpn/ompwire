"""WhatsApp transport for ompwire, over a linked device (neonize / whatsmeow).

Same rules as the Telegram transport: this module owns the platform (receiving, sending, who
may talk, routing a message into the running turn or the queue) and the engine owns the turn.

Who may talk:
- a direct chat: the sender's phone number must be on the allow list;
- a group: the group must be listed, the sender must be on the allow list, and the group's mode
  decides when the bot answers: `mention` (an @mention or a reply to the bot), `all`, or `read`
  (observed, never answered).
Anyone else gets no answer at all. A number that is not allowed is a stranger messaging a
personal-looking number; an "unauthorized" reply would only confirm a bot is there.

There are no buttons for a linked device. A question, an approval or a picker is a numbered
list; the next message from that chat that is a number answers it.

Threading: neonize runs whatsmeow in Go and calls back into this event loop; the ACP client
has its own reader thread, bridged by the engine exactly as for Telegram.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, engine, omp_config, omp_sessions
from . import schedules as schedule_lib
from .attachments import incoming_summary, safe_name
from .attachments import save as save_incoming
from .audience import GROUP, PRIVATE, Audience
from .config import Config, approval_policy, save_override
from .engine import RESUME_PROMPT, agent_rows
from .format import now_wib, tokens, wib_time
from .format import uptime as fmt_uptime
from .gateways import remember_chat, user_id
from .rpc import AgentError
from .telegram import COMMANDS, FORWARD_HINT, configure_logging
from .wa_client import (
    GROUP_SERVER,
    LID_SERVER,
    PHONE_SERVER,
    linked_number,
    new_client,
    session_path,
    strip_mention,
    user_part,
)
from .wa_surface import (
    SKIP_WORDS,
    STEERED,
    WhatsAppSurface,
    card,
    held_text,
    numbered,
    queued_text,
)

log = logging.getLogger("ompwire.whatsapp")

KEY_PREFIX = "wa:"
IDLE_CHECK_INTERVAL = 60.0
# A linked device that answers in a burst is what WhatsApp's spam heuristics look for.
MIN_GAP = 1.5
# commands the WhatsApp transport answers itself; anything else starting with "/" is the agent's
HANDLED = {"new", "stop", "status", "usage", "context", "sessions", "resume", "continue", "fork",
           "cwd", "model", "mode", "thinking", "approve", "verbose", "reasoning", "queue",
           "steer", "compact", "compaction", "yolo", "kill", "whoami", "help", "schedule"}


@dataclass
class Incoming:
    """One message as the transport routes it: who, where, what, and how to quote it."""

    chat: str             # JID string of the chat: "<phone>@s.whatsapp.net" or "<id>@g.us"
    sender: str           # phone number of the person, digits only ("" when unknown)
    text: str             # what the person wrote, the bot's own @mention removed
    group: bool
    event: Any = None     # neonize's MessageEv, kept to quote it in a reply
    images: list[dict[str, Any]] = field(default_factory=list)
    who: str = ""         # the sender's display name, for a group
    original: str | None = None
    resumes: int = 0

    @property
    def prompt(self) -> str:
        """What the agent reads. One session serves a whole group, so it is told who asks."""
        if self.group and self.who and not self.resumes:
            return f"[{self.who}] {self.text}"
        return self.text


class Choice:
    """The numbered list a chat is currently answering: a question, an approval, a picker."""

    def __init__(self, kind: str, size: int, data: Any = None) -> None:
        self.kind = kind
        self.size = size
        self.data = data


class Transport:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.started_at = time.time()
        self.name = str(config.get("gateway", "name", "whatsapp") or "whatsapp")
        self.audience = Audience.from_config(config)
        # (chat, title) already written to seen_chats.json by this process
        self._noted: set[tuple[str, str]] = set()
        self.allowed = self.audience.allowed
        self.home = user_id(config.get("gateway", "home_channel")) or next(iter(sorted(self.allowed)), "")
        self.session = session_path(self.name)
        self.client: Any = None
        self.me: set[str] = set()   # the bot's own phone and LID user parts
        self.agents: dict[str, engine.ChatAgent] = {}
        self.queues: dict[str, asyncio.Queue[tuple[str, Any]]] = {}
        self.workers: dict[str, asyncio.Task] = {}
        self.held: dict[str, list[Incoming]] = {}
        self.answer_armed: set[str] = set()
        self.choices: dict[str, Choice] = {}
        self.sent_ids: set[str] = set()   # our own message ids, to recognise a reply to the bot
        self._last_send = 0.0
        self._send_lock = asyncio.Lock()
        self.stopping = False
        self._connected = asyncio.Event()

    # ------------------------------------------------------------ sending
    def _jid(self, chat: str):
        from neonize.utils.jid import build_jid

        user, _, server = chat.partition("@")
        return build_jid(user, server or PHONE_SERVER)

    async def _paced(self) -> None:
        wait = self._last_send + MIN_GAP - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_send = time.monotonic()

    async def send_text(self, chat: str, text: str, quote: Any = None) -> str | None:
        """Send one message; returns its id, or None when WhatsApp refused it."""
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

        if not text.strip():
            return None
        async with self._send_lock:
            await self._paced()
            try:
                if quote is not None:
                    message = await self.client.build_reply_message(text, quote)
                    # neonize reads every "@<digits>" as a mention; agent text is full of them
                    # (ids, ports, emails), and a group would get strangers tagged
                    del message.extendedTextMessage.contextInfo.mentionedJID[:]
                else:
                    # a plain conversation message carries no context, so no mention at all
                    message = Message(conversation=text)
                response = await self.client.send_message(self._jid(chat), message)
            except Exception as exc:
                log.warning("send failed chat=%s: %s", chat, exc)
                return None
        self.sent_ids.add(response.ID)
        if len(self.sent_ids) > 2000:
            self.sent_ids = set(list(self.sent_ids)[-1000:])
        return response.ID

    async def edit_text(self, chat: str, message_id: str, text: str) -> None:
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

        async with self._send_lock:
            await self._paced()
            try:
                await self.client.edit_message(self._jid(chat), message_id, Message(conversation=text))
            except Exception as exc:
                log.debug("edit failed chat=%s: %s", chat, exc)

    async def send_file(self, chat: str, path: Path, as_photo: bool) -> None:
        async with self._send_lock:
            await self._paced()
            if as_photo:
                await self.client.send_image(self._jid(chat), str(path))
            else:
                await self.client.send_document(self._jid(chat), str(path), filename=path.name,
                                                title=path.name)

    async def presence(self, chat: str, typing: bool) -> None:
        from neonize.utils.enum import ChatPresence, ChatPresenceMedia

        state = ChatPresence.CHAT_PRESENCE_COMPOSING if typing else ChatPresence.CHAT_PRESENCE_PAUSED
        try:
            await self.client.send_chat_presence(self._jid(chat), state,
                                                 ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
        except Exception as exc:
            log.debug("presence failed chat=%s: %s", chat, exc)

    async def mark_read(self, event: Any) -> None:
        """Blue ticks: the person can see the bot has the message while the turn runs."""
        from neonize.utils.enum import ReceiptType

        source = event.Info.MessageSource
        try:
            await self.client.mark_read(event.Info.ID, chat=source.Chat, sender=source.Sender,
                                        receipt=ReceiptType.READ)
        except Exception as exc:
            log.debug("mark read failed: %s", exc)

    async def reply(self, incoming: Incoming, text: str) -> None:
        """An acknowledgement that quotes the message it is about."""
        await self.send_text(incoming.chat, text, quote=incoming.event)

    # numbered-answer slots the surface opens and closes
    def open_question(self, chat: str, size: int) -> None:
        self.choices[chat] = Choice("question", size)

    def open_approval(self, chat: str, token: str, size: int) -> None:
        self.choices[chat] = Choice("approval", size, token)

    def close_approval(self, chat: str) -> None:
        current = self.choices.get(chat)
        if current is not None and current.kind == "approval":
            self.choices.pop(chat, None)

    def open_schedule(self, chat: str, name: str) -> None:
        self.choices[chat] = Choice("schedule", 2, name)

    # ------------------------------------------------------------ agents
    def agent_for(self, chat: str) -> engine.ChatAgent:
        agent = self.agents.get(chat)
        if agent is None:
            agent = engine.ChatAgent(self.config, f"{KEY_PREFIX}{chat}", WhatsAppSurface(self, chat),
                                     shared=chat.endswith(f"@{GROUP_SERVER}"))
            self.agents[chat] = agent
        return agent

    def enqueue(self, chat: str, kind: str, payload: Any) -> int:
        queue = self.queues.setdefault(chat, asyncio.Queue())
        queue.put_nowait((kind, payload))
        if chat not in self.workers or self.workers[chat].done():
            self.workers[chat] = asyncio.create_task(self._worker(chat))
        return queue.qsize()

    async def _worker(self, chat: str) -> None:
        queue = self.queues[chat]
        agent = self.agent_for(chat)
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=IDLE_CHECK_INTERVAL)
            except TimeoutError:
                idle = float(self.config.get("session", "idle_timeout", 1800))
                if not agent.busy and time.time() - agent.last_used > idle and agent.client is not None:
                    log.info("idle eviction for chat %s", chat)
                    await agent.stop_process()
                if queue.empty():
                    return
                continue
            try:
                if self.stopping:
                    continue
                if kind == "prompt":
                    try:
                        await agent.ask(payload.prompt, images=payload.images or None,
                                        resumes=payload.resumes, record=payload.original)
                    except AgentError as exc:
                        if not self.stopping:
                            await self.send_text(chat, f"agent error: {exc}")
                    except Exception as exc:
                        log.exception("turn failed")
                        await self.send_text(chat, f"turn failed: {exc}")
                elif kind == "resume":
                    try:
                        await agent.load(payload)
                        await self.send_text(chat, self.session_card(agent, "⏪ resumed",
                                                                     "history lives in the agent session"))
                    except AgentError as exc:
                        await self.send_text(chat, f"resume failed: {exc}")
            finally:
                queue.task_done()

    # ------------------------------------------------------------ receiving
    def _sender(self, source: Any) -> str:
        """The person's phone number. A LID sender carries the number in SenderAlt."""
        for jid in (source.Sender, source.SenderAlt):
            if jid.User and jid.Server == PHONE_SERVER:
                return user_id(jid.User)
        return ""

    def _chat(self, source: Any) -> str:
        """Where to answer. A direct chat answers the person's phone JID, not their LID: the
        session key has to be the same whichever form WhatsApp used for this message."""
        if source.IsGroup:
            return f"{source.Chat.User}@{GROUP_SERVER}"
        phone = self._sender(source)
        if phone:
            return f"{phone}@{PHONE_SERVER}"
        return f"{source.Chat.User}@{source.Chat.Server or LID_SERVER}"

    @staticmethod
    def _context(message: Any) -> Any:
        for name in ("extendedTextMessage", "imageMessage", "documentMessage", "videoMessage"):
            if message.HasField(name):
                return getattr(message, name).contextInfo
        return None

    def addressed(self, message: Any) -> bool:
        """In a group: an @mention of the bot, or a reply to one of its messages."""
        context = self._context(message)
        if context is None:
            return False
        if any(user_part(jid) in self.me for jid in context.mentionedJID):
            return True
        if context.stanzaID and context.stanzaID in self.sent_ids:
            return True
        return bool(context.participant) and user_part(context.participant) in self.me

    async def on_message(self, _client: Any, event: Any) -> None:
        source = event.Info.MessageSource
        if source.IsFromMe:
            return
        message = event.Message
        chat = self._chat(source)
        sender = self._sender(source)
        if source.IsGroup:
            self._note_group(chat)
        reason = self.audience.admit(chat=chat, kind=GROUP if source.IsGroup else PRIVATE,
                                     sender=sender, addressed=bool(source.IsGroup)
                                     and self.addressed(message))
        if reason:
            log.info("message dropped chat=%s sender=%s: %s", chat, sender or "?", reason)
            return
        text = message.conversation or message.extendedTextMessage.text
        images: list[dict[str, Any]] = []
        if message.HasField("imageMessage"):
            image = message.imageMessage
            try:
                raw = await self.client.download_any(message)
            except Exception as exc:
                log.warning("image download failed chat=%s: %s", chat, exc)
                await self.send_text(chat, f"could not fetch the image: {exc}")
                return
            images.append({"type": "image", "mimeType": image.mimetype or "image/jpeg",
                           "data": base64.b64encode(raw).decode("ascii")})
            text = image.caption or ""
            log.info("photo chat=%s bytes=%s", chat, len(raw))
        elif message.HasField("documentMessage"):
            text = await self._document(chat, message)
            if text is None:
                return
        elif not text:
            kind = next((f.name for f, _ in message.ListFields()), "message")
            log.info("unsupported message chat=%s kind=%s", chat, kind)
            if kind in {"audioMessage", "videoMessage", "stickerMessage"}:
                await self.send_text(chat, "that kind of message is not supported yet; send text, "
                                           "a photo or a file")
            return
        text = strip_mention(text, self.me)
        if images and not text.strip():
            text = "Describe this image."
        incoming = Incoming(chat=chat, sender=sender, text=text, group=bool(source.IsGroup),
                            event=event, images=images, who=event.Info.Pushname or sender)
        log.info("message chat=%s sender=%s chars=%s images=%s", chat, sender, len(text), len(images))
        await self.mark_read(event)
        await self.handle(incoming)

    async def _document(self, chat: str, message: Any) -> str | None:
        document = message.documentMessage
        max_mb = float(self.config.get("files", "max_mb", 20.0))
        size = int(document.fileLength or 0)
        if size and max_mb and size > max_mb * 1024 * 1024:
            await self.send_text(chat, f"too large: {size / (1024 * 1024):.1f} MB, "
                                       f"limit is {max_mb:g} MB")
            return None
        try:
            raw = await self.client.download_any(message)
        except Exception as exc:
            log.warning("document download failed chat=%s: %s", chat, exc)
            await self.send_text(chat, f"could not fetch the file: {exc}")
            return None
        base = Path(str(self.config.get("files", "incoming_dir", "~/.local/state/ompwire/files")))
        directory = base.expanduser() / chat.replace("@", "_")
        try:
            target = save_incoming(raw, safe_name(document.fileName or "file"), directory, max_mb)
        except (OSError, ValueError) as exc:
            await self.send_text(chat, f"could not save the file: {exc}")
            return None
        log.info("document chat=%s saved=%s bytes=%s", chat, target, len(raw))
        return incoming_summary(target, document.mimetype or "", document.caption or "")

    # ------------------------------------------------------------ routing
    async def handle(self, incoming: Incoming) -> None:
        """A numbered answer, a command, or a prompt for the agent."""
        word = incoming.text.strip()
        choice = self.choices.get(incoming.chat)
        if choice is not None and not incoming.images and await self.answer(incoming, choice, word):
            return
        if word.startswith("/") and not incoming.images:
            name = word[1:].split()[0].lower() if len(word) > 1 else ""
            if name in HANDLED:
                await self.command(incoming, name, word.split()[1:])
                return
        await self.submit(incoming)

    async def answer(self, incoming: Incoming, choice: Choice, word: str) -> bool:
        """True when this message answered the open list."""
        chat = incoming.chat
        agent = self.agent_for(chat)
        lowered = word.lower()
        if choice.kind == "question":
            if lowered in SKIP_WORDS:
                self.choices.pop(chat, None)
                agent.pending_options = []
                await agent.surface.retire_options("skipped")
                await self._release_held(chat)
                return True
            if lowered.isdigit() and 1 <= int(lowered) <= choice.size:
                answer = agent.pending_options[int(lowered) - 1]
                self.choices.pop(chat, None)
                agent.pending_options = []
                log.info("question answered chat=%s answer=%r", chat, answer)
                await agent.surface.retire_options(f"answered: {answer}")
                await self._route(chat, agent, Incoming(chat, incoming.sender, answer,
                                                         incoming.group, incoming.event,
                                                         who=incoming.who))
                await self._release_held(chat)
                return True
            # anything else is the person's own answer
            self.choices.pop(chat, None)
            agent.pending_options = []
            await agent.surface.retire_options("answered in your own words")
            await self._route(chat, agent, incoming)
            await self._release_held(chat)
            return True
        if not (lowered.isdigit() and 1 <= int(lowered) <= choice.size):
            return False
        index = int(lowered) - 1
        if choice.kind == "approval":
            label = await agent.answer_permission(choice.data, index)
            if label is None:
                await self.reply(incoming, "that approval expired")
            return True
        if choice.kind == "schedule":
            self.choices.pop(chat, None)
            if index == 0:
                await agent._schedule_create(choice.data)
            else:
                agent.pending_schedules.pop(choice.data, None)
                await self.send_text(chat, "schedule proposal cancelled")
            return True
        if choice.kind == "option":
            config_id, values = choice.data
            self.choices.pop(chat, None)
            try:
                await agent.ensure()
                previous = _current(agent, config_id)
                await agent.set_option(config_id, values[index])
                current = _current(agent, config_id)
                if config_id in engine.PREF_KEYS and current:
                    agent.store.set_pref(agent.key, config_id, current)
                await self.send_text(chat, card(f"⚙️ {config_id}", current,
                                                [("previous", previous)],
                                                "selection applied" if current != previous
                                                else "already selected"))
            except AgentError as exc:
                await self.send_text(chat, f"{config_id} failed: {exc}")
            return True
        if choice.kind == "policy":
            self.choices.pop(chat, None)
            await self.set_policy(chat, ("ask", "allow", "deny")[index])
            return True
        return False

    async def submit(self, incoming: Incoming) -> None:
        chat = incoming.chat
        agent = self.agent_for(chat)
        if chat in self.answer_armed:
            self.answer_armed.discard(chat)
            await self._route(chat, agent, incoming)
            await self._release_held(chat)
            return
        if agent.pending_options and agent.plan_review == "pending":
            # a plan waits for review: whatever is typed now is feedback on it, not a new topic
            agent.pending_options = []
            await agent.surface.retire_options("feedback sent; revising the plan")
            await self._route(chat, agent, incoming)
            return
        if agent.pending_options:
            held = self.held.setdefault(chat, [])
            held.append(incoming)
            log.info("held while a question is open chat=%s position=%s", chat, len(held))
            await self._event(agent, incoming, held_text(len(held)))
            return
        await self._route(chat, agent, incoming)

    async def _release_held(self, chat: str) -> None:
        held = self.held.pop(chat, [])
        for incoming in held:
            self.enqueue(chat, "prompt", incoming)
        if held:
            log.info("released %s held message(s) chat=%s", len(held), chat)

    async def _route(self, chat: str, agent: engine.ChatAgent, incoming: Incoming) -> None:
        if not agent.busy:
            self.enqueue(chat, "prompt", incoming)
            return
        if str(self.config.get("whatsapp", "busy_message", "steer")) == "steer":
            outcome, reason = await agent.steer(incoming.prompt, incoming.images or None)
            if outcome == "injected":
                await self._event(agent, incoming, STEERED)
                return
            if outcome == "promptRequired":
                self.enqueue(chat, "prompt", incoming)
                return
            log.warning("steer not accepted chat=%s outcome=%s reason=%s; queueing", chat,
                        outcome, reason or "-")
        depth = self.enqueue(chat, "prompt", incoming)
        await self._event(agent, incoming, queued_text(depth))

    async def _event(self, agent: engine.ChatAgent, incoming: Incoming, text: str) -> None:
        """Where an ordinary message went (steered, queued, held). A quiet chat only ever sees
        the answer, so there it is not said."""
        if agent.verbose:
            await self.reply(incoming, text)

    # ------------------------------------------------------------ commands
    def session_card(self, agent: engine.ChatAgent, title: str, note: str = "") -> str:
        return card(title, now_wib(), agent_rows(agent), note)

    async def command(self, incoming: Incoming, name: str, args: list[str]) -> None:
        chat = incoming.chat
        agent = self.agent_for(chat)
        log.info("command /%s chat=%s", name, chat)
        handler = getattr(self, f"cmd_{name}")
        await handler(incoming, agent, args)

    async def _busy(self, incoming: Incoming, agent: engine.ChatAgent, command: str) -> bool:
        if not agent.busy:
            return False
        await self.reply(incoming, f"a turn is still running; {command} waits until it ends, "
                                   f"or /stop it first")
        return True

    async def cmd_new(self, incoming: Incoming, agent: engine.ChatAgent, _args: list[str]) -> None:
        if await self._busy(incoming, agent, "/new"):
            return
        await agent.new_session()
        if agent.pending_options or incoming.chat in self.answer_armed:
            agent.pending_options = []
            self.answer_armed.discard(incoming.chat)
            self.choices.pop(incoming.chat, None)
            await agent.surface.retire_options("closed by /new")
        await self._release_held(incoming.chat)
        await self.send_text(incoming.chat, self.session_card(
            agent, "🆕 new session", "old sessions stay resumable with /sessions"))

    async def cmd_stop(self, incoming: Incoming, agent: engine.ChatAgent, _args: list[str]) -> None:
        ok = await agent.cancel()
        await self.reply(incoming, "cancel sent" if ok else "nothing running")

    async def cmd_status(self, incoming: Incoming, agent: engine.ChatAgent, _args: list[str]) -> None:
        queue = self.queues.get(incoming.chat)
        alive = agent.client is not None and agent.client.alive
        rows = [
            *agent_rows(agent),
            ("process", f"{'alive' if alive else 'stopped'} · "
                        f"{fmt_uptime(time.time() - agent.started_at)}"),
            ("state", "busy" if agent.busy else "idle"),
            ("queue", queue.qsize() if queue else 0),
            ("work", f"{agent.turns} turns · {agent.tools_used} tools"),
            *_context_rows(agent, _session_usage(agent)),
            ("clock", now_wib()),
        ]
        await self.send_text(incoming.chat, card("📊 status", "", rows))

    async def cmd_usage(self, incoming: Incoming, agent: engine.ChatAgent, _args: list[str]) -> None:
        seen = _session_usage(agent)
        if seen is None:
            rows: list[tuple[str, object]] = [("tokens", "unknown (no transcript yet)")]
        else:
            rows = [("input", tokens(seen.input)), ("output", tokens(seen.output)),
                    ("cache", f"{tokens(seen.cache_read)} read · {tokens(seen.cache_write)} written"),
                    ("calls", f"{seen.calls:,}")]
        rows += _context_rows(agent, seen)
        await self.send_text(incoming.chat, card("🧮 usage", f"session {agent.session_id[:8] or '-'}",
                                                 rows))

    async def cmd_context(self, incoming: Incoming, agent: engine.ChatAgent, _args: list[str]) -> None:
        await self._agent_prompt(incoming, agent, "/context")

    async def cmd_compact(self, incoming: Incoming, agent: engine.ChatAgent, _args: list[str]) -> None:
        await self._agent_prompt(incoming, agent, "/compact")

    async def _agent_prompt(self, incoming: Incoming, agent: engine.ChatAgent, text: str) -> None:
        """An agent command that must run as its own turn, never steered into another one."""
        # an agent command is not a person speaking: no name in front of it
        payload = Incoming(incoming.chat, incoming.sender, text, incoming.group, incoming.event)
        depth = self.enqueue(incoming.chat, "prompt", payload)
        if agent.busy:
            await self.reply(incoming, queued_text(depth))

    async def cmd_compaction(self, incoming: Incoming, agent: engine.ChatAgent, args: list[str]) -> None:
        arg = (args or [""])[0].strip().lower().rstrip("%")
        if not arg:
            current = omp_config.read_compaction()
            state = "off" if current.get("enabled") is False else "on"
            line = engine.context_line(agent.context["used"], agent.context["size"])
            await self.send_text(incoming.chat, card(
                "🗜️ compaction", f"{state} at {current.get('thresholdPercent', '?')}%",
                [("context", line if agent.context["size"] > 0 else "unknown")],
                "/compaction 10-95 · /compaction off · /compaction on"))
            return
        try:
            if arg in {"off", "on"}:
                new = omp_config.set_compaction(enabled=arg == "on")
            else:
                new = omp_config.set_compaction(thresholdPercent=int(arg))
        except ValueError as exc:
            await self.reply(incoming, f"usage: /compaction [10-95|off|on] ({exc})")
            return
        target = "off" if new.get("enabled") is False else "on"
        await self.send_text(incoming.chat, f"⚙️ compaction {target} at "
                                            f"{new.get('thresholdPercent', '?')}% · new agent processes")

    async def cmd_sessions(self, incoming: Incoming, agent: engine.ChatAgent, _args: list[str]) -> None:
        await agent.ensure()
        data = {"sessions": await agent.list_sessions()}
        rows = (data.get("sessions") or [])[:10]
        if not rows:
            await self.reply(incoming, "no sessions recorded")
            return
        table = "\n".join(f"{str(row.get('sessionId') or '')[:8]}  {str(row.get('updatedAt') or '')[:16]}"
                          for row in rows)
        await self.send_text(incoming.chat, f"*🗂 sessions* · _{agent.cwd}_\n```{table}```\n"
                                            f"_/resume <id> to continue one_")

    async def cmd_resume(self, incoming: Incoming, agent: engine.ChatAgent, args: list[str]) -> None:
        if not args:
            await self.reply(incoming, "usage: /resume <session-id>")
            return
        await agent.ensure()
        data = {"sessions": await agent.list_sessions()}
        match = next((str(r.get("sessionId")) for r in (data.get("sessions") or [])
                      if str(r.get("sessionId", "")).startswith(args[0])), None)
        if match is None:
            await self.reply(incoming, f"no session starting with {args[0]}")
            return
        self.enqueue(incoming.chat, "resume", match)

    async def cmd_continue(self, incoming: Incoming, agent: engine.ChatAgent, _args: list[str]) -> None:
        record = agent.store.get(agent.key)
        if record is None:
            await self.reply(incoming, "nothing to continue, use /new")
            return
        self.enqueue(incoming.chat, "resume", record.session_id)

    async def cmd_fork(self, incoming: Incoming, agent: engine.ChatAgent, _args: list[str]) -> None:
        if await self._busy(incoming, agent, "/fork"):
            return
        try:
            await agent.fork()
        except AgentError as exc:
            await self.reply(incoming, f"fork failed: {exc}")
            return
        await self.send_text(incoming.chat, self.session_card(agent, "🌿 forked",
                                                              "context copied into a new session id"))

    async def cmd_cwd(self, incoming: Incoming, agent: engine.ChatAgent, args: list[str]) -> None:
        if not args:
            await self.reply(incoming, f"cwd {agent.cwd}")
            return
        import os

        target = os.path.expanduser(args[0])
        if not os.path.isdir(target):
            await self.reply(incoming, f"not a directory: {target}")
            return
        if await self._busy(incoming, agent, "/cwd"):
            return
        agent.cwd = target
        await agent.new_session()
        await self.send_text(incoming.chat, self.session_card(
            agent, "📁 cwd changed", "a new session starts in that directory"))

    async def _picker(self, incoming: Incoming, agent: engine.ChatAgent, config_id: str,
                      args: list[str]) -> None:
        await agent.ensure()
        option = (agent.options or {}).get(config_id) or {}
        values = [str(entry.get("value")) for entry in option.get("options") or []]
        current = str(option.get("currentValue") or "?")
        query = " ".join(args).lower()
        if query:
            exact = [value for value in values if value.lower() == query]
            matches = exact or [value for value in values if query in value.lower()]
            if len(matches) == 1:
                self.choices[incoming.chat] = Choice("option", 1, (config_id, matches))
                await self.answer(incoming, self.choices[incoming.chat], "1")
                return
            values = matches
            if not values:
                await self.reply(incoming, f"no {config_id} matches {query!r}")
                return
        shown = values[:30]
        labels = [f"{value}{'  ← current' if value == current else ''}" for value in shown]
        more = "" if len(values) <= 30 else f"\n_{len(values) - 30} more; narrow it: /{config_id} <text>_"
        await self.send_text(incoming.chat, card(f"⚙️ {config_id}", current,
                                                 footer="reply with the number")
                             + "\n\n" + numbered(labels) + more)
        self.choices[incoming.chat] = Choice("option", len(shown), (config_id, shown))

    async def cmd_model(self, incoming: Incoming, agent: engine.ChatAgent, args: list[str]) -> None:
        await self._picker(incoming, agent, "model", args)

    async def cmd_mode(self, incoming: Incoming, agent: engine.ChatAgent, args: list[str]) -> None:
        await self._picker(incoming, agent, "mode", args)

    async def cmd_thinking(self, incoming: Incoming, agent: engine.ChatAgent, args: list[str]) -> None:
        await self._picker(incoming, agent, "thinking", args)

    async def cmd_approve(self, incoming: Incoming, _agent: engine.ChatAgent, args: list[str]) -> None:
        policy = approval_policy(self.config)
        if args and args[0].lower() in {"ask", "allow", "deny"}:
            await self.set_policy(incoming.chat, args[0].lower())
            return
        await self.send_text(incoming.chat, card("🛡️ approval", policy, footer="reply with the number")
                             + "\n\n" + numbered(["ask · a numbered prompt for each tool call",
                                                  "allow · every tool call approved",
                                                  "deny · every tool call rejected"]))
        self.choices[incoming.chat] = Choice("policy", 3)

    async def set_policy(self, chat: str, value: str) -> None:
        previous = approval_policy(self.config)
        if value == previous:
            await self.send_text(chat, f"approval already {value}")
            return
        self.config.data.setdefault("approval", {})["policy"] = value
        save_override("approval", "policy", value, self.config.overrides_path)
        log.info("config change approval.policy %s -> %s", previous, value)
        await self.send_text(chat, f"⚙️ approval {previous} → *{value}* · saved")

    async def cmd_verbose(self, incoming: Incoming, agent: engine.ChatAgent, _args: list[str]) -> None:
        agent.set_display("verbose", not agent.verbose)
        await self.send_text(incoming.chat,
                             f"activity (trace, todo list, notices): {'on' if agent.verbose else 'off'} · this chat")

    async def cmd_reasoning(self, incoming: Incoming, agent: engine.ChatAgent, args: list[str]) -> None:
        wanted = (args[0].lower() if args else "").strip()
        if wanted in {"on", "off"}:
            agent.set_display("reasoning", wanted == "on")
        await self.send_text(incoming.chat, f"reasoning: {'on' if agent.reasoning else 'off'}"
                                            + ("" if wanted else " · /reasoning on|off"))

    async def cmd_queue(self, incoming: Incoming, agent: engine.ChatAgent, args: list[str]) -> None:
        if not args:
            await self.reply(incoming, "usage: /queue <prompt>")
            return
        payload = Incoming(incoming.chat, incoming.sender, " ".join(args), incoming.group,
                           incoming.event, who=incoming.who)
        depth = self.enqueue(incoming.chat, "prompt", payload)
        await self.reply(incoming, queued_text(depth) if agent.busy else "▶️ starting now")

    async def cmd_steer(self, incoming: Incoming, agent: engine.ChatAgent, args: list[str]) -> None:
        if not args:
            await self.reply(incoming, "usage: /steer <text>")
            return
        text = " ".join(args)
        prompt = f"[{incoming.who}] {text}" if incoming.group and incoming.who else text
        outcome, reason = await agent.steer(prompt)
        if outcome == "injected":
            await self.reply(incoming, STEERED)
        elif outcome == "promptRequired":
            self.enqueue(incoming.chat, "prompt",
                         Incoming(incoming.chat, incoming.sender, text, incoming.group,
                                  incoming.event, who=incoming.who))
            await self.reply(incoming, "nothing running; sent as a new prompt")
        else:
            await self.reply(incoming, f"steer was not accepted ({reason or outcome}); "
                                       f"/queue it, or /stop and resend")

    async def cmd_yolo(self, incoming: Incoming, agent: engine.ChatAgent, _args: list[str]) -> None:
        if await self._busy(incoming, agent, "/yolo"):
            return
        target = not agent.yolo
        await agent.respawn(yolo=target)
        await self.send_text(incoming.chat, f"⚙️ yolo {'on' if target else 'off'} · agent respawned")

    async def cmd_kill(self, incoming: Incoming, agent: engine.ChatAgent, _args: list[str]) -> None:
        if agent.client is None:
            await self.reply(incoming, "no process running")
            return
        agent.forget_turn()
        await agent.stop_process()
        agent.client = None
        await self.send_text(incoming.chat, "agent process stopped")

    async def cmd_whoami(self, incoming: Incoming, _agent: engine.ChatAgent, _args: list[str]) -> None:
        await self.reply(incoming, f"you {incoming.sender or '?'} · chat {incoming.chat} · "
                                   f"mode {self.audience.mode(incoming.chat) if incoming.group else '-'}")

    async def cmd_schedule(self, incoming: Incoming, agent: engine.ChatAgent, args: list[str]) -> None:
        action = args[0].lower() if args else "list"
        if action in ("remove", "rm") and len(args) > 1:
            await agent._schedule_remove(args[1])
            return
        if action == "run" and len(args) > 1:
            await agent._schedule_run(args[1])
            return
        items = schedule_lib.load()
        if not items:
            await self.reply(incoming, 'no schedules yet. Just ask, for example: "every morning '
                                       'at 7, summarise the repo".')
            return
        rows = [(name, f"{item.cron or item.calendar} · {item.timezone} → {item.gateway} · "
                       f"{item.last_status or 'never ran'}") for name, item in sorted(items.items())]
        await self.send_text(incoming.chat, card("🗓️ schedules", str(len(items)), rows,
                                                 "/schedule run <name> · /schedule rm <name>"))

    async def cmd_help(self, incoming: Incoming, _agent: engine.ChatAgent, _args: list[str]) -> None:
        names = dict(COMMANDS)
        groups = [
            ("session", ["new", "sessions", "resume", "continue", "fork", "cwd", "kill"]),
            ("agent", ["model", "mode", "thinking", "approve", "verbose", "reasoning", "yolo"]),
            ("running", ["stop", "queue", "steer", "status", "usage", "context", "compact",
                         "compaction", "schedule"]),
            ("info", ["whoami", "help"]),
        ]
        lines: list[str] = []
        for heading, wanted in groups:
            lines.append(f"*{heading}*")
            lines.extend(f"/{name} · _{names[name]}_" for name in wanted if name in names)
            lines.append("")
        lines.append(f"_{FORWARD_HINT}_")
        lines.append("_A list with numbers is answered with the number._")
        await self.send_text(incoming.chat, "\n".join(lines))

    # ------------------------------------------------------------ lifecycle
    async def on_connected(self, client: Any, _event: Any) -> None:
        device = await client.get_me()
        self.me = {part for part in (device.JID.User, device.LID.User) if part}
        log.info("whatsapp connected as %s (allowed: %s, groups: %s, mode: %s)",
                 device.JID.User, sorted(self.allowed), sorted(self.audience.chats) or "none",
                 self.audience.default_mode)
        self._connected.set()
        try:
            groups = await client.get_joined_groups()
        except Exception as exc:
            log.warning("could not list joined groups: %s", exc)
            return
        for group in groups:
            self._note_group(f"{group.JID.User}@{GROUP_SERVER}",
                             group.GroupName.Name or group.JID.User)

    def _note_group(self, chat: str, title: str = "") -> None:
        """Remember a group by id and name, for `gateway update` to offer later. Reading the
        list from the CLI needs the device connection, which the running gateway holds."""
        if (chat, title) in self._noted:
            return
        self._noted.add((chat, title))
        try:
            remember_chat(self.name, chat, title, "group")
        except OSError as exc:
            log.warning("could not record group %s: %s", chat, exc)

    async def on_logged_out(self, _client: Any, _event: Any) -> None:
        log.error("whatsapp logged this device out; relink with `ompwire gateway start %s --relink`",
                  self.name)
        self._stop.set()

    async def run(self) -> None:
        from neonize.aioze.events import ConnectedEv, LoggedOutEv, MessageEv

        if not linked_number(self.session):
            raise LinkMissing(self.name)
        self._stop = asyncio.Event()
        self.client = new_client(self.session)
        self.client.event(ConnectedEv)(self.on_connected)
        self.client.event(MessageEv)(self.on_message)
        self.client.event(LoggedOutEv)(self.on_logged_out)

        async def no_qr(_client: Any, _data: bytes) -> None:
            # a QR here means the stored link is gone; the unit must not sit showing codes
            log.error("whatsapp asked for a new QR: the link is gone, relink with "
                      "`ompwire gateway start %s --relink`", self.name)
            self._stop.set()

        self.client.event.qr(no_qr)
        runner = asyncio.create_task(self.client.connect())
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signal_name, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=90)
        except TimeoutError:
            log.error("whatsapp did not connect within 90s")
            runner.cancel()
            raise
        await self.startup_banner()
        await self.resume_interrupted()
        outbox = asyncio.create_task(self._outbox_loop())
        try:
            await self._stop.wait()
        finally:
            self.stopping = True
            outbox.cancel()
            # each step is bounded: a send stuck on a wedged connection must not hold the unit
            # past systemd's stop timeout, which ends in SIGKILL and an unclean session file
            await _bounded("shutdown notice", self.shutdown_notice(), 10)
            for agent in self.agents.values():
                await _bounded("agent shutdown", agent.shutdown(), 15)
            await _bounded("disconnect", self.client.disconnect(), 10)
            runner.cancel()
            log.info("whatsapp gateway stopped")

    def home_chat(self) -> str:
        return f"{self.home}@{PHONE_SERVER}" if self.home else ""

    async def startup_banner(self) -> None:
        chat = self.home_chat()
        if not chat:
            return
        rows: list[tuple[str, object]] = []
        agent = self.agent_for(chat)
        try:
            await agent.ensure()
        except Exception as exc:
            rows.append(("error", f"agent failed to start: {exc}"))
            log.warning("startup banner agent probe failed: %s", exc)
        rows += [*agent_rows(agent),
                 ("users", ", ".join(sorted(self.allowed)) or "none"),
                 ("groups", f"{len(self.audience.chats)} · mode {self.audience.default_mode}"
                  if self.audience.chats else "none"),
                 ("clock", now_wib())]
        await self.send_text(chat, card("🟢 ompwire", f"{__version__} · whatsapp {self.name}", rows,
                                        "send /help for commands"))

    async def shutdown_notice(self) -> None:
        chat = self.home_chat()
        if not chat:
            return
        rows = [("uptime", fmt_uptime(time.time() - self.started_at)),
                ("turns", sum(agent.turns for agent in self.agents.values())),
                ("tools", sum(agent.tools_used for agent in self.agents.values()))]
        await self.send_text(chat, card("🔴 ompwire stopping", now_wib(), rows,
                                        "the next start resumes the same sessions"))

    async def resume_interrupted(self) -> None:
        enabled = bool(self.config.get("whatsapp", "resume_interrupted", True))
        resumable, exhausted = engine.resume_plan(str(self.config.get("session", "store")), KEY_PREFIX)
        exhausted_keys = {key for key, _prompt, _resumes in exhausted}
        for key, prompt, resumes in [*resumable, *exhausted]:
            chat = key[len(KEY_PREFIX):]
            agent = self.agent_for(chat)
            if not enabled or key in exhausted_keys:
                agent.store.end_turn(key)
                log.warning("interrupted turn dropped chat=%s resumes=%s", chat, resumes)
                await self.send_text(chat, "a turn was interrupted by a gateway restart and was "
                                           "not resumed; send it again if you still need it")
                continue
            log.info("resuming interrupted turn chat=%s attempt=%s", chat, resumes + 1)
            await self.send_text(chat, "🔁 gateway restarted mid-turn, picking it back up")
            payload = Incoming(chat, "", RESUME_PROMPT.format(prompt=prompt), chat.endswith(GROUP_SERVER))
            payload.original = prompt
            payload.resumes = resumes + 1
            self.enqueue(chat, "prompt", payload)

    async def _outbox_loop(self) -> None:
        """Scheduled runs cannot open a second link on this session; they drop their report in
        the gateway's outbox, and the running gateway sends it."""
        from . import wa_outbox

        while True:
            for report in wa_outbox.pending(self.name):
                try:
                    await wa_outbox.send(self, report)
                except Exception as exc:
                    log.warning("outbox report %s failed: %s", report.path.name, exc)
                    wa_outbox.fail(report, str(exc))
            await asyncio.sleep(2)


async def _bounded(step: str, work: Any, seconds: float) -> None:
    try:
        await asyncio.wait_for(work, seconds)
    except TimeoutError:
        log.warning("stopping: %s took longer than %ss, moving on", step, seconds)
    except Exception as exc:
        log.warning("stopping: %s failed: %s", step, exc)


class LinkMissing(RuntimeError):
    def __init__(self, name: str) -> None:
        super().__init__(f"gateway {name!r} is not linked to a WhatsApp number")
        self.name = name


def _current(agent: engine.ChatAgent, config_id: str) -> str:
    return str(((agent.options or {}).get(config_id) or {}).get("currentValue") or "?")


def _session_usage(agent: engine.ChatAgent) -> omp_sessions.SessionUsage | None:
    if not agent.session_id:
        return None
    try:
        return omp_sessions.usage(agent.session_id)
    except OSError:
        return None


def _context_rows(agent: engine.ChatAgent, seen: omp_sessions.SessionUsage | None) -> list[tuple[str, object]]:
    used, size = agent.context["used"], agent.context["size"]
    if not used and seen is not None:
        used = seen.context
    rows: list[tuple[str, object]] = [
        ("context", engine.context_line(used, size) if size > 0
         else (f"{tokens(used)} / limit not reported yet" if used else "unknown"))]
    if seen is not None and seen.compactions:
        last = seen.compactions[-1]
        rows.append(("last", f"{wib_time(last.at)} · {tokens(last.before)} → {tokens(last.after)}"))
    return rows


def serve(config: Config) -> int:
    configure_logging(config)
    transport = Transport(config)
    if not transport.allowed:
        print(f"error: gateway {transport.name!r} has no allowed numbers, nobody could talk to it")
        return 2
    try:
        asyncio.run(transport.run())
    except KeyboardInterrupt:
        pass
    except LinkMissing as exc:
        print(f"FAIL {exc}. Link it: ompwire gateway start {exc.name}")
        return 2
    return 0
