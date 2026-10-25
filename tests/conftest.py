"""Tests must not touch the machine they run on.

Every path that a command can write to is redirected here, once: a test that forgets to
patch `unit_path` would otherwise disable a real systemd unit, and a test that reads the
real `overrides.json` reports a per-chat choice as if it were the user's config. Both of
those happened before this file existed.
"""

from __future__ import annotations

import pytest

from ompwire import config as config_module
from ompwire import gateways, lock, omp_config, omp_sessions, service


@pytest.fixture(autouse=True)
def isolate_real_paths(monkeypatch, tmp_path):
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)

    monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": units / f"{gateway}.service")
    monkeypatch.setattr(service, "legacy_unit_path", lambda: units / "legacy.service")
    monkeypatch.setattr(gateways, "gateways_file", lambda: tmp_path / "gateways.toml")
    monkeypatch.setattr(lock, "lock_path", lambda gateway="": state / f"serve-{gateway or 'default'}.lock")
    monkeypatch.setattr(config_module, "OVERRIDES_PATH", state / "overrides.json")
    # every gateway's sessions, overrides, log and lock live under this; a status test once
    # created an empty store in the real ~/.local/state/ompwire/gateways/telegram
    monkeypatch.setattr(config_module, "default_state_dir", lambda: state)
    # the session store holds per-chat preferences; a stray fixture once wrote a real one
    monkeypatch.setitem(config_module.DEFAULTS["session"], "store", str(state / "sessions.db"))
    # systemd is the host's; nothing in the suite should call it for real
    monkeypatch.setattr(service, "_systemd_available", lambda: False)
    # the agent is the developer's: a test that cares about a missing agent patches this to None
    monkeypatch.setattr(service, "agent_binary", lambda command="": "/usr/bin/omp")
    monkeypatch.setattr(service, "agent_command", lambda: "omp")
    # pacing is real time; the tests about pacing set their own gap
    monkeypatch.setitem(config_module.DEFAULTS["stream"], "min_gap", 0.0)
    # omp's own files: the transcripts the usage card reads and the config /compaction edits
    monkeypatch.setattr(omp_sessions, "SESSIONS_DIR", tmp_path / "omp-sessions")
    monkeypatch.setattr(omp_sessions, "_cache", {})
    monkeypatch.setattr(omp_config, "CONFIG_PATH", tmp_path / "omp-config.yml")
    return tmp_path
