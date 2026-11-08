"""A turn rendered from a fake agent: reasoning, tool trace, subagent, answer.

The reasoning display cannot be tested against the live model, because omp only emits
agent_thought_chunk on turns where the model actually reasons. A fake client makes the stream
deterministic, which is what a display needs anyway.
"""

from __future__ import annotations

import asyncio
import types

import pytest
from telegram.error import BadRequest

from ompwire import engine, mdrender, telegram
from ompwire.config import load_config
from ompwire.rpc import PromptResult, Update

TELEGRAM_CAP = 4096  # what the Bot API refuses, counted after entities are parsed


class FakeBot:
    """Telegram, as far as the cap goes: a message over 4096 is refused, never truncated."""

    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **_kw):
        if mdrender.visible_length(text) > TELEGRAM_CAP:
            raise BadRequest("Message is too long")
        if text.count("<pre>") != text.count("</pre>"):
            raise BadRequest("can't parse entities: unclosed tag")
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

    def state(self):
        return {}

    def prompt(self, _text, images=None, timeout=900.0):
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


def tool_call(call_id, title, tasks=None, command=""):
    raw = {"sessionUpdate": "tool_call", "toolCallId": call_id, "kind": "other",
           "title": title, "status": "in_progress", "rawInput": {}}
    if command:
        raw["rawInput"]["command"] = command
    if tasks is not None:
        raw["rawInput"]["tasks"] = tasks
    return Update(kind="tool_call", session_id="s", raw=raw)


def tool_update(call_id, title=""):
    raw = {"sessionUpdate": "tool_call_update", "toolCallId": call_id,
           "kind": "other", "title": title, "status": "in_progress"}
    return Update(kind="tool_call_update", session_id="s", raw=raw)


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
    ("gh repo view acme/infra-deploy-prod", True),
    ("gh api repos/acme/infra-deploy-prod", True),
    ("kubectl get pods -n prod-workloads", True),
    ("cat README.md | head -20", True),
    # anything that writes, deletes, deploys, or hides a second command
    ("gh repo clone acme/other ~/x", False),
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
        frame = {"method": "select", "title": "Allow tool: bash\nCommand: git log --oneline -3",
                 "options": ["Approve", "Deny"]}
        answered.append(agent._on_permission(frame))
        return agent

    asyncio.run(go())
    assert answered == ["Approve"]
    assert not bot.messages  # no button was posted


@pytest.mark.parametrize(("policy", "expected"), [
    # plan mode is stronger than "allow": the write is denied, the read allowed
    ("allow", ["Deny", "Approve", "Approve"]),
    # "deny" is stronger than plan mode: even the read is refused, in or out of plan
    ("deny", ["Deny", "Deny", "Deny"]),
])
def test_the_strictest_of_policy_and_plan_mode_decides(policy, expected):
    """Review finding: [approval] policy = "allow" answered before the plan-mode check, so a
    chat in plan mode could write."""
    import types as _types

    from ompwire.config import load_config

    config = load_config(env={})
    config.data.setdefault("approval", {})["policy"] = policy
    write = {"method": "select", "title": "Allow tool: write\nPath: /tmp/x.txt",
             "options": ["Approve", "Deny"]}
    read = {"method": "select", "title": "Allow tool: read\nPath: /tmp/x.txt",
            "options": ["Approve", "Deny"]}

    async def go():
        agent = telegram.chat_agent(config, 1, key=f"plan-{policy}",
                                   app=_types.SimpleNamespace(bot=FakeBot()))
        agent.options = {"mode": {"currentValue": "plan"}}
        answers = [agent._on_permission(write), agent._on_permission(read)]
        agent.options = {"mode": {"currentValue": "default"}}
        return answers + [agent._on_permission(write)]

    # plan write, plan read, then a write outside plan mode
    assert asyncio.run(go()) == expected


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

    def prompt(self, _text, images=None, timeout=900.0):
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


def test_an_emoji_answer_arrives_in_messages_telegram_accepts(rendered):
    """Telegram counts the message after parsing entities, in UTF-16 units, so 3000 emoji are
    6000 of them while the markdown splitter only saw 3000 characters. The send was refused,
    the plain fallback was refused the same way, and the turn died with the answer unsent."""
    messages = rendered([message("🔧" * 3000)])
    assert len(messages) > 1
    assert "".join(mdrender.strip_html(body) for body in messages).count("🔧") == 3000


