"""The ask flow: option, free text, skip and timeout become exact bodies to omp; a follow_up
while idle reports promptRequired; agent_args forces rpc-ui; a retry event renders only in a
verbose chat; btw frames never reach the turn."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import textwrap
import threading
import types

from ompwire import engine, telegram
from ompwire.config import load_config
from ompwire.rpc import AgentClient, PromptResult, Update

# --------------------------------------------------------------- agent_args → rpc-ui

def test_agent_args_maps_every_older_mode_to_rpc_ui():
    config = load_config(env={})

    def set_args(value):
        config.data["acp"]["args"] = value

    for older, want in ((["--mode", "rpc"], ["--mode", "rpc-ui"]),
                        (["--mode", "acp"], ["--mode", "rpc-ui"]),
                        (["--mode=acp"], ["--mode=rpc-ui"]),
                        (["--mode=rpc"], ["--mode=rpc-ui"])):
        set_args(older)
        assert engine.agent_args(config) == want, older
    set_args([])
    assert engine.agent_args(config) == ["--mode", "rpc-ui"]
    set_args(["--mode", "rpc-ui", "--no-session"])
    assert engine.agent_args(config) == ["--mode", "rpc-ui", "--no-session"]


# --------------------------------------------------------------- ask flow on a ChatAgent

class FakeSurface(engine.Surface):
    label = "test"
    limit = 4000
    streams = False

    def __init__(self):
        self.cards: list[tuple[list[str], str]] = []
        self.retired: list[str] = []

    async def post(self, body, *, kind):
        return True

    def trace(self, t):
        return ""

    def reasoning(self, text):
        return text

    def advisor(self, notes):
        return "\n".join(note["note"] for note in notes)

    def plan(self, items):
        return ""

    def job(self, card):
        return ""

    def jobs(self, group, spawned):
        return ""

    def dropped(self, count):
        return ""

    def join(self, blocks):
        return "\n".join(blocks)

    @contextlib.asynccontextmanager
    async def typing(self):
        yield

    async def offer_options(self, options, question=""):
        self.cards.append((list(options), question))

    async def retire_options(self, note):
        self.retired.append(note)

    async def ask_permission(self, *a):
        return None

    async def settle_permission(self, *a):
        return None

    async def send_file(self, *a, **k):
        return None

    async def offer_schedule(self, *a):
        return None

    async def notice(self, text):
        return None


class DialogClient:
    """prompt() opens an ask and blocks until the dialog callback answers it."""

    alive = True

    def __init__(self, questions=None):
        self.on_dialog = None
        self.bodies: list[dict] = []
        self.questions = questions or [{"id": "q1", "question": "which one?",
                                        "options": [{"label": "alpha"}, {"label": "beta"}]}]

    def state(self):
        return {}

    def prompt(self, _text, images=None, timeout=900.0):
        self.bodies.append(self.on_dialog({"method": "ask", "questions": self.questions}))
        return PromptResult(stop_reason="end_turn", usage={}, text="done", updates=[])
    def follow_up(self, text, images=None, timeout=30.0):
        # no turn runs here, which is exactly what the real client reports while idle
        return "promptRequired", ""

    def abort(self):
        return None

    def close(self):
        return None


def _agent(surface, client, config):
    agent = engine.ChatAgent(config, "test", surface)
    agent.verbose = True
    agent.client = client
    # the client normally gets these from _build_client; a stand-in must be wired the same way
    client.on_dialog = agent._on_dialog
    client.on_update = agent._on_update
    agent.session_id = "s"
    return agent


def _config(tmp_path, approval_timeout=None):
    config = load_config(env={})
    config.data["session"]["store"] = str(tmp_path / "s.db")
    config.data.setdefault("approval", {})["timeout"] = approval_timeout or 2.0
    return config


def _run_ask(tmp_path, tap, *, timeout=None, questions=None):
    """One turn whose ask is answered by `tap` once the card shows; returns (bodies, surface)."""
    surface = FakeSurface()
    client = DialogClient(questions)
    config = _config(tmp_path, approval_timeout=timeout)

    async def go():
        agent = _agent(surface, client, config)

        async def open_dialog():
            for _ in range(500):
                if agent.pending_ask is not None:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("the ask never opened")
            await asyncio.sleep(0.05)  # the card is offered on the loop; let it land
            if callable(tap):
                await tap(agent)

        task = asyncio.create_task(asyncio.to_thread(client.prompt, "choose"))
        await open_dialog()
        return await asyncio.wait_for(task, 5)

    asyncio.run(go())
    return client.bodies, surface


def test_ask_answered_by_option_sends_selected_option(tmp_path):
    async def tap(agent):
        await agent.answer_ask(None, option_index=1)

    bodies, surface = _run_ask(tmp_path, tap)
    assert bodies == [{"answers": [{"id": "q1", "selectedOptions": ["beta"]}]}]
    assert surface.cards[0][0] == ["alpha", "beta"]
    assert surface.cards[0][1] == "which one?"
    assert surface.retired, "the options card must be retired after the answer"


def test_ask_answered_by_free_text_sends_custom_input(tmp_path):
    async def tap(agent):
        await agent.answer_ask("lain saja")

    bodies, _ = _run_ask(tmp_path, tap)
    assert bodies == [{"answers": [{"id": "q1", "selectedOptions": [],
                                    "customInput": "lain saja"}]}]


def test_ask_skipped_takes_the_recommended_option_and_keeps_the_turn(tmp_path):
    """Measured on omp 18.12.6: a bare `cancelled` aborts the whole turn, while
    `cancelled` + `timedOut` makes omp answer with the recommended option (else the first)
    and carry on. Skip promises the second."""
    async def tap(agent):
        await agent.answer_ask(None, skip=True)

    bodies, _ = _run_ask(tmp_path, tap)
    assert bodies == [{"cancelled": True, "timedOut": True}]


def test_ask_timed_out_reports_timedout(tmp_path):
    bodies, surface = _run_ask(tmp_path, None, timeout=0.4)
    assert bodies == [{"cancelled": True, "timedOut": True}]
    assert surface.retired


def test_multi_question_ask_answers_each_in_order(tmp_path):
    async def tap(agent):
        # answer question one; question two's card then shows and is answered too
        await agent.answer_ask(None, option_index=0)
        while len(agent.pending_options or []) and agent.pending_ask and \
                agent.pending_ask["index"] == 1:
            await asyncio.sleep(0.01)
            if agent.pending_ask["index"] == 1:
                await agent.answer_ask("teks kedua")
                break

    questions = [{"id": "q1", "question": "first?", "options": [{"label": "alpha"}]},
                 {"id": "q2", "question": "second?", "options": [{"label": "beta"}]}]

    async def go():
        surface = FakeSurface()
        client = DialogClient(questions)
        config = _config(tmp_path)

        async def open_dialog():
            while agent.pending_ask is None:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # the first card is up; answer both in order
            await agent.answer_ask(None, option_index=0)
            for _ in range(200):
                if agent.pending_ask and agent.pending_ask["index"] == 1:
                    await agent.answer_ask("teks kedua")
                    return
                await asyncio.sleep(0.01)

        agent = _agent(surface, client, config)
        task = asyncio.create_task(asyncio.to_thread(client.prompt, "choose"))
        await open_dialog()
        await asyncio.wait_for(task, 5)
        assert client.bodies == [{"answers": [{"id": "q1", "selectedOptions": ["alpha"]},
                                              {"id": "q2", "selectedOptions": [],
                                               "customInput": "teks kedua"}]}]

    asyncio.run(go())


# --------------------------------------------------------------- follow_up when idle

def test_follow_up_when_idle_reports_prompt_required(tmp_path):
    async def go():
        agent = _agent(FakeSurface(), DialogClient(), _config(tmp_path))
        return await agent.follow_up("nanti")

    assert asyncio.run(go()) == ("promptRequired", "")


# --------------------------------------------------------------- event rendering

class EventClient:
    alive = True

    def __init__(self, updates):
        self.updates = updates
        self.on_update = None

    def state(self):
        return {}

    def prompt(self, _text, images=None, timeout=900.0):
        for update in self.updates:
            if self.on_update is not None:
                self.on_update(update)
        text = "".join(u.text for u in self.updates if u.kind == "agent_message_chunk")
        return PromptResult(stop_reason="end_turn", usage={}, text=text, updates=[])

    def abort(self):
        return None

    def close(self):
        return None


def _retry():
    return Update(kind="retry", session_id="s",
                  raw={"type": "auto_retry_start", "attempt": 2, "maxAttempts": 3,
                       "delayMs": 5000, "errorMessage": "boom"})


def _message(text):
    return Update(kind="agent_message_chunk", session_id="s",
                  raw={"content": {"type": "text", "text": text}})


def _render(tmp_path, updates, verbose, *, advisor=True):
    config = _config(tmp_path)
    config.data.setdefault("stream", {})["min_gap"] = 0.0
    bot_messages: list[str] = []

    class Bot:
        async def send_message(self, _chat, text, **_kw):
            bot_messages.append(text)
            return types.SimpleNamespace()

    class TgSurface(telegram.TelegramSurface):
        def __init__(self):
            self.app = types.SimpleNamespace(bot=Bot())
            self.chat_id = 1
            self.label = "test"
            self.limit = 4000
            self.streams = False

        async def ask_permission(self, *a):
            return None

    async def go():
        surface = TgSurface.__new__(TgSurface)
        surface.app = types.SimpleNamespace(bot=Bot())
        surface.chat_id = 1
        surface.label = "test"
        surface.limit = 4000
        surface.streams = False
        agent = engine.ChatAgent(config, "t", surface)
        agent.verbose = verbose
        agent.advisor = advisor
        agent.client = EventClient(updates)
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        await agent.ask("halo")
        return bot_messages

    return asyncio.run(go())


def test_a_retry_event_shows_in_a_verbose_chat_in_order(tmp_path):
    messages = _render(tmp_path, [_message("sebentar"), _retry(), _message("selesai")], True)
    notice = next(m for m in messages if "retry 2/3" in m)
    assert "🔁 retry 2/3 in 5s · boom" in notice
    assert messages.index(notice) > 0


def test_a_retry_event_is_silent_in_a_quiet_chat(tmp_path):
    messages = _render(tmp_path, [_message("sebentar"), _retry(), _message("selesai")], False)
    assert not any("retry" in m for m in messages)


def test_marker_options_carry_their_question_onto_the_card(tmp_path):
    """The card said "pick an answer", and the question was somewhere up the chat."""
    messages = _render(tmp_path, [_message("Two ways.\n\nWhich one?\n<<options: a | b>>")], False)
    card = next(m for m in messages if "1.</b> a" in m)
    assert "❓ Which one?" in card


def test_answering_with_other_keeps_the_question_on_the_card(tmp_path):
    edits: list[str] = []

    class Sent:
        async def edit_text(self, text, **_kw):
            edits.append(text)

    class Bot:
        async def send_message(self, _chat, text, **_kw):
            return Sent()

    surface = telegram.TelegramSurface.__new__(telegram.TelegramSurface)
    surface.app = types.SimpleNamespace(bot=Bot())
    surface.chat_id = 1
    surface.options_message = None
    surface.options_listed = ""
    surface.options_question = ""

    async def go():
        await surface.offer_options(["a", "b"], question="Which one?")
        await surface.retire_options("type your answer; your next message is it")

    asyncio.run(go())
    assert edits and "❓ Which one?" in edits[0]
    assert "type your answer" in edits[0]


def _start():
    return Update(kind="agent_start", session_id="s", raw={})


def _end(terminal=True):
    return Update(kind="agent_end", session_id="s",
                  raw={"isTerminal": terminal, "yielded": True})


def _tool(call_id, command):
    return Update(kind="tool_call", session_id="s", raw={
        "toolCallId": call_id, "toolName": "bash", "kind": "bash", "title": call_id,
        "status": "completed", "rawInput": {"command": command}})


def _frame(update):
    """The omp RPC frame(s) an Update stands for."""
    if update.kind == "agent_message_chunk":
        return {"type": "message_update", "messageId": "m",
                "assistantMessageEvent": {"type": "text_delta", "delta": update.text}}
    if update.kind == "tool_call":
        raw = update.raw
        return [{"type": "tool_execution_start", "toolCallId": raw["toolCallId"],
                 "toolName": raw["toolName"], "args": raw["rawInput"]},
                {"type": "tool_execution_end", "toolCallId": raw["toolCallId"],
                 "toolName": raw["toolName"],
                 "result": {"content": [{"type": "text", "text": "ok"}]}, "isError": False}]
    return {"type": update.kind, **update.raw}


class _Chat:
    """A chat on a real ChatAgent and Telegram surface; `sent` is every posted message."""

    def __init__(self, tmp_path, prompt_updates):
        config = _config(tmp_path)
        config.data.setdefault("stream", {})["min_gap"] = 0.0
        self.sent: list[str] = []
        self.typing = 0
        chat = self

        class Bot:
            async def send_message(self, _chat, text, **_kw):
                chat.sent.append(text)
                return types.SimpleNamespace()

            async def send_chat_action(self, *_a, **_kw):
                chat.typing += 1

        surface = telegram.TelegramSurface.__new__(telegram.TelegramSurface)
        surface.app = types.SimpleNamespace(bot=Bot())
        surface.chat_id, surface.label, surface.limit, surface.streams = 1, "t", 4000, False
        self.agent = engine.ChatAgent(config, "t", surface)
        self.agent.verbose = True
        # omp wraps every run it is prompted for in agent_start … agent_end
        self.agent.client = EventClient([_start(), *prompt_updates, _end()])
        self.agent.client.on_update = self.agent._on_update
        self.agent.session_id = "s"

    def feed(self, *updates):
        """Frames as omp writes them, through the real client: what rpc.py forwards is what the
        engine sees, so a frame type the client drops is dropped here too."""
        reader = AgentClient(on_update=self.agent._on_update)
        for update in updates:
            frames = _frame(update)
            for frame in frames if isinstance(frames, list) else [frames]:
                reader._dispatch(frame)


def test_a_run_the_agent_starts_on_its_own_posts_while_it_runs(tmp_path):
    """A background job's result woke the agent between turns. Its messages waited, first for
    the person's next message (a burst of old tool cards), then for the run to end (minutes of
    silence in the chat while the agent was working)."""
    async def go():
        chat = _Chat(tmp_path, [_message("jawaban baru")])
        chat.feed(_start(), _message("hasil job"), _tool("bg1", "echo from-the-job"))
        await asyncio.sleep(0.1)
        while_running = list(chat.sent)
        chat.feed(_end())
        await asyncio.sleep(0.1)
        await chat.agent.ask("pesan berikutnya")
        return while_running, chat

    while_running, chat = asyncio.run(go())
    assert any("hasil job" in m for m in while_running), "text posted before the run ended"
    assert any("from-the-job" in m for m in while_running), "tool card posted before it ended"
    assert chat.typing > 0, "the chat shows the agent working"
    assert chat.sent[-1] == "jawaban baru"
    assert sum("jawaban baru" in m for m in chat.sent) == 1


def test_a_frame_of_the_agents_own_run_during_a_prompts_hand_over_is_not_lost(tmp_path):
    """The person writes while the agent's own run streams. omp queues the prompt behind that
    run (streamingBehavior followUp), so the run's last frames arrive while ask() is already
    under way. In 0.17.x such a frame opened a second renderer on the turn queue; two drains
    then shared one queue and the prompt's answer was never posted."""
    async def go():
        chat = _Chat(tmp_path, [])
        reader = AgentClient(on_update=chat.agent._on_update)
        for frame in (_frame(_start()), _frame(_message("awal job"))):
            reader._dispatch(frame)
        await asyncio.sleep(0.05)

        class Queued(EventClient):
            def prompt(self, text, images=None, timeout=900.0):
                # omp's order: the old run streams on and ends, then the prompt's run starts
                for update in (_message(" ekor job"), _tool("bg2", "echo tail"), _end(),
                               _start(), _message("jawaban baru"), _end()):
                    frames = _frame(update)
                    for frame in frames if isinstance(frames, list) else [frames]:
                        reader._dispatch(frame)
                return PromptResult(stop_reason="end_turn", usage={}, text="jawaban baru",
                                    updates=[])

        chat.agent.client = Queued([])
        await asyncio.wait_for(chat.agent.ask("pesan berikutnya"), timeout=3)
        await asyncio.sleep(0.1)
        return chat

    chat = asyncio.run(go())
    assert chat.sent[-1] == "jawaban baru", chat.sent  # its own message, nothing glued on
    assert any("ekor job" in m for m in chat.sent[:-1]), chat.sent
    assert any("tail" in m for m in chat.sent[:-1]), chat.sent
    assert chat.agent._idle_run is None


