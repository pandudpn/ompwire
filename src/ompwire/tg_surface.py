"""The Telegram output side: how one chat shows a turn, asks a question and gets a file.

`TelegramSurface` is what the engine posts through for one chat; `TelegramDelivery` sends a
scheduled run's report while no poller is running. Neither routes messages: that is
`telegram.py`, which owns polling, commands and buttons.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application

from . import engine
from .attachments import Outgoing
from .delivery import Delivery
from .engine import FloodWait, ToolTrace
from .format import now_wib
from .mdrender import strip_html, to_html, to_plain
from .tg_html import card, esc, run_card, run_status, tool_block

log = logging.getLogger("ompwire.telegram")


TELEGRAM_LIMIT = 4000  # leave headroom under the 4096 hard cap


def chunk_lines(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Cut a rendered message on line boundaries; one overlong line is cut as a last resort."""
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                parts.append(current)
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return parts or [""]


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


def _retry_window(exc: Exception) -> float:
    """PTB gives seconds today and a timedelta from v23; both mean the same wait."""
    value = getattr(exc, "retry_after", 5)
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    return float(value or 5)


STEERED = "↪️ steered · the agent reads this at its next step in the running turn"


def held_text(position: int) -> str:
    return f"⏸️ held #{position} · answer the question above first, or tap Skip"


def queued_text(position: int) -> str:
    return f"📥 queued #{position} · runs after the current turn"


class TelegramSurface(engine.Surface):
    """The engine's output side for one Telegram chat: posting, cards, buttons, files."""

    def __init__(self, app: Application, chat_id: int) -> None:
        self.app = app
        self.chat_id = chat_id
        self.label = str(chat_id)
        self.limit = TELEGRAM_LIMIT
        # the card of the question still open, so its buttons can be taken away
        self.options_message: Any = None
        self.options_listed = ""

    async def post(self, body: str, *, kind: str):
        try:
            if kind == "text":
                return await send_md(self.app.bot, self.chat_id, body)
            return await send_html(self.app.bot, self.chat_id, body)
        except RetryAfter as exc:
            raise FloodWait(_retry_window(exc)) from exc

    def trace(self, t: ToolTrace) -> str:
        return tool_block(t.kind, t.title, t.command, t.output, t.status, t.subagent,
                          t.steps, t.brief)

    def reasoning(self, text: str) -> str:
        # prose, so not a code block: monospace reads badly on a phone and a <pre> cannot
        # collapse. An expandable quote stays one line until tapped.
        return f"🧠 <i>reasoning</i>\n<blockquote expandable>{esc(text)}</blockquote>"

    def dropped(self, count: int) -> str:
        return f"<i>… {count} tool call(s) not shown (Telegram flood control)</i>"

    def join(self, blocks: list[str]) -> str:
        return "\n\n".join(blocks)

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

    async def offer_options(self, options: list[str]) -> None:
        """Numbered buttons plus a free-text escape hatch.

        Telegram cuts an inline button's label to one line on the phone, so a sentence-long
        option arrived as "Teks per segmen + 1 pesan…". The full text goes in the message body,
        numbered, and each button carries its number so the choice is never ambiguous.
        """
        rows = [[InlineKeyboardButton(f"{index + 1}. {option}"[:64], callback_data=f"qo:{index}")]
                for index, option in enumerate(options)]
        rows.append([InlineKeyboardButton("✏️ Other answer", callback_data="qo:other"),
                     InlineKeyboardButton("⏭️ Skip", callback_data="qo:skip")])
        listed = "\n".join(f"<b>{index + 1}.</b> {esc(option)}"
                           for index, option in enumerate(options))
        self.options_listed = listed
        self.options_message = await self.app.bot.send_message(
            self.chat_id,
            card("❓ pick an answer", "tap one, Other to type it, or Skip", [],
                 "messages sent before you answer wait for it") + f"\n\n{listed}",
            reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
        )

    async def retire_options(self, note: str) -> None:
        """Drop the buttons of the open question, keeping the listed options readable."""
        message, self.options_message = self.options_message, None
        if message is None:
            return
        try:
            await message.edit_text(card("❓ question", note, []) + f"\n\n{self.options_listed}",
                                    parse_mode=ParseMode.HTML)
        except (TelegramError, AttributeError) as exc:
            log.debug("could not retire the options card: %s", exc)

    async def ask_permission(self, token: str, kind: str, detail: str,
                             labels: list[str]):
        rows = [[InlineKeyboardButton(label, callback_data=f"{token}:{index}")]
                for index, label in enumerate(labels)]
        head = card("🛡️ approval needed", kind, [],
                    "pick one · the run is paused until you answer")
        body = head + (f"\n<pre>{esc(detail[:1200])}</pre>" if detail else "")
        return await self.app.bot.send_message(self.chat_id, body,
                                               reply_markup=InlineKeyboardMarkup(rows),
                                               parse_mode=ParseMode.HTML)

    async def settle_permission(self, handle, kind: str, detail: str, outcome: str,
                                expired: bool) -> None:
        if expired:
            body = card("🛡️ approval", f"expired · {now_wib()}", [("tool", kind)],
                        "no answer in time, the tool call was denied")
        else:
            body = card("🛡️ approval", f"{outcome} · {now_wib()}", [("tool", kind)],
                        "decision recorded")
        if detail:
            body += f"\n<pre>{esc(detail[:800])}</pre>"
        try:
            await handle.edit_text(body, parse_mode=ParseMode.HTML)
        except BadRequest as exc:
            if expired:
                pass
            else:
                log.debug("approval edit failed: %s", exc)

    async def send_file(self, path, as_photo: bool) -> None:
        with path.open("rb") as handle:
            if as_photo:
                await self.app.bot.send_photo(self.chat_id, photo=handle)
            else:
                await self.app.bot.send_document(self.chat_id, document=handle)

    async def offer_schedule(self, name: str, expression: str, gateway: str,
                             prompt: str) -> None:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Create schedule", callback_data=f"sched:new:{name}"),
            InlineKeyboardButton("Cancel", callback_data=f"sched:cancel:{name}"),
        ]])
        body = card("schedule", expression, [
            ("delivers to", gateway),
            ("prompt", prompt[:200]),
        ], "nothing runs until you press Create")
        await self.app.bot.send_message(self.chat_id, body, parse_mode=ParseMode.HTML,
                                        reply_markup=keyboard)

    async def notice(self, text: str):
        return await send_md(self.app.bot, self.chat_id, text)


