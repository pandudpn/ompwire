"""Telegram buttons, commands that must not cut a running turn, and what a restart keeps."""

from __future__ import annotations

import asyncio
import types

from ompwire import allowlist, telegram
from ompwire.acp import PromptResult, Update
from ompwire.config import load_config
from ompwire.store import SessionStore


class Bot:
    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, _chat_id, text, **_kw):
        self.messages.append(text)
        return types.SimpleNamespace(edit_text=self._record, reply_text=self._record,
                                     set_reaction=self._noop)

    async def _record(self, body, **_kw):
        self.messages.append(body)

    async def _noop(self, *_a, **_k):
        return None

    async def send_chat_action(self, *_a, **_k):
        return None


def _config(tmp_path):
    config = load_config(env={})
    config.data["session"]["store"] = str(tmp_path / "s.db")
    config.data["gateway"] = {"name": "telegram", "allowed_users": [7]}
    return config


def _transport(config):
    transport = telegram.Transport(config)
    transport.app = types.SimpleNamespace(bot=Bot())
    return transport


class Query:
    def __init__(self, data):
        self.data = data
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.message = types.SimpleNamespace(reply_text=self._reply, set_reaction=self._reply)

    async def answer(self, text="", **_kw):
        self.answers.append(text)

    async def edit_message_text(self, body, **_kw):
        self.edits.append(body)

    async def _reply(self, *_a, **_k):
        return None


def _update(query=None, text=""):
    replies: list[str] = []

    async def reply_text(body, **_kw):
        replies.append(body)

    message = types.SimpleNamespace(text=text, reply_text=reply_text)
    update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=7),
                                   effective_chat=types.SimpleNamespace(id=7),
                                   effective_message=message, callback_query=query)
    return update, replies


