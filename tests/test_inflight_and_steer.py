"""A turn cut by a restart is picked back up; a message sent mid-turn steers it."""

from __future__ import annotations

import asyncio
import types

from ompwire import engine, telegram
from ompwire.config import load_config
from ompwire.rpc import PromptResult, Update
from ompwire.store import SessionStore


class Bot:
    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, _chat_id, text, **_kw):
        self.messages.append(text)
        return types.SimpleNamespace(edit_text=self._edit, reply_text=self._reply)

    async def _edit(self, body, **_kw):
        self.messages.append(body)

    async def _reply(self, body, **_kw):
        self.messages.append(body)

    async def send_chat_action(self, *_a, **_k):
        return None


class Client:
    alive = True

    def __init__(self, stop_reason="end_turn", steer_outcome="injected"):
        self.stop_reason = stop_reason
        self.steer_outcome = steer_outcome
        self.on_update = None
        self.steered: list[str] = []

    def state(self):
        return {}

    def prompt(self, *_a, **_k):
        return PromptResult(stop_reason=self.stop_reason, usage={}, text="", updates=[])

    def steer(self, text, images=None, timeout=30.0):
        self.steered.append(text)
        return self.steer_outcome, ""

    def abort(self):
        return None

    def close(self):
        return None


def _config(tmp_path):
    config = load_config(env={})
    config.data["session"]["store"] = str(tmp_path / "s.db")
    return config


def _agent(config, client):
    agent = telegram.chat_agent(config, 7, "tg:7", types.SimpleNamespace(bot=Bot()))
    agent.client = client
    agent.session_id = "s"
    return agent


def _pending(config):
    store = SessionStore(config.get("session", "store"))
    try:
        return store.interrupted(telegram.KEY_PREFIX)
    finally:
        store.close()


def test_a_turn_cut_by_a_restart_stays_on_record(tmp_path):
    config = _config(tmp_path)

    async def go():
        await _agent(config, Client(stop_reason="cancelled")).ask("deploy the thing")

    asyncio.run(go())
    assert _pending(config) == [("tg:7", "deploy the thing", 0)]


def test_a_finished_turn_leaves_nothing_to_resume(tmp_path):
    config = _config(tmp_path)

    async def go():
        await _agent(config, Client()).ask("halo")

    asyncio.run(go())
    assert _pending(config) == []


def test_a_stop_from_the_person_is_not_resumed(tmp_path):
    config = _config(tmp_path)

    async def go():
        agent = _agent(config, Client(stop_reason="cancelled"))
        task = asyncio.create_task(agent.ask("long job"))
        await asyncio.sleep(0)
        await task
        # /stop arrives as the cancel; simulate the order the handler produces
        agent.store.begin_turn(agent.key, "long job")
        await agent.cancel()

    asyncio.run(go())
    assert _pending(config) == []


def test_a_resume_records_the_original_words_not_the_wrapper(tmp_path):
    config = _config(tmp_path)

    async def go():
        await _agent(config, Client(stop_reason="cancelled")).ask(
            engine.RESUME_PROMPT.format(prompt="fix it"), resumes=1, record="fix it")

    asyncio.run(go())
    assert _pending(config) == [("tg:7", "fix it", 1)]


def _submit(config, client, busy, msg):
    async def go():
        transport, queued = _transport(config, client, busy)
        await transport.submit(7, msg, msg)
        return queued

    return asyncio.run(go())


def _transport(config, client, busy):
    transport = telegram.Transport(config)
    transport.app = types.SimpleNamespace(bot=Bot())
    agent = _agent(config, client)
    agent.busy = busy
    transport.agents[7] = agent
    queued: list[str] = []
    transport.enqueue = lambda _chat, _kind, payload: queued.append(payload.text) or len(queued)
    return transport, queued


class Msg:
    def __init__(self, text):
        self.text = text
        self.images = []
        self.reactions: list[str] = []
        self.replies: list[str] = []
        self.quoted: list[bool] = []

    async def set_reaction(self, emoji):
        self.reactions.append(emoji)

    async def reply_text(self, text, do_quote=None, **_kw):
        self.replies.append(text)
        self.quoted.append(do_quote is True)


def test_a_message_sent_mid_turn_steers_it(tmp_path):
    client = Client(steer_outcome="injected")
    msg = Msg("use staging, not prod")
    queued = _submit(_config(tmp_path), client, True, msg)
    assert client.steered == ["use staging, not prod"]
    assert queued == []
    assert msg.reactions == []
    assert msg.replies and msg.replies[0].startswith("↪️ steered")
    assert msg.quoted == [True]  # a quoted reply, not a new row under it


