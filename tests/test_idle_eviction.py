"""The idle sweep must not kill an agent that is still working.

A run omp starts on its own — a background job reported back, a subagent woke the agent — leaves
`busy` False, because `busy` follows only a turn a message asked for. The sweep read `busy`, so it
evicted the process mid-run: chat 749994884 on 2026-11-11 12:05, where a 29-minute turn ended and
the agent woke to finish a follow-up ten seconds later, then died 50 seconds after that
(`run started by the agent ended ... tools=7`, the run's 7th tool cut off).
"""

from __future__ import annotations

import asyncio
import inspect
import time
import types

from ompwire import engine, telegram, whatsapp
from ompwire.config import load_config


def _config(tmp_path, idle=1800):
    config = load_config(env={})
    config.data["session"]["store"] = str(tmp_path / "s.db")
    config.data["session"]["idle_timeout"] = idle
    config.data["gateway"] = {"name": "telegram", "allowed_users": [7]}
    return config


class Surface:
    """The bits of a transport surface ChatAgent touches while the sweep runs."""

    label = "tg:7"

    class typing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    async def post(self, *_a, **_k):
        return None

    async def render(self, *_a, **_k):
        return None


def _sweep(config, monkeypatch, *, busy=False, idle_run=None, pending_wait=None, age=0,
           interval=0.01):
    """One idle interval of the worker, with nothing queued. Returns whether it evicted.

    The agent is built inside the loop: ChatAgent captures the running loop, the way it does
    when a real chat is opened.
    """
    evicted: list[float] = []

    async def go():
        agent = engine.ChatAgent(config, "tg:7", Surface(), shared=False)
        agent.busy = busy
        agent._idle_run = idle_run
        agent._pending_wait = pending_wait
        agent.last_used = time.time() - age
        agent.client = types.SimpleNamespace(alive=True)  # a process the sweep could close

        async def stop_process():
            evicted.append(time.time())

        agent.stop_process = stop_process

        transport = telegram.Transport(config)
        transport.agent_for = lambda _chat: agent
        transport.queues[7] = asyncio.Queue()
        monkeypatch.setitem(telegram.__dict__, "IDLE_CHECK_INTERVAL", interval)
        # the worker returns once its queue stays empty, so one interval is the whole run
        await asyncio.wait_for(transport._worker(7), timeout=2)
        return agent

    agent = asyncio.run(go())
    return evicted, agent


def test_a_genuinely_idle_agent_is_evicted(tmp_path, monkeypatch):
    evicted, _agent = _sweep(_config(tmp_path, idle=60), monkeypatch, age=120)
    assert evicted, "an idle agent must still be reclaimed"


def test_an_agents_own_run_is_not_evicted(tmp_path, monkeypatch):
    """The bug: `busy` is False during a run omp started itself, and the sweep killed it."""
    run = types.SimpleNamespace(queue=asyncio.Queue())
    evicted, agent = _sweep(_config(tmp_path, idle=60), monkeypatch, age=120, idle_run=run)
    assert evicted == []
    assert agent.working is True


def test_a_pending_wait_is_not_evicted(tmp_path, monkeypatch):
    """Background work the agent is waiting on counts as working, not as idle."""
    evicted, agent = _sweep(_config(tmp_path, idle=60), monkeypatch, age=120,
                            pending_wait=types.SimpleNamespace())
    assert evicted == []
    assert agent.working is True


def test_a_working_agent_is_not_evicted_however_old_the_last_message(tmp_path, monkeypatch):
    """Even a turn the person started long ago must survive while it is still running."""
    evicted, _agent = _sweep(_config(tmp_path, idle=60), monkeypatch, busy=True, age=9999)
    assert evicted == []


def test_every_agent_frame_refreshes_last_used(tmp_path, monkeypatch):
    """A long turn used to leave `last_used` at its start, so the chat looked idle mid-work."""
    async def go():
        agent = engine.ChatAgent(_config(tmp_path), "tg:7", Surface(), shared=False)
        before = agent.last_used = time.time() - 9999
        agent._route_update = lambda _update: None
        agent._on_update(engine.AgentUpdate(kind="agent_message_chunk", raw={}, session_id="s"))
        return before, agent.last_used

    before, after = asyncio.run(go())
    assert after > before


def test_both_workers_judge_working_not_busy():
    """WhatsApp carried the identical line; neither transport may keep its own copy of the bug."""
    for transport in (telegram.Transport, whatsapp.Transport):
        source = inspect.getsource(transport._worker)
        assert "agent.busy" not in source, f"{transport.__module__} still sweeps on `busy`"
        assert "agent.working" in source