# ------------------------------------------------------------------ option buttons
def test_an_option_button_sends_the_option_text_as_the_next_prompt(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        agent.pending_options = ["postgres", "mysql"]
        sent: list[str] = []
        transport.enqueue = lambda _chat, _kind, payload: sent.append(payload.text) or 1
        query = Query("qo:1")
        update, _ = _update(query)
        await transport.on_callback(update, None)
        return sent, agent.pending_options, query

    sent, left, query = asyncio.run(go())
    assert sent == ["mysql"]
    assert left == []  # a second tap on the same card cannot send it again
    assert "mysql" in query.edits[0]


def test_other_answer_removes_the_buttons(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        await agent.surface.offer_options(["postgres", "mysql"])
        agent.pending_options = ["postgres", "mysql"]
        card = agent.surface.options_message
        edits: list[dict] = []

        async def edit_text(body, **kw):
            edits.append({"body": body, **kw})

        card.edit_text = edit_text
        await transport.on_callback(_update(Query("qo:other"))[0], None)
        sent: list[str] = []
        transport.enqueue = lambda _chat, _kind, payload: sent.append(payload.text) or 1
        await transport.on_callback(_update(Query("qo:0"))[0], None)  # a late tap
        return edits, sent

    edits, sent = asyncio.run(go())
    assert len(edits) == 1 and "reply_markup" not in edits[0]  # no keyboard left
    assert "postgres" in edits[0]["body"]  # the options stay readable
    assert sent == []


def _open_question(transport, options):
    agent = transport.agent_for(7)
    agent.pending_options = list(options)
    sent: list[str] = []
    transport.enqueue = lambda _chat, _kind, payload: sent.append(payload.text) or len(sent)
    return agent, sent


class _Typed:
    def __init__(self, text):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, body, **_kw):
        self.replies.append(body)


def test_a_message_about_something_else_waits_for_the_open_question(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent, sent = _open_question(transport, ["fix it", "change the format"])
        other = _Typed("one more issue: CI is red")
        await transport.submit(7, other, other)
        before = list(sent)
        await transport.on_callback(_update(Query("qo:0"))[0], None)
        return before, sent, other.replies, agent.pending_options

    before, sent, replies, left = asyncio.run(go())
    assert before == []  # it did not run ahead of the answer
    assert sent == ["fix it", "one more issue: CI is red"]  # answer first, then the rest
    assert replies and replies[0].startswith("⏸️ held #1")
    assert left == []


def test_other_answer_takes_the_next_message_as_the_answer(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        _agent, sent = _open_question(transport, ["a", "b"])
        early = _Typed("unrelated thing")
        await transport.submit(7, early, early)
        await transport.on_callback(_update(Query("qo:other"))[0], None)
        answer = _Typed("neither, use c")
        await transport.submit(7, answer, answer)
        later = _Typed("after the answer")
        await transport.submit(7, later, later)
        return sent

    assert asyncio.run(go()) == ["neither, use c", "unrelated thing", "after the answer"]


def test_skip_releases_what_was_held(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent, sent = _open_question(transport, ["a"])
        held = _Typed("do this instead")
        await transport.submit(7, held, held)
        await transport.on_callback(_update(Query("qo:skip"))[0], None)
        return sent, agent.pending_options

    sent, left = asyncio.run(go())
    assert sent == ["do this instead"]
    assert left == []


def test_a_stale_option_button_says_so_instead_of_sending(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        sent: list[str] = []
        transport.enqueue = lambda _chat, _kind, payload: sent.append(payload.text) or 1
        query = Query("qo:3")
        update, _ = _update(query)
        await transport.on_callback(update, None)
        return sent, query.answers

    sent, answers = asyncio.run(go())
    assert sent == []
    assert answers == ["that option expired"]


def test_page_counter_buttons_do_nothing(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        results = []
        for data in ("sk:noop", "lg:noop"):
            query = Query(data)
            update, _ = _update(query)
            await transport.on_callback(update, None)
            results.append(query.answers)
        return results

    assert asyncio.run(go()) == [[""], [""]]  # answered quietly, no "bad callback"


def test_a_button_from_someone_else_is_ignored(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        agent.pending_options = ["yes"]
        sent: list[str] = []
        transport.enqueue = lambda _chat, _kind, payload: sent.append(payload.text) or 1
        query = Query("qo:0")
        update, _ = _update(query)
        update.effective_user = types.SimpleNamespace(id=99)
        await transport.on_callback(update, None)
        return sent

    assert asyncio.run(go()) == []


# ------------------------------------------------------- commands during a turn
def test_new_and_yolo_wait_for_a_running_turn(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        agent.busy = True
        calls: list[str] = []

        async def new_session():
            calls.append("new")

        async def respawn(yolo):
            calls.append("respawn")

        agent.new_session = new_session
        agent.respawn = respawn
        out = []
        for handler in (transport.cmd_new, transport.cmd_yolo):
            update, replies = _update()
            await handler(update, types.SimpleNamespace(args=[]))
            out.append(replies)
        return calls, out

    calls, out = asyncio.run(go())
    assert calls == []  # the running turn was not cut
    assert all("still running" in replies[0] for replies in out)


# ------------------------------------------------------------- a restart keeps
def test_cwd_survives_a_restart(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    config = _config(tmp_path)
    store = SessionStore(config.get("session", "store"))
    store.put("tg:7", "sess", str(work))
    store.close()

    async def go():
        return telegram.chat_agent(config, 7, "tg:7", types.SimpleNamespace(bot=Bot())).cwd

    assert asyncio.run(go()) == str(work)


def test_a_removed_cwd_falls_back_to_the_config(tmp_path):
    config = _config(tmp_path)
    config.data["agent"]["cwd"] = str(tmp_path)
    store = SessionStore(config.get("session", "store"))
    store.put("tg:7", "sess", str(tmp_path / "gone"))
    store.close()

    async def go():
        return telegram.chat_agent(config, 7, "tg:7", types.SimpleNamespace(bot=Bot())).cwd

    assert asyncio.run(go()) == str(tmp_path)


# --------------------------------------------------- two answers in one turn
def _chunk(text, message_id):
    return Update(kind="agent_message_chunk", session_id="s",
                  raw={"sessionUpdate": "agent_message_chunk", "messageId": message_id,
                       "content": {"type": "text", "text": text}})


def test_two_answers_in_one_turn_stay_two_messages(tmp_path):
    """A steered turn held two answers; the second began right after the first's `>>`."""
    config = _config(tmp_path)
    config.data["stream"]["min_gap"] = 0.0
    bot = Bot()
    updates = [_chunk("first answer\n<<options: a | b>>", "m1"),
               _chunk("second answer\n<<options: c | d>>", "m2")]

    class Client:
        alive = True
        on_update = None

        def prompt(self, *_a, **_k):
            for update in updates:
                self.on_update(update)
            return PromptResult(stop_reason="end_turn", usage={}, text="", updates=[])

    async def go():
        agent = telegram.chat_agent(config, 7, "tg:7", types.SimpleNamespace(bot=bot))
        agent.client = Client()
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        await agent.ask("x")
        return agent.pending_options

    options = asyncio.run(go())
    answers = [body for body in bot.messages if body.endswith("answer")]
    assert len(answers) == 2
    assert all("<<" not in body for body in answers)
    assert options == ["c", "d"]


# --------------------------------------------------------- the allowlist
def test_a_flag_written_with_equals_is_still_a_write():
    for command in ("git log --output=/tmp/x", "git diff --output=/tmp/x",
                    "sort --output=/tmp/x f", "rg --pre=touch x", "rg --pre touch x",
                    "journalctl --vacuum-time=1s", "gh api repos/x --method=POST"):
        assert allowlist.is_read_only(command) is False, command


def test_display_flags_named_like_writes_stay_read_only():
    for command in ("journalctl -u ompwire --output=short-precise -n 20",
                    "git log --oneline -5", "rg -n foo src", "sort -n f"):
        assert allowlist.is_read_only(command) is True, command
