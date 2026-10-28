"""The WhatsApp output side: how one chat (a person or a group) shows a turn.

WhatsApp has no inline buttons for a linked device, so every choice is a numbered list and the
answer is the number typed back. The transport reads that number; this module only writes. A
sent message can be edited, which is how a question or an approval turns into its outcome
instead of leaving a stale list that still looks open.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from . import engine
from .engine import PlanItem, ToolTrace
from .format import TOOL_LINE_WIDTH, _clip, now_wib
from .tg_html import tool_icon, tool_label
from .wa_client import chunks, markdown_messages

log = logging.getLogger("ompwire.whatsapp")

WHATSAPP_LIMIT = 4000  # WhatsApp takes ~65k; past a screen a phone stops being readable

STEERED = "↪️ steered · the agent reads this at its next step"
FOLLOWED_UP = "📥 follow-up queued · it runs right after this turn"
SKIP_WORDS = {"0", "skip", "lewati"}


def held_text(position: int) -> str:
    return f"⏸️ held #{position} · answer the question above first (a number, your own words, or 0 to skip)"


def queued_text(position: int) -> str:
    return f"📥 queued #{position} · runs after the current turn"


def card(title: str, subtitle: str = "", rows: list[tuple[str, object]] | None = None,
         footer: str = "") -> str:
    """A status card in WhatsApp markup: bold title, aligned rows in a code block, footer."""
    head = f"*{title}*" + (f" · _{subtitle}_" if subtitle else "")
    parts = [head]
    pairs = [(str(key), str(value)) for key, value in (rows or []) if value not in (None, "")]
    if pairs:
        width = max(len(key) for key, _ in pairs)
        parts.append("```" + "\n".join(f"{key.ljust(width)}  {value}" for key, value in pairs)
                     + "```")
    if footer:
        parts.append(f"_{footer}_")
    return "\n".join(parts)


def numbered(options: list[str]) -> str:
    return "\n".join(f"*{index}.* {option}" for index, option in enumerate(options, 1))


def tool_text(t: ToolTrace) -> str:
    tool = t.tool or t.kind or "tool"
    head = f"{tool_icon(tool)} *{tool_label(tool)}*"
    if t.status == "in_progress":
        head += " · _running_"
    elif t.status and t.status not in {"completed", "pending"}:
        head += f" · _{t.status}_"
    lines: list[str] = []
    width = TOOL_LINE_WIDTH + 40
    if t.command:
        command = t.command.splitlines()
        lines.append(f"$ {_clip(command[0], lines=1, width=width)}")
        lines.extend(f"  {_clip(line, lines=1, width=width)}" for line in command[1:6])
        if len(command) > 6:
            lines.append("  …")
    elif t.title:
        lines.append(_clip(t.title, lines=1, width=width))
    if t.subagent:
        lines.append(_clip(f"subagent: {t.subagent}", lines=1, width=width))
    if t.brief:
        lines.append(_clip(f"task: {t.brief}", lines=1, width=width))
    lines.extend(_clip(f"  · {step}", lines=1, width=width) for step in t.steps[-4:])
    if t.output.strip():
        lines.append(_clip(t.output))
    if not lines:
        return head
    # a fence keeps the command and its output monospace and stops WhatsApp reading *x* in it
    return head + "\n```" + "\n".join(lines).replace("```", "'''") + "```"


class WhatsAppSurface(engine.Surface):
    """One chat on WhatsApp. `send` is the transport's sender: (chat, text, quoted) -> message id."""

    def __init__(self, transport: Any, chat: str) -> None:
        self.transport = transport
        self.chat = chat
        self.label = chat
        self.limit = WHATSAPP_LIMIT
        self.options_message: str | None = None
        self.options_listed = ""
        self.options_question = ""

    async def post(self, body: str, *, kind: str) -> Any | None:
        # an answer is cut on its markdown, so a code block split across messages stays code
        parts = markdown_messages(body, self.limit) if kind == "text" else chunks(body, self.limit)
        sent = None
        for part in parts:
            sent = await self.transport.send_text(self.chat, part)
        return sent

    def trace(self, t: ToolTrace) -> str:
        return tool_text(t)

    def plan(self, items: list[PlanItem]) -> str:
        marks = {"completed": "☑️", "in_progress": "▶️"}
        done = sum(1 for item in items if item.status == "completed")
        lines = [f"📋 *Todo* · _{done}/{len(items)} done_"]
        for item in items:
            text = item.content
            if item.status == "completed":
                text = f"~{text}~"
            elif item.status == "in_progress":
                text = f"*{text}*"
            lines.append(f"{marks.get(item.status, '⬜')} {text}")
        return "\n".join(lines)

    def reasoning(self, text: str) -> str:
        return f"🧠 *reasoning*\n```{text}```"

    def advisor(self, notes: list[dict[str, str]]) -> str:
        body = "\n\n".join(f"{engine.severity_mark(note['severity'])} {note['severity']}\n"
                           f"{note['note']}" for note in notes)
        return f"⚖️ *advisor*\n```{body}```"

    def dropped(self, count: int) -> str:
        return f"_… {count} tool call(s) not shown_"

    def join(self, blocks: list[str]) -> str:
        return "\n\n".join(blocks)

    def job(self, card: engine.JobCard) -> str:
        details = engine.job_details(card)
        width = max((len(label) for label, _ in details), default=0)
        body = "\n".join(f"{label.ljust(width)}  {value}" for label, value in details)
        return f"🤖 *subagent* - {card.name} · _{engine.job_line(card)}_\n```{body}```"

    def jobs(self, group: list[engine.JobCard], spawned: int) -> str:
        word = "agent" if spawned == 1 else "agents"
        background = "background " if any(card.detached for card in group) else ""
        lines: list[str] = []
        for card in group:
            lines.append(engine.job_row(card))
            if card.task:
                lines.append(f"  {card.task}")
        totals = engine.group_totals(group)
        if totals:
            lines += ["", totals]
        return f"🤖 *subagent* (spawned {spawned} {background}{word})\n```" + \
            "\n".join(lines) + "```"

    @contextlib.asynccontextmanager
    async def typing(self):
        """WhatsApp drops the "typing…" state after ~25s; refresh it while the turn runs."""
        stop = asyncio.Event()

        async def loop() -> None:
            while not stop.is_set():
                await self.transport.presence(self.chat, typing=True)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=10.0)
                except TimeoutError:
                    continue

        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            await self.transport.presence(self.chat, typing=False)

    async def offer_options(self, options: list[str], question: str = "") -> None:
        """The question (omp's ask tool sends one) and its numbered answers."""
        listed = numbered(options)
        self.options_listed = listed
        self.options_question = question.strip()
        title = "❓ " + (self.options_question or "pick an answer")
        body = (card(title, "reply with a number, your own words, or 0 to skip")
                + f"\n\n{listed}\n\n_messages sent before you answer wait for it_")
        self.options_message = await self.transport.send_text(self.chat, body)
        self.transport.open_question(self.chat, len(options))

    async def retire_options(self, note: str) -> None:
        message, self.options_message = self.options_message, None
        if message is None:
            return
        await self.transport.edit_text(self.chat, message,
                                       card(f"❓ {self.options_question or 'question'}", note)
                                       + f"\n\n{self.options_listed}")

    async def ask_permission(self, token: str, kind: str, detail: str, labels: list[str]) -> Any:
        body = card("🛡️ approval needed", kind, footer="reply with the number · the run waits")
        if detail:
            body += "\n```" + detail[:1200].replace("```", "'''") + "```"
        body += "\n\n" + numbered(labels)
        message = await self.transport.send_text(self.chat, body)
        self.transport.open_approval(self.chat, token, len(labels))
        return message

    async def settle_permission(self, handle: Any, kind: str, detail: str, outcome: str,
                                expired: bool) -> None:
        self.transport.close_approval(self.chat)
        if expired:
            body = card("🛡️ approval", f"expired · {now_wib()}", [("tool", kind)],
                        "no answer in time, the tool call was denied")
        else:
            body = card("🛡️ approval", f"{outcome} · {now_wib()}", [("tool", kind)],
                        "decision recorded")
        if detail:
            body += "\n```" + detail[:800].replace("```", "'''") + "```"
        if handle is not None:
            await self.transport.edit_text(self.chat, handle, body)

    async def send_file(self, path, as_photo: bool) -> None:
        await self.transport.send_file(self.chat, path, as_photo)

    async def offer_schedule(self, name: str, expression: str, gateway: str, prompt: str) -> None:
        body = card("🗓️ schedule", expression, [("delivers to", gateway), ("prompt", prompt[:200])],
                    "nothing runs until you confirm")
        body += "\n\n*1.* Create schedule\n*2.* Cancel"
        await self.transport.send_text(self.chat, body)
        self.transport.open_schedule(self.chat, name)

    async def notice(self, text: str) -> Any | None:
        return await self.post(text, kind="text")
