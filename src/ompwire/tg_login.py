"""Provider login from the chat: `/login`, its pickers and the `omp login` process it drives.

A mixin of `telegram.Transport`: it uses the transport's `app`, `config`, `agent_for`,
`_authorized` and `_deny`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from .catalog import credential_snapshot, list_providers
from .format import now_wib
from .login import LoginSession
from .tg_html import card, esc, paginate, two_columns

log = logging.getLogger("ompwire.telegram")


class LoginMixin:
    LOGIN_PAGE = 10

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

        def code_prompt() -> None:
            asyncio.run_coroutine_threadsafe(
                self.app.bot.send_message(
                    chat_id, "🔑 omp is waiting for the key/code — paste it here."),
                loop)

        session = LoginSession(
            target, emit, done,
            command=str(self.config.get("acp", "command", "omp")),
            menu_index=None if target else provider.index,
        )
        session.on_code_prompt = code_prompt
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

    def close_logins(self) -> None:
        for chat_id in list(self.logins):
            self.close_login(chat_id)