def test_a_background_jobs_progress_posts_nothing_and_the_run_it_wakes_shows_typing(tmp_path):
    """omp's order, recorded from a real run: a background job of an earlier turn reports its
    output with no run going, then wakes a run of its own. Each report opened a run that
    posted the same "running" card and closed at once, so the chat got the card three times
    and the typing indicator never stayed up."""
    async def go():
        chat = _Chat(tmp_path, [])
        opened = []
        original = chat.agent._open_idle_run

        def counting():
            opened.append(1)
            original()

        chat.agent._open_idle_run = counting
        reader = AgentClient(on_update=chat.agent._on_update)
        progress = {"type": "tool_execution_update", "toolCallId": "job", "toolName": "bash",
                    "partialResult": {"content": [{"type": "text", "text": "probe-done"}]}}
        for _ in range(3):
            reader._dispatch(progress)
        await asyncio.sleep(0.1)
        quiet = (list(chat.sent), len(opened))
        for frame in ({"type": "agent_start"}, progress,
                      {"type": "message_update", "messageId": "m",
                       "assistantMessageEvent": {"type": "text_delta", "delta": "mengecek"}}):
            reader._dispatch(frame)
        await asyncio.sleep(0.1)
        typing_while_running = chat.typing
        reader._dispatch({"type": "agent_end", "isTerminal": True})
        await asyncio.sleep(0.1)
        return quiet, opened, typing_while_running, chat

    quiet, opened, typing_while_running, chat = asyncio.run(go())
    assert quiet == ([], 0), "progress of a job nobody is running a turn for posts nothing"
    assert len(opened) == 1
    assert typing_while_running > 0
    assert chat.sent == ["mengecek"]
    assert chat.agent._idle_run is None