def test_a_plan_card_too_long_for_one_message_still_arrives(rendered):
    """A plan checklist is rendered before the transport sees it. Sixty todos were one HTML
    string over the cap, and the plain fallback is exactly as long, so the card killed the
    turn instead of arriving as two messages."""
    items = [(f"step {i}: port the hermes memory records over and check each one is recallable",
              "pending") for i in range(60)]
    messages = rendered([plan(*items), message("selesai")])
    steps = [body for body in messages if "⬜ step" in body]
    assert len(steps) == 2
    assert "step 0:" in steps[0]
    assert "step 59:" in steps[1]


def plan(*entries):
    return Update(kind="plan", session_id="s", raw={
        "sessionUpdate": "plan",
        "entries": [{"content": text, "status": status, "priority": "medium"}
                    for text, status in entries]})


def kinded_call(call_id, kind, title, raw_input, name=""):
    raw = {"toolCallId": call_id, "kind": kind, "title": title, "status": "pending",
           "rawInput": raw_input}
    if name:
        raw["toolName"] = name
    return Update(kind="tool_call", session_id="s", raw=raw)


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


def test_a_tool_is_named_by_omp_not_by_its_kind(rendered):
    """omp names every call (wait, an MCP tool); the coarse kind is only the fallback."""
    messages = rendered([
        kinded_call("t1", "other", "Collecting suite result", {}, name="wait"),
        tool_progress("t1", "3 passed", status="completed"),
        kinded_call("t2", "other", "Reading the ticket", {"id": "x"}, name="mcp__kaneo_get_task"),
        tool_progress("t2", "", status="completed"),
        kinded_call("t3", "other", "Listing devices", {"path": "xd://github/pulls"}, name="read"),
        tool_progress("t3", "", status="completed"),
        message("selesai"),
    ])
    traces = "\n".join(messages)
    assert "⏳ <code>wait</code>" in traces
    assert "🔌 <code>kaneo_get_task</code>" in traces
    assert "<code>github</code>" in traces  # a device names itself over the read tool
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


def test_a_model_switch_mid_turn_lands_in_order_with_its_name(rendered, monkeypatch):
    """A notice sent straight to the platform jumped ahead of traces still waiting to go out."""
    from ompwire import engine as engine_mod

    original = engine_mod.ChatAgent.ask

    async def ask(self, *args, **kwargs):
        self.options = {"model": {"id": "model", "currentValue": "a/one", "options": [
            {"value": "a/one", "name": "Model One"}, {"value": "a/two", "name": "Model Two"}]}}
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(engine_mod.ChatAgent, "ask", ask)
    messages = rendered([
        kinded_call("t1", "execute", "ls", {"command": "ls"}),
        tool_progress("t1", "", status="completed"),
        Update(kind="config_option_update", session_id="s",
               raw={"event": "model_changed", "model": {"provider": "a", "id": "two"}}),
        message("selesai"),
    ])
    order = [i for i, body in enumerate(messages) if "<code>ls</code>" in body or "$ ls" in body]
    switch = [i for i, body in enumerate(messages) if "omp switched model" in body]
    assert switch and order and order[0] < switch[0], messages
    assert "Model One" in messages[switch[0]] and "Model Two" in messages[switch[0]]


def test_a_quiet_chat_gets_the_answer_and_nothing_else(rendered):
    """A group runs quiet: no trace, checklist, reasoning or usage line, only what was asked for."""
    messages = rendered([
        thought("mari saya cek"),
        kinded_call("t1", "think", "Planning", {"op": "init"}),
        tool_progress("t1", "", status="completed"),
        plan(("cek server", "in_progress")),
        tool_call("c1", "Running ls"),
        tool_progress("c1", "ok", status="completed"),
        message("server aman"),
    ], verbose=False)
    assert messages == ["server aman"], messages


class SlowFirstFrameClient(FakeClient):
    """A first frame a moment in, then a silence longer than one quiet window."""

    def __init__(self, updates, quiet_seconds, first_frame_at=0.3):
        super().__init__(updates)
        self.quiet_seconds = quiet_seconds
        self.first_frame_at = first_frame_at

    def prompt(self, _text, images=None, timeout=900.0):
        import time as _time

        _time.sleep(self.first_frame_at)  # the turn opens with no frame at all
        for update in self.updates[:1]:
            self.on_update(update)
        _time.sleep(self.quiet_seconds)  # model-side silence: no frames
        for update in self.updates[1:]:
            self.on_update(update)
        return PromptResult(stop_reason="end_turn", usage={}, text="done", updates=[])


