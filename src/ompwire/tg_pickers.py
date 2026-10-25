"""The model, mode and thinking pickers: one keyboard per config option the agent offers.

A mixin of `telegram.Transport`: it uses the transport's `app`, `config`, `agent_for`,
`_authorized` and `_deny`.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from . import engine
from .engine import PREF_KEYS

log = logging.getLogger("ompwire.telegram")


class PickerMixin:
    # -------------------------------------------------- config pickers (UI)
    PAGE_SIZE = 8          # single-column configs (mode, thinking)

    PAGE_SIZE_TWO_COL = 10  # two columns x five rows

    def _values(self, agent: engine.ChatAgent, config_id: str) -> list[str]:
        option = (getattr(agent, "options", {}) or {}).get(config_id) or {}
        return [str(o.get("value")) for o in (option.get("options") or [])]

    def _current(self, agent: engine.ChatAgent, config_id: str) -> str:
        option = (getattr(agent, "options", {}) or {}).get(config_id) or {}
        return str(option.get("currentValue") or "?")

    def _providers(self, agent: engine.ChatAgent, config_id: str) -> list[tuple[str, list[int]]]:
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
        agent: engine.ChatAgent,
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

    async def _apply_pick(self, agent: engine.ChatAgent, config_id: str, value: str) -> tuple[str, str]:
        """Set a config option; returns (previous, current) so the caller can render once."""
        previous = self._current(agent, config_id)
        await agent.set_option(config_id, value)
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