def test_work_left_running_keeps_the_chat_typing_until_it_reports_back(tmp_path):
    """A bash job or a subagent the agent did not wait for: the turn ends ("mulai"), the work
    goes on, and its result wakes the agent later. The chat showed no typing in between, so it
    looked done while the request was still being worked on. omp flags both kinds the same way
    (get_state hasPendingAsyncWork), and the wake's agent_start ends the wait."""
    async def go():
        chat = _Chat(tmp_path, [_message("mulai")])
        pending = {"hasPendingAsyncWork": True}
        chat.agent.client.state = lambda: dict(pending)
        chat.agent._sleep = lambda _s: asyncio.sleep(0.01)  # recheck at once, not every 30s
        await chat.agent.ask("jalankan di background")
        await asyncio.sleep(0.05)
        typing_after_turn = chat.typing
        waiting = chat.agent._pending_wait is not None and not chat.agent._pending_wait.done()
        chat.feed(_start(), _message("sls"), _end())
        await asyncio.sleep(0.1)
        woke = chat.agent._pending_wait
        # the wake ran and nothing is left: the next end leaves the chat quiet
        pending["hasPendingAsyncWork"] = False
        return typing_after_turn, waiting, woke, chat

    typing_after_turn, waiting, woke, chat = asyncio.run(go())
    assert waiting, "typing holds after the turn while the job runs"
    assert typing_after_turn > 0
    assert woke is None or woke.done(), "the wake's agent_start ended the wait"
    assert chat.sent == ["mulai", "sls"]


