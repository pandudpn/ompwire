"""Telegram groups and channels: who is answered where, what the agent reads, and how loud.

The gate reads four facts from a Telegram update (chat, chat type, sender, whether the bot was
addressed); these updates carry exactly the fields python-telegram-bot fills in for them.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from ompwire import telegram
from ompwire.audience import Audience
from ompwire.config import load_config

OWNER = 749994884
BOT_ID = 555
GROUP = -1001111
CHANNEL = -1002222


def entity(kind: str, offset: int, length: int, user=None):
    return types.SimpleNamespace(type=kind, offset=offset, length=length, user=user)


def update(text: str, *, chat: int = GROUP, kind: str = "supergroup", sender: int | None = OWNER,
           entities=(), reply_to_bot: bool = False):
    reply = (types.SimpleNamespace(from_user=types.SimpleNamespace(id=BOT_ID))
             if reply_to_bot else None)
    message = types.SimpleNamespace(text=text, caption=None, entities=list(entities),
                                    caption_entities=[], reply_to_message=reply)
    return types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=chat, type=kind),
        effective_user=types.SimpleNamespace(id=sender) if sender is not None else None,
        effective_message=message, callback_query=None)


@pytest.fixture
def gate():
    def build(mode: str = "mention", chats=(GROUP, CHANNEL), modes=None):
        item = telegram.Transport.__new__(telegram.Transport)
        item.audience = Audience(allowed={str(OWNER)}, chats={str(c) for c in chats},
                                 default_mode=mode, modes={str(k): v for k, v in (modes or {}).items()})
        item.username, item.bot_id = "vidura_bot", BOT_ID
        return item
    return build


def test_a_private_chat_from_the_owner_is_taken(gate):
    assert gate()._authorized(update("halo", chat=OWNER, kind="private"))


def test_a_stranger_in_a_private_chat_is_refused(gate):
    assert gate()._refusal(update("halo", chat=99, kind="private", sender=99)) == "sender not allowed"


def test_a_group_message_without_a_mention_is_ignored(gate):
    assert gate()._refusal(update("ngobrol biasa")) == "not addressed"


def test_an_at_mention_of_this_bot_addresses_it(gate):
    text = "@vidura_bot cek server"
    assert gate()._authorized(update(text, entities=[entity("mention", 0, 11)]))


def test_a_mention_of_another_bot_does_not(gate):
    text = "@other_bot cek"
    assert gate()._refusal(update(text, entities=[entity("mention", 0, 10)])) == "not addressed"


def test_a_reply_to_the_bot_addresses_it(gate):
    assert gate()._authorized(update("salah", reply_to_bot=True))


def test_only_a_command_written_for_this_bot_counts(gate):
    """A bare /status in a group reaches every bot in it; /status@this_bot is ours."""
    mine = update("/status@vidura_bot", entities=[entity("bot_command", 0, 18)])
    bare = update("/status", entities=[entity("bot_command", 0, 7)])
    assert gate()._authorized(mine)
    assert gate()._refusal(bare) == "not addressed"


def test_a_group_that_is_not_listed_is_ignored_even_when_addressed(gate):
    message = update("x", chat=-1009999, reply_to_bot=True)
    assert gate()._refusal(message) == "group not listed"


def test_a_member_who_is_not_allowed_cannot_use_the_bot_in_a_listed_group(gate):
    assert gate()._refusal(update("x", sender=42, reply_to_bot=True)) == "sender not allowed"


@pytest.mark.parametrize(("mode", "taken"), [("all", True), ("read", False)])
def test_all_answers_every_message_and_read_answers_none(gate, mode, taken):
    assert gate(mode)._authorized(update("tanpa mention")) is taken


def test_a_group_can_have_its_own_mode(gate):
    item = gate("mention", modes={GROUP: "all"})
    assert item._authorized(update("tanpa mention"))


def test_a_listed_channel_post_has_no_sender_and_is_taken_in_all_mode(gate):
    post = update("posting baru", chat=CHANNEL, kind="channel", sender=None)
    assert gate("all")._authorized(post)
    assert gate("read")._refusal(post) == "channel mode is read"


def test_an_unlisted_channel_is_ignored(gate):
    post = update("posting", chat=-1003333, kind="channel", sender=None)
    assert gate("all")._refusal(post) == "channel not listed"


def test_a_refusal_says_nothing_in_the_chat(gate):
    """It used to reply "not authorized. your user id: ..." in front of the whole group."""
    replies: list[str] = []
    message = update("halo", chat=99, kind="private", sender=99)

    async def reply_text(text, **_kw):
        replies.append(text)

    message.effective_message.reply_text = reply_text
    asyncio.run(gate()._deny(message))
    assert replies == []


def test_a_group_prompt_names_the_sender_and_drops_the_bots_mention(gate):
    item = gate()
    message = types.SimpleNamespace(from_user=types.SimpleNamespace(full_name="Pandu", username="p"),
                                    sender_chat=None, chat=None)
    payload = telegram._TextOnly("@vidura_bot cek server", message)
    assert item._framed(payload, message).text == "[Pandu] cek server"


def test_a_channel_post_is_framed_with_the_channel_title(gate):
    item = gate()
    message = types.SimpleNamespace(from_user=None, sender_chat=types.SimpleNamespace(title="Rilis"),
                                    chat=None)
    assert item._framed(telegram._TextOnly("v1.2 keluar", message), message).text == "[Rilis] v1.2 keluar"


def test_groups_are_quiet_until_they_ask(tmp_path):
    """The tool trace and the reasoning stay out of a chat other people read."""
    config = load_config(env={})
    config.data["session"]["store"] = str(tmp_path / "s.db")
    config.data.setdefault("stream", {}).update({"show_tool_calls": True, "show_reasoning": True})

    async def go():
        app = types.SimpleNamespace(bot=None)
        private = telegram.chat_agent(config, OWNER, f"tg:{OWNER}", app)
        group = telegram.chat_agent(config, GROUP, f"tg:{GROUP}", app, shared=True)
        group.set_display("verbose", True)
        again = telegram.chat_agent(config, GROUP, f"tg:{GROUP}", app, shared=True)
        return (private.verbose, private.reasoning), (group.reasoning,), (again.verbose,)

    assert asyncio.run(go()) == ((True, True), (False,), (True,))


def test_the_bot_sends_every_message_without_a_link_preview(monkeypatch):
    """A URL in an answer became a preview card, which read as a file the bot attached
    (api.github.com/meta showed up as meta.json under an options card)."""
    built = {}

    class Builder:
        def token(self, value):
            return self

        def defaults(self, value):
            built["defaults"] = value
            return self

        def build(self):
            raise SystemExit  # stop before serving; the defaults are what this test reads

    monkeypatch.setattr(telegram.Application, "builder", staticmethod(lambda: Builder()))
    item = telegram.Transport.__new__(telegram.Transport)
    item.token = "1:x"
    with pytest.raises(SystemExit):
        asyncio.run(item.run())
    assert built["defaults"].link_preview_options.is_disabled is True
