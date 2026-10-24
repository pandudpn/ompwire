"""Offline tests for the transport helpers: MCP shaping and the picker grouping.

Both encode behaviour that only shows up against the real agent, so they are
pinned here without needing a bot token or an ACP process.
"""

from __future__ import annotations

from ompwire.config import load_config
from ompwire.telegram import Transport, normalize_mcp_servers


class FakeAgent:
    def __init__(self, options):
        self.options = options


def test_env_object_is_converted_to_array():
    shaped = normalize_mcp_servers([{"name": "x", "command": "npx", "args": ["-y", "pkg"], "env": {"A": "1"}}])
    assert shaped[0]["env"] == [{"name": "A", "value": "1"}]
    assert shaped[0]["type"] == "stdio"


def test_missing_env_becomes_empty_list():
    # omp 18.12.6 rejects session/new when env is absent or an object
    shaped = normalize_mcp_servers([{"name": "y", "command": "bun"}])
    assert shaped[0]["env"] == []
    assert shaped[0]["args"] == []


def test_providers_group_by_slash_prefix():
    agent = FakeAgent({
        "model": {
            "currentValue": "zai/glm-4.6",
            "options": [
                {"value": "zai/glm-4.6"},
                {"value": "zai/glm-5.1"},
                {"value": "commandcode/claude-fable-5"},
                {"value": "bare-model"},
            ],
        }
    })
    transport = Transport(load_config(env={}))
    providers = transport._providers(agent, "model")
    assert [name for name, _ in providers] == ["commandcode", "other", "zai"]
    counts = dict(providers)
    assert len(counts["zai"]) == 2
    assert len(counts["other"]) == 1
    assert transport._current(agent, "model") == "zai/glm-4.6"
    assert transport._values(agent, "model")[0] == "zai/glm-4.6"


def test_single_provider_stays_on_one_list():
    agent = FakeAgent({"mode": {"currentValue": "default", "options": [{"value": "default"}, {"value": "plan"}]}})
    transport = Transport(load_config(env={}))
    assert [name for name, _ in transport._providers(agent, "mode")] == ["other"]
