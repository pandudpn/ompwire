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


def test_page_slice_clamps_and_pages():
    values = [str(i) for i in range(20)]
    window, page, pages = Transport._page_slice(values, 0, 8)
    assert (window[0], len(window), page, pages) == ("0", 8, 0, 3)
    window, page, pages = Transport._page_slice(values, 2, 8)
    assert (window[0], len(window), page, pages) == ("16", 4, 2, 3)
    # out of range clamps instead of raising
    window, page, pages = Transport._page_slice(values, 99, 8)
    assert (page, pages) == (2, 3)
    assert Transport._page_slice([], 0, 8) == ([], 0, 1)


def test_pick_indices_map_to_the_full_option_list():
    """A page inside a provider must select the global option, not a local one."""
    agent = FakeAgent({
        "model": {
            "currentValue": "zai/a",
            "options": [{"value": "commandcode/x"}, {"value": "zai/a"}, {"value": "zai/b"}],
        }
    })
    transport = Transport(load_config(env={}))
    providers = dict(transport._providers(agent, "model"))
    values = transport._values(agent, "model")
    zai_indices = providers["zai"]
    assert [values[i] for i in zai_indices] == ["zai/a", "zai/b"]
    assert zai_indices == [1, 2]