def test_a_turn_that_leaves_nothing_running_does_not_hold_typing(tmp_path):
    async def go():
        chat = _Chat(tmp_path, [_message("ok")])
        chat.agent.client.state = lambda: {"hasPendingAsyncWork": False}
        await chat.agent.ask("halo")
        return chat.agent._pending_wait

    assert asyncio.run(go()) is None


def _background_frames(command, wall_ms):
    """omp's frames for one bash call it moved to the background, as recorded from a real run:
    the call's start and end inside the turn, then progress with no run going, the last one
    with state completed and the wall time."""
    call = "call_1|fc_call_1"
    running = {"async": {"state": "running", "jobId": "bg_1", "type": "bash"}}
    start = [{"type": "tool_execution_start", "toolCallId": call, "toolName": "bash",
              "args": {"async": True, "command": command}},
             {"type": "tool_execution_end", "toolCallId": call, "toolName": "bash",
              "result": {"content": [{"type": "text", "text": "Backgrounded as job bg_1"}],
                         "details": {**running, "timeoutSeconds": 300}}}]

    def progress(text, state="running"):
        details = {"async": {"state": state, "jobId": "bg_1", "type": "bash"}}
        if state != "running":
            details["wallTimeMs"] = wall_ms
        return {"type": "tool_execution_update", "toolCallId": call, "toolName": "bash",
                "args": {"async": True, "command": command},
                "partialResult": {"content": [{"type": "text", "text": text}],
                                  "details": details}}

    return start, progress


