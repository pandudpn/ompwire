"""Uninstall removes ompwire and leaves the agent alone."""

from __future__ import annotations

from ompwire import uninstall


def _layout(monkeypatch, tmp_path):
    config = tmp_path / "config" / "ompwire"
    state = tmp_path / "state" / "ompwire"
    data = tmp_path / "share" / "ompwire"
    config.mkdir(parents=True)
    (config / "config.toml").write_text("[acp]\n", encoding="utf-8")
    (config / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\n", encoding="utf-8")
    state.mkdir(parents=True)
    (state / "sessions.db").write_text("x", encoding="utf-8")
    data.mkdir(parents=True)
    (data / "mcp").mkdir()
    (data / "mcp" / "blob.bin").write_bytes(b"0" * 2048)
    monkeypatch.setattr(uninstall, "CONFIG_DIR", config)
    monkeypatch.setattr(uninstall, "STATE_DIR", state)
    monkeypatch.setattr(uninstall, "DATA_DIR", data)
    monkeypatch.delenv("OMPWIRE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(uninstall, "running_serves", lambda: [])
    monkeypatch.setattr(uninstall, "package_command", lambda: [])
    return config, state, data


def test_targets_cover_config_state_and_data(monkeypatch, tmp_path):
    config, state, data = _layout(monkeypatch, tmp_path)
    plan = [path for path, _note in uninstall.targets()]
    assert plan == [config, state, data]


def test_keep_config_leaves_the_token_behind(monkeypatch, tmp_path):
    config, state, data = _layout(monkeypatch, tmp_path)
    plan = [path for path, _note in uninstall.targets(keep_config=True)]
    assert config not in plan
    assert plan == [state, data]


def test_dry_run_removes_nothing(monkeypatch, tmp_path, capsys):
    config, state, data = _layout(monkeypatch, tmp_path)
    assert uninstall.run(dry_run=True) == 0
    assert config.is_dir() and state.is_dir() and data.is_dir()
    out = capsys.readouterr().out
    assert "dry run" in out and "Nothing was removed" in out


def test_yes_removes_everything_ompwire_created(monkeypatch, tmp_path, capsys):
    config, state, data = _layout(monkeypatch, tmp_path)
    assert uninstall.run(yes=True) == 0
    assert not config.exists()
    assert not state.exists()
    assert not data.exists()
    out = capsys.readouterr().out
    assert "Left untouched: ~/.omp" in out


def test_declining_the_prompt_removes_nothing(monkeypatch, tmp_path, capsys):
    config, state, _data = _layout(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert uninstall.run(yes=False) == 0
    assert config.is_dir() and state.is_dir()
    assert "nothing removed" in capsys.readouterr().out


def test_a_running_transport_blocks_removal(monkeypatch, tmp_path, capsys):
    config, _state, _data = _layout(monkeypatch, tmp_path)
    monkeypatch.setattr(uninstall, "running_serves", lambda: [(4242, "ompwire serve")])

    assert uninstall.run(yes=True) == 1
    assert config.is_dir()
    out = capsys.readouterr().out
    assert "4242" in out and "--force" in out


def test_force_stops_the_transport_before_removing(monkeypatch, tmp_path):
    _config, _state, _data = _layout(monkeypatch, tmp_path)
    monkeypatch.setattr(uninstall, "running_serves", lambda: [(4242, "ompwire serve")])
    stopped: list[int] = []
    monkeypatch.setattr(uninstall, "stop", lambda pid: stopped.append(pid) or True)

    assert uninstall.run(yes=True, force=True) == 0
    assert stopped == [4242]


def test_agent_state_is_never_touched(monkeypatch, tmp_path, capsys):
    _layout(monkeypatch, tmp_path)
    agent_dir = tmp_path / "omp"
    agent_dir.mkdir()
    (agent_dir / "agent.db").write_text("credentials", encoding="utf-8")
    monkeypatch.setattr(uninstall, "NEVER_TOUCH", (agent_dir,))
    # an ompwire directory nested under the agent's home must be skipped, not deleted
    monkeypatch.setattr(uninstall, "DATA_DIR", agent_dir / "share")
    (agent_dir / "share").mkdir()
    (agent_dir / "share" / "junk").write_text("x", encoding="utf-8")

    assert uninstall.run(yes=True) == 0
    assert (agent_dir / "agent.db").is_file()
    assert "skip" in capsys.readouterr().out


def test_package_command_follows_the_installer(monkeypatch):
    monkeypatch.setattr("ompwire.update.install_mode", lambda: "uv")
    assert uninstall.package_command() == ["uv", "tool", "uninstall", "ompwire"]
    monkeypatch.setattr("ompwire.update.install_mode", lambda: "pipx")
    assert uninstall.package_command() == ["pipx", "uninstall", "ompwire"]
    monkeypatch.setattr("ompwire.update.install_mode", lambda: "checkout")
    assert uninstall.package_command() == []


def test_human_readable_sizes():
    assert uninstall.human(512) == "512B"
    assert uninstall.human(2048) == "2.0K"
    assert uninstall.human(5 * 1024 * 1024) == "5.0M"
