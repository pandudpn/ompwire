"""Setup: agent, then provider, then token, then settings and MCP."""

from __future__ import annotations

import json
import tomllib

from ompwire import gateways, setup

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


def _gateway_file(monkeypatch, tmp_path, body: str = ""):
    """Where gateway definitions live, isolated from the real machine."""
    target = tmp_path / "gateways.toml"
    if body:
        target.write_text(body, encoding="utf-8")
    monkeypatch.setattr(gateways, "gateways_file", lambda: target)
    # the legacy fallback reads config.toml, which on a real machine has a [telegram] block
    monkeypatch.setattr(gateways, "_from_config", lambda config=None: [])
    return target


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
    assert "gateway" not in data and "telegram" not in data
    assert data["agent"]["mode"] == "default"
    assert data["agent"]["thinking"] == "xhigh"
    assert data["agent"]["yolo"] is False  # approvals stay on unless someone asks for yolo
    assert "telegram" not in data
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
    assert "telegram" not in data          # gateways live in gateways.toml now
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
    assert data["agent"]["thinking"] == "high"           # the typed value stuck


def test_force_starts_from_the_agent_defaults_instead_of_the_file(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _token(monkeypatch)
    config = tmp_path / "config.toml"
    config.write_text(EXISTING, encoding="utf-8")

    assert setup.run(yes=True, force=True) == 0
    data = tomllib.loads(config.read_text())
    assert data["agent"]["model"] == "zai/glm-5.1"       # the agent's current model
    assert data["agent"]["yolo"] is False  # approvals stay on unless someone asks for yolo
    assert "telegram" not in data


def test_a_fresh_install_does_not_turn_approvals_off(monkeypatch, tmp_path):
    """A coding agent with blanket approval and a home-directory workspace rewrites things."""
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _token(monkeypatch)

    assert setup.run(yes=True) == 0
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["agent"]["yolo"] is False
    assert data["agent"]["approval_mode"] == "write"


def test_setup_never_asks_for_a_bot_token(monkeypatch, tmp_path, capsys):
    """The gateway is its own command now; setup configures the agent only."""
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _gateway_file(monkeypatch, tmp_path)
    asked: list[str] = []

    def answer(prompt: str, default: str, *, yes: bool = False) -> str:
        asked.append(prompt)
        return default

    monkeypatch.setattr(setup, "_ask", answer)
    monkeypatch.setattr(setup, "choose", lambda prompt, choices, default, **kwargs: default)
    monkeypatch.setattr(setup, "_ask_secret",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("setup asked for a secret")))

    assert setup.run(yes=False) == 0
    assert not any("token" in prompt for prompt in asked)
    assert not (tmp_path / ".env").exists()
    out = capsys.readouterr().out
    assert "ompwire gateway start" in out


def test_setup_says_when_a_gateway_already_exists(monkeypatch, tmp_path, capsys):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _gateway_file(monkeypatch, tmp_path, '[gateway.telegram]\ntype = "telegram"\n'
                                         "allowed_users = [42]\n")

    assert setup.run(yes=True) == 0
    out = capsys.readouterr().out
    assert "gateways already configured: telegram" in out
    assert "no gateway yet" not in out


def test_the_written_config_has_no_gateway_or_telegram_section(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    _gateway_file(monkeypatch, tmp_path)

    assert setup.run(yes=True) == 0
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert "gateway" not in data
    assert "telegram" not in data


def test_a_rerun_leaves_the_gateway_file_alone(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _agent(monkeypatch)
    target = _gateway_file(monkeypatch, tmp_path,
                           '[gateway.telegram]\ntype = "telegram"\nallowed_users = [7]\n')
    before = target.read_text()

    assert setup.run(yes=True) == 0
    assert target.read_text() == before


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
    assert data["agent"]["approval_mode"] == "write"   # the fallback that reaches a human


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


def test_the_setup_command_does_not_read_the_gateway_token_flag(monkeypatch):
    """It crashed with AttributeError: 'Namespace' object has no attribute 'token_stdin'.

    load_config touches the real filesystem and run() prompts, so both are replaced; what is under
    test is that the parsed Namespace and cmd_setup still agree about which flags exist.
    """
    from ompwire import __main__ as entry
    from ompwire import setup as setup_module

    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(setup_module, "run", fake_run)
    monkeypatch.setattr(entry, "load_config", lambda _path=None: {})

    assert entry.main(["setup", "--yes"]) == 0
    assert seen == {"yes": True, "force": False, "install_omp": False}


def test_the_default_approval_mode_reaches_a_human():
    """Measured against omp 18.12: `always-ask` denies a write without asking anyone."""
    from ompwire.config import DEFAULTS

    agent = DEFAULTS["agent"]
    assert agent["yolo"] is False
    assert agent["approval_mode"] == "write"
