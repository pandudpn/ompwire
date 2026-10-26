"""Context window display, compaction notices, omp config edits, and forwarded commands.

ChatAgent grabs the running loop in its constructor, so every test builds its agent inside
`asyncio.run` and hands plain data out for the asserts.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from ompwire import engine, omp_config, telegram
from ompwire.acp import PromptResult, Update
from ompwire.config import load_config


class Bot:
    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, _chat_id, text, **_kw):
        self.messages.append(text)
        return types.SimpleNamespace(
            edit_text=lambda body, **kw: None,
            reply_text=lambda body, **kw: None,
        )


class Client:
    alive = True

    def __init__(self, updates):
        self.updates = updates
        self.on_update = None

    def close(self, _sid):
        return None

    def prompt(self, _sid, _text, images=None, timeout=900.0, collect=True):
        for update in self.updates:
            if self.on_update is not None:
                self.on_update(update)
        text = "".join(u.text for u in self.updates if u.kind == "agent_message_chunk")
        return PromptResult(stop_reason="end_turn", usage={}, text=text, updates=[])


def usage(used, size):
    return Update(kind="usage_update", session_id="s",
                  raw={"sessionUpdate": "usage_update", "used": used, "size": size})


def message(text):
    return Update(kind="agent_message_chunk", session_id="s",
                  raw={"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": text}})


def _config(tmp_path, **stream):
    config = load_config(env={})
    config.data["session"]["store"] = str(tmp_path / "s.db")
    config.data.setdefault("stream", {}).update({"min_gap": 0.0, **stream})
    return config


def _make_agent(config, updates):
    """Returns a factory; call it inside a running loop."""
    def make():
        agent = telegram.chat_agent(config, 7, "tg:7", types.SimpleNamespace(bot=Bot()))
        agent.client = Client(updates)
        agent.client.on_update = agent._on_update
        agent.session_id = "s"

        async def ensure(yolo=False):
            # /new drops the client; the real ensure() would spawn omp, which CI does not have
            if agent.client is None:
                agent.client = Client(updates)
                agent.client.on_update = agent._on_update
                agent.session_id = "s2"
                agent._seed_context()

        agent.ensure = ensure
        return agent
    return make


class _Message:
    def __init__(self, text):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, body, **_kw):
        self.replies.append(body)

    async def set_reaction(self, *_a, **_k):
        raise AttributeError("no reactions in tests")


def _transport(tmp_path, agent):
    config = _config(tmp_path)
    transport = telegram.Transport.__new__(telegram.Transport)
    transport.config = config
    transport.app = types.SimpleNamespace(bot=Bot())
    transport.agents = {7: agent}
    transport.queues = {}
    transport.workers = {}
    transport.stopping = False
    transport.allowed = {1}
    transport.audience = telegram.Audience(allowed={"1"})
    return transport


def _update(text, user_id=1):
    return types.SimpleNamespace(
        effective_message=_Message(text),
        effective_chat=types.SimpleNamespace(id=7, type="private"),
        effective_user=types.SimpleNamespace(id=user_id),
        callback_query=None,
    )


# ------------------------------------------------------------------ engine


def test_usage_update_updates_context_and_skips_the_turn_queue(tmp_path):
    async def go():
        agent = _make_agent(_config(tmp_path), [])()
        agent._on_usage(74886, 1048576)
        await asyncio.sleep(0)
        return agent

    agent = asyncio.run(go())
    assert agent.context == {"used": 74886, "size": 1048576}
    # nothing queued: the turn reader must not see usage updates
    assert agent._updates.empty()


def test_token_counts_read_like_a_person_wrote_them():
    from ompwire.format import humanize_token_report, tokens

    assert [tokens(n) for n in (0, 940, 1_234, 58_555, 94_611, 1_048_576, None)] == \
        ["0", "940", "1.23k", "58.6k", "94.6k", "1.05M", "?"]
    assert [tokens(n) for n in (999_949, 999_950, 999_999, 999_950_000, -999_999)] == \
        ["999.9k", "1M", "1M", "1G", "-1M"]
    assert humanize_token_report(
        "Compaction complete. Tokens: 94611 -> 58555 (saved 36056).") == \
        "Compaction complete. Tokens: 94.6k → 58.6k (saved 36.1k)."


def test_context_line_formats_k_m_and_percent():
    assert engine.context_line(124_000, 1_000_000) == "124k / 1M (12%)"
    assert engine.context_line(0, 0) == "unknown"
    assert engine.context_line(100, 0) == "unknown"


def test_warning_fires_once_on_crossing_and_re_arms(tmp_path):
    async def go():
        agent = _make_agent(_config(tmp_path), [])()
        agent._on_usage(500_000, 1_000_000)  # at 50%: no warning
        await asyncio.sleep(0)
        agent._on_usage(700_000, 1_000_000)  # crossing: one warning
        await asyncio.sleep(0)
        agent._on_usage(750_000, 1_000_000)  # still above: silent
        await asyncio.sleep(0)
        agent._on_usage(100_000, 1_000_000)  # back below: re-arm
        await asyncio.sleep(0)
        agent._on_usage(700_000, 1_000_000)  # crossing again: one more warning
        await asyncio.sleep(0.1)
        return agent

    agent = asyncio.run(go())
    warnings = [m for m in agent.surface.app.bot.messages if "⚠️" in m]
    assert len(warnings) == 2
    assert all("context at 70%" in m for m in warnings)


def test_no_warning_before_size_is_known(tmp_path):
    async def go():
        agent = _make_agent(_config(tmp_path), [])()
        agent._on_usage(0, 0)
        await asyncio.sleep(0.05)
        return agent

    agent = asyncio.run(go())
    assert not agent.surface.app.bot.messages


# ------------------------------------------------------------------ omp_config


def test_omp_config_round_trip_preserves_the_file(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    original = """# my omp config
model: anthropic/claude-x  # keep this comment
compaction:
  thresholdPercent: 70