def test_a_background_job_has_one_card_that_moves_while_it_runs_and_closes_when_it_ends(
        tmp_path):
    """A bash job the agent did not wait for: its progress came with no run going and was
    dropped, so the chat could not see what it was doing. It now has one card: running with
    the newest output while it works, at most once a minute, and done with its time at once."""
    async def go():
        chat = _Chat(tmp_path, [])
        now = [0.0]
        chat.agent._clock = lambda: now[0]
        ticks = asyncio.Event()

        async def sleep(_s):
            await ticks.wait()
            ticks.clear()

        chat.agent._sleep = sleep
        start, progress = _background_frames("./build.sh", 95_000)
        reader = AgentClient(on_update=chat.agent._on_update)
        chat.agent._prompt_pending = True  # the call's start and end belong to a turn
        chat.agent.busy = True
        for frame in start:
            reader._dispatch(frame)
        await asyncio.sleep(0.05)
        chat.agent.busy = chat.agent._prompt_pending = False
        opened = list(chat.sent)
        now[0] = 30.0
        reader._dispatch(progress("compiling 3/10"))
        await asyncio.sleep(0.05)
        within_a_minute = list(chat.sent)
        now[0] = 65.0
        reader._dispatch(progress("compiling 9/10"))
        await asyncio.sleep(0.05)
        after_a_minute = list(chat.sent)
        now[0] = 95.0
        reader._dispatch(progress("compiling 10/10\nok\n\n\nWall time: 95.00 seconds",
                                  state="completed"))
        await asyncio.sleep(0.05)
        return opened, within_a_minute, after_a_minute, chat

    opened, within_a_minute, after_a_minute, chat = asyncio.run(go())
    cards = [m for m in chat.sent if "build.sh" in m]
    assert len(opened) == 1 and "bg_1 · running · 0s" in opened[0]
    assert within_a_minute == opened, "a running card moves at most once a minute"
    assert "compiling 9/10" in after_a_minute[-1] and "running · 1m05s" in after_a_minute[-1]
    assert "bg_1 · done · 1m35s" in cards[-1] and "ok" in cards[-1]
    assert "Wall time" not in cards[-1], "the status line already says how long it ran"
    assert len(cards) == 3
    assert chat.agent._background == {}
    assert chat.agent._idle_run is None, "job frames never open a run of their own"


