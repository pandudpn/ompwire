"""A turn rendered from a fake agent: reasoning, tool trace, subagent, answer.

The reasoning display cannot be tested against the live model, because omp only emits
agent_thought_chunk on turns where the model actually reasons. A fake client makes the stream
deterministic, which is what a display needs anyway.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from ompwire import telegram
from ompwire.acp import PromptResult, Update
from ompwire.config import load_config


class FakeBot:
    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **_kw):
        self.messages.append(text)
        log = self.messages

        class Sent:
            def __init__(self, body):
                self.body = body

            async def edit_text(self, body, **_kwargs):
                log.append(body)

        return Sent(text)

    async def send_photo(self, *_a, **_k):
        return None

    async def send_document(self, *_a, **_k):
        return None


class FakeClient:
    alive = True

    def __init__(self, updates):
        self.updates = updates
        self.on_update = None
        self.cwd = "/tmp"

    def prompt(self, _session_id, _text, images=None, timeout=900.0, collect=True):
        for update in self.updates:
            if self.on_update is not None:
                self.on_update(update)
        text = "".join(u.text for u in self.updates if u.kind == "agent_message_chunk")
        return PromptResult(stop_reason="end_turn", usage={}, text=text, updates=[])


def thought(text):
    return Update(kind="agent_thought_chunk", session_id="s",
                  raw={"sessionUpdate": "agent_thought_chunk",
                       "content": {"type": "text", "text": text}})


def message(text):
    return Update(kind="agent_message_chunk", session_id="s",
                  raw={"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": text}})


def tool_progress(call_id, text, status="in_progress"):
    return Update(kind="tool_call_update", session_id="s",
                  raw={"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                       "status": status, "content": [{"type": "text", "text": text}]})


def tool_call(call_id, title, tasks=None):
    raw = {"sessionUpdate": "tool_call", "toolCallId": call_id, "kind": "other",
           "title": title, "status": "in_progress", "rawInput": {}}
    if tasks is not None:
        raw["rawInput"]["tasks"] = tasks
    return Update(kind="tool_call", session_id="s", raw=raw)


@pytest.fixture
def rendered(monkeypatch):
    def build(updates, *, reasoning=False, verbose=True):
        config = load_config(env={})
        config.data.setdefault("stream", {})
        config.data["stream"].update({"show_tool_calls": verbose,
                                      "edit_interval": 0.0,
                                      "show_reasoning": reasoning})
        bot = FakeBot()
        app = types.SimpleNamespace(bot=bot)

        async def go():
            agent = telegram.ChatAgent(config, chat_id=1, key="test", app=app)
            agent.client = FakeClient(updates)
            agent.client.on_update = agent._on_update  # ensure() would otherwise leave it unset
            agent.session_id = "s"
            agent.reasoning = reasoning
            agent.verbose = verbose
            await agent.ask("halo")
            return bot.messages

        return asyncio.run(go())

    return build


def test_the_answer_is_sent(rendered):
    messages = rendered([message("jawaban singkat")])
    assert any("jawaban singkat" in body for body in messages)


def test_reasoning_is_hidden_until_it_is_asked_for(rendered):
    messages = rendered([thought("mari saya hitung"), message("42")], reasoning=False)
    assert not any("💭" in body for body in messages)


def test_reasoning_is_rendered_when_it_is_on(rendered):
    messages = rendered([thought("mari saya hitung dulu"), message("42")], reasoning=True)
    thinking = [body for body in messages if "💭" in body]
    assert thinking, messages
    assert "mari saya hitung" in thinking[0]


def test_a_subagent_is_named_in_the_trace(rendered):
    messages = rendered([
        tool_call("t1", "Reading note.txt via subagent",
                  tasks=[{"name": "ReadNote", "agent": "sonic"}]),
        message("selesai"),
    ])
    assert any("subagent: ReadNote (sonic)" in body for body in messages)


def test_a_long_reasoning_stream_is_cut_to_its_tail(rendered):
    long = "x" * (telegram.REASONING_CHARS + 500) + "EKOR"
    messages = rendered([thought(long), message("selesai")], reasoning=True)
    thinking = [body for body in messages if "💭" in body][-1]
    assert "EKOR" in thinking
    assert len(thinking) < len(long)


def test_a_scheduled_run_is_a_card_not_a_log_line():
    """What the first version looked like: a bare "schedule · name" line above raw text."""
    from ompwire.format import run_card, run_status

    card = run_card("morning", "every day at 07:00", run_status(True, "done"),
                    [("took", "12.4s"), ("sent to", "telegram · ephemeral")],
                    "<b>All clear</b>", "20:20:00 WIB · 12.4s · morning")

    assert card.startswith("🗓️ <b>morning</b>")
    assert "✅ <b>done</b>" in card
    assert "<pre>took" in card
    assert card.endswith("</i>")
    assert "schedule · " not in card


def test_a_failed_run_says_so_in_the_card():
    from ompwire.format import run_card, run_status

    card = run_card("morning", "every day at 07:00", run_status(False, "agent failed"),
                    [("took", "0.4s")], "<pre>ACP command not found: omp</pre>", "")
    assert "⛔" in card and "agent failed" in card
    assert "ACP command not found" in card


def test_a_subagent_is_labelled_subagent_not_other(rendered):
    """The ACP kind for omp's task tool is "other", which tells the reader nothing."""
    messages = rendered([
        tool_call("t1", "Reading note.txt via subagent",
                  tasks=[{"name": "ReadNote", "agent": "sonic"}]),
        message("done"),
    ])
    trace = [body for body in messages if "🔧" in body][-1]
    assert "<code>subagent</code>" in trace
    assert "<code>other</code>" not in trace


