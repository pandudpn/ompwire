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