def test_a_steer_the_agent_declines_falls_back_to_the_queue(tmp_path):
    msg = Msg("later")
    queued = _submit(_config(tmp_path), Client(steer_outcome="failed"), True, msg)
    assert queued == ["later"]
    assert msg.replies == ["📥 queued #1 · runs after the current turn"]
    assert msg.quoted == [True]


def test_a_turn_that_ended_before_the_steer_becomes_a_prompt(tmp_path):
    msg = Msg("next thing")
    queued = _submit(_config(tmp_path), Client(steer_outcome="promptRequired"), True, msg)
    assert queued == ["next thing"]
    assert msg.replies == []


def test_queue_mode_keeps_the_old_fifo(tmp_path):
    config = _config(tmp_path)
    config.data.setdefault("telegram", {})["busy_message"] = "queue"
    client = Client()
    msg = Msg("wait your turn")
    queued = _submit(config, client, True, msg)
    assert client.steered == []
    assert queued == ["wait your turn"]


def test_an_idle_evicted_chat_still_works_on_the_next_message(tmp_path):
    config = _config(tmp_path)

    async def go():
        agent = _agent(config, Client())
        agent.client.close = lambda *_a, **_k: None
        await agent.stop_process()
        agent.client = Client()
        await agent.ask("after eviction")  # used to raise: Cannot operate on a closed database

    asyncio.run(go())
    assert _pending(config) == []


def test_schedule_lists_without_crashing(tmp_path, monkeypatch):
    config = _config(tmp_path)
    sent: list[str] = []

    class SchedBot(Bot):
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)
            return await super().send_message(chat_id, text, **kw)

    monkeypatch.setattr(telegram.schedule_lib, "load", lambda: {})
    config.data["gateway"] = {"name": "telegram", "allowed_users": [7]}
    update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=7),
                                   effective_chat=types.SimpleNamespace(id=7, type="private"),
                                   effective_message=None, callback_query=None)

    async def go():
        transport = telegram.Transport(config)
        transport.app = types.SimpleNamespace(bot=SchedBot())
        await transport.cmd_schedule(update, types.SimpleNamespace(args=[]))

    asyncio.run(go())
    assert any("no schedules yet" in body for body in sent)


class Planner(Client):
    """Answers every prompt with a plan and records what the agent was actually sent."""

    def __init__(self):
        super().__init__()
        self.prompts: list[str] = []

    def prompt(self, text, *_a, **_k):
        self.prompts.append(text)
        if self.on_update is not None:  # the answer streams, as omp's does
            self.on_update(Update(kind="agent_message_chunk", session_id="s",
                                  raw={"content": {"type": "text", "text": "1. do it"}}))
        return PromptResult(stop_reason="end_turn", usage={}, text="1. do it", updates=[])


def _planning(config):
    planner = Planner()
    agent = _agent(config, planner)
    planner.on_update = agent._on_update
    agent.options = {"mode": {"currentValue": "plan"}}

    async def respawn():
        agent.client = planner  # a new omp process resuming the same session

    agent.ensure = respawn
    return agent


def test_plan_mode_ends_with_review_buttons_and_approve_implements(tmp_path):
    config = _config(tmp_path)

    async def go():
        agent = _planning(config)
        await agent.ask("add a readme")
        offered = list(agent.pending_options)
        await agent.ask(engine.PLAN_OPTIONS[0])
        return agent, offered

    agent, offered = asyncio.run(go())
    assert offered == list(engine.PLAN_OPTIONS)
    first, second = agent.client.prompts
    assert "Plan mode is on" in first and "add a readme" in first
    assert second == engine.PLAN_APPROVED
    assert agent.mode == "default"


def test_deny_takes_the_next_message_as_feedback_and_stays_in_plan_mode(tmp_path):
    config = _config(tmp_path)

    async def go():
        agent = _planning(config)
        await agent.ask("add a readme")
        await agent.ask(engine.PLAN_OPTIONS[1])  # Deny: no turn, it asks what to change
        await agent.ask("also add a license")
        return agent

    agent = asyncio.run(go())
    _first, revised = agent.client.prompts
    assert "also add a license" in revised and "Revise the plan" in revised
    assert agent.mode == "plan"
    assert agent.plan_review == "pending"  # the revised plan is up for review again


def test_a_restart_in_plan_mode_starts_the_agent_read_only(tmp_path):
    """After a restart the options are empty until omp answers; the stored mode must decide."""
    config = _config(tmp_path)

    async def build():
        agent = _agent(config, Client())
        agent.store.set_pref(agent.key, "mode", "plan")
        agent.options = {}
        return agent._build_client().args

    assert "--approval-mode=always-ask" in asyncio.run(build())