edit:
  mode: hashline  # trailing comment
"""
    cfg.write_text(original, encoding="utf-8")
    monkeypatch.setattr(omp_config, "CONFIG_PATH", cfg)

    omp_config.set_compaction(thresholdPercent=55)
    text = cfg.read_text(encoding="utf-8")
    assert "thresholdPercent: 55" in text
    assert "keep this comment" in text and "trailing comment" in text
    backup = cfg.with_name("config.yml.ompwire-bak")
    assert backup.read_text(encoding="utf-8") == original

    omp_config.set_compaction(enabled=False)
    assert "enabled: false" in cfg.read_text(encoding="utf-8")
    assert omp_config.read_compaction() == {"thresholdPercent": 55, "enabled": False}

    # backup written once: still the original
    assert backup.read_text(encoding="utf-8") == original


def test_omp_config_creates_a_missing_block(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    cfg.write_text("model: x\nother:\n  key: 1\n", encoding="utf-8")
    monkeypatch.setattr(omp_config, "CONFIG_PATH", cfg)

    omp_config.set_compaction(thresholdPercent=40)
    text = cfg.read_text(encoding="utf-8")
    assert "thresholdPercent: 40" in text
    assert "key: 1" in text
    assert omp_config.read_compaction() == {"thresholdPercent": 40}


def test_omp_config_rejects_an_invalid_percent(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    cfg.write_text("compaction:\n  thresholdPercent: 70\n", encoding="utf-8")
    monkeypatch.setattr(omp_config, "CONFIG_PATH", cfg)

    with pytest.raises(ValueError):
        omp_config.set_compaction(thresholdPercent=5)
    with pytest.raises(ValueError):
        omp_config.set_compaction(thresholdPercent=99)
    assert "thresholdPercent: 70" in cfg.read_text(encoding="utf-8")
    assert not cfg.with_name("config.yml.ompwire-bak").exists()


# ------------------------------------------------------------------ telegram


def test_telegram_compaction_updates_the_config_and_replies(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    cfg.write_text("compaction:\n  thresholdPercent: 70\n", encoding="utf-8")
    monkeypatch.setattr(omp_config, "CONFIG_PATH", cfg)

    async def go():
        agent = _make_agent(_config(tmp_path), [])()
        transport = _transport(tmp_path, agent)
        await transport.cmd_compaction(_update("/compaction 60"),
                                       types.SimpleNamespace(args=["60"]))
        return agent

    asyncio.run(go())
    assert "thresholdPercent: 60" in cfg.read_text(encoding="utf-8")


def test_telegram_compact_while_busy_is_queued_not_steered(tmp_path):
    async def go():
        agent = _make_agent(_config(tmp_path), [])()
        agent.busy = True
        steered: list[str] = []

        async def fake_steer(_text, _images=None):
            steered.append(_text)
            return "injected", ""

        agent.steer = fake_steer
        transport = _transport(tmp_path, agent)
        queued: list[str] = []

        def fake_enqueue(chat_id, kind, payload):
            queued.append(payload.text)
            return 1

        transport.enqueue = fake_enqueue
        await transport.cmd_compact(_update("/compact"), None)
        return steered, queued

    steered, queued = asyncio.run(go())
    assert not steered
    assert queued == ["/compact"]


def test_unregistered_command_is_forwarded_as_a_prompt(tmp_path):
    async def go():
        agent = _make_agent(_config(tmp_path), [])()
        transport = _transport(tmp_path, agent)
        submitted: list[str] = []

        async def fake_submit(chat_id, payload, message):
            submitted.append(payload.text)

        transport.submit = fake_submit
        await transport.on_command(_update("/skill:foo bar"), None)
        return submitted

    assert asyncio.run(go()) == ["/skill:foo bar"]


def test_registered_command_never_reaches_the_forwarder(tmp_path):
    async def go():
        agent = _make_agent(_config(tmp_path), [])()
        transport = _transport(tmp_path, agent)

        async def fake_submit(*_a):
            raise AssertionError("registered commands must stop at their handler")

        transport.submit = fake_submit
        await transport.on_command(_update("/status"), None)
        return True

    assert asyncio.run(go())


# ------------------------------------------------------------------ cli


def test_cli_context_prints_the_line(tmp_path, monkeypatch, capsys):
    from ompwire import cli

    config = _config(tmp_path)
    lines = iter(["/context", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(lines))

    async def go():
        surface = cli.TerminalSurface(config)
        agent = engine.ChatAgent(config, "cli-test", surface)
        agent._on_usage(74886, 1_048_576)
        await asyncio.sleep(0)
        return await cli._repl(agent, surface)

    assert asyncio.run(go()) == 0
    out = capsys.readouterr().out
    assert "74.9k / 1.05M (7%)" in out


def _transcript(root, session_id, entries):
    import json

    folder = root / "omp-sessions" / "-"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"2026-10-24T15-09-06-491Z_{session_id}.jsonl"
    with path.open("a") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return path


def _call(inp, out, read=0, write=0):
    return {"type": "message", "message": {"role": "assistant", "usage": {
        "input": inp, "output": out, "cacheRead": read, "cacheWrite": write}}}


def test_session_totals_come_from_the_transcript_and_survive_a_restart(tmp_path):
    from ompwire import omp_sessions

    path = _transcript(tmp_path, "abc", [_call(100, 50, 700_000), _call(395, 216)])
    first = omp_sessions.usage("abc")
    assert (first.calls, first.input, first.output, first.cache_read) == (2, 495, 266, 700_000)
    # the 611-token call is an unanchored report, not the context
    assert first.context == 700_150
    _transcript(tmp_path, "abc", [_call(1_744, 245, 724_501)])
    assert omp_sessions.usage("abc").calls == 3
    # a new process has an empty cache and still reads the whole history
    omp_sessions._cache.clear()
    again = omp_sessions.usage("abc")
    assert (again.calls, again.input, again.context) == (3, 2_239, 726_490)
    assert path.exists()


def test_a_compaction_entry_lets_the_context_fall(tmp_path):
    from ompwire import omp_sessions

    _transcript(tmp_path, "c", [_call(10, 10, 700_000), {"type": "compaction"}, _call(30_000, 100)])
    assert omp_sessions.usage("c").context == 30_100


def test_a_collapsed_report_after_a_restart_keeps_the_context_and_says_nothing(tmp_path):
    _transcript(tmp_path, "s", [_call(103, 544, 718_041)])

    async def go():
        agent = _make_agent(_config(tmp_path), [])()
        agent._seed_context()  # what ensure() does after session/load
        agent._on_usage(395, 1_048_576)
        await asyncio.sleep(0.05)
        agent._on_usage(726_490, 1_048_576)
        await asyncio.sleep(0.05)
        return agent

    agent = asyncio.run(go())
    assert agent.context["used"] == 726_490
    assert not [m for m in agent.surface.app.bot.messages if "compacted" in m]


def test_usage_card_shows_session_totals_without_the_reset_footer(tmp_path):
    _transcript(tmp_path, "s", [_call(419_018, 418_615, 239_017_895)])
    _transcript(tmp_path, "s", [_call(1_000, 1_000, 732_000)])

    async def go():
        agent = _make_agent(_config(tmp_path), [])()
        agent.context.update(used=734_000, size=1_048_576)
        cfg = tmp_path / "omp-config.yml"
        cfg.write_text("compaction:\n  thresholdPercent: 70\n")
        transport = _transport(tmp_path, agent)
        msg = types.SimpleNamespace(replies=[])

        async def reply_text(body, **_kw):
            msg.replies.append(body)

        msg.reply_text = reply_text
        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=7, type="private"),
                                       effective_user=types.SimpleNamespace(id=1),
                                       effective_message=msg)
        transport._authorized = lambda _u: True
        await transport.cmd_usage(update, None)
        return msg.replies[0]

    body = asyncio.run(go())
    assert "compacts  at 70% (734k)" in body
    assert "input     420k" in body and "239.7M read" in body and "calls     2" in body
    assert "734k / 1.05M (69%)" in body
    assert "reset" not in body


def _compaction(before, after, at="2026-10-25T03:41:30.184Z"):
    return {"type": "compaction", "timestamp": at, "tokensBefore": before, "tokensAfter": after}


def test_a_compaction_omp_logged_during_a_turn_gets_one_notice(tmp_path):
    _transcript(tmp_path, "s", [_call(100, 500, 734_000)])

    async def go():
        agent = _make_agent(_config(tmp_path), [message("ok")])()
        agent._seed_context()
        await agent.ask("first")  # nothing new in the transcript: silent
        _transcript(tmp_path, "s", [_compaction(735_061, 68_185), _call(30_000, 900, 100_000)])
        await agent.ask("second")
        await agent.ask("third")  # the same compaction is not reported twice
        return agent

    agent = asyncio.run(go())
    notices = [m for m in agent.surface.app.bot.messages if "compacted" in m]
    assert len(notices) == 1
    assert "735.1k → 68.2k" in notices[0] and "WIB" in notices[0]


def test_compactions_already_in_the_transcript_are_not_announced_after_a_restart(tmp_path):
    _transcript(tmp_path, "s", [_call(100, 500, 734_000), _compaction(735_061, 68_185),
                                _call(30_000, 900, 100_000)])

    async def go():
        agent = _make_agent(_config(tmp_path), [message("ok")])()
        agent._seed_context()
        await agent.ask("hello")
        return agent

    agent = asyncio.run(go())
    assert not [m for m in agent.surface.app.bot.messages if "compacted" in m]


def test_a_compact_turn_does_not_repeat_omps_own_report(tmp_path):
    _transcript(tmp_path, "s", [_call(100, 500, 734_000)])

    async def go():
        agent = _make_agent(_config(tmp_path), [message("Compaction complete.")])()
        agent._seed_context()
        _transcript(tmp_path, "s", [_compaction(734_600, 60_000)])
        await agent.ask("/compact")
        return agent

    agent = asyncio.run(go())
    assert not [m for m in agent.surface.app.bot.messages if "omp compacted" in m]


def test_the_context_limit_is_remembered_across_a_restart(tmp_path):
    config = _config(tmp_path)

    async def go():
        first = _make_agent(config, [])()
        first._on_usage(200_000, 1_048_576)
        second = _make_agent(config, [])()  # a new process: no usage_update yet
        return second.context["size"]

    assert asyncio.run(go()) == 1_048_576


def test_usage_card_shows_when_omp_last_compacted(tmp_path):
    _transcript(tmp_path, "s", [_call(100, 500, 734_000), _compaction(735_061, 68_185),
                                _call(30_000, 900, 166_000)])

    async def go():
        agent = _make_agent(_config(tmp_path), [])()
        transport = _transport(tmp_path, agent)
        transport._authorized = lambda _u: True
        update = _update("/usage")
        await transport.cmd_usage(update, None)
        return update.effective_message.replies[0]

    body = asyncio.run(go())
    assert "735.1k → 68.2k" in body
    assert "limit not reported yet" in body


def test_the_transcript_names_each_tool_call_by_its_id():
    """ACP sends only a coarse kind; the tool's real name comes from omp's own transcript."""
    import json

    from ompwire import omp_sessions

    folder = omp_sessions.SESSIONS_DIR / "-"
    folder.mkdir(parents=True)
    path = folder / "2026-10-25T00-00-00-000Z_xyz.jsonl"
    call = {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "cek"},
        {"type": "toolCall", "id": "toolu_1|fc_toolu_1", "name": "wait", "arguments": {}}]}}
    path.write_text(json.dumps(call) + "\n")
    assert omp_sessions.usage("xyz").tools == {"toolu_1|fc_toolu_1": "wait"}
    later = {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": "toolu_2", "name": "mcp__kaneo_get_task", "arguments": {}}]}}
    with path.open("a") as handle:
        handle.write(json.dumps(later) + "\n")
    assert omp_sessions.usage("xyz").tools["toolu_2"] == "mcp__kaneo_get_task"


def _options(model, mode="default"):
    return [{"id": "model", "currentValue": model, "options": []},
            {"id": "mode", "currentValue": mode, "options": []},
            {"id": "thinking", "currentValue": "high", "options": []}]


def _option_update(model, mode="default"):
    return Update(kind="config_option_update", session_id="s",
                  raw={"sessionUpdate": "config_option_update", "configOptions": _options(model, mode)})


def test_a_model_or_mode_switch_made_by_omp_is_told_to_the_chat(tmp_path):
    """omp can change model or mode itself (a fallback, plan approval); it used to happen silently."""
    async def go():
        agent = _make_agent(_config(tmp_path), [])()
        agent.options = {opt["id"]: opt for opt in _options("a/one")}
        agent._on_update(_option_update("a/two"))
        agent._on_update(Update(kind="current_mode_update", session_id="s",
                                raw={"sessionUpdate": "current_mode_update", "currentModeId": "plan"}))
        for _ in range(5):
            await asyncio.sleep(0)
        return agent

    agent = asyncio.run(go())
    sent = agent.surface.app.bot.messages
    assert any("model" in body and "a/one" in body and "a/two" in body for body in sent), sent
    assert any("mode" in body and "default" in body and "plan" in body for body in sent), sent
    assert agent.options["model"]["currentValue"] == "a/two"
    assert agent.options["mode"]["currentValue"] == "plan"
    assert agent._updates.empty()  # never reaches the turn renderer


def test_a_switch_the_chat_asked_for_is_not_reported_back(tmp_path):
    """omp echoes /model as config_option_update before it replies; that is not omp's own change."""
    async def go():
        agent = _make_agent(_config(tmp_path), [])()
        agent.options = {opt["id"]: opt for opt in _options("a/one")}

        def set_config_option(_sid, config_id, value):
            agent._on_update(_option_update(value))  # the echo arrives first
            return {"configOptions": _options(value)}

        agent.client.set_config_option = set_config_option
        await agent.set_option("model", "a/two")
        for _ in range(5):
            await asyncio.sleep(0)
        return agent

    agent = asyncio.run(go())
    assert agent.options["model"]["currentValue"] == "a/two"
    assert not any("switched" in body for body in agent.surface.app.bot.messages)