def test_quiet_notice_fires_during_silence_with_last_action():
    config = load_config(env={})
    config.data["stream"]["quiet_minutes"] = 0.01  # 0.6 s
    config.data["stream"]["min_gap"] = 0.0
    bot = FakeBot()

    async def go():
        agent = telegram.chat_agent(config, 1, key="quiet", app=types.SimpleNamespace(bot=bot))
        agent.client = SlowFirstFrameClient(
            [tool_call("c1", "running tests", command="pytest -q && echo <done>"),
             message("done")], quiet_seconds=1.6)
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        await agent.ask("halo")
        return bot.messages

    messages = asyncio.run(go())
    notices = [body for body in messages if "still working" in body]
    assert notices, messages
    assert "pytest -q &amp;&amp; echo &lt;done&gt;" in notices[0]  # escaped: the body is HTML
    assert "&" not in notices[0].replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")


def test_quiet_notice_stays_silent_while_frames_flow():
    config = load_config(env={})
    config.data["stream"]["quiet_minutes"] = 0.01  # 0.6 s
    config.data["stream"]["min_gap"] = 0.0
    bot = FakeBot()
    frames = [tool_call("c1", "step"), tool_update("c1", "step"),
              tool_update("c1", "step"), message("done")]

    async def go():
        agent = telegram.chat_agent(config, 1, key="busy", app=types.SimpleNamespace(bot=bot))
        agent.client = SpacedClient(frames, pause=0.05)
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        await agent.ask("halo")
        return bot.messages

    messages = asyncio.run(go())
    assert not any("still working" in body for body in messages), messages


def test_the_quiet_notice_is_a_heartbeat_that_keeps_reporting():
    """Every quiet window says the turn is alive, with what it has done so far."""
    config = load_config(env={})
    config.data["stream"]["quiet_minutes"] = 0.01  # 0.6 s
    config.data["stream"]["min_gap"] = 0.0
    bot = FakeBot()

    async def go():
        agent = telegram.chat_agent(config, 1, key="stretch", app=types.SimpleNamespace(bot=bot))
        agent.client = SlowFirstFrameClient(
            [tool_call("c1", "waiting"), message("done")], quiet_seconds=2.4)
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        await agent.ask("halo")
        return bot.messages

    notices = [body for body in asyncio.run(go()) if "still working" in body]
    assert len(notices) >= 2, notices  # a heartbeat, not one alarm
    assert all("tool" in body for body in notices)  # what it has done so far
    assert notices[-1] != notices[0]  # and it moves as the turn goes on


class EditableBot(FakeBot):
    """A bot whose cards can be edited: retiring a question rewrites the message it sent."""

    async def send_message(self, chat_id, text, **_kw):
        await super().send_message(chat_id, text, **_kw)

        class Sent:
            async def edit_text(self, body, **_kwargs):
                self.body = body

        return Sent()


class AskClient(FakeClient):
    """A turn that asks a question, then works quietly once the chat has answered it."""

    def __init__(self, updates, after_seconds=2.0):
        super().__init__(updates)
        self.on_dialog = None
        self.after_seconds = after_seconds

    def prompt(self, _text, images=None, timeout=900.0):
        import time as _time

        self.on_update(self.updates[0])  # one tool call, then the agent wants an answer
        self.on_dialog({"method": "ask", "questions": [
            {"id": "q1", "question": "Choosing reference code format",
             "options": [{"label": "Full random 12"}, {"label": "NOOV + 8 random"}]}]})
        _time.sleep(self.after_seconds)  # model-side silence after the answer
        self.on_update(self.updates[1])
        return PromptResult(stop_reason="end_turn", usage={}, text="done", updates=[])


