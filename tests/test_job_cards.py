"""Subagents as live cards: what the chat shows while the `task` tool's jobs run.

The wire is measured in rpc.py: omp is subscribed with `set_subagent_subscription {level:
"progress"}` and every subagent frame becomes an `Update(kind="subagent")`. This file covers the
chat side: one summary per `task` call, one card per job, a minute between edits, and the last
state of a job always shown. Cards are edited, not reposted, so a surface needs the clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import types

from ompwire import cli, engine, tg_surface, wa_surface
from ompwire.config import load_config
from ompwire.format import TOOL_LINE_WIDTH
from ompwire.rpc import PromptResult, Update

PARENT = "toolu_parent"
MODEL = "omniroute/coding-worker:xhigh"


# ------------------------------------------------------------------ the frames
def lifecycle(name, status="started", *, parent=PARENT, agent="task", detached=True):
    """The frame omp sends when a subagent starts and when it ends."""
    return Update(kind="subagent", session_id="s", raw={
        "type": "subagent_lifecycle",
        "payload": {"id": name, "agent": agent, "agentSource": "bundled", "status": status,
                    "parentToolCallId": parent, "detached": detached,
                    "sessionFile": "/tmp/omp/session.jsonl", "index": 0}})


def progress(name, *, parent=PARENT, status="running", intent="Counting py files", tool="bash",
             args="find . -name '*.py'", tools=1, ms=6562, model=MODEL):
    """One progress frame; omp sends 16-19 of them for a short subagent."""
    return Update(kind="subagent", session_id="s", raw={
        "type": "subagent_progress",
        "payload": {"index": 0, "agent": "task", "task": "count the python files",
                    "assignment": "count", "parentToolCallId": parent,
                    "progress": {"id": name, "agent": "task", "status": status,
                                 "lastIntent": intent,
                                 "recentTools": [{"tool": tool, "args": args, "intent": intent,
                                                  "isError": False, "endMs": 1}],
                                 "recentOutput": ["6"], "toolCount": tools, "requests": 2,
                                 "tokens": 7274, "cost": 0, "durationMs": ms,
                                 "resolvedModel": model,
                                 "resolvedModelIdentity": "omniroute/coding-worker",
                                 "resolvedThinkingLevel": "xhigh", "modelRole": "task"}}})


def message(text):
    return Update(kind="agent_message_chunk", session_id="s",
                  raw={"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": text}})


class Clock:
    """The engine reads time through `_clock`, so a minute of throttling costs one line."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


class Cards(engine.Surface):
    """A surface that keeps its job cards instead of showing them."""

    label = "test"
    limit = 4000

    def __init__(self):
        self.cards: list[tuple[str, str]] = []
        self.posted: list[tuple[str, str]] = []

    async def post(self, body, *, kind):
        self.posted.append((kind, body))
        return True

    def trace(self, t):
        return f"trace {t.tool}"

    def reasoning(self, text):
        return ""

    def plan(self, items):
        return ""

    def job(self, card):
        return "\n".join(["job " + card.name, engine.job_line(card), engine.job_action(card)])

    def jobs(self, group, spawned):
        return f"spawned {spawned}: " + ", ".join(engine.job_row(card) for card in group)

    def dropped(self, count):
        return ""

    def join(self, blocks):
        return "\n".join(blocks)

    @contextlib.asynccontextmanager
    async def typing(self):
        yield

    async def offer_options(self, options, question=""):
        return None

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

    async def job_card(self, key, body):
        self.cards.append((key, body))

    def bodies(self, key):
        return [body for card_key, body in self.cards if card_key == key]


class EventClient:
    """prompt() hands its frames to the engine the way the reader thread does."""

    alive = True

    def __init__(self, updates):
        self.updates = updates
        self.on_update = None

    def state(self):
        return {}

    def prompt(self, _text, images=None, timeout=900.0):
        for update in self.updates:
            self.on_update(update)
        return PromptResult(stop_reason="end_turn", usage={}, text="", updates=[])


def make_agent(tmp_path, *, verbose=True):
    config = load_config(env={})
    config.data["session"]["store"] = str(tmp_path / "s.db")
    surface = Cards()
    agent = engine.ChatAgent(config, "test", surface)
    agent.verbose = verbose
    agent._clock = Clock()
    agent._job_open_delay = 0  # the delay is for live frames a moment apart; tests feed at once
    return agent, surface