class TelegramDelivery(Delivery):
    """A scheduled run's report, sent straight to the Bot API with this gateway's token.

    It does not go through `TelegramSurface`: a timer runs with no poller and no chat object
    around, so all a finished run needs is the one call that reaches the person.
    """

    def __init__(self, token: str) -> None:
        self.token = token

    async def deliver(self, target: str, *, title: str, when: str, ok: bool, word: str,
                      rows: Iterable[tuple[str, str]], body_markdown: str, error: str,
                      footer: str, files: list[Outgoing], rejected: list[str]) -> int:
        bot = Bot(self.token)
        chat_id = int(target)
        # a failure carries the reason, not a half answer: the card is read hours later
        body_html = f"<pre>{esc(error[:400])}</pre>" if error else to_html(body_markdown)
        run = run_card(title, when, run_status(ok, word), rows, body_html, footer)
        sent = 0
        for chunk in chunk_lines(run):
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)
            sent += 1
        for outgoing in files:
            with outgoing.path.open("rb") as handle:
                if outgoing.as_photo:
                    await bot.send_photo(chat_id=chat_id, photo=handle)
                else:
                    await bot.send_document(chat_id=chat_id, document=handle)
            sent += 1
        if rejected:
            await bot.send_message(chat_id=chat_id, text="skipped: " + " · ".join(rejected[:4]))
        return sent


async def app_send(app: Application, chat_id: int, text: str) -> None:
    await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
