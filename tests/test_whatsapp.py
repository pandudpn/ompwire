"""WhatsApp: who may talk, when the bot answers in a group, numbered answers, and markup.

neonize talks to WhatsApp through a Go library, so these tests build the transport with fake
events shaped like neonize's (the fields the transport reads) and record what it would send.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from ompwire import wa_surface, whatsapp
from ompwire.config import load_config
from ompwire.rpc import AgentError
from ompwire.wa_client import chunks, markdown_messages, strip_mention, to_whatsapp

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

def test_a_fence_under_a_bullet_or_with_tildes_is_still_one_code_block():
    assert to_whatsapp("- step\n  ```bash\n  ls\n  ```\n- two") == "• step\n```ls\n```\n• two"
    assert to_whatsapp("~~~\nhello\n~~~") == "```hello\n```"
    nested = to_whatsapp("````md\n```bash\nls **x**\n```\n````")
    assert "ls **x**" in nested  # the inner fence is code: nothing in it is restyled



def test_a_long_message_is_cut_on_lines():
    parts = chunks("\n".join(f"line {i}" for i in range(400)), 500)
    assert all(len(part) <= 500 for part in parts)
    assert "\n".join(parts).split("\n") == [f"line {i}" for i in range(400)]

def test_a_long_code_block_is_closed_and_reopened_across_messages():
    code = "\n".join(f"line {i}" for i in range(400))
    parts = markdown_messages("Intro.\n\n```bash\n" + code + "\n```\n\nOutro.", 500)
    assert len(parts) > 2 and all(len(part) <= 500 for part in parts)
    for part in parts:
        assert part.count("```") % 2 == 0, part[-40:]  # every message closes what it opens
    joined = "\n".join(parts).replace("```", "")
    assert [line for line in joined.split("\n") if line.startswith("line")] == \
        [f"line {i}" for i in range(400)]


def test_a_long_nested_fence_stays_one_code_block_per_message():
    """Inside ```` the ``` lines are content; reading converted markup mistook them for the end."""
    inner = "\n".join(f"line {i}" for i in range(200))
    parts = markdown_messages("````md\n```bash\n" + inner + "\n```\n" + inner + "\n````", 500)
    assert len(parts) > 2
    for part in parts:
        assert part.startswith("```") and part.endswith("```"), part[:30]


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


CHAT = f"{OWNER}@s.whatsapp.net"


@pytest.fixture
def wire():
    """A transport whose sends are recorded, so the answer and command paths stay visible."""
    def build():
        config = load_config(env={})
        config.data["gateway"] = {"name": "wa", "allowed_users": [OWNER]}
        item = whatsapp.Transport(config)
        item.sent = []

        async def send_text(chat, text, quote=None):
            item.sent.append((chat, text))
            return f"m{len(item.sent)}"

        item.send_text = send_text
        return item
    return build


def texts(item):
    return [text for _chat, text in item.sent]


async def no_process():
    """What these tests care about is routing, not starting an agent process."""


def asking(item, options=("staging", "production")):
    """An agent with omp's ask tool waiting for an answer."""
    agent = item.agent_for(CHAT)
    agent.pending_ask = {"token": "ask-1", "questions": [{"id": "q1"}], "index": 0}
    agent.pending_options = list(options)
    calls = []

    async def answer_ask(text, *, option_index=None, skip=False):
        calls.append((text, option_index, skip))
        return True

    agent.answer_ask = answer_ask
    return agent, calls


@pytest.mark.parametrize(("reply", "expected"), [
    ("2", (None, 1, False)),
    ("skip", (None, None, True)),
    ("0", (None, None, True)),
    ("pakai postgres", ("pakai postgres", None, False)),
])
def test_an_ask_question_takes_the_number_the_skip_word_or_the_text(wire, reply, expected):
    item = wire()

    async def go():
        agent, calls = asking(item)
        item.open_question(CHAT, 2)
        await item.handle(whatsapp.Incoming(CHAT, OWNER, reply, False))
        return calls, agent

    calls, agent = asyncio.run(go())
    assert calls == [expected]
    # the core re-offers the next question itself, so the local slot is released here
    assert agent.pending_options == ["staging", "production"]


def test_held_messages_go_out_once_the_question_is_gone(wire):
    """A question that times out never answers itself, so what waited behind it must go out."""
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)
        agent.pending_options = ["teh", "kopi"]
        await item.submit(whatsapp.Incoming(CHAT, OWNER, "pesan yang menunggu", False))
        waiting = item.queues.get(CHAT)
        agent.pending_options = []   # the ask timed out: the core cleared its own state
        await item.submit(whatsapp.Incoming(CHAT, OWNER, "pesan baru", False))
        return waiting, item.queues[CHAT].get_nowait(), item.queues[CHAT].get_nowait()

    waiting, first, second = asyncio.run(go())
    assert waiting is None
    assert [first[1].text, second[1].text] == ["pesan yang menunggu", "pesan baru"]


def test_a_held_prompt_goes_out_when_the_ask_is_answered(wire):
    """Waiting for the core to clear pending_ask would strand it: it queues behind the turn."""
    item = wire()

    async def go():
        _agent, calls = asking(item)
        item.open_question(CHAT, 2)
        photo = [{"type": "image", "mimeType": "image/jpeg", "data": "aGk="}]
        await item.submit(whatsapp.Incoming(CHAT, OWNER, "Describe this image.", False,
                                           images=photo))
        held = len(item.held[CHAT])
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "1", False))
        return calls, held, item.held.get(CHAT), item.queues[CHAT].get_nowait()

    calls, held, still_held, queued = asyncio.run(go())
    assert calls == [(None, 0, False)]
    assert (held, still_held) == (1, None)
    assert queued[1].text == "Describe this image."


def test_an_ask_answer_before_its_card_is_shown_still_reaches_the_core(wire):
    """The card is posted from a task; a fast reply must not become a prompt instead."""
    item = wire()

    async def go():
        _agent, calls = asking(item)
        await item.submit(whatsapp.Incoming(CHAT, OWNER, "1", False))
        return calls

    assert asyncio.run(go()) == [(None, 0, False)]


def test_a_photo_while_an_ask_waits_is_held_not_taken_as_the_answer(wire):
    """An ask answers with text; a picture is the next prompt and waits its turn."""
    item = wire()

    async def go():
        _agent, calls = asking(item)
        photo = [{"type": "image", "mimeType": "image/jpeg", "data": "aGk="}]
        await item.submit(whatsapp.Incoming(CHAT, OWNER, "Describe this image.", False,
                                           images=photo))
        return calls, len(item.held[CHAT])

    assert asyncio.run(go()) == ([], 1)


def test_a_number_the_core_refuses_still_reaches_it_as_text(wire):
    """The card can be a question behind; a number that no longer fits is still an answer."""
    item = wire()

    async def go():
        agent, calls = asking(item)
        item.open_question(CHAT, 2)

        async def answer_ask(text, *, option_index=None, skip=False):
            calls.append((text, option_index, skip))
            return option_index is None

        agent.answer_ask = answer_ask
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "2", False))
        return calls

    assert asyncio.run(go()) == [(None, 1, False), ("2", None, False)]


def test_a_native_ask_shows_its_question_above_the_options(wire):
    item = wire()

    async def go():
        surface = item.agent_for(CHAT).surface
        await surface.offer_options(["teh", "kopi"], question="Minum apa?")
        asked = texts(item)[-1]
        await surface.offer_options(["satu", "dua"])   # a marker question has no heading
        return asked, texts(item)[-1], item.choices[CHAT]

    asked, marker, choice = asyncio.run(go())
    assert "Minum apa?" in asked
    assert "teh" in asked and "kopi" in asked
    assert "pick an answer" in marker
    assert choice.kind == "question" and choice.size == 2


def followup_agent(item):
    agent = item.agent_for(CHAT)
    agent.store.set_pref(agent.key, "busy", "followup")
    agent.busy = True
    agent.verbose = True
    return agent


def test_a_followup_chat_gives_the_midturn_message_to_omp(wire):
    item = wire()

    async def go():
        agent = followup_agent(item)
        calls = []

        async def follow_up(text, images=None):
            calls.append(text)
            return "injected", ""

        agent.follow_up = follow_up
        await item._route(CHAT, agent, whatsapp.Incoming(CHAT, OWNER, "cek juga disk", False))
        return calls

    assert asyncio.run(go()) == ["cek juga disk"]
    assert item.queues.get(CHAT) is None
    assert wa_surface.FOLLOWED_UP in texts(item)[-1]


def test_a_followup_omp_refuses_falls_back_to_the_queue(wire):
    item = wire()

    async def go():
        agent = followup_agent(item)

        async def follow_up(_text, images=None):
            return "failed", "the turn just ended"

        agent.follow_up = follow_up
        await item._route(CHAT, agent, whatsapp.Incoming(CHAT, OWNER, "cek juga disk", False))
        return item.queues[CHAT].qsize()

    assert asyncio.run(go()) == 1
    assert "queued #1" in texts(item)[-1]


def test_the_busy_picker_sets_the_mode_by_number(wire):
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/busy", False))
        picker = texts(item)[-1]
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "2", False))
        return agent.busy_mode, picker

    mode, picker = asyncio.run(go())
    assert mode == "followup"
    assert picker.count("← current") == 1
    assert "steer" in picker and "queue" in picker


def test_busy_with_a_value_sets_the_mode_directly(wire):
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/busy queue", False))
        return agent.busy_mode

    assert asyncio.run(go()) == "queue"


def test_the_sessions_picker_resumes_the_numbered_session(wire):
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)
        agent.ensure = no_process
        agent.session_id = "bbbb2222"
        sessions = [{"sessionId": "aaaa1111", "updatedAt": "2026-10-25T10:00:00Z"},
                    {"sessionId": "bbbb2222", "updatedAt": "2026-10-26T10:00:00Z"}]

        async def list_sessions():
            return sessions

        agent.list_sessions = list_sessions
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/sessions", False))
        listed = texts(item)[-1]
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "1", False))
        return listed, item.queues[CHAT].get_nowait()

    listed, queued = asyncio.run(go())
    assert queued == ("resume", "aaaa1111")
    assert "bbbb2222"[:8] + " · " in listed and "← current" in listed


def test_resuming_a_session_waits_for_the_running_turn(wire):
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)
        agent.ensure = no_process

        async def list_sessions():
            return [{"sessionId": "aaaa1111", "updatedAt": "2026-10-25T10:00:00Z"}]

        agent.list_sessions = list_sessions
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/sessions", False))
        agent.busy = True
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "1", False))
        return item.queues.get(CHAT), CHAT in item.choices

    queue, still_open = asyncio.run(go())
    assert queue is None
    assert still_open          # the same number works once the turn ends
    assert "still running" in texts(item)[-1]


def test_sessions_lists_no_more_than_ten(wire):
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)
        agent.ensure = no_process

        async def list_sessions():
            return [{"sessionId": f"id{index:04d}", "updatedAt": "2026-10-26T10:00:00Z"}
                    for index in range(14)]

        agent.list_sessions = list_sessions
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/sessions", False))
        return item.choices[CHAT]

    choice = asyncio.run(go())
    assert (choice.kind, choice.size, len(choice.data)) == ("session", 10, 10)


def test_stats_reports_what_omp_counts(wire):
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)

        async def session_stats():
            return {"userMessages": 7, "assistantMessages": 5, "toolCalls": 11,
                    "tokens": {"input": 1500, "output": 900, "cacheRead": 80000,
                               "cacheWrite": 200},
                    "cost": 0.42, "premiumRequests": 3,
                    "contextUsage": {"tokens": 42000, "contextWindow": 200000}}

        agent.session_stats = session_stats
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/stats", False))

    asyncio.run(go())
    body = texts(item)[-1]
    assert "7 you" in body and "5 agent" in body and "11 tools" in body
    assert "1.5k in" in body and "900 out" in body
    assert "$0.42" in body and "42k / 200k" in body


def test_a_failed_read_reports_in_one_line(wire):
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)

        async def session_stats():
            raise AgentError("the agent process is gone")

        agent.session_stats = session_stats
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/stats", False))

    asyncio.run(go())
    assert texts(item)[-1] == "stats failed: the agent process is gone"


def test_name_renames_the_session_and_says_how_without_a_name(wire):
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)
        named = []

        async def rename(name):
            named.append(name)

        agent.rename = rename
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/name", False))
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/name deploy rabu", False))
        return named

    assert asyncio.run(go()) == ["deploy rabu"]
    assert "usage" in texts(item)[0]


def test_export_sends_the_html_as_a_document(wire, tmp_path):
    item = wire()
    target = tmp_path / "session.html"

    async def go():
        agent = item.agent_for(CHAT)
        files = []

        async def export_html():
            target.write_text("<html>hello</html>")
            return target

        async def send_file(chat, path, as_photo):
            files.append((chat, path, as_photo))

        agent.export_html = export_html
        item.send_file = send_file
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/export", False))
        return files

    assert asyncio.run(go()) == [(CHAT, target, False)]


def test_handoff_waits_for_the_running_turn(wire):
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)
        agent.busy = True
        called = []

        async def handoff(instructions=""):
            called.append(instructions)
            return {}

        agent.handoff = handoff
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/handoff ringkas", False))
        return called

    assert asyncio.run(go()) == []
    assert "still running" in texts(item)[-1]


def test_handoff_reports_where_the_summary_was_saved(wire):
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)

        async def handoff(instructions=""):
            assert instructions == "ringkas"
            return {"savedPath": "/tmp/handoff-notes.md"}

        agent.handoff = handoff
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/handoff ringkas", False))

    asyncio.run(go())
    assert "/tmp/handoff-notes.md" in texts(item)[-1]


def test_btw_is_answered_while_a_turn_runs(wire):
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)
        agent.busy = True

        async def btw(question):
            return f"jawaban untuk {question}"

        agent.btw = btw
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/btw pakai python apa", False))

    asyncio.run(go())
    body = texts(item)[-1]
    assert "pakai python apa" in body and "jawaban untuk pakai python apa" in body


def test_btw_reports_a_broken_side_channel_in_one_line(wire):
    item = wire()

    async def go():
        agent = item.agent_for(CHAT)

        async def btw(_question):
            raise AgentError("the session is closed")

        agent.btw = btw
        await item.handle(whatsapp.Incoming(CHAT, OWNER, "/btw halo", False))

    asyncio.run(go())
    assert texts(item)[-1] == "btw failed: the session is closed"


@pytest.mark.parametrize("reply", ["/steer lanjut", "/queue nanti", "/resume aaaa1111",
                                   "/continue"])
def test_a_retired_command_is_sent_to_the_agent_as_a_prompt(wire, reply):
    item = wire()

    async def go():
        await item.handle(whatsapp.Incoming(CHAT, OWNER, reply, False))
        return item.queues[CHAT].get_nowait()

    kind, payload = asyncio.run(go())
    assert (kind, payload.text) == ("prompt", reply)