async def settle(agent):
    """Let the scheduled frames run, then wait for the cards they queued."""
    for _ in range(10):
        await asyncio.sleep(0)
    if agent._job_flush is not None:
        await agent._job_flush


async def feed(agent, *frames):
    for frame in frames:
        agent._on_update(frame)
    await settle(agent)


# --------------------------------------------------------------- engine: cards
def test_two_subagents_get_one_summary_and_a_card_each(tmp_path):
    async def go():
        agent, surface = make_agent(tmp_path)
        await feed(agent, lifecycle("CountPy"))
        assert surface.bodies(f"group:{PARENT}") == ["spawned 1: CountPy · task · running"]
        assert [key for key, _body in surface.cards] == [f"group:{PARENT}"], \
            "a start frame opens the summary, not a card with nothing on it yet"
        await feed(agent, lifecycle("DateNow"))
        assert surface.bodies(f"group:{PARENT}")[-1] == (
            "spawned 2: CountPy · task · running, DateNow · task · running")
        agent._clock.tick(61)
        await feed(agent, progress("CountPy"),
                   progress("DateNow", intent="Reading the date", args="date -u"))
        return surface

    surface = asyncio.run(go())
    keys = [key for key, _body in surface.cards]
    assert keys[0] == f"group:{PARENT}", "the summary has to open the group"
    assert set(keys) == {f"group:{PARENT}", f"job:{PARENT}:CountPy", f"job:{PARENT}:DateNow"}
    card = surface.bodies(f"job:{PARENT}:CountPy")[-1]
    # first heard of at its start frame (0s), then 61s on the clock: the card's own age
    assert card.splitlines() == ["job CountPy", "running · 1m01s · 1 tool",
                                 "Counting py files · bash: find . -name '*.py'"]
    assert "Reading the date · bash: date -u" in surface.bodies(f"job:{PARENT}:DateNow")[-1]


def test_the_summary_waits_for_the_call_and_goes_out_before_its_cards(tmp_path):
    """Seen live: with progress landing before the summary opened, the job cards went first and
    the summary under them said "spawned 1". It now waits briefly, counts both, and leads."""
    async def go():
        agent, surface = make_agent(tmp_path)
        agent._job_open_delay = 0.05
        await feed(agent, lifecycle("CountPy"), progress("CountPy"), lifecycle("DateNow"),
                   progress("DateNow", intent="Reading the date"))
        assert surface.cards == [], "nothing goes out while the summary waits"
        await asyncio.sleep(0.1)
        await settle(agent)
        return surface

    surface = asyncio.run(go())
    keys = [key for key, _body in surface.cards]
    assert keys == [f"group:{PARENT}", f"job:{PARENT}:CountPy", f"job:{PARENT}:DateNow"]
    assert surface.bodies(f"group:{PARENT}") == [
        "spawned 2: CountPy · task · running, DateNow · task · running"]


def test_a_running_card_is_rewritten_at_most_once_a_minute(tmp_path):
    async def go():
        agent, surface = make_agent(tmp_path)
        key = f"job:{PARENT}:CountPy"
        await feed(agent, progress("CountPy", ms=6000))  # the first real step opens the card
        assert len(surface.bodies(key)) == 1
        agent._clock.tick(30)
        await feed(agent, progress("CountPy", ms=36000, tools=3, intent="Still counting"))
        assert len(surface.bodies(key)) == 1, "a card was rewritten inside the minute"
        agent._clock.tick(31)
        await feed(agent, progress("CountPy", ms=67000, tools=4, intent="Almost done"))
        return surface.bodies(key)

    bodies = asyncio.run(go())
    assert len(bodies) == 2
    assert "running · 1m07s · 4 tools" in bodies[-1]
    assert "Almost done" in bodies[-1]


def test_a_card_that_says_nothing_new_is_not_sent_again(tmp_path):
    async def go():
        agent, surface = make_agent(tmp_path)
        await feed(agent, progress("CountPy"))
        await feed(agent, progress("CountPy"))  # the same state straight after
        return surface

    surface = asyncio.run(go())
    assert len(surface.bodies(f"job:{PARENT}:CountPy")) == 1
    assert len(surface.bodies(f"group:{PARENT}")) == 1


