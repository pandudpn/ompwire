"""The terminal is a Surface: a turn prints, and a numbered answer is the option's text."""

from __future__ import annotations

import asyncio
import builtins

from ompwire import cli, engine
from ompwire.config import load_config
from ompwire.rpc import PromptResult, Update


class FakeClient:
    alive = True

    def __init__(self, turns):
        self.turns = list(turns)
        self.on_update = None
        self.cwd = "/tmp"
        self.prompts: list[str] = []
        self.options: dict[str, str] = {}

    def state(self):
        return {}

    def prompt(self, text, images=None, timeout=900.0):
        self.prompts.append(text)
        for update in self.turns.pop(0) if self.turns else []:
            if self.on_update is not None:
                self.on_update(update)
        return PromptResult(stop_reason="end_turn", usage={}, text=text, updates=[])

    def set_config_option(self, _session_id, config_id, value):
        self.options[config_id] = value
        return {}


def message(text):
    return Update(kind="agent_message_chunk", session_id="s",
                  raw={"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": text}})


def tool_done(call_id, title, command, output):
    return Update(kind="tool_call", session_id="s", raw={
        "sessionUpdate": "tool_call", "toolCallId": call_id, "kind": "execute",
        "title": title, "status": "completed",
        "rawInput": {"command": command},
        "rawOutput": {"content": [{"type": "text", "text": output}]},
    })


def run_turns(turns, body, *, show_reasoning=False):
    """A ChatAgent needs a loop, so it is built inside one, the way run_chat builds it."""
    config = load_config(env={})
    config.data["stream"].update({"show_tool_calls": True,
                                  "show_reasoning": show_reasoning, "min_gap": 0.0})

    async def go():
        surface = cli.TerminalSurface(config)
        agent = engine.ChatAgent(config, "cli:/tmp", surface)
        surface.agent = agent
        agent.client = FakeClient(turns)
        agent.client.on_update = agent._on_update  # ensure() would otherwise leave it unset
        agent.session_id = "s"
        return await body(agent, surface)

    return asyncio.run(go())


def test_a_turn_prints_the_answer_and_one_compact_line_per_tool(capsys):
    turns = [[
        message("cek dulu\n"),
        tool_done("t1", "ls -la", "ls -la", "a\nb\nc\nd"),
        message("selesai"),
    ]]

    async def turn(agent, _surface):
        await agent.ask("halo")

    run_turns(turns, turn)
    out = capsys.readouterr().out
    assert "cek dulu" in out
    assert "[tool] execute · ls -la · completed" in out
    assert "    a\n    b\n    c" in out and "    d" not in out
    assert "+1 line(s)" in out
    assert "selesai" in out
    assert "not shown" not in out  # every printed line was delivered; nothing was dropped


def test_a_numbered_answer_is_sent_as_the_option_text(monkeypatch, capsys):
    turns = [
        [message("pilih dulu\n<<options: postgres | mysql>>")],
        [message("baik")],
    ]
    lines = iter(["halo", "2", "/exit"])
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(lines))

    async def repl(agent, surface):
        await cli._repl(agent, surface)
        return agent.client.prompts

    prompts = run_turns(turns, repl)
    assert prompts == ["halo", "mysql"]
    assert "2. mysql" in capsys.readouterr().out


def test_the_answer_is_streamed_as_it_arrives_and_markers_never_show(capsys):
    """A terminal has no rate limit: text is printed piece by piece, not per segment."""
    written: list[str] = []
    turns = [[
        message("satu "), message("dua "),
        tool_done("t1", "ls", "ls", "x"),
        message("tiga\n<<options: a | "), message("b>>\nempat"),
    ]]

    async def turn(agent, surface):
        original = surface.stream

        async def spy(delta):
            written.append(delta)
            await original(delta)

        surface.stream = spy
        await agent.ask("halo")

    run_turns(turns, turn)
    out = capsys.readouterr().out
    assert len(written) >= 3  # pieces, not one block per segment
    assert written[0].strip() == "satu"  # trailing space waits for the next word
    assert out.index("satu dua") < out.index("[tool] execute") < out.index("tiga")
    assert "<<" not in out and "options:" not in out  # a marker is held back, then removed
    assert "empat" in out
