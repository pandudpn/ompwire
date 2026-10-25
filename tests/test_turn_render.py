"""A turn rendered from a fake agent: reasoning, tool trace, subagent, answer.

The reasoning display cannot be tested against the live model, because omp only emits
agent_thought_chunk on turns where the model actually reasons. A fake client makes the stream
deterministic, which is what a display needs anyway.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from ompwire import engine, telegram
from ompwire.acp import PromptResult, Update
from ompwire.config import load_config


class FakeBot:
    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **_kw):
        self.messages.append(text)

        class Sent:
            def __init__(self, body):
                self.body = body

            async def edit_text(self, body, **_kwargs):
                raise AssertionError("a sent message must never be edited")

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
                                      "show_reasoning": reasoning, "min_gap": 0.0})
        bot = FakeBot()
        app = types.SimpleNamespace(bot=bot)

        async def go():
            agent = telegram.chat_agent(config, 1, key="test", app=app)
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
    assert not any("🧠" in body for body in messages)


def test_reasoning_is_rendered_when_it_is_on(rendered):
    messages = rendered([thought("mari saya hitung dulu"), message("42")], reasoning=True)
    thinking = [body for body in messages if "🧠" in body]
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
    long = "x" * (engine.REASONING_CHARS + 500) + "EKOR"
    messages = rendered([thought(long), message("selesai")], reasoning=True)
    thinking = [body for body in messages if "🧠" in body][-1]
    assert "EKOR" in thinking
    assert len(thinking) < len(long)


def test_a_scheduled_run_is_a_card_not_a_log_line():
    """What the first version looked like: a bare "schedule · name" line above raw text."""
    from ompwire.tg_html import run_card, run_status

    card = run_card("morning", "every day at 07:00", run_status(True, "done"),
                    [("took", "12.4s"), ("sent to", "telegram · ephemeral")],
                    "<b>All clear</b>", "20:20:00 WIB · 12.4s · morning")

    assert card.startswith("🗓️ <b>morning</b>")
    assert "✅ <b>done</b>" in card
    assert "<pre>took" in card
    assert card.endswith("</i>")
    assert "schedule · " not in card


def test_a_failed_run_says_so_in_the_card():
    from ompwire.tg_html import run_card, run_status

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
    trace = [body for body in messages if "<code>subagent</code>" in body][-1]
    assert trace.startswith("🤖")
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
    trace = [body for body in messages if "<code>subagent</code>" in body][-1]
    assert "· Spawned agent ReadNote" in trace
    assert "· Running agent ReadNote" in trace


def test_a_waiting_call_is_not_shown_as_a_step(rendered):
    messages = rendered([
        tool_call("t1", "Reading note.txt via subagent",
                  tasks=[{"name": "ReadNote", "agent": "sonic"}]),
        tool_call("t2", "Waiting for ReadNote result"),
        message("done"),
    ])
    trace = [body for body in messages if "<code>subagent</code>" in body][-1]
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
    trace = [body for body in messages if "<code>subagent</code>" in body][-1]
    assert "task: File: a.txt in the working directory" in trace
    assert "tools: read, shell" in trace


@pytest.mark.parametrize(("brief", "shown"), [
    ("# Target\nFile: a.txt and report it.\n# Acceptance", "File: a.txt and report it."),
    ("", ""),
    ("```\ncode\n```", "code"),
])
def test_a_brief_skips_markdown_labels(brief, shown):
    from ompwire.engine import _task_brief

    assert _task_brief(brief) == shown


@pytest.mark.parametrize(("command", "expected"), [
    # reads that real turns ask for, and that nobody should have to approve
    ("ls -d ~/ompwire ~/*/ompwire 2>/dev/null", True),
    ("find src -name '*.py' -printf '%s %p\\n' | sort -n", True),
    ("awk 'BEGIN{system(\"rm -rf /tmp/x\")}'", False),
    ("sort -o out.txt in.txt", False),
    ("find . -fprint files.txt", False),
    ("git branch -D main", False),
    ("git branch --show-current", True),
    ("git remote add origin https://x", False),
    ("git remote -v", True),
    ("git log --oneline -5 && git status --short", True),
    ("git log --oneline 2>&1 | head", True),
    ("gh repo view noovoleum/infra-deploy-prod", True),
    ("gh api repos/noovoleum/infra-deploy-prod", True),
    ("kubectl get pods -n ucollect", True),
    ("cat README.md | head -20", True),
    # anything that writes, deletes, deploys, or hides a second command
    ("gh repo clone noovoleum/x ~/x", False),
    ("rm -rf /tmp/x", False),
    ("echo hi > hasil.txt", False),
    ("cat a > b.txt", False),
    ("find . -name x -delete", False),
    ("find . -name x -exec rm {} +", False),
    ("git log && git checkout main", False),
    ("sed -i s/a/b/ file", False),
    ("kubectl apply -f x.yaml", False),
    ("docker run -d nginx", False),
    ("gh api -X POST repos/x", False),
    ("ls | xargs rm", False),
    ("cat x | sh", False),
    ("git log & sleep 1", False),
    ("sudo systemctl restart x", False),
    ("ls $(rm -rf /tmp/x)", False),
])
def test_only_commands_that_cannot_change_anything_run_without_a_button(command, expected):
    from ompwire.allowlist import is_read_only

    assert is_read_only(command) is expected


def test_a_read_only_command_does_not_wait_for_a_click(rendered):
    """Forty approvals for `git log` trains the tap-without-reading habit the gate exists to stop."""
    import types as _types

    from ompwire.config import load_config

    class Bot(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            self.messages.append(text)
            return None

    config = load_config(env={})
    config.data.setdefault("agent", {})["yolo"] = False
    config.data.setdefault("approval", {})["timeout"] = 3.0
    bot = Bot()
    answered: list[str] = []

    async def go():
        agent = telegram.chat_agent(config, 1, key="readonly",
                                   app=_types.SimpleNamespace(bot=bot))
        params = {"toolCall": {"kind": "execute", "title": "git log",
                              "rawInput": {"command": "git log --oneline -3"}},
                  "options": [{"optionId": "allow_once", "kind": "allow_once"},
                              {"optionId": "reject_once", "kind": "reject_once"}]}
        answered.append(agent._on_permission(params))
        return agent

    asyncio.run(go())
    assert answered == ["allow_once"]


def test_text_is_cut_at_each_tool_call_into_separate_messages(rendered):
    """Each step keeps its own timestamp: text, then the tool, then the text after it."""
    messages = rendered([
        message("aku cek dulu."),
        tool_call("t1", "ls"),
        tool_progress("t1", "", status="completed"),
        message("ketemu, ini jawabannya."),
    ])
    order = [("cek" if "aku cek dulu" in body else "tool" if "🔧" in body
              else "answer" if "ketemu" in body else "?") for body in messages]
    assert order == ["cek", "tool", "answer"]


class SpacedClient(FakeClient):
    """Updates with a real pause between them, the way tool calls arrive from a live agent."""

    def __init__(self, updates, pause=0.05):
        super().__init__(updates)
        self.pause = pause

    def prompt(self, _session_id, _text, images=None, timeout=900.0, collect=True):
        import time as _time

        for update in self.updates:
            self.on_update(update)
            _time.sleep(self.pause)
        return PromptResult(stop_reason="end_turn", usage={}, text="", updates=[])


def _paced_turn(bot, updates, *, min_gap=0.0, pause=0.05):
    from ompwire.config import load_config

    config = load_config(env={})
    config.data["stream"]["min_gap"] = min_gap

    async def go():
        agent = telegram.chat_agent(config, 1, key="paced",
                                   app=types.SimpleNamespace(bot=bot))
        agent.client = SpacedClient(updates, pause)
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        await agent.ask("halo")

    asyncio.run(go())


def test_each_tool_call_is_its_own_message_posted_once():
    bot = FakeBot()
    _paced_turn(bot, [tool_call("t1", "first"), tool_progress("t1", "half", status="in_progress"),
                      tool_progress("t1", "", status="completed"),
                      tool_call("t2", "second"), tool_progress("t2", "", status="failed"),
                      message("selesai")])
    tools = [body for body in bot.messages if "🔧" in body]
    assert len(tools) == 2  # one per call, not one per progress update
    assert "first" in tools[0] and "second" in tools[1]
    assert "failed" in tools[1]


def test_messages_are_paced_and_traces_that_pile_up_are_merged():
    """Five calls finish while the sender is still inside its gap: they go out as one message."""
    import itertools
    import time as _time

    stamps: list[float] = []

    class StampBot(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            stamps.append(_time.monotonic())
            return await super().send_message(chat_id, text, **kw)

    bot = StampBot()
    updates = [message("mulai.")]
    for index in range(5):
        updates += [tool_call(f"t{index}", f"grep {index}"),
                    tool_progress(f"t{index}", "", status="completed")]
    updates.append(message("selesai."))
    _paced_turn(bot, updates, min_gap=0.3, pause=0.01)

    gaps = [later - earlier for earlier, later in itertools.pairwise(stamps)]
    assert all(gap >= 0.25 for gap in gaps), gaps
    traces = [body for body in bot.messages if "🔧" in body]
    assert len(traces) < 5  # merged, not five separate messages
    assert sum(body.count("🔧") for body in traces) == 5  # and none lost
    assert bot.messages[0] == "mulai." and "selesai" in bot.messages[-1]


def test_a_trace_dropped_by_flood_control_is_named_in_the_chat(monkeypatch):
    from telegram.error import RetryAfter

    class FloodTraces(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            if "🔧" in text and "not shown" not in text:
                raise RetryAfter(30)  # longer than a trace is worth waiting for
            return await super().send_message(chat_id, text, **kw)

    real_sleep = asyncio.sleep

    async def short_sleep(delay, *a, **k):  # the 30 s flood window, without waiting 30 s
        return await real_sleep(min(delay, 0.01), *a, **k)

    monkeypatch.setattr(engine.asyncio, "sleep", short_sleep)
    bot = FloodTraces()
    _paced_turn(bot, [tool_call("t1", "a"), tool_progress("t1", "", status="completed"),
                      message("jawaban")])
    assert any("jawaban" in body for body in bot.messages)
    assert any("1 tool call(s) not shown" in body for body in bot.messages)


def test_a_tool_call_that_never_finishes_is_still_shown_at_the_end(rendered):
    messages = rendered([tool_call("t1", "long job"), message("selesai")])
    assert any("long job" in body for body in messages)


def test_a_flood_window_on_the_answer_is_waited_out(monkeypatch):
    from telegram.error import RetryAfter

    from ompwire.config import load_config

    config = load_config(env={})

    class FloodOnce(FakeBot):
        def __init__(self):
            super().__init__()
            self.flooded = False

        async def send_message(self, chat_id, text, **kw):
            if not self.flooded:
                self.flooded = True
                raise RetryAfter(1)
            return await super().send_message(chat_id, text, **kw)

    async def no_wait(_delay, *_a, **_k):
        return None

    monkeypatch.setattr(engine.asyncio, "sleep", no_wait)
    bot = FloodOnce()

    async def go():
        agent = telegram.chat_agent(config, 1, key="floodonce",
                                   app=types.SimpleNamespace(bot=bot))
        agent.client = FakeClient([message("jawaban lengkap")])
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        await agent.ask("halo")

    asyncio.run(go())
    assert any("jawaban lengkap" in body for body in bot.messages)


def test_a_long_answer_is_split_into_messages_once(rendered):
    messages = rendered([message("a" * 3700), message(" ekor")])
    answer = [body for body in messages if "aaaa" in body or "ekor" in body]
    assert len(answer) == 2


def plan(*entries):
    return Update(kind="plan", session_id="s", raw={
        "sessionUpdate": "plan",
        "entries": [{"content": text, "status": status, "priority": "medium"}
                    for text, status in entries]})


def kinded_call(call_id, kind, title, raw_input):
    return Update(kind="tool_call", session_id="s", raw={
        "sessionUpdate": "tool_call", "toolCallId": call_id, "kind": kind, "title": title,
        "status": "pending", "rawInput": raw_input})


def test_the_todo_list_is_posted_as_a_checklist_each_time_it_changes(rendered):
    """omp sends the todo list as a `plan` update; it used to be dropped without a trace."""
    messages = rendered([
        kinded_call("t1", "think", "Planning", {"op": "init"}),
        tool_progress("t1", "", status="completed"),
        plan(("tulis tes", "in_progress"), ("rilis", "pending")),
        kinded_call("t2", "think", "Marking item 1 done", {"op": "done"}),
        tool_progress("t2", "", status="completed"),
        plan(("tulis tes", "completed"), ("rilis", "in_progress")),
        plan(("tulis tes", "completed"), ("rilis", "in_progress")),  # a reminder repeats it
        plan(),  # omp clears the list once everything is done
        message("selesai"),
    ])
    cards = [body for body in messages if body.startswith("📋 <b>Todo</b>")]
    assert len(cards) == 2, messages
    assert "0/2 done" in cards[0] and "▶️ <b>tulis tes</b>" in cards[0] and "⬜ rilis" in cards[0]
    assert "1/2 done" in cards[1] and "☑️ <s>tulis tes</s>" in cards[1]
    # the checklist replaces the todo call itself, which said only "Marking item 1 done"
    assert not any("<code>todo</code>" in body for body in messages)


def test_a_todo_call_with_no_list_after_it_still_shows(rendered):
    messages = rendered([
        kinded_call("t1", "think", "Marking item 1 done", {"op": "done"}),
        tool_progress("t1", "", status="completed"),
        message("selesai"),
    ])
    assert any(body.startswith("📋 <code>todo</code>") for body in messages), messages


def test_a_tool_is_named_from_the_transcript_not_acp_kind(rendered, tmp_path):
    """ACP files wait, ask, lsp and every MCP tool under "other"; omp's transcript has the name."""
    import json

    from ompwire import omp_sessions

    folder = omp_sessions.SESSIONS_DIR / "-"
    folder.mkdir(parents=True)
    entry = {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": "t1", "name": "wait", "arguments": {}},
        {"type": "toolCall", "id": "t2", "name": "mcp__kaneo_get_task", "arguments": {}}]}}
    (folder / "2026-10-25T00-00-00-000Z_s.jsonl").write_text(json.dumps(entry) + "\n")
    messages = rendered([
        kinded_call("t1", "other", "Collecting suite result", {}),
        tool_progress("t1", "3 passed", status="completed"),
        kinded_call("t2", "other", "Reading the ticket", {"id": "x"}),
        tool_progress("t2", "", status="completed"),
        kinded_call("t3", "other", "Listing devices", {"path": "xd://github/pulls"}),
        tool_progress("t3", "", status="completed"),
        message("selesai"),
    ])
    traces = "\n".join(messages)
    assert "⏳ <code>wait</code>" in traces
    assert "🔌 <code>kaneo_get_task</code>" in traces
    assert "<code>github</code>" in traces
    assert "<code>other</code>" not in traces