def test_a_background_job_shows_no_card_in_a_quiet_chat(tmp_path):
    async def go():
        chat = _Chat(tmp_path, [])
        chat.agent.verbose = False
        start, progress = _background_frames("sleep 5", 5_000)
        reader = AgentClient(on_update=chat.agent._on_update)
        for frame in [*start, progress("x", state="completed")]:
            reader._dispatch(frame)
        await asyncio.sleep(0.05)
        return chat

    chat = asyncio.run(go())
    assert not any("sleep 5" in m for m in chat.sent)
    assert chat.agent._background == {}


class _JobsClient(EventClient):
    """omp with background work running: get_state says so until /jobs kill all stops it.
    `calls` records what reached omp, in order, so a test can see the kill land after the
    turn's abort and never inside the turn."""

    def __init__(self, updates, *, pending=True, during_turn=None):
        super().__init__(updates)
        self.pending = pending
        self.calls: list[str] = []
        self.during_turn = during_turn

    def state(self):
        return {"hasPendingAsyncWork": self.pending}

    def abort(self):
        self.calls.append("abort")

    in_turn = False

    def prompt(self, text, images=None, timeout=900.0):
        if text == "/jobs kill all":
            # mid-turn, omp would queue this behind the run and replay it as a prompt
            self.calls.append("/jobs kill all (inside the turn)" if self.in_turn else text)
            self.pending = False
            return PromptResult(stop_reason="end_turn", usage={}, text="", updates=[])
        self.calls.append(text)
        self.in_turn = True
        try:
            if self.during_turn is not None:
                self.during_turn()
            return super().prompt(text, images, timeout)
        finally:
            self.in_turn = False


