"""Config merging: which layer wins, and what a runtime toggle is for."""

from __future__ import annotations

import json

from ompwire import config as config_module


def test_a_runtime_toggle_beats_the_config_file(tmp_path, monkeypatch):
    """`/yolo` and `/approval` are decisions made after the file was written."""
    target = tmp_path / "config.toml"
    target.write_text('[agent]\nyolo = false\n\n[approval]\npolicy = "ask"\n', encoding="utf-8")
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({"approval": {"policy": "allow"}}), encoding="utf-8")
    monkeypatch.setattr(config_module, "OVERRIDES_PATH", overrides)

    loaded = config_module.load_config(str(target), env={})
    assert config_module.approval_policy(loaded) == "allow"


def test_a_config_file_value_survives_an_unrelated_override(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text('[agent]\nmodel = "omniroute/coding-max"\n', encoding="utf-8")
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({"stream": {"show_reasoning": True}}), encoding="utf-8")
    monkeypatch.setattr(config_module, "OVERRIDES_PATH", overrides)

    loaded = config_module.load_config(str(target), env={})
    assert loaded.get("agent", "model") == "omniroute/coding-max"
    assert loaded.get("stream", "show_reasoning") is True


def test_config_dir_override_reads_only_that_directory(tmp_path, monkeypatch):
    """A sandbox install must not inherit the real install's credentials or settings."""
    from ompwire import config as config_module

    real = tmp_path / "real"
    real.mkdir()
    (real / ".env").write_text("TELEGRAM_BOT_TOKEN=real-token\n")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setattr(config_module, "DEFAULT_ENV_PATHS", (real / ".env",))
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATHS", (real / "config.toml",))
    (real / "config.toml").write_text('[agent]\nmodel = "real-model"\n')

    loaded = config_module.load_config(env={"OMPWIRE_CONFIG_DIR": str(sandbox)})

    assert loaded.get("agent", "model") == ""
    assert not loaded.get("telegram", "bot_token")


def test_state_migration_moves_idle_gateways_and_leaves_a_running_one(tmp_path):
    """Gateways restart one at a time; the first must not take a sibling's live database.
    Every gateway folder already exists in the new place, holding its gateway.toml."""
    import os

    old = config_module.LEGACY_STATE_DIR
    new = tmp_path / "config"
    for name in ("idle", "busy"):
        (old / "gateways" / name).mkdir(parents=True)
        (old / "gateways" / name / "sessions.db").write_text(name, encoding="utf-8")
        (new / "gateways" / name).mkdir(parents=True)
        (new / "gateways" / name / "gateway.toml").write_text("", encoding="utf-8")
    (old / "gateways" / "busy" / "serve.lock").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (old / "cli").mkdir()
    (old / "schedules" / "job").mkdir(parents=True)
    (old / "schedules" / "job" / "run.txt").write_text("out", encoding="utf-8")

    moved = config_module.migrate_state_dir(new)

    assert (new / "gateways" / "idle" / "sessions.db").read_text(encoding="utf-8") == "idle"
    assert (new / "gateways" / "idle" / "gateway.toml").is_file()
    assert (new / "cli").is_dir()
    assert (new / "schedule-logs" / "job" / "run.txt").read_text(encoding="utf-8") == "out"
    assert (old / "gateways" / "busy" / "sessions.db").is_file()
    assert not (new / "gateways" / "busy" / "sessions.db").exists()
    assert len(moved) == 3

    # once the busy one stops, its own restart finishes the move and the old root goes away
    (old / "gateways" / "busy" / "serve.lock").write_text("999999999\n", encoding="utf-8")
    config_module.migrate_state_dir(new)
    assert (new / "gateways" / "busy" / "sessions.db").read_text(encoding="utf-8") == "busy"
    assert not old.exists()


def test_state_migration_never_overwrites_a_newer_store(tmp_path):
    old = config_module.LEGACY_STATE_DIR
    (old / "gateways" / "bot").mkdir(parents=True)
    (old / "gateways" / "bot" / "sessions.db").write_text("old", encoding="utf-8")
    (old / "gateways" / "bot" / "ompwire.log").write_text("old log", encoding="utf-8")
    new = tmp_path / "config"
    (new / "gateways" / "bot").mkdir(parents=True)
    (new / "gateways" / "bot" / "sessions.db").write_text("new", encoding="utf-8")

    moved = config_module.migrate_state_dir(new)

    assert [line.rsplit("/", 1)[-1] for line in moved] == ["ompwire.log"]
    assert (new / "gateways" / "bot" / "sessions.db").read_text(encoding="utf-8") == "new"
    assert (old / "gateways" / "bot" / "sessions.db").read_text(encoding="utf-8") == "old"
