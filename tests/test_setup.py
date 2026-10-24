"""Setup: agent, then provider, then token, then settings and MCP."""

from __future__ import annotations

import json
import tomllib

from ompwire import setup

CATALOG = {
    "models": ["zai/glm-5.1", "zai/glm-5.3-flash", "anthropic/claude-opus-5", "web/zai"],
    "modes": ["default", "plan"],
    "thinking": ["off", "low", "high", "xhigh"],
    "current": {"model": "zai/glm-5.1", "mode": "default", "thinking": "xhigh"},
}


def _sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("OMPWIRE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(setup, "providers", lambda: {"zai": ("api_key", "2026-10-24")})
    monkeypatch.setattr(setup, "login_provider", lambda yes: False)
    monkeypatch.setattr(setup, "_mcp_wizard", lambda yes, path=None: None)
    monkeypatch.setattr(setup, "agent_catalog", lambda cwd, command="omp": CATALOG)
    return tmp_path


def _agent(monkeypatch, found: str | None = "/usr/bin/omp"):
    monkeypatch.setattr(setup, "agent_path", lambda command="omp": found)


def _answers(monkeypatch, mapping: dict[str, str] | None = None):
    """Answer prompts by substring, so the order of questions can change freely."""
    mapping = mapping or {}

    def answer(prompt: str, default: str, *, yes: bool = False) -> str:
        for needle, value in mapping.items():
            if needle in prompt:
                return value
        return default

    monkeypatch.setattr(setup, "_ask", answer)
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)


def _token(monkeypatch, value: str = "123:abc", username: str = "test_bot"):
    monkeypatch.setattr(setup, "_ask_secret", lambda prompt, yes: value, raising=False)
    monkeypatch.setattr(setup, "bot_identity", lambda token, timeout=10.0: username)


def test_missing_agent_stops_before_writing_anything(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch, None)
    assert setup.run(yes=True) == 1                     # --yes means "do not install"
    assert "omp.sh/install" in capsys.readouterr().out
    assert not (tmp_path / "config.toml").exists()