def test_a_card_keeps_counting_while_omp_is_quiet(tmp_path):
    """omp's durationMs stands still while a job sits on one long call; the card's age must not."""
    async def go():
        agent, surface = make_agent(tmp_path)
        await feed(agent, progress("CountPy", ms=5_000))
        agent._clock.tick(120)
        await feed(agent, progress("CountPy", ms=5_000))  # omp still says 5s
        return surface

    surface = asyncio.run(go())
    bodies = surface.bodies(f"job:{PARENT}:CountPy")
    assert "running · 5s" in bodies[0]
    assert "running · 2m05s" in bodies[-1]


def test_the_end_of_a_job_is_shown_at_once(tmp_path):
    async def go():
        agent, surface = make_agent(tmp_path)
        key = f"job:{PARENT}:CountPy"
        await feed(agent, progress("CountPy"))
        assert len(surface.bodies(key)) == 1
        agent._clock.tick(5)  # well inside the minute: the end still goes out
        await feed(agent, lifecycle("CountPy", "completed"))
        return surface

    surface = asyncio.run(go())
    bodies = surface.bodies(f"job:{PARENT}:CountPy")
    assert len(bodies) == 2
    assert bodies[-1].splitlines()[1] == "✅ done"
    assert "CountPy · task · ✅ done" in surface.bodies(f"group:{PARENT}")[-1]


def test_a_failed_job_says_so(tmp_path):
    async def go():
        agent, surface = make_agent(tmp_path)
        await feed(agent, lifecycle("CountPy"), lifecycle("CountPy", "failed"))
        return surface

    surface = asyncio.run(go())
    assert "⛔ failed" in surface.bodies(f"job:{PARENT}:CountPy")[-1]

def test_a_failed_job_says_why_from_its_transcript(tmp_path):
    """Seen live: two cards said only "⛔ failed"; the job's transcript held a 429 limit."""
    transcript = tmp_path / "CountPy.jsonl"
    transcript.write_text(
        '{"type": "session_init"}\n'
        '{"type": "message", "message": {"role": "assistant", "stopReason": "error", '
        '"errorMessage": "429 [zai/glm-5.3-flash] [429]: Usage limit reached for 5 hour."}}\n')

    def started(status):
        frame = lifecycle("CountPy", status)
        frame.raw["payload"]["sessionFile"] = str(transcript)
        return frame

    async def go():
        agent, surface = make_agent(tmp_path)
        await feed(agent, started("started"), started("failed"))
        return surface

    card = asyncio.run(go()).bodies(f"job:{PARENT}:CountPy")[-1]
    assert "⛔ failed · 429 [zai/glm-5.3-flash] [429]: Usage limit reached for 5 hour." in card



def test_a_quiet_chat_shows_no_job_cards(tmp_path):
    async def go():
        agent, surface = make_agent(tmp_path, verbose=False)
        await feed(agent, lifecycle("CountPy"), progress("CountPy"),
                   lifecycle("CountPy", "completed"))
        return surface

    surface = asyncio.run(go())
    assert surface.cards == []
    assert surface.posted == []


def test_a_group_is_opened_once_and_named_with_its_call(tmp_path):
    """Two `task` calls spawn separate groups: each gets its own summary."""
    async def go():
        agent, surface = make_agent(tmp_path)
        await feed(agent, lifecycle("CountPy", parent="toolu_a"),
                   lifecycle("Other", parent="toolu_b"))
        return surface

    surface = asyncio.run(go())
    assert surface.bodies("group:toolu_a") == ["spawned 1: CountPy · task · running"]
    assert surface.bodies("group:toolu_b") == ["spawned 1: Other · task · running"]


def test_the_summary_counts_what_the_task_call_asked_for(tmp_path):
    """Seen live: the first job's start frame read "spawned 1" while the call spawned two.
    omp's parentToolCallId is "<call id>|fc_<call id>"; the task call's own id is the first part."""
    async def go():
        agent, surface = make_agent(tmp_path)
        agent._job_spawned["toolu_x"] = 2
        await feed(agent, lifecycle("CountPy", parent="toolu_x|fc_toolu_x"))
        return surface

    surface = asyncio.run(go())
    assert surface.bodies("group:toolu_x|fc_toolu_x") == ["spawned 2: CountPy · task · running"]


class Registry:
    """omp's get_subagents: what each poll returns, in order; then the job is gone."""

    alive = True

    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def subagents(self):
        self.calls += 1
        return self.snapshots.pop(0) if self.snapshots else []