def test_stop_between_turns_stops_the_background_jobs_and_subagents(tmp_path):
    """omp's abort ends a turn only; a job the agent left running kept going, kept the chat
    typing and woke the agent again after /stop."""
    async def go():
        chat = _Chat(tmp_path, [])
        client = _JobsClient([])
        chat.agent.client = client
        chat.agent._sleep = lambda _s: asyncio.sleep(3600)
        await chat.agent._wait_on_pending_work()
        typing_before = chat.agent._pending_wait is not None
        stopped = await chat.agent.cancel()
        await asyncio.sleep(0)
        return typing_before, stopped, client, chat.agent

    typing_before, stopped, client, agent = asyncio.run(go())
    assert typing_before
    assert stopped is True
    assert client.calls == ["/jobs kill all"]
    assert client.pending is False
    assert agent._pending_wait is None, "the typing held for the jobs stops with them"


def test_stop_mid_turn_kills_the_jobs_only_after_the_turn_has_ended(tmp_path):
    """Sent while the turn still runs, the command queues behind it (prompts are followUp) and
    comes back as a prompt; it has to wait for the turn to be gone."""
    async def go():
        chat = _Chat(tmp_path, [])
        client = None

        def stop_now():
            # /stop arrives while the turn is running: the prompt waits here, on the agent's
            # thread, until the loop has handled the whole /stop
            done = threading.Event()

            async def stop():
                await chat.agent.cancel()
                done.set()

            chat.agent._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(stop()))
            assert done.wait(5)

        client = _JobsClient([_start(), _message("kerja"), _end()], during_turn=stop_now)
        client.on_update = chat.agent._on_update
        chat.agent.client = client
        await chat.agent.ask("mulai kerja")
        await asyncio.sleep(0.05)
        return client, chat.agent

    client, agent = asyncio.run(go())
    assert client.calls == ["mulai kerja", "abort", "/jobs kill all"]
    assert agent._kill_jobs_after_turn is False
    assert agent._pending_wait is None, "nothing is left to show typing for"


def test_stop_with_nothing_running_reports_nothing(tmp_path):
    async def go():
        chat = _Chat(tmp_path, [])
        client = _JobsClient([], pending=False)
        chat.agent.client = client
        return await chat.agent.cancel(), client.calls

    stopped, calls = asyncio.run(go())
    assert stopped is False
    assert calls == []


def test_a_non_terminal_end_closes_the_run_and_the_next_wake_opens_another(tmp_path):
    """omp ends a run with isTerminal=false while a background job is pending, then starts a
    new run when the job reports: two runs, two sets of messages, nothing carried over."""
    async def go():
        chat = _Chat(tmp_path, [])
        chat.feed(_start(), _message("run satu"), _end(terminal=False))
        await asyncio.sleep(0.1)
        first = list(chat.sent)
        chat.feed(_start(), _message("run dua"), _end())
        await asyncio.sleep(0.1)
        return first, chat

    first, chat = asyncio.run(go())
    assert first == ["run satu"]
    assert chat.sent == ["run satu", "run dua"]
    assert chat.agent._idle_run is None