def test_agent_is_installed_before_the_config_is_written(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    calls: list[str] = []
    state = {"found": None}

    def fake_install():
        calls.append("install-omp")
        state["found"] = "/home/x/.local/bin/omp"
        return True

    monkeypatch.setattr(setup, "agent_path", lambda command="omp": state["found"])
    monkeypatch.setattr(setup, "install_agent", fake_install)
    _token(monkeypatch)

    assert setup.run(yes=True, install_omp=True) == 0
    assert calls == ["install-omp"]
    assert (tmp_path / "config.toml").is_file()


def test_config_is_written_and_parses(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _token(monkeypatch)

    assert setup.run(yes=True) == 0

    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["acp"]["command"] == "omp"
    assert data["acp"]["args"] == ["--mode", "acp"]
    assert data["gateway"]["platform"] == "telegram"
    assert data["agent"]["mode"] == "default"
    assert data["agent"]["thinking"] == "xhigh"
    assert data["agent"]["yolo"] is True
    assert data["telegram"]["allowed_users"] == []
    assert data["mcp"]["source"] == "none"      # omp owns its servers; no hub merge by default
    assert "fenced code" in data["agent"]["append_system_prompt"]


EXISTING = """[agent]
cwd = "/srv/app"
model = "anthropic/claude-opus-5"
mode = "plan"
thinking = "low"
yolo = false
approval_mode = "write"

[approval]
policy = "deny"

[telegram]
allowed_users = [42, 43]
home_channel = 42

[mcp]
source = "none"
"""


def test_rerunning_setup_prefills_and_keeps_the_existing_values(monkeypatch, tmp_path, capsys):
    """A second run edits the config; it does not silently replace it."""
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _token(monkeypatch)
    config = tmp_path / "config.toml"
    config.write_text(EXISTING, encoding="utf-8")

    assert setup.run(yes=True) == 0                      # yes = accept every current value

    data = tomllib.loads(config.read_text())
    assert data["agent"]["model"] == "anthropic/claude-opus-5"
    assert data["agent"]["mode"] == "plan"
    assert data["agent"]["thinking"] == "low"
    assert data["agent"]["yolo"] is False
    assert data["agent"]["approval_mode"] == "write"
    assert data["approval"]["policy"] == "deny"          # ompwire's own policy, untouched
    assert data["agent"]["cwd"] == "/srv/app"
    assert data["telegram"]["allowed_users"] == [42, 43]
    assert (tmp_path / "config.toml.bak").is_file()
    out = capsys.readouterr().out
    assert "editing" in out and "press enter to keep" in out


def test_a_typed_answer_replaces_one_value_and_leaves_the_rest(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _token(monkeypatch)
    config = tmp_path / "config.toml"
    config.write_text(EXISTING, encoding="utf-8")

    def answer(prompt: str, default: str, *, yes: bool = False) -> str:
        if prompt.startswith("allowed telegram user ids"):
            return "99"
        return default

    monkeypatch.setattr(setup, "_ask", answer)
    monkeypatch.setattr(setup, "choose",
                        lambda prompt, choices, default, **kwargs: "high" if prompt == "thinking"
                        else default)

    assert setup.run(yes=False) == 0
    data = tomllib.loads(config.read_text())
    assert data["agent"]["thinking"] == "high"           # changed
    assert data["agent"]["model"] == "anthropic/claude-opus-5"   # kept
    assert data["agent"]["mode"] == "plan"               # kept
    assert data["telegram"]["allowed_users"] == [99]     # changed


def test_force_starts_from_the_agent_defaults_instead_of_the_file(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _token(monkeypatch)
    config = tmp_path / "config.toml"
    config.write_text(EXISTING, encoding="utf-8")

    assert setup.run(yes=True, force=True) == 0
    data = tomllib.loads(config.read_text())
    assert data["agent"]["model"] == "zai/glm-5.1"       # the agent's current model
    assert data["agent"]["yolo"] is True
    assert data["telegram"]["allowed_users"] == []


def test_an_existing_token_is_kept_without_asking(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _answers(monkeypatch)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=999:abc\n", encoding="utf-8")
    monkeypatch.setattr(setup, "bot_identity", lambda token, timeout=10.0: "kept_bot")
    monkeypatch.setattr(setup, "_ask_secret", lambda prompt, yes: (_ for _ in ()).throw(
        AssertionError("must not ask for a token that already works")), raising=False)

    assert setup.run(yes=True) == 0
    assert "999:abc" in (tmp_path / ".env").read_text()
    assert "token valid: @kept_bot" in capsys.readouterr().out


def test_token_goes_to_env_with_0600_and_is_verified(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _answers(monkeypatch)
    _token(monkeypatch, "123:abc", "my_bot")

    assert setup.run(yes=False) == 0
    env_file = tmp_path / ".env"
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert env_file.read_text().strip() == "TELEGRAM_BOT_TOKEN=123:abc"
    out = capsys.readouterr().out
    assert "token valid: @my_bot" in out
    assert "123:abc" not in out                       # never echoed back


def test_allowed_users_are_parsed_and_the_first_becomes_home(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _answers(monkeypatch, {"allowed telegram user ids": "111, 222 ,x"})
    _token(monkeypatch)

    assert setup.run(yes=False) == 0
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["telegram"]["allowed_users"] == [111, 222]
    assert data["telegram"]["home_channel"] == 111


def test_no_token_fails_because_the_gateway_cannot_start(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _answers(monkeypatch)
    monkeypatch.setattr(setup, "_ask_secret", lambda prompt, yes: "", raising=False)

    assert setup.run(yes=False) == 1
    out = capsys.readouterr().out
    assert "no usable TELEGRAM_BOT_TOKEN" in out
    assert not (tmp_path / "config.toml").exists()     # nothing half-written
    assert not (tmp_path / ".env").exists()


def test_unverified_token_is_retried_and_reported(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _answers(monkeypatch)
    monkeypatch.setattr(setup, "_ask_secret", lambda prompt, yes: "bad:token", raising=False)
    monkeypatch.setattr(setup, "bot_identity", lambda token, timeout=10.0: "")

    assert setup.run(yes=False) == 1
    out = capsys.readouterr().out
    assert "failed getMe" in out
    assert "bad:token" not in out


def test_env_token_is_reused_without_prompting(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:fromenv")
    _agent(monkeypatch)
    _answers(monkeypatch)
    monkeypatch.setattr(setup, "bot_identity", lambda token, timeout=10.0: "env_bot")
    monkeypatch.setattr(setup, "_ask_secret", lambda prompt, yes: (_ for _ in ()).throw(
        AssertionError("must not prompt when the env already has a token")), raising=False)

    assert setup.run(yes=True) == 0
    assert "999:fromenv" in (tmp_path / ".env").read_text()


def test_approval_policy_is_asked_when_yolo_is_off(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _token(monkeypatch)
    asked: list[str] = []

    def answer(prompt: str, default: str, *, yes: bool = False) -> str:
        asked.append(prompt)
        if "yolo mode" in prompt:
            return "no"
        if "agent approval mode" in prompt:
            return "write"
        return default

    monkeypatch.setattr(setup, "_ask", answer)
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)

    assert setup.run(yes=False) == 0
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["agent"]["yolo"] is False
    assert data["agent"]["approval_mode"] == "write"
    assert data["approval"]["policy"] == "ask"           # ompwire's own policy, separate
    assert any("agent approval mode" in prompt for prompt in asked)


def test_yolo_on_skips_the_approval_question(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _token(monkeypatch)
    asked: list[str] = []

    def answer(prompt: str, default: str, *, yes: bool = False) -> str:
        asked.append(prompt)
        return "yes" if "yolo mode" in prompt else default

    monkeypatch.setattr(setup, "_ask", answer)
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)

    assert setup.run(yes=False) == 0
    assert not any("agent approval mode" in prompt for prompt in asked)
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["agent"]["approval_mode"] == "always-ask"   # fallback, never "yolo"


def test_missing_provider_warns_but_does_not_block(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "providers", lambda: {})
    _agent(monkeypatch)
    _token(monkeypatch)

    assert setup.run(yes=True) == 0
    assert "no provider logged in" in capsys.readouterr().out


def test_choose_pins_the_current_value_into_the_window(monkeypatch):
    """Alphabetical lists can put the current model past the cut-off."""
    choices = [f"prov/model-{index}" for index in range(40)]
    current = "zzz/current-model"
    choices.append(current)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print",
                        lambda *args, **kwargs: printed.append(" ".join(map(str, args))))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    assert setup.choose("model", choices, current, yes=False, limit=5) == current
    assert any(current in line for line in printed)


def test_mcp_server_is_written_into_omps_own_file(monkeypatch, tmp_path):
    target = tmp_path / "omp" / "mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "$schema": "keep-me",
        "mcpServers": {"existing": {"command": "/bin/true"}},
        "disabledServers": ["noisy"],
    }), encoding="utf-8")

    setup.add_mcp_server("filesystem", "npx",
                         ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                         env={"ROOT": "/tmp"}, path=target)

    data = json.loads(target.read_text())
    assert data["$schema"] == "keep-me"                    # untouched
    assert data["disabledServers"] == ["noisy"]            # untouched
    assert set(data["mcpServers"]) == {"existing", "filesystem"}
    entry = data["mcpServers"]["filesystem"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "npx"
    assert entry["args"][0] == "-y"
    assert entry["env"] == {"ROOT": "/tmp"}
    assert setup.read_mcp_servers(target)["existing"]["command"] == "/bin/true"


def test_mcp_file_is_created_with_a_schema_when_absent(monkeypatch, tmp_path):
    target = tmp_path / "agent" / "mcp.json"
    setup.add_mcp_server("probe", "/bin/true", [], path=target)
    data = json.loads(target.read_text())
    assert data["$schema"] == setup.MCP_SCHEMA
    assert data["mcpServers"]["probe"] == {"type": "stdio", "command": "/bin/true"}


def test_mcp_wizard_does_nothing_on_yes(monkeypatch, tmp_path):
    target = tmp_path / "mcp.json"
    assert setup._mcp_wizard(True, path=target) is None
    assert not target.exists()


def test_mcp_wizard_collects_a_server_from_the_terminal(monkeypatch, tmp_path):
    target = tmp_path / "mcp.json"
    answers = iter(["y", "github", "npx", "-y @modelcontextprotocol/server-github", "TOKEN=x", "n"])

    def fake_input(prompt: str = "") -> str:
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(setup, "_ask", lambda prompt, default, *, yes=False: next(answers))

    written = setup._mcp_wizard(False, path=target)
    assert written == target
    data = json.loads(target.read_text())
    assert data["mcpServers"]["github"]["command"] == "npx"
    assert data["mcpServers"]["github"]["args"] == ["-y", "@modelcontextprotocol/server-github"]
    assert data["mcpServers"]["github"]["env"] == {"TOKEN": "x"}


def test_provider_and_model_are_one_step(monkeypatch, tmp_path):
    """The model list must be narrowed to the provider chosen just before it."""
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _token(monkeypatch)
    seen: list[tuple[str, list[str]]] = []

    def choose(prompt: str, choices: list[str], default: str, **kwargs) -> str:
        seen.append((prompt, list(choices)))
        return default

    monkeypatch.setattr(setup, "choose", choose)
    monkeypatch.setattr(setup, "_ask", lambda prompt, default, *, yes=False: default)

    assert setup.run(yes=False) == 0
    prompts = [prompt for prompt, _ in seen]
    provider_index = next(i for i, prompt in enumerate(prompts) if prompt == "provider")
    model_index = next(i for i, prompt in enumerate(prompts) if prompt.startswith("model ("))
    assert provider_index < model_index
    assert seen[provider_index][1] == ["anthropic", "web", "zai"]
    assert seen[model_index][1] == ["zai/glm-5.1", "zai/glm-5.3-flash"]
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["agent"]["model"] == "zai/glm-5.1"


def test_gateway_is_the_last_step_and_asks_for_the_token(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    order: list[str] = []

    def answer(prompt: str, default: str, *, yes: bool = False) -> str:
        order.append(prompt)
        return default

    def secret(prompt: str, yes: bool) -> str:
        order.append(prompt)
        return "123:abc"

    monkeypatch.setattr(setup, "_ask", answer)
    monkeypatch.setattr(setup, "_ask_secret", secret)
    monkeypatch.setattr(setup, "bot_identity", lambda token, timeout=10.0: "late_bot")
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)

    assert setup.run(yes=False) == 0
    token_index = next(i for i, prompt in enumerate(order) if "bot token" in prompt)
    assert "yolo mode" in order[0]
    assert token_index >= len(order) - 2          # the gateway comes last
    assert "working directory" in " ".join(order[:token_index])


def test_gateway_rejects_a_pasted_secret_without_echoing_it(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _token(monkeypatch)
    state = {"platform_tries": 0}

    def answer(prompt: str, default: str, *, yes: bool = False) -> str:
        if "gateway platform" in prompt:
            state["platform_tries"] += 1
            return "8384807287:AAEwqsecret" if state["platform_tries"] == 1 else "telegram"
        return default

    monkeypatch.setattr(setup, "_ask", answer)
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)

    assert setup.run(yes=False) == 0
    out = capsys.readouterr().out
    assert "AAEwqsecret" not in out
    assert "looks like a secret" in out
