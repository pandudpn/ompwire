"""Setup: agent first, then config, then the token — in that order."""

from __future__ import annotations

import tomllib

from ompwire import setup


def _sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("OMPWIRE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return tmp_path


def _with_token(monkeypatch):
    """Pretend a valid token was typed and verified, and never read stdin."""
    monkeypatch.setattr(setup, "_ask_secret", lambda prompt, yes: "123:abc", raising=False)
    monkeypatch.setattr(setup, "bot_identity", lambda token, timeout=10.0: "test_bot")
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)


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
    _with_token(monkeypatch)

    assert setup.run(yes=True, install_omp=True) == 0
    assert calls == ["install-omp"]
    assert (tmp_path / "config.toml").is_file()


def test_config_is_written_and_parses(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": "/usr/bin/omp")
    _with_token(monkeypatch)
    assert setup.run(yes=True) == 0

    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["acp"]["command"] == "omp"
    assert data["acp"]["args"] == ["--mode", "acp"]
    assert data["gateway"]["platform"] == "telegram"
    assert data["telegram"]["allowed_users"] == []
    assert data["approval"]["policy"] == "always-ask"
    assert data["agent"]["thinking"] == "xhigh"
    assert data["agent"]["yolo"] is True
    assert data["agent"]["mode"] == "default"
    assert "fenced code" in data["agent"]["append_system_prompt"]


def test_existing_config_is_kept_unless_forced(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": "/usr/bin/omp")
    _with_token(monkeypatch)
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
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)
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
    def answer(prompt: str, default: str, *, yes: bool = False) -> str:
        if "allowed telegram user ids" in prompt:
            return "111, 222 ,x"
        return default

    monkeypatch.setattr(setup, "_ask", answer)
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)
    _with_token(monkeypatch)

    assert setup.run(yes=False) == 0
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["telegram"]["allowed_users"] == [111, 222]
    assert data["telegram"]["home_channel"] == 111


def test_no_token_fails_because_the_gateway_cannot_start(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": "/usr/bin/omp")
    monkeypatch.setattr(setup, "_ask", lambda prompt, default, yes=False: default)
    monkeypatch.setattr(setup, "_ask_secret", lambda prompt, yes: "", raising=False)
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)

    assert setup.run(yes=False) == 1
    out = capsys.readouterr().out
    assert "no usable TELEGRAM_BOT_TOKEN" in out
    assert (tmp_path / "config.toml").is_file()          # config still written
    assert not (tmp_path / ".env").exists()              # but no empty token file


def test_unverified_token_is_reported_and_stops(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": "/usr/bin/omp")
    monkeypatch.setattr(setup, "_ask", lambda prompt, default, yes=False: default)
    monkeypatch.setattr(setup, "_ask_secret", lambda prompt, yes: "bad:token", raising=False)
    monkeypatch.setattr(setup, "bot_identity", lambda token, timeout=10.0: "")
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)

    assert setup.run(yes=False) == 1
    out = capsys.readouterr().out
    assert "failed getMe" in out
    assert "bad:token" not in out


def test_unsupported_platform_stops_early(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": "/usr/bin/omp")
    monkeypatch.setattr(setup, "_ask", lambda prompt, default, yes=False: "discord")
    _with_token(monkeypatch)

    assert setup.run(yes=False) == 1
    assert "only telegram is implemented" in capsys.readouterr().out
    assert not (tmp_path / "config.toml").exists()


def test_env_token_is_reused_without_prompting(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:fromenv")
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": "/usr/bin/omp")
    monkeypatch.setattr(setup, "_ask", lambda prompt, default, yes=False: default)
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)
    monkeypatch.setattr(setup, "bot_identity", lambda token, timeout=10.0: "env_bot")
    monkeypatch.setattr(setup, "_ask_secret", lambda prompt, yes: (_ for _ in ()).throw(
        AssertionError("must not prompt when the env already has a token")), raising=False)

    assert setup.run(yes=True) == 0
    assert "999:fromenv" in (tmp_path / ".env").read_text()


def test_choose_pins_the_current_value_into_the_window(monkeypatch):
    """Alphabetical lists can put the current model past the cut-off."""
    choices = [f"prov/model-{index}" for index in range(40)]
    current = "zzz/current-model"
    choices.append(current)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(map(str, args))))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    picked = setup.choose("model", choices, current, yes=False, limit=5)
    assert picked == current
    assert any(current in line for line in printed)


def test_platform_error_does_not_echo_something_that_looks_like_a_token(
        monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": "/usr/bin/omp")
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)
    monkeypatch.setattr(setup, "_ask", lambda prompt, default, yes=False:
                        "8384807287:AAEwqLnYsecret" if "platform" in prompt else default)

    assert setup.run(yes=False) == 1
    out = capsys.readouterr().out
    assert "AAEwqLnYsecret" not in out
    assert "looks like a secret" in out