def _ask_turn(bot, config, *, key, answer_after=1.2, after_seconds=2.0):
    """One turn whose ask is answered after `answer_after` seconds; returns the chat's messages."""

    async def go():
        agent = telegram.chat_agent(config, 1, key=key, app=types.SimpleNamespace(bot=bot))
        client = AskClient([tool_call("c1", "waiting"), message("done")],
                           after_seconds=after_seconds)
        agent.client = client
        agent.client.on_update = agent._on_update
        agent.client.on_dialog = agent._on_dialog
        agent.session_id = "s"

        async def answer() -> None:
            for _ in range(500):
                if agent.pending_ask is not None:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("the ask never opened")
            await asyncio.sleep(answer_after)  # a person reading the card, nothing running
            await agent.answer_ask(None, option_index=0)

        task = asyncio.create_task(answer())
        await agent.ask("halo")
        await task
        return bot.messages

    return asyncio.run(go())


def _quiet_config(seconds=0.01):
    config = load_config(env={})
    config.data["stream"]["quiet_minutes"] = seconds  # 0.6 s
    config.data["stream"]["min_gap"] = 0.0
    config.data.setdefault("approval", {})["timeout"] = 30.0
    return config


def test_no_heartbeat_while_the_turn_waits_for_an_answer():
    """A question card on screen means nothing is running behind it, so ⏳ still working is a
    lie that repeats the same line every window until somebody answers. Reported from a live
    chat: eight identical heartbeats under one ask."""
    bot = EditableBot()
    # four windows pass with the card on screen, then the answer ends the turn straight away
    messages = _ask_turn(bot, _quiet_config(), key="waiting-ask",
                         answer_after=2.4, after_seconds=0.2)

    notices = [body for body in messages if "still working" in body]
    assert not notices, notices
    assert any("Choosing reference code format" in body for body in messages)


def test_the_heartbeat_comes_back_once_the_answer_is_in():
    """The wait is not silence to report on, and it must not poison the clock either: a quiet
    stretch after the answer is a quiet stretch."""
    bot = EditableBot()
    messages = _ask_turn(bot, _quiet_config(), key="answered-then-quiet",
                         answer_after=0.3, after_seconds=2.4)

    notices = [body for body in messages if "still working" in body]
    assert notices, messages


def test_a_timed_out_approval_leaves_nothing_waiting():
    """The slot stayed in the table after the deadline, so every later read said a person was
    still deciding: the heartbeat it now gates would never come back."""
    config = load_config(env={})
    config.data.setdefault("approval", {})["timeout"] = 0.3
    config.data.setdefault("agent", {})["yolo"] = False
    expired: list[bool] = []

    async def go():
        agent = telegram.chat_agent(config, 1, key="perm-timeout",
                                    app=types.SimpleNamespace(bot=EditableBot()))
        agent.surface.settle_permission = _recording_settle(expired)
        # what ensure() seeds from get_state; without it the reader thread falls back to the
        # store for the mode, and its sqlite connection belongs to this thread
        agent.options = {"mode": {"currentValue": "default"}}
        frame = {"method": "select", "title": "Allow tool: write\nPath: /tmp/x.txt",
                 "options": ["Approve", "Deny"]}
        answer = await asyncio.to_thread(agent._on_permission, frame)
        await asyncio.sleep(0.1)  # the expiry card is written on the loop
        return agent, answer

    agent, answer = asyncio.run(go())
    assert answer is None  # a timeout denies
    assert agent._pending_perm == {}
    assert expired == [True]


def _recording_settle(seen: list[bool]):
    async def settle(_handle, _kind, _detail, _outcome, expired: bool) -> None:
        seen.append(expired)

    return settle


def test_the_heartbeat_counts_this_turn_not_the_session():
    """`tools_used` is the session's lifetime total, so a turn that had done one thing reported
    "94 tools" and read like work nobody had started."""
    config = load_config(env={})
    config.data["stream"]["quiet_minutes"] = 0.01  # 0.6 s
    config.data["stream"]["min_gap"] = 0.0
    bot = FakeBot()

    async def go():
        agent = telegram.chat_agent(config, 1, key="per-turn", app=types.SimpleNamespace(bot=bot))
        agent.tools_used = 94  # what earlier turns in this session spent
        agent.client = SlowFirstFrameClient(
            [tool_call("c1", "running tests"), message("done")], quiet_seconds=1.6)
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        await agent.ask("halo")
        return bot.messages

    notices = [body for body in asyncio.run(go()) if "still working" in body]
    assert notices, "the heartbeat must still fire"
    assert "94 tools" not in notices[0], notices[0]
    assert "1 tool" in notices[0], notices[0]
