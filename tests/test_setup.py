"""Setup: agent first, then config, then the token — in that order."""

from __future__ import annotations

import tomllib

from ompwire import setup


def _sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("OMPWIRE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return tmp_path


def test_missing_agent_stops_before_writing_anything(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": None)
    assert setup.run(yes=True) == 1                     # --yes means "do not install"
    out = capsys.readouterr().out
    assert "omp.sh/install" in out
    assert not (tmp_path / "config.toml").exists()


def test_agent_install_runs_before_config(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    calls: list[str] = []
    state = {"found": None}

    def fake_install():
        calls.append("install-omp")
        state["found"] = "/home/x/.local/bin/omp"
        return True

    monkeypatch.setattr(setup, "agent_path", lambda command="omp": state["found"])
    monkeypatch.setattr(setup, "install_agent", fake_install)

    assert setup.run(yes=True, install_omp=True) == 0
    assert calls == ["install-omp"]
    assert (tmp_path / "config.toml").is_file()


def test_config_is_written_and_parses(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": "/usr/bin/omp")
    assert setup.run(yes=True) == 0

    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["acp"]["command"] == "omp"
    assert data["acp"]["args"] == ["--mode", "acp"]
    assert data["telegram"]["allowed_users"] == []
    assert data["approval"]["policy"] == "always-ask"
    assert "fenced code" in data["agent"]["append_system_prompt"]


def test_existing_config_is_kept_unless_forced(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": "/usr/bin/omp")
    config = tmp_path / "config.toml"
    config.write_text("[acp]\ncommand = \"mine\"\n", encoding="utf-8")

    assert setup.run(yes=True) == 0
    assert "mine" in config.read_text()
    assert "config kept" in capsys.readouterr().out

    assert setup.run(yes=True, force=True) == 0
    assert (tmp_path / "config.toml.bak").is_file()
    assert "[acp]" in config.read_text() and "mine" not in config.read_text()


def test_token_goes_to_env_with_0600_and_is_verified(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": "/usr/bin/omp")
    monkeypatch.setattr(setup, "bot_identity", lambda token, timeout=10.0: "my_bot")
    monkeypatch.setattr(setup, "_ask", lambda prompt, default, yes=False: default)
    monkeypatch.setattr(setup, "_ask_secret", lambda prompt, yes: "123:abc", raising=False)

    assert setup.run(yes=False) == 0
    env_file = tmp_path / ".env"
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert env_file.read_text().strip() == "TELEGRAM_BOT_TOKEN=123:abc"
    out = capsys.readouterr().out
    assert "token valid: @my_bot" in out
    assert "123:abc" not in out                      # never echoed back


def test_allowed_users_are_parsed_into_a_list(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": "/usr/bin/omp")
    monkeypatch.setattr(setup, "_ask", lambda prompt, default, yes=False: "111, 222 ,x")
    monkeypatch.setattr(setup, "_ask_secret", lambda prompt, yes: "", raising=False)

    assert setup.run(yes=False) == 0
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["telegram"]["allowed_users"] == [111, 222]
    assert data["telegram"]["home_channel"] == 111
