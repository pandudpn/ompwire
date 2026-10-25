"""Browse and read the agent's skills: `/skills` and its buttons.

A mixin of `telegram.Transport`: it uses the transport's `app`, `config`, `agent_for`,
`_authorized` and `_deny`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from .catalog import list_skills, skill_body
from .mdrender import split_blocks
from .tg_html import card, esc, paginate, two_columns

log = logging.getLogger("ompwire.telegram")


class SkillsMixin:
    SKILL_PAGE = 10   # two columns x five rows, same shape as the model picker

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
        offset = index * self.SKILL_PAGE
        buttons = [InlineKeyboardButton(skill.name[:28], callback_data=f"skv:{offset + position}")
                   for position, skill in enumerate(window)]
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
