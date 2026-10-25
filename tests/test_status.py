"""status reports state; doctor judges it. Both must show the allow list."""

from __future__ import annotations

from ompwire import __main__ as cli
from ompwire import config as config_module
from ompwire import gateways, status

CONFIG = """[agent]
cwd = "/srv/app"
model = "zai/glm-5.1"
mode = "default"
thinking = "xhigh"
yolo = true
approval_mode = "always-ask"

[telegram]
allowed_users = [111, 222]

[approval]
policy = "ask"

[mcp]
source = "none"

[session]
store = "{store}"

[logging]
file = "{log}"
"""


def _sandbox(monkeypatch, tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    store = tmp_path / "sessions.db"
    log = tmp_path / "ompwire.log"
    log.write_text("line\n", encoding="utf-8")
    (config_dir / "config.toml").write_text(
        CONFIG.format(store=store, log=log), encoding="utf-8")
    monkeypatch.setenv("OMPWIRE_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_module, "_load_env_files", lambda env: None)
    # gateways live in their own file now; the legacy [telegram] block is elsewhere
    (config_dir / "gateways.toml").write_text(
        '[gateway.telegram]\ntype = "telegram"\nenabled = true\n'
        "allowed_users = [111, 222]\nhome_channel = 111\n", encoding="utf-8")
    (config_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=111:tok\n", encoding="utf-8")
    monkeypatch.setattr(gateways, "gateways_file", lambda: config_dir / "gateways.toml")
    monkeypatch.setattr(gateways, "_from_config", lambda config=None: [])
    monkeypatch.setattr("ompwire.setup.bot_identity", lambda token, timeout=10.0: "test_bot")
    monkeypatch.setattr("ompwire.service.state", lambda gateway="telegram": "active")
    monkeypatch.setattr(status, "_gateway_state", lambda: ("stopped", "not running"))
    monkeypatch.setattr(status, "_agent_version", lambda command, timeout=6.0: "omp/9.9.9")
    return config_dir / "config.toml", store, log


def test_status_shows_every_setting_that_decides_behaviour(monkeypatch, tmp_path):
    config_file, store, log = _sandbox(monkeypatch, tmp_path)

    rows = dict(status.collect(str(config_file)))

    assert rows["gateway telegram"] == \
        "telegram · @test_bot · users 111, 222 · active"
    assert rows["model"] == "zai/glm-5.1"
    assert rows["thinking"] == "xhigh"
    assert rows["cwd"] == "/srv/app"
    assert rows["approvals"] == "yolo (bypassed)"
    assert "telegram token" not in rows
    assert rows["mcp"] == "source none"
    assert str(store) in rows["sessions cli"]  # `ompwire chat` keeps the configured store
    assert "gateways/telegram/sessions.db" in rows["sessions telegram"]  # each bot its own
    assert str(log) in rows["log"]
    assert rows["processes"] == "not running"


def test_status_says_so_when_nobody_is_allowed(monkeypatch, tmp_path):
    config_file, _store, _log = _sandbox(monkeypatch, tmp_path)
    target = config_file.parent / "gateways.toml"
    target.write_text(target.read_text().replace("allowed_users = [111, 222]",
                                                 "allowed_users = []"), encoding="utf-8")

    rows = dict(status.collect(str(config_file)))
    assert "users no users" in rows["gateway telegram"]


def test_status_says_how_to_add_a_gateway(monkeypatch, tmp_path):
    config_file, _store, _log = _sandbox(monkeypatch, tmp_path)
    (config_file.parent / "gateways.toml").unlink()

    rows = dict(status.collect(str(config_file)))
    assert "gateway start" in rows["gateways"]


def test_status_exits_zero_and_prints_aligned_rows(monkeypatch, tmp_path, capsys):
    config_file, _store, _log = _sandbox(monkeypatch, tmp_path)
    assert status.run(str(config_file)) == 0
    out = capsys.readouterr().out
    assert "gateway telegram" in out and "111, 222" in out


def _doctor(monkeypatch, tmp_path, allowed: str):
    class Info:
        def get(self, key, default=None):
            return {"agentInfo": {"name": "omp", "version": "9.9.9"},
                    "agentCapabilities": {"loadSession": True},
                    "protocolVersion": 1}.get(key, default)

    class Session:
        get = staticmethod(lambda key, default=None: {"sessionId": "abc12345",
                                                      "configOptions": []}.get(key, default))

    class Client:
        def __init__(self, **kwargs):
            pass

        def initialize(self):
            return Info()

        def new_session(self, cwd, servers):
            return Session()

        def close_session(self, session_id):
            pass

        def close(self):
            pass

    config_dir = tmp_path / "cfg"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text(
        CONFIG.format(store=tmp_path / "s.db", log=tmp_path / "l.log")
        .replace("[111, 222]", allowed), encoding="utf-8")
    monkeypatch.setenv("OMPWIRE_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_module, "_load_env_files", lambda env: None)
    # gateways live in their own file now; the legacy [telegram] block is elsewhere
    monkeypatch.setattr(gateways, "gateways_file", lambda: config_dir / "gateways.toml")
    (config_dir / "gateways.toml").write_text(
        '[gateway.telegram]\ntype = "telegram"\nenabled = true\n'
        f"allowed_users = {allowed}\nhome_channel = 111\n", encoding="utf-8")
    (config_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=111:tok\n", encoding="utf-8")
    monkeypatch.setattr(gateways, "_from_config", lambda config=None: [])
    monkeypatch.setattr("ompwire.setup.bot_identity", lambda token, timeout=10.0: "test_bot")
    monkeypatch.setattr("ompwire.service.state", lambda gateway="telegram": "active")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/omp")
    monkeypatch.setattr(cli, "SessionStore", lambda path: _Closable())
    monkeypatch.setattr(cli, "AcpClient", Client)
    return cli.main(["--config", str(config_dir / "config.toml"), "doctor"])


class _Closable:
    def close(self):
        pass


def test_doctor_prints_the_allowed_users(monkeypatch, tmp_path, capsys):
    assert _doctor(monkeypatch, tmp_path, "[111, 222]") == 0
    out = capsys.readouterr().out
    assert "ok   gateway telegram: @test_bot" in out
    assert "ok   allowed users for telegram: 111, 222" in out


def test_doctor_flags_an_empty_allow_list(monkeypatch, tmp_path, capsys):
    assert _doctor(monkeypatch, tmp_path, "[]") == 0
    out = capsys.readouterr().out
    assert "allowed users for telegram: none" in out
    assert "nobody can talk" in out


class TestMcpRow:
    """The banner said "mcp none" while the hub supplied every server."""

    def test_a_short_list_is_named_in_full(self):
        from ompwire.engine import mcp_label

        assert mcp_label(["obsidian", "context7"]) == "obsidian, context7"

    def test_a_long_list_is_cut_with_a_count(self):
        from ompwire.engine import mcp_label

        label = mcp_label([f"s{i}" for i in range(9)])
        assert label.endswith(" +3")
        assert label.count(",") == 5

    def test_an_empty_list_says_none(self):
        from ompwire.engine import mcp_label

        assert mcp_label([]) == "none"
