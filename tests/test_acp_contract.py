"""Contract tests against the live `omp --mode acp` agent.

`--mode acp` is not advertised in `omp --help`, so these assertions are the only
thing standing between us and silent upstream drift. They are skipped when the
agent binary is absent (CI without omp), never mocked: the point is the real wire.
"""

from __future__ import annotations

import shutil

import pytest

from ompwire.acp import AcpClient, pick_permission_option

pytestmark = pytest.mark.skipif(shutil.which("omp") is None, reason="omp not installed")


@pytest.fixture()
def client(tmp_path):
    agent = AcpClient(cwd=str(tmp_path))
    yield agent
    agent.close()


def test_initialize_reports_omp_and_session_capabilities(client):
    info = client.initialize()
    assert info.get("protocolVersion") == 1
    agent_info = info.get("agentInfo") or {}
    assert agent_info.get("name") == "omp"
    assert agent_info.get("version"), "agent must report a version"
    caps = info.get("agentCapabilities") or {}
    assert caps.get("loadSession") is True, "resume across processes depends on loadSession"
    assert "close" in (caps.get("sessionCapabilities") or {})


def test_session_new_exposes_model_and_mode_options(client, tmp_path):
    client.initialize()
    session = client.new_session(str(tmp_path))
    assert session.get("sessionId")
    options = {opt.get("id"): opt for opt in (session.get("configOptions") or [])}
    assert "model" in options, "model picker needs a configOptions entry with id=model"
    assert "mode" in options
    assert options["mode"].get("currentValue") in {"default", "plan"}
    client.close_session(session["sessionId"])


def test_prompt_streams_text_and_reports_usage(client, tmp_path):
    client.initialize()
    session = client.new_session(str(tmp_path))
    result = client.prompt(session["sessionId"], "Reply with exactly: OK. Do not use any tools.", timeout=300)
    client.close_session(session["sessionId"])
    assert result.stop_reason == "end_turn"
    assert "OK" in result.text
    assert result.usage.get("inputTokens", 0) > 0
    kinds = {update.kind for update in result.updates}
    assert "agent_message_chunk" in kinds


def test_permission_option_picker_prefers_allow():
    params = {"options": [{"optionId": "x", "kind": "reject_once"}, {"optionId": "y", "kind": "allow_once"}]}
    assert pick_permission_option(params, "allow") == "y"
    assert pick_permission_option(params, "deny") == "x"
