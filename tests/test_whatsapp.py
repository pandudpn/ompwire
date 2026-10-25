"""WhatsApp: who may talk, when the bot answers in a group, numbered answers, and markup.

neonize talks to WhatsApp through a Go library, so these tests build the transport with fake
events shaped like neonize's (the fields the transport reads) and record what it would send.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from ompwire import whatsapp
from ompwire.config import load_config
from ompwire.wa_client import chunks, strip_mention, to_whatsapp

ME_PHONE = "6283875181609"
ME_LID = "132220769575140"
OWNER = "6282122274139"
GROUP = "120363412948575786@g.us"


def jid(user: str, server: str):
    return types.SimpleNamespace(User=user, Server=server)


class Context:
    def __init__(self, mentions=(), participant="", stanza=""):
        self.mentionedJID = list(mentions)
        self.participant = participant
        self.stanzaID = stanza


class Message:
    """The few neonize Message fields the transport reads, for a text message."""

    def __init__(self, text: str, context: Context | None = None) -> None:
        self.conversation = "" if context is not None else text
        self.extendedTextMessage = types.SimpleNamespace(text=text,
                                                         contextInfo=context or Context())
        self._context = context

    def HasField(self, name: str) -> bool:
        return name == "extendedTextMessage" and self._context is not None

    def ListFields(self):
        return []


def event(text: str, *, group: bool = False, phone: str = OWNER, context: Context | None = None,
          chat: str = GROUP, name: str = "Pandu"):
    """A message as neonize delivers it today: the sender as a LID, the phone in SenderAlt."""
    source = types.SimpleNamespace(
        IsFromMe=False, IsGroup=group,
        Sender=jid("948330", "lid"), SenderAlt=jid(phone, "s.whatsapp.net"),
        Chat=jid(chat.split("@")[0], "g.us") if group else jid("948330", "lid"))
    return types.SimpleNamespace(Info=types.SimpleNamespace(MessageSource=source, ID="M1",
                                                            Pushname=name),
                                 Message=Message(text, context))


@pytest.fixture
def transport():
    """A transport that records what reaches routing instead of starting turns."""
    def build(mode: str = "mention", groups: tuple[str, ...] = (GROUP,)):
        config = load_config(env={})
        config.data["gateway"] = {"name": "wa", "allowed_users": [OWNER], "groups": list(groups),
                                  "group_mode": mode}
        item = whatsapp.Transport(config)
        item.me = {ME_PHONE, ME_LID}

        async def mark_read(_event):
            return None

        item.mark_read = mark_read  # the real one imports neonize, which leaves child processes
        item.handled = []

        async def handle(incoming):
            item.handled.append(incoming)

        item.handle = handle
        return item
    return build


def deliver(item, *events):
    async def go():
        for each in events:
            await item.on_message(None, each)
    asyncio.run(go())
    return item.handled


def test_a_direct_message_from_an_allowed_number_is_taken(transport):
    handled = deliver(transport(), event("halo"))
    assert [(i.chat, i.sender, i.prompt) for i in handled] == [
        (f"{OWNER}@s.whatsapp.net", OWNER, "halo")]


def test_a_stranger_gets_nothing(transport):
    """No "not authorized" reply either: that would confirm a bot answers this number."""
    assert deliver(transport(), event("halo", phone="6280000000")) == []


def test_a_group_message_without_a_mention_is_ignored_in_mention_mode(transport):
    assert deliver(transport(), event("ngobrol biasa", group=True)) == []


def test_a_mention_of_the_bot_is_answered_and_the_mention_removed(transport):
    handled = deliver(transport(), event(f"@{ME_LID} cek server", group=True,
                                         context=Context(mentions=[f"{ME_LID}@lid"])))
    assert [i.prompt for i in handled] == ["[Pandu] cek server"]


def test_a_reply_to_the_bot_counts_as_addressing_it(transport):
    handled = deliver(transport(), event("salah", group=True,
                                         context=Context(participant=f"{ME_LID}@lid", stanza="X")))
    assert [i.text for i in handled] == ["salah"]


def test_a_group_that_is_not_listed_is_ignored_even_when_mentioned(transport):
    message = event(f"@{ME_LID} x", group=True, chat="999@g.us",
                    context=Context(mentions=[f"{ME_LID}@lid"]))
    assert deliver(transport(), message) == []


def test_an_unlisted_member_cannot_use_the_bot_in_a_listed_group(transport):
    message = event(f"@{ME_LID} x", group=True, phone="6281111111",
                    context=Context(mentions=[f"{ME_LID}@lid"]))
    assert deliver(transport(), message) == []


@pytest.mark.parametrize(("mode", "taken"), [("all", 1), ("read", 0)])
def test_group_mode_all_answers_everything_and_read_answers_nothing(transport, mode, taken):
    plain = event("tanpa mention", group=True)
    mentioned = event(f"@{ME_LID} pakai mention", group=True,
                      context=Context(mentions=[f"{ME_LID}@lid"]))
    assert len(deliver(transport(mode), plain, mentioned)) == taken * 2


def test_a_number_answers_the_open_question_and_routes_the_choice(transport):
    item = transport()
    chat = f"{OWNER}@s.whatsapp.net"
    routed: list[str] = []
    retired: list[str] = []

    async def go():
        agent = item.agent_for(chat)  # a ChatAgent binds to the running loop
        agent.pending_options = ["staging", "production"]
        item.open_question(chat, 2)

        async def route(_chat, _agent, incoming):
            routed.append(incoming.text)

        async def retire(note):
            retired.append(note)

        item._route = route
        agent.surface.retire_options = retire
        await whatsapp.Transport.handle(item, whatsapp.Incoming(chat, OWNER, "2", False))
        return agent

    agent = asyncio.run(go())
    assert routed == ["production"]
    assert retired == ["answered: production"]
    assert chat not in item.choices and agent.pending_options == []


def test_a_number_out_of_range_is_not_an_approval(transport):
    """Only 1..n answers an approval; anything else is an ordinary message."""
    item = transport()
    chat = f"{OWNER}@s.whatsapp.net"
    answered: list[int] = []

    async def go():
        agent = item.agent_for(chat)

        async def answer_permission(_token, index):
            answered.append(index)
            return "ok"

        agent.answer_permission = answer_permission
        item.open_approval(chat, "perm-x", 3)
        choice = item.choices[chat]
        return (await item.answer(whatsapp.Incoming(chat, OWNER, "7", False), choice, "7"),
                await item.answer(whatsapp.Incoming(chat, OWNER, "2", False), choice, "2"))

    assert asyncio.run(go()) == (False, True)
    assert answered == [1]


def test_markdown_becomes_whatsapp_markup_and_code_is_left_alone():
    text = "## Hasil\n**penting** dan *miring* ~~coret~~ [docs](https://x.io)\n- satu\n`**x**`"
    assert to_whatsapp(text) == ("*Hasil*\n*penting* dan _miring_ ~coret~ docs (https://x.io)\n"
                                 "• satu\n`**x**`")


def test_a_heading_that_is_already_bold_stays_one_bold():
    assert to_whatsapp("### **sudah bold**") == "*sudah bold*"


def test_a_fence_keeps_its_body_and_loses_the_language_tag():
    assert to_whatsapp("```bash\nls **x**\n```") == "```ls **x**\n```"


def test_a_long_message_is_cut_on_lines():
    parts = chunks("\n".join(f"line {i}" for i in range(400)), 500)
    assert all(len(part) <= 500 for part in parts)
    assert "\n".join(parts).split("\n") == [f"line {i}" for i in range(400)]


def test_the_bots_own_mention_is_removed_and_others_are_kept():
    assert strip_mention(f"@{ME_LID} tanya @6281234 dong", {ME_LID}) == "tanya @6281234 dong"


def test_each_client_delivers_its_events_to_its_own_loop(tmp_path, monkeypatch):
    """neonize keeps one module-global loop and set it only once, so the group list read after
    linking (a second asyncio.run in the same `gateway start`) got 'Event loop is closed'."""
    import sys

    from ompwire import wa_client

    events = types.SimpleNamespace(event_global_loop=None)

    def set_once(loop):  # neonize's own setter: the first loop wins forever
        if events.event_global_loop is None:
            events.event_global_loop = loop

    fake = types.ModuleType("neonize")
    fake_aioze = types.ModuleType("neonize.aioze")
    fake_client = types.ModuleType("neonize.aioze.client")
    fake_client.NewAClient = lambda path: types.SimpleNamespace(path=path)
    fake_aioze.events = events
    fake_aioze.client = fake_client
    monkeypatch.setitem(sys.modules, "neonize", fake)
    monkeypatch.setitem(sys.modules, "neonize.aioze", fake_aioze)
    monkeypatch.setitem(sys.modules, "neonize.aioze.client", fake_client)
    monkeypatch.setitem(sys.modules, "neonize.aioze.events", events)

    async def one_run():
        wa_client.new_client(tmp_path / "s.db")
        set_once(asyncio.get_running_loop())  # what connect() does after the client exists
        return asyncio.get_running_loop()

    asyncio.run(one_run())
    second = asyncio.run(one_run())
    assert events.event_global_loop is second
    assert not second.is_closed() or events.event_global_loop is second