def snapshot(name, intent, duration_ms):
    return {"id": name, "agent": "task", "status": "running", "parentToolCallId": PARENT,
            "progress": {"id": name, "agent": "task", "status": "running", "lastIntent": intent,
                         "durationMs": duration_ms, "resolvedModel": "m/one"}}


def poll_agent(tmp_path, registry):
    agent, surface = make_agent(tmp_path)
    agent.client = registry
    minutes: list[float] = []

    async def sleep(seconds):
        minutes.append(seconds)
        agent._clock.tick(seconds)
        await asyncio.sleep(0)

    agent._sleep = sleep
    return agent, surface, minutes


def test_a_running_job_is_polled_every_minute_even_when_omp_is_silent(tmp_path):
    """A job waiting on a long tool sends no progress; the card still moves once a minute."""
    async def go():
        registry = Registry([snapshot("CountPy", "Reading the tree", 61_000)],
                            [snapshot("CountPy", "Counting files", 121_000)])
        agent, surface, minutes = poll_agent(tmp_path, registry)
        await feed(agent, lifecycle("CountPy"), progress("CountPy", intent="Starting"))
        await agent._job_poll
        await settle(agent)
        return surface, minutes, registry

    surface, minutes, registry = asyncio.run(go())
    assert minutes and all(gap == 60.0 for gap in minutes)
    assert registry.calls == 3  # two running snapshots, then the list no longer has it
    bodies = surface.bodies(f"job:{PARENT}:CountPy")
    assert any("Reading the tree" in body for body in bodies)
    assert any("Counting files" in body for body in bodies)
    assert "done" in bodies[-1], "a job that left omp's list is shown as ended"


def test_polling_stops_once_every_job_has_ended(tmp_path):
    async def go():
        registry = Registry()
        agent, _surface, _minutes = poll_agent(tmp_path, registry)
        await feed(agent, lifecycle("CountPy"), progress("CountPy"),
                   lifecycle("CountPy", "completed"))
        if agent._job_poll is not None:
            await agent._job_poll
        return registry

    assert asyncio.run(go()).calls == 0


def test_subagent_frames_never_reach_the_turn_renderer(tmp_path):
    async def go():
        agent, surface = make_agent(tmp_path)
        agent.client = EventClient([lifecycle("CountPy"), progress("CountPy"),
                                    lifecycle("CountPy", "completed"), message("selesai")])
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        await agent.ask("halo")
        await settle(agent)
        return surface

    surface = asyncio.run(go())
    turn = "\n".join(body for _kind, body in surface.posted)
    assert "selesai" in turn
    assert "CountPy" not in turn and "subagent" not in turn
    assert surface.bodies(f"job:{PARENT}:CountPy")


def task_call(call_id="toolu_parent"):
    return Update(kind="tool_call", session_id="s", raw={
        "toolCallId": call_id, "toolName": "task", "kind": "task", "title": "Spawning jobs",
        "status": "in_progress",
        "rawInput": {"tasks": [{"name": "CountPy", "agent": "task", "task": "count"}]}})


def task_done(call_id="toolu_parent"):
    return Update(kind="tool_call_update", session_id="s", raw={
        "toolCallId": call_id, "status": "completed",
        "rawOutput": {"content": [{"type": "text", "text": "Spawned 1 background agent"}]}})


def test_the_task_trace_gives_way_to_the_job_cards(tmp_path):
    """Seen live: the trace's "Spawned 2 background agents" and the summary card both opened
    the call. When omp reports the jobs, the cards are the one account of it."""
    async def go():
        agent, surface = make_agent(tmp_path)
        agent.client = EventClient([task_call(), lifecycle("CountPy", parent=f"{PARENT}|fc_{PARENT}"),
                                    task_done(), message("selesai")])
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        await agent.ask("halo")
        await settle(agent)
        return surface

    surface = asyncio.run(go())
    assert not any(body.startswith("trace subagent") for _kind, body in surface.posted)
    assert surface.bodies(f"group:{PARENT}|fc_{PARENT}")


def test_the_task_trace_stays_when_omp_sends_no_job_frames(tmp_path):
    async def go():
        agent, surface = make_agent(tmp_path)
        agent.client = EventClient([task_call(), task_done(), message("selesai")])
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        await agent.ask("halo")
        await settle(agent)
        return surface

    surface = asyncio.run(go())
    assert any(body.startswith("trace subagent") for _kind, body in surface.posted)

