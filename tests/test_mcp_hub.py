"""mcp-hub -> ACP conversion. omp rejects entries whose array fields are missing."""

from __future__ import annotations

import pytest

from ompwire.mcp_hub import hub_servers, normalize

yaml = pytest.importorskip("yaml")

HUB = """
servers:
  remote-one:
    type: remote
    url: https://example.test/mcp
    headers:
      Authorization: Bearer abc
  stdio-one:
    command: ['npx', '-y', 'some-mcp@1']
    env:
      TOKEN: xyz
  disabled-one:
    command: ['npx', '-y', 'nope']
    enabled: false
  skip-me:
    command: ['npx', '-y', 'skip']
"""


@pytest.fixture()
def hub_file(tmp_path):
    path = tmp_path / "servers.yaml"
    path.write_text(HUB, encoding="utf-8")
    return path


def test_remote_entry_gets_type_url_and_headers_array(hub_file):
    entries = {e["name"]: e for e in hub_servers(hub_file, exclude=["skip-me"])}
    remote = entries["remote-one"]
    assert remote == {
        "name": "remote-one",
        "type": "http",
        "url": "https://example.test/mcp",
        "headers": [{"name": "Authorization", "value": "Bearer abc"}],
    }


def test_stdio_entry_splits_argv_and_maps_env(hub_file):
    entries = {e["name"]: e for e in hub_servers(hub_file, exclude=["skip-me"])}
    stdio = entries["stdio-one"]
    assert stdio["type"] == "stdio"
    assert stdio["command"] == "npx"
    assert stdio["args"] == ["-y", "some-mcp@1"]
    assert stdio["env"] == [{"name": "TOKEN", "value": "xyz"}]


def test_disabled_entries_are_skipped(hub_file):
    names = {e["name"] for e in hub_servers(hub_file, exclude=["skip-me"])}
    assert "disabled-one" not in names
    assert names == {"remote-one", "stdio-one"}


def test_include_whitelists(hub_file):
    names = [e["name"] for e in hub_servers(hub_file, include=["stdio-one"])]
    assert names == ["stdio-one"]


def test_normalize_fills_required_arrays():
    stdio, remote = normalize([{"name": "a", "command": "bun"},
                               {"name": "b", "type": "http", "url": "https://x"}])
    assert stdio["env"] == [] and stdio["args"] == []
    assert remote["headers"] == []
    assert stdio["command"] == "bun"


def test_missing_hub_file_is_not_fatal(tmp_path):
    assert hub_servers(tmp_path / "nope.yaml") == []
