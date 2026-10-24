"""status reports state; doctor judges it. Both must show the allow list."""

from __future__ import annotations

import os

from ompwire import __main__ as cli
from ompwire import config as config_module
from ompwire import status

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
    monkeypatch.setattr(status, "_gateway_state", lambda: ("stopped", "not running"))
    monkeypatch.setattr(status, "_agent_version", lambda command, timeout=6.0: "omp/9.9.9")
    return config_dir / "config.toml", store, log


def test_status_shows_every_setting_that_decides_behaviour(monkeypatch, tmp_path):
    config_file, store, log = _sandbox(monkeypatch, tmp_path)

    rows = dict(status.collect(str(config_file)))

    assert rows["allowed users"] == "111, 222"
    assert rows["model"] == "zai/glm-5.1"
    assert rows["thinking"] == "xhigh"
    assert rows["cwd"] == "/srv/app"
    assert rows["approvals"] == "yolo (bypassed)"
    assert rows["telegram token"] == "missing (run ompwire setup)"
    assert rows["mcp"] == "source none"
    assert str(store) in rows["sessions"]
    assert str(log) in rows["log"]
    assert rows["gateway"].startswith("stopped")


def test_status_says_so_when_nobody_is_allowed(monkeypatch, tmp_path):
    config_file, _store, _log = _sandbox(monkeypatch, tmp_path)
    text = config_file.read_text().replace("allowed_users = [111, 222]", "allowed_users = []")
    config_file.write_text(text, encoding="utf-8")

    rows = dict(status.collect(str(config_file)))
    assert "nobody can talk" in rows["allowed users"]


def test_status_exits_zero_and_prints_aligned_rows(monkeypatch, tmp_path, capsys):
    config_file, _store, _log = _sandbox(monkeypatch, tmp_path)
    assert status.run(str(config_file)) == 0
    out = capsys.readouterr().out
    assert "allowed users" in out and "111, 222" in out


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
    assert "ok   allowed users: 111, 222" in out


def test_doctor_flags_an_empty_allow_list(monkeypatch, tmp_path, capsys):
    assert _doctor(monkeypatch, tmp_path, "[]") == 0
    out = capsys.readouterr().out
    assert "allowed users: none" in out
    assert "nobody can talk" in out