def test_what_the_subagent_is_doing_is_shown(rendered):
    messages = rendered([
        tool_call("t1", "Reading note.txt via subagent",
                  tasks=[{"name": "ReadNote", "agent": "sonic"}]),
        tool_progress("t1", "Spawned agent ReadNote"),
        tool_progress("t1", "Running agent ReadNote"),
        message("done"),
    ])
    trace = [body for body in messages if "🔧" in body][-1]
    assert "· Spawned agent ReadNote" in trace
    assert "· Running agent ReadNote" in trace


def test_a_waiting_call_is_not_shown_as_a_step(rendered):
    messages = rendered([
        tool_call("t1", "Reading note.txt via subagent",
                  tasks=[{"name": "ReadNote", "agent": "sonic"}]),
        tool_call("t2", "Waiting for ReadNote result"),
        message("done"),
    ])
    trace = [body for body in messages if "🔧" in body][-1]
    assert "Waiting for" not in trace


def test_the_subagent_brief_is_shown_so_you_know_what_it_was_asked(rendered):
    messages = rendered([
        tool_call("t1", "Reading a.txt via subagent", tasks=[{
            "name": "ReadNote", "agent": "sonic",
            "task": "# Target\nFile: a.txt in the working directory and report its contents.\n",
            "tools": ["read", "shell"],
        }]),
        message("done"),
    ])
    trace = [body for body in messages if "🔧" in body][-1]
    assert "task: File: a.txt in the working directory" in trace
    assert "tools: read, shell" in trace


@pytest.mark.parametrize(("brief", "shown"), [
    ("# Target\nFile: a.txt and report it.\n# Acceptance", "File: a.txt and report it."),
    ("", ""),
    ("```\ncode\n```", "code"),
])
def test_a_brief_skips_markdown_labels(brief, shown):
    from ompwire.telegram import _task_brief

    assert _task_brief(brief) == shown


class FloodBot(FakeBot):
    """Telegram answering every edit with RetryAfter, which used to end the turn."""

    def __init__(self, retry_after=255):
        super().__init__()
        self.retry_after = retry_after
        self.edits = 0

    async def send_message(self, chat_id, text, **_kw):
        from telegram.error import RetryAfter

        self.messages.append(text)
        bot = self

        class Sent:
            def __init__(self, body):
                self.body = body

            async def edit_text(self, body, **_kwargs):
                bot.edits += 1
                raise RetryAfter(bot.retry_after)

        return Sent(text)


def test_a_flood_limit_does_not_end_the_turn(monkeypatch):
    """The regression from a real turn: 49 subagent updates, one edit each, then 255s of flood."""
    import types as _types

    from ompwire.config import load_config

    config = load_config(env={})
    config.data.setdefault("stream", {}).update({"edit_interval": 0.0, "verbose": True})
    bot = FloodBot()
    updates = [tool_call("t1", "Reading a.txt via subagent",
                         tasks=[{"name": "Read", "agent": "scout", "task": "# Target\nread a.txt"}])]
    updates += [tool_progress("t1", f"step {index}") for index in range(40)]
    updates.append(message("selesai"))

    async def go():
        agent = telegram.ChatAgent(config, chat_id=1, key="flood",
                                   app=_types.SimpleNamespace(bot=bot))
        agent.client = FakeClient(updates)
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        agent.verbose = True
        await agent.ask("halo")
        return agent

    agent = asyncio.run(go())
    assert any("selesai" in body for body in bot.messages)
    assert agent._quiet_until > 0  # it noticed, and stopped editing
    assert bot.edits <= 2  # one attempt, then quiet; not forty


def test_the_tool_trace_is_throttled_without_a_flood(monkeypatch):
    import types as _types

    from ompwire.config import load_config

    edits = {"count": 0}

    class CountingBot(FakeBot):
        async def send_message(self, chat_id, text, **_kw):
            self.messages.append(text)

            class Sent:
                def __init__(self, body):
                    self.body = body

                async def edit_text(self, body, **_kwargs):
                    edits["count"] += 1

            return Sent(text)

    config = load_config(env={})
    config.data.setdefault("stream", {}).update({"edit_interval": 0.0, "verbose": True,
                                                 "tool_edit_interval": 2.0})
    updates = [tool_call("t1", "Reading a.txt via subagent",
                         tasks=[{"name": "Read", "agent": "scout", "task": "read a.txt"}])]
    updates += [tool_progress("t1", f"step {index}") for index in range(40)]
    updates.append(message("selesai"))

    async def go():
        agent = telegram.ChatAgent(config, chat_id=1, key="throttle",
                                   app=_types.SimpleNamespace(bot=CountingBot()))
        agent.client = FakeClient(updates)
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        agent.verbose = True
        await agent.ask("halo")

    asyncio.run(go())
    assert edits["count"] <= 3  # inside one interval only the final flush renders