def test_a_todo_call_that_changes_nothing_keeps_its_trace(rendered):
    """A list equal to the last one is not posted again, so the call behind it must show."""
    messages = rendered([
        plan(("tulis tes", "in_progress")),
        kinded_call("t1", "think", "Reading the list", {"op": "view"}),
        tool_progress("t1", "", status="completed"),
        plan(("tulis tes", "in_progress")),
        message("selesai"),
    ])
    assert sum(body.startswith("📋 <b>Todo</b>") for body in messages) == 1
    assert any(body.startswith("📋 <code>todo</code>") for body in messages), messages


def test_an_option_switch_mid_turn_lands_in_order_with_its_name(rendered, monkeypatch):
    """A notice sent straight to the platform jumped ahead of traces still waiting to go out."""
    from ompwire import engine as engine_mod

    def options(model):
        return [{"id": "model", "currentValue": model, "options": [
            {"value": "a/one", "name": "Model One"}, {"value": "a/two", "name": "Model Two"}]},
            {"id": "mode", "currentValue": "default", "options": []}]

    original = engine_mod.ChatAgent.ask

    async def ask(self, *args, **kwargs):
        self.options = {opt["id"]: opt for opt in options("a/one")}
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(engine_mod.ChatAgent, "ask", ask)
    messages = rendered([
        kinded_call("t1", "execute", "ls", {"command": "ls"}),
        tool_progress("t1", "", status="completed"),
        Update(kind="config_option_update", session_id="s",
               raw={"sessionUpdate": "config_option_update", "configOptions": options("a/two")}),
        message("selesai"),
    ])
    order = [i for i, body in enumerate(messages) if "<code>ls</code>" in body or "$ ls" in body]
    switch = [i for i, body in enumerate(messages) if "omp switched model" in body]
    assert switch and order and order[0] < switch[0], messages
    assert "Model One" in messages[switch[0]] and "Model Two" in messages[switch[0]]
