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