def test_a_background_job_that_ends_after_the_turn_still_reports(tmp_path):
    async def go():
        agent, surface = make_agent(tmp_path)
        agent.client = EventClient([lifecycle("CountPy"), progress("CountPy"),
                                    message("selesai")])
        agent.client.on_update = agent._on_update
        agent.session_id = "s"
        await agent.ask("halo")
        await settle(agent)
        assert "✅ done" not in surface.bodies(f"job:{PARENT}:CountPy")[-1]
        # the turn is over; the detached job finishes later and its card still moves
        await feed(agent, lifecycle("CountPy", "completed"))
        return surface

    surface = asyncio.run(go())
    assert "✅ done" in surface.bodies(f"job:{PARENT}:CountPy")[-1]


# ------------------------------------------------------------------- the frames
def test_a_frame_says_only_what_it_carries():
    started = engine.job_from_frame(lifecycle("CountPy").raw)
    assert started == engine.JobCard(name="CountPy", agent="task", status="started",
                                    parent=PARENT, detached=True,
                                    session_file="/tmp/omp/session.jsonl")
    running = engine.job_from_frame(progress("CountPy").raw)
    assert running.model == MODEL
    assert running.tool_count == 1 and running.duration_ms == 6562
    assert running.last_tool == "bash: find . -name '*.py'"


def test_a_progress_frame_without_tools_still_names_the_job():
    raw = progress("CountPy").raw
    raw["payload"]["progress"]["recentTools"] = []
    card = engine.job_from_frame(raw)
    assert card.last_tool == "" and card.last_intent == "Counting py files"
    assert engine.job_action(card) == "Counting py files"


def test_an_unknown_frame_is_not_a_job():
    assert engine.job_from_frame({"type": "queue_update", "payload": {}}) is None
    assert engine.job_from_frame({"type": "subagent_progress"}) is None


def test_a_long_tool_call_is_clipped_to_one_line():
    card = engine.job_from_frame(progress("CountPy", args="x" * 400).raw)
    assert "\n" not in card.last_tool and len(card.last_tool) <= TOOL_LINE_WIDTH


def test_durations_and_states_read_like_the_card():
    assert engine.job_duration(0) == "0s"
    assert engine.job_duration(420) == "<1s", "a job that just started is not at 0s"
    assert engine.job_duration(6_562) == "6s"
    assert engine.job_duration(80_000) == "1m20s"
    assert engine.job_duration(3_930_000) == "1h05m"
    assert engine.job_status("started") == "running"
    assert engine.job_status("completed") == "✅ done"
    assert engine.job_status("failed") == "⛔ failed"
    assert engine.job_status("aborted") == "⛔ aborted"


def test_the_status_line_counts_tools_and_stops_at_the_end():
    running = engine.JobCard(name="CountPy", status="running", duration_ms=80_000, tool_count=3)
    assert engine.job_line(running) == "running · 1m20s · 3 tools"
    one = engine.JobCard(name="CountPy", status="running", duration_ms=6_562, tool_count=1)
    assert engine.job_line(one) == "running · 6s · 1 tool"
    done = engine.JobCard(name="CountPy", status="completed", duration_ms=80_000, tool_count=3)
    assert engine.job_line(done) == "✅ done"


# ------------------------------------------------------------- telegram surface
class SentMessage:
    """A message the bot sent; its edit_text is what a job card rewrites."""

    def __init__(self, text, *, refuses=None):
        self.text = text
        self.edits: list[str] = []
        self.refuses = refuses

    async def edit_text(self, body, **_kwargs):
        if self.refuses is not None:
            error, self.refuses = self.refuses, None
            raise error
        self.edits.append(body)


class FakeBot:
    """Raises `refuses` on the first edit only: a real refusal does not repeat forever."""

    def __init__(self, *, refuses=None):
        self.messages: list[SentMessage] = []
        self.refuses = refuses

    async def send_message(self, _chat, body, **_kwargs):
        error, self.refuses = self.refuses, None
        sent = SentMessage(body, refuses=error)
        self.messages.append(sent)
        return sent


def tg_surface_with(bot) -> tg_surface.TelegramSurface:
    return tg_surface.TelegramSurface(types.SimpleNamespace(bot=bot), 7)


