"""Uninstall removes ompwire and leaves the agent alone."""

from __future__ import annotations

from ompwire import uninstall


def _layout(monkeypatch, tmp_path):
    """One install: config, each gateway's definition and state, all in one folder."""
    config = tmp_path / "config" / "ompwire"
    data = tmp_path / "share" / "ompwire"
    bot = config / "gateways" / "bot"
    bot.mkdir(parents=True)
    (config / "config.toml").write_text("[acp]\n", encoding="utf-8")
    (bot / "gateway.toml").write_text('type = "telegram"\n', encoding="utf-8")
    (bot / ".env").write_text("OMPWIRE_GATEWAY_BOT_TOKEN=123:abc\n", encoding="utf-8")
    (bot / "sessions.db").write_text("x", encoding="utf-8")
    (config / "cli").mkdir()
    (config / "cli" / "sessions.db").write_text("x", encoding="utf-8")
    data.mkdir(parents=True)
    (data / "mcp").mkdir()
    (data / "mcp" / "blob.bin").write_bytes(b"0" * 2048)
    monkeypatch.setattr(uninstall, "CONFIG_DIR", config)
    monkeypatch.setattr(uninstall, "LEGACY_STATE_DIR", tmp_path / "old" / "state" / "ompwire")
    monkeypatch.setattr(uninstall, "DATA_DIR", data)
    monkeypatch.delenv("OMPWIRE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(uninstall, "running_serves", lambda: [])
    monkeypatch.setattr(uninstall, "package_command", lambda: [])
    return config, bot / "sessions.db", data


def test_targets_cover_config_state_and_data(monkeypatch, tmp_path):
    config, _state, data = _layout(monkeypatch, tmp_path)
    plan = [path for path, _note in uninstall.targets()]
    assert plan == [config, data]


def test_keep_config_leaves_definitions_and_tokens_and_takes_the_state(monkeypatch, tmp_path):
    config, state, data = _layout(monkeypatch, tmp_path)
    assert uninstall.run(yes=True, keep_config=True) == 0
    bot = config / "gateways" / "bot"
    assert (config / "config.toml").is_file()
    assert (bot / "gateway.toml").is_file() and (bot / ".env").is_file()
    assert not state.exists() and not (config / "cli").exists() and not data.exists()


def test_dry_run_removes_nothing(monkeypatch, tmp_path, capsys):
    config, state, data = _layout(monkeypatch, tmp_path)
    assert uninstall.run(dry_run=True) == 0
    assert config.is_dir() and state.is_file() and data.is_dir()
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
    assert config.is_dir() and state.is_file()
    assert "nothing removed" in capsys.readouterr().out


def test_a_running_transport_blocks_removal(monkeypatch, tmp_path, capsys):
    config, _state, _data = _layout(monkeypatch, tmp_path)
    monkeypatch.setattr(uninstall, "running_serves", lambda: [(4242, "ompwire gateway _run telegram")])

    assert uninstall.run(yes=True) == 1
    assert config.is_dir()
    out = capsys.readouterr().out
    assert "4242" in out and "--force" in out


def test_force_stops_the_transport_before_removing(monkeypatch, tmp_path):
    _config, _state, _data = _layout(monkeypatch, tmp_path)
    monkeypatch.setattr(uninstall, "running_serves", lambda: [(4242, "ompwire gateway _run telegram")])
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


def test_every_generated_unit_is_removed_not_just_the_telegram_one(monkeypatch, tmp_path):
    from ompwire import service

    _layout(monkeypatch, tmp_path)
    unit_dir = tmp_path / "units"
    unit_dir.mkdir(exist_ok=True)
    removed: list[str] = []

    def unit_path(gateway: str = "telegram") -> object:
        return unit_dir / service.unit_name(gateway)

    monkeypatch.setattr(service, "unit_path", unit_path)
    for name in ("ompwire-telegram.service", "ompwire-schedule-morning.service",
                 "ompwire-schedule-morning.timer", "ompwire-other.service"):
        (unit_dir / name).write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(service, "remove",
                        lambda *a, **kw: removed.append(kw["path"].name) or 0)

    assert uninstall.run(yes=True) == 0
    assert sorted(removed) == ["ompwire-other.service", "ompwire-schedule-morning.service",
                               "ompwire-schedule-morning.timer", "ompwire-telegram.service"]


def test_a_sandboxed_run_never_reaches_the_real_install(monkeypatch, tmp_path):
    real_config = tmp_path / "real-config"
    real_legacy = tmp_path / "real-legacy"
    real_data = tmp_path / "real-data"
    for path in (real_config, real_legacy, real_data):
        path.mkdir()
    monkeypatch.setattr(uninstall, "CONFIG_DIR", real_config)
    monkeypatch.setattr(uninstall, "LEGACY_STATE_DIR", real_legacy)
    monkeypatch.setattr(uninstall, "DATA_DIR", real_data)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("OMPWIRE_CONFIG_DIR", str(sandbox))

    for keep in (False, True):
        paths = [path for path, _note in uninstall.targets(keep_config=keep)]
        assert not {real_config, real_legacy, real_data} & set(paths)
        assert all(sandbox in path.parents or path == sandbox for path in paths)
