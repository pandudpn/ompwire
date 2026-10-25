"""Who a gateway answers, and where: one rule for every platform.

A transport reads the platform's message into the four facts this module needs (which chat,
what kind of chat, who sent it, and whether it was addressed to the bot) and asks `admit`.
The rule is the same on Telegram and WhatsApp, so it lives here once:

- a private chat: the sender must be on the allow list;
- a group: the group must be listed, the sender must be on the allow list, and the group's mode
  decides: `mention` (an @mention of the bot, a reply to it, or a command meant for it), `all`
  (every message), `read` (nothing is answered);
- a channel: the channel must be listed and its mode must not be `read`. A channel post has no
  person behind it, only the channel; listing a channel is what trusts whoever can post in it.

Anyone else gets nothing back. A refusal would tell a stranger a bot answers here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .gateways import GROUP_MODES, chat_id, user_id

PRIVATE, GROUP, CHANNEL = "private", "group", "channel"


@dataclass
class Audience:
    allowed: set[str] = field(default_factory=set)
    chats: set[str] = field(default_factory=set)
    default_mode: str = "mention"
    modes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Any) -> Audience:
        mode = str(config.get("gateway", "group_mode", "mention") or "mention").lower()
        modes = config.get("gateway", "group_modes") or {}
        return cls(
            allowed={user for user in map(user_id, config.get("gateway", "allowed_users") or [])
                     if user},
            chats={chat for chat in map(chat_id, config.get("gateway", "groups") or []) if chat},
            default_mode=mode if mode in GROUP_MODES else "mention",
            modes={chat_id(chat): str(value).lower() for chat, value in dict(modes).items()
                   if str(value).lower() in GROUP_MODES},
        )

    def mode(self, chat: str) -> str:
        return self.modes.get(chat_id(chat), self.default_mode)

    def admit(self, *, chat: str, kind: str, sender: str, addressed: bool) -> str:
        """"" to take the message, otherwise why it is dropped (for the log, never the chat)."""
        if kind == PRIVATE:
            return "" if sender in self.allowed else "sender not allowed"
        if chat_id(chat) not in self.chats:
            return f"{kind} not listed"
        mode = self.mode(chat)
        if mode == "read":
            return f"{kind} mode is read"
        if kind == CHANNEL:
            return ""
        if sender not in self.allowed:
            return "sender not allowed"
        if mode == "mention" and not addressed:
            return "not addressed"
        return ""