def running_card(**extra) -> engine.JobCard:
    data = {"name": "CountPy", "agent": "task", "model": MODEL, "status": "running",
            "last_intent": "Counting py files", "last_tool": "bash: find . -name '*.py'",
            "tool_count": 3, "duration_ms": 80_000, "parent": PARENT, "detached": True}
    data.update(extra)
    return engine.JobCard(**data)


def test_telegram_posts_every_job_card_update_as_a_new_message():
    """Asked for in chat: a card edited in place changed out of sight; each update is a message."""
    bot = FakeBot()
    surface = tg_surface_with(bot)

    async def go():
        await surface.job_card("job:1", "first")
        await surface.job_card("job:1", "second")
        await surface.job_card("job:2", "other")

    asyncio.run(go())
    assert [message.text for message in bot.messages] == ["first", "second", "other"]
    assert all(message.edits == [] for message in bot.messages)


def test_telegram_renders_the_job_card():
    body = tg_surface_with(FakeBot()).job(running_card())
    assert body.splitlines()[0] == "🤖 <b>subagent</b> - CountPy"
    assert "Job type: task" in body
    assert f"Model: {MODEL}" in body
    assert "running · 1m20s · 3 tools" in body
    assert "Last action: Counting py files · bash: find . -name '*.py'" in body


def test_telegram_renders_the_summary():
    done = running_card(name="DateNow", status="completed", duration_ms=1_000, tool_count=1)
    body = tg_surface_with(FakeBot()).jobs([running_card(), done], 2)
    assert body.splitlines() == ["🤖 <b>subagent</b> (spawned 2 background agents)",
                                "CountPy · task · running",
                                "DateNow · task · ✅ done"]


def test_telegram_marks_a_job_without_known_type_and_model():
    body = tg_surface_with(FakeBot()).job(engine.JobCard(name="<odd&name>", status="running"))
    assert "Job type: task" in body and "Model: ?" in body
    assert "&lt;odd&amp;name&gt;" in body, "a job name is escaped like any other text"
    # the line is always there: a job that has not called a tool yet says it is starting
    assert "Last action:" in body


# ------------------------------------------------------------- whatsapp surface
class WaTransport:
    def __init__(self):
        self.sent: list[str] = []
        self.edits: list[tuple[str, str]] = []

    async def send_text(self, _chat, text, quote=None):
        self.sent.append(text)
        return f"msg{len(self.sent)}"

    async def edit_text(self, _chat, message_id, text):
        self.edits.append((message_id, text))


def test_whatsapp_posts_every_job_card_update_as_a_new_message():
    transport = WaTransport()
    surface = wa_surface.WhatsAppSurface(transport, "6281234")

    async def go():
        await surface.job_card("job:1", "first")
        await surface.job_card("job:1", "second")
        await surface.job_card("job:2", "other")

    asyncio.run(go())
    assert transport.sent == ["first", "second", "other"]
    assert transport.edits == []


def test_whatsapp_renders_the_card_in_its_own_markup():
    surface = wa_surface.WhatsAppSurface(WaTransport(), "6281234")
    body = surface.job(running_card())
    assert body.splitlines()[0] == "🤖 *subagent* - CountPy"
    assert "Job type: task" in body and f"Model: {MODEL}" in body
    assert "_running · 1m20s · 3 tools_" in body
    assert "Last action: Counting py files · bash: find . -name '*.py'" in body
    summary = surface.jobs([running_card()], 1)
    assert summary.splitlines()[0] == "🤖 *subagent* (spawned 1 background agent)"


# ------------------------------------------------------------------ terminal
def test_the_terminal_prints_a_card_per_update(capsys):
    surface = cli.TerminalSurface(load_config(env={}))
    card = running_card()

    async def go():
        await surface.job_card("job:1", surface.job(card))
        await surface.job_card("job:1", surface.job(card))

    asyncio.run(go())
    out = capsys.readouterr().out
    assert out.count("[job] CountPy · task · " + MODEL) == 2, "a terminal has no message to edit"
    assert "[job] CountPy · task · " + MODEL in out
    assert "  running · 1m20s · 3 tools" in out
    assert "  last: Counting py files · bash: find . -name '*.py'" in out


def test_the_terminal_renders_the_summary():
    surface = cli.TerminalSurface(load_config(env={}))
    done = running_card(name="DateNow", status="completed")
    body = surface.jobs([running_card(), done], 2)
    assert body.splitlines() == ["[jobs] 2 background agents spawned",
                                "  CountPy · task · running",
                                "  DateNow · task · ✅ done"]