def _advice(severity, note):
    return Update(kind="advisor", session_id="s",
                  raw={"notes": [{"severity": severity, "note": note}]})


def test_an_advisor_note_shows_in_order_as_a_code_block(tmp_path):
    """Asked for in chat: the advisor's notes went nowhere, unlike reasoning."""
    messages = _render(tmp_path, [_message("sebentar"),
                                  _advice("concern", "the 400 is from your own <probe>"),
                                  _message("selesai")], True)
    card = next(m for m in messages if "advisor" in m)
    assert "⚠️ <i>concern</i>\n<pre>the 400 is from your own &lt;probe&gt;</pre>" in card
    assert 0 < messages.index(card) < len(messages) - 1, "between the text around it"


def test_advisor_off_hides_the_notes(tmp_path):
    config_dir = tmp_path
    messages = _render(config_dir, [_message("ok"), _advice("nit", "x")], True,
                       advisor=False)
    assert not any("advisor" in m for m in messages)


def test_a_skill_change_names_the_action_and_the_skill(tmp_path):
    call = Update(kind="tool_call", session_id="s", raw={
        "toolCallId": "t1", "toolName": "manage_skill", "kind": "manage_skill",
        "title": "Recording the deploy findings",
        "rawInput": {"action": "update", "name": "superset-embed", "body": "..."},
        "status": "in_progress"})
    done = Update(kind="tool_call_update", session_id="s", raw={
        "toolCallId": "t1", "toolName": "manage_skill", "status": "completed",
        "rawOutput": {"content": [{"type": "text", "text": "Updated managed skill"}]}})
    messages = _render(tmp_path, [call, done, _message("done")], True)
    trace = next(m for m in messages if "manage_skill" in m)
    assert "🎓" in trace and "update superset-embed" in trace


# --------------------------------------------------------------- btw isolation

def test_btw_updates_never_reach_the_turn_renderer(tmp_path):
    updates = [_message("jawaban"),
               Update(kind="btw", session_id="s",
                      raw={"type": "btw_record",
                           "record": {"id": "r1", "status": "complete",
                                      "answer": "side answer"}})]
    messages = _render(tmp_path, updates, True)
    assert not any("side answer" in m for m in messages), messages


def test_btw_waits_for_the_final_record_not_the_running_one(tmp_path):
    """omp sends btw_record twice: `running` with an empty answer, then `complete`."""
    config = _config(tmp_path)

    def record(status, answer):
        return Update(kind="btw", session_id="s", raw={"type": "btw_record", "record": {
            "id": "r1", "status": status, "answer": answer}})

    class BtwClient:
        alive = True

        def btw(self, question):
            agent._on_update(record("running", ""))
            threading.Timer(0.2, lambda: agent._on_update(record("complete", "Four"))).start()
            return {"record": {"id": "r1", "status": "running", "answer": ""}}

    async def go():
        nonlocal agent
        agent = engine.ChatAgent(config, "test", FakeSurface())
        agent.client = BtwClient()
        agent.session_id = "s"
        return await agent.btw("2+2?")

    agent = None
    assert asyncio.run(go()) == "Four"


# --------------------------------------------------------------- set_ask_dialog on start

def test_start_sends_set_ask_dialog(tmp_path):
    log = tmp_path / "wire.log"
    script = tmp_path / "omp.py"
    script.write_text(textwrap.dedent(f"""
        import json, sys
        log = open({str(log)!r}, "w")
        def send(frame):
            sys.stdout.write(json.dumps(frame) + "\\n"); sys.stdout.flush()
        send({{"type": "ready"}})
        for line in sys.stdin:
            log.write(line)
            log.flush()
            msg = json.loads(line)
            send({{"type": "response", "id": msg.get("id"), "command": msg.get("type"),
                  "success": True, "data": {{}}}})
    """))
    client = AgentClient(command=sys.executable, args=[str(script)], startup_timeout=10.0)
    client.start()
    client.close()
    lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    found = [line for line in lines if line.get("type") == "set_ask_dialog"]
    assert found and found[0].get("enabled") is True, lines
