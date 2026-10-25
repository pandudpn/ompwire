"""The gateway lock and the systemd unit, both about the bot staying up."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from ompwire import lock, service


class TestLock:
    def test_second_acquire_is_refused_while_the_first_holds_it(self, tmp_path):
        target = tmp_path / "serve.lock"
        handle = lock.acquire(target)
        try:
            # flock is per open file description, so a fresh descriptor in another
            # process is what a second `ompwire serve` looks like
            probe = textwrap.dedent(f"""
                import sys
                from pathlib import Path
                from ompwire import lock
                try:
                    lock.acquire(Path({str(target)!r}))
                except lock.AlreadyRunning as busy:
                    print("blocked", busy.pid)
                else:
                    print("acquired")
            """)
            result = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                                    text=True, timeout=60, check=False)
            assert result.stdout.startswith("blocked")
            assert str(__import__("os").getpid()) in result.stdout
        finally:
            lock.release(handle, target)
        assert not target.exists()

    def test_a_dead_holder_does_not_block_a_start(self, tmp_path):
        target = tmp_path / "serve.lock"
        target.write_text("999999\n", encoding="utf-8")   # no such pid
        assert lock.holder(target) is None
        handle = lock.acquire(target)
        lock.release(handle, target)

    def test_holder_reads_the_pid_it_wrote(self, tmp_path):
        target = tmp_path / "serve.lock"
        handle = lock.acquire(target)
        try:
            assert lock.holder(target) == __import__("os").getpid()
        finally:
            lock.release(handle, target)


class TestUnit:
    def test_unit_is_generated_from_the_paths_on_this_machine(self):
        body = service.render_unit("/home/you/.local/bin/ompwire",
                                   __import__("pathlib").Path("/home/you/.config/ompwire/.env"),
                                   "/home/you", "telegram")
        assert "ExecStart=/home/you/.local/bin/ompwire gateway _run telegram" in body
        assert "Description=ompwire gateway: telegram" in body
        assert "EnvironmentFile=/home/you/.config/ompwire/.env" in body
        assert "WorkingDirectory=/home/you" in body
        assert "Restart=always" in body
        assert "[Install]" in body and "WantedBy=default.target" in body
        # the token belongs in the env file, never in the unit
        assert "TOKEN=" not in body


def test_start_refuses_when_a_gateway_is_running(monkeypatch, capsys):
    monkeypatch.setattr(service, "_systemd_available", lambda: True)
    monkeypatch.setattr(service, "unit_path",
                        lambda gateway="telegram": __import__("pathlib").Path("/tmp/none.service"))
    monkeypatch.setattr("ompwire.update.serving_processes",
                        lambda: [(4242, "python3 ompwire gateway _run telegram")])

    assert service.start("telegram") == 1
    out = capsys.readouterr().out
    assert "already running outside systemd (pid 4242)" in out
    assert "409" in out


def test_start_refuses_without_systemd(monkeypatch, capsys):
    monkeypatch.setattr(service, "_systemd_available", lambda: False)
    assert service.start("telegram") == 1
    assert "systemd --user is not available" in capsys.readouterr().out


def test_start_writes_the_unit_and_enables_it(monkeypatch, tmp_path, capsys):
    unit = tmp_path / "ompwire.service"
    env = tmp_path / "ompwire.env"
    env.write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(service, "_systemd_available", lambda: True)
    monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": unit)
    monkeypatch.setattr(service, "_env_file", lambda: env)
    monkeypatch.setattr(service, "_workdir", lambda: "/home/you")
    monkeypatch.setattr(service, "command_path", lambda: "/home/you/.local/bin/ompwire")
    monkeypatch.setattr("ompwire.update.serving_processes", lambda: [])
    monkeypatch.setattr(service, "_credential_works", lambda gateway: True)
    monkeypatch.setattr(service, "agent_binary", lambda command="": "/home/you/.local/bin/omp")
    monkeypatch.setattr(service.subprocess, "run",
                        lambda command, **kwargs: calls.append(command) or Result())

    assert service.start("telegram") == 0
    body = unit.read_text(encoding="utf-8")
    assert "ExecStart=/home/you/.local/bin/ompwire gateway _run telegram" in body
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "ompwire-telegram.service"] in calls
    assert "journalctl --user -u ompwire-telegram.service -f" in capsys.readouterr().out


def test_start_rewrites_a_stale_unit(monkeypatch, tmp_path, capsys):
    """The unit is generated from this machine's paths, so start always refreshes it."""
    unit = tmp_path / "ompwire-telegram.service"
    unit.write_text("[Service]\nExecStart=/old ompwire serve\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(service, "_systemd_available", lambda: True)
    monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": unit)
    monkeypatch.setattr(service, "_env_file", lambda: env)
    monkeypatch.setattr(service, "_workdir", lambda: "/home/you")
    monkeypatch.setattr(service, "_credential_works", lambda gateway: True)
    monkeypatch.setattr(service, "agent_binary", lambda command="": "/home/you/.local/bin/omp")
    monkeypatch.setattr("ompwire.update.serving_processes", lambda: [])
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: Result())

    assert service.start("telegram") == 0
    body = unit.read_text()
    assert "gateway _run telegram" in body
    assert "ompwire serve" not in body


def test_remove_disables_and_removes(monkeypatch, tmp_path, capsys):
    unit = tmp_path / "ompwire-telegram.service"
    unit.write_text("unit\n", encoding="utf-8")
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(service, "_systemd_available", lambda: True)
    monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": unit)
    monkeypatch.setattr(service.subprocess, "run",
                        lambda command, **kwargs: calls.append(command) or Result())

    assert service.remove("telegram") == 0
    assert not unit.exists()
    assert ["systemctl", "--user", "disable", "--now", "ompwire-telegram.service"] in calls
    assert "removed" in capsys.readouterr().out


def test_remove_keeps_the_file_when_asked(monkeypatch, tmp_path):
    unit = tmp_path / "ompwire.service"
    unit.write_text("unit\n", encoding="utf-8")
    monkeypatch.setattr(service, "_systemd_available", lambda: False)
    monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": unit)

    assert service.remove("telegram", keep_unit_file=True) == 0
    assert unit.exists()


def test_state_says_when_there_is_no_unit(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "unit_path",
                        lambda gateway="telegram": tmp_path / "missing.service")
    assert service.state("telegram") == "no unit"


def test_pytest_is_importable_here():
    """Guards the import list above: the suite runs with the project venv."""
    assert pytest.__version__


class TestUninstallInteraction:
    def test_a_dry_run_only_reports_the_unit(self, monkeypatch, tmp_path, capsys):
        from ompwire import uninstall

        unit = tmp_path / "ompwire.service"
        unit.write_text("unit\n", encoding="utf-8")
        monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": unit)
        monkeypatch.setattr(uninstall, "targets", lambda keep_config: [])
        monkeypatch.setattr(uninstall, "running_serves", lambda: [])

        assert uninstall.run(dry_run=True, force=True) == 0
        out = capsys.readouterr().out
        assert "would be disabled and removed" in out
        assert unit.exists()

    def test_remove_disables_the_unit_before_deleting_files(
            self, monkeypatch, tmp_path):
        from ompwire import uninstall

        unit = tmp_path / "ompwire.service"
        unit.write_text("unit\n", encoding="utf-8")
        calls: list[str] = []
        monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": unit)
        monkeypatch.setattr(service, "remove",
                            lambda *a, **kwargs: calls.append("service") or 0)
        monkeypatch.setattr(uninstall, "targets", lambda keep_config: [])
        monkeypatch.setattr(uninstall, "running_serves", lambda: [])

        assert uninstall.run(yes=True, force=True) == 0
        assert calls == ["service"]


class TestOneGatewayOneUnit:
    def test_each_gateway_gets_its_own_unit(self):
        assert service.unit_name("telegram") == "ompwire-telegram.service"
        assert service.unit_name("whatsapp") == "ompwire-whatsapp.service"
        assert service.unit_path("telegram") != service.unit_path("whatsapp")

    def test_the_unit_runs_only_its_own_gateway(self):
        body = service.render_unit("/usr/bin/ompwire", __import__("pathlib").Path("/e"),
                                   "/w", "whatsapp")
        assert "gateway _run whatsapp" in body
        assert "telegram" not in body


def test_start_refuses_a_rejected_credential(monkeypatch, tmp_path, capsys):
    """Restart=always on a revoked token is a crash loop, not a service."""
    from ompwire import gateways

    monkeypatch.setattr(service, "_systemd_available", lambda: True)
    monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": _tmp_unit(gateway))
    monkeypatch.setattr(service, "_env_file", lambda: _env(tmp_path))
    monkeypatch.setattr("ompwire.update.serving_processes", lambda: [])
    monkeypatch.setattr(gateways, "load", lambda config=None: [gateways.Gateway(name="telegram")])
    monkeypatch.setattr(gateways, "token", lambda item: "old:token")
    monkeypatch.setattr("ompwire.setup.bot_identity", lambda token, timeout=10.0: "")

    assert service.start("telegram") == 1
    out = capsys.readouterr().out
    assert "not accepted" in out and "gateway start telegram" in out


def test_start_proceeds_when_the_credential_verifies(monkeypatch, tmp_path, capsys):
    from ompwire import gateways

    unit = tmp_path / "ompwire-telegram.service"
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(service, "_systemd_available", lambda: True)
    monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": unit)
    monkeypatch.setattr(service, "_env_file", lambda: env)
    monkeypatch.setattr(service, "_workdir", lambda: "/home/you")
    monkeypatch.setattr("ompwire.update.serving_processes", lambda: [])
    monkeypatch.setattr(service.subprocess, "run",
                        lambda command, **kwargs: Result())
    monkeypatch.setattr(gateways, "load", lambda config=None: [gateways.Gateway(name="telegram")])
    monkeypatch.setattr(gateways, "token", lambda item: "good:token")
    monkeypatch.setattr("ompwire.setup.bot_identity", lambda token, timeout=10.0: "vidura_omp_bot")

    assert service.start("telegram") == 0
    assert unit.exists()


def _tmp_unit(gateway: str = "telegram"):
    from pathlib import Path

    return Path(f"/tmp/ompwire-test-{gateway}.service")


def _env(path):
    from pathlib import Path

    target = Path(path) / ".env"
    target.write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")
    return target


class TestUnitPath:
    """The failure this guards: the poller starts, then every turn says ACP command not found."""

    def test_the_unit_carries_the_directory_the_agent_was_found_in(self, monkeypatch, tmp_path):
        agent = tmp_path / "bin" / "omp"
        agent.parent.mkdir()
        agent.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(service, "agent_binary", lambda command="": str(agent))
        monkeypatch.setattr(service, "command_path", lambda: str(tmp_path / "ompwire"))

        path = service.service_path("omp")
        assert path.split(":")[0] == str(agent.parent)
        assert "/usr/bin" in path.split(":")
        assert len(path.split(":")) == len(set(path.split(":")))

    def test_the_rendered_unit_sets_that_path(self, tmp_path):
        unit = service.render_unit("/home/you/.local/bin/ompwire", tmp_path / ".env",
                                   "/home/you", "telegram", path="/a/bin:/usr/bin")
        assert "Environment=PATH=/a/bin:/usr/bin" in unit

    def test_a_stale_unit_is_rewritten_before_the_restart(self, monkeypatch, tmp_path):
        target = tmp_path / "ompwire-telegram.service"
        target.write_text("[Service]\nExecStart=old\n", encoding="utf-8")
        monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": target)
        monkeypatch.setattr(service, "_systemd_available", lambda: True)
        monkeypatch.setattr(service, "command_path", lambda: "/usr/bin/ompwire")
        monkeypatch.setattr(service, "_env_file", lambda: tmp_path / ".env")
        monkeypatch.setattr(service, "_workdir", lambda: "/home/you")
        monkeypatch.setattr(service, "state", lambda gateway="telegram": "active")

        calls: list[tuple[str, ...]] = []

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_systemctl(*args, **_kwargs):
            calls.append(args)
            return Done()

        monkeypatch.setattr(service, "_systemctl", fake_systemctl)

        assert service.restart("telegram") == 0
        assert "Environment=PATH=" in target.read_text(encoding="utf-8")
        assert calls[0] == ("daemon-reload",)
        assert calls[1][0] == "restart"

    def test_a_unit_without_the_path_is_named_by_doctor(self, monkeypatch, tmp_path, capsys):
        from ompwire import __main__ as entry

        units = tmp_path / "systemd"
        units.mkdir()
        stale = units / "ompwire-telegram.service"
        stale.write_text("[Service]\nExecStart=/usr/bin/ompwire gateway _run telegram\n",
                         encoding="utf-8")
        monkeypatch.setattr(entry, "_home_dir", lambda: tmp_path, raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        (tmp_path / ".config" / "systemd" / "user").mkdir(parents=True)
        (tmp_path / ".config" / "systemd" / "user" / "ompwire-telegram.service").write_text(
            stale.read_text(encoding="utf-8"), encoding="utf-8")

        entry._doctor_units("omp")
        out = capsys.readouterr().out
        assert "predates PATH in its unit" in out
        assert "ompwire gateway restart telegram" in out


class TestStartPreconditions:
    """The two refusals that turn a mystery in the chat into a message in the terminal."""

    def test_a_missing_agent_is_refused_before_a_unit_is_written(self, monkeypatch, tmp_path):
        unit = tmp_path / "ompwire-telegram.service"
        monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": unit)
        monkeypatch.setattr(service, "_systemd_available", lambda: True)
        monkeypatch.setattr(service, "agent_binary", lambda command="": None)
        monkeypatch.setattr("ompwire.update.serving_processes", lambda: [])
        monkeypatch.setattr(service, "_env_file", lambda: tmp_path / ".env")
        (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")
        monkeypatch.setattr(service, "_credential_works", lambda gateway="": True)

        assert service.start("telegram") == 1
        assert not unit.exists()

    def test_force_writes_the_unit_anyway(self, monkeypatch, tmp_path, capsys):
        unit = tmp_path / "ompwire-telegram.service"
        monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": unit)
        monkeypatch.setattr(service, "_systemd_available", lambda: True)
        monkeypatch.setattr(service, "agent_binary", lambda command="": None)
        monkeypatch.setattr("ompwire.update.serving_processes", lambda: [])
        monkeypatch.setattr(service, "_env_file", lambda: tmp_path / ".env")
        (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")
        monkeypatch.setattr(service, "_credential_works", lambda gateway="": True)
        monkeypatch.setattr(service, "_workdir", lambda: "/home/you")
        monkeypatch.setattr(service, "command_path", lambda: "/usr/bin/ompwire")
        monkeypatch.setattr(service, "_systemctl", lambda *a, **k: type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": ""})())

        assert service.start("telegram", force=True) == 0
        assert unit.exists()
        assert "--force given" in capsys.readouterr().out


class TestSupervisedIsNotStray:
    def test_a_process_under_systemd_is_not_a_stray_poller(self, monkeypatch):
        from ompwire import uninstall

        monkeypatch.setattr(uninstall, "under_systemd", lambda pid: True)
        monkeypatch.setattr(uninstall.subprocess, "run", lambda *a, **k: type(
            "R", (), {"stdout": "4242 ompwire gateway _run telegram\n"})())
        assert uninstall.running_serves() == []

    def test_a_hand_started_process_is_reported(self, monkeypatch):
        from ompwire import uninstall

        monkeypatch.setattr(uninstall, "under_systemd", lambda pid: False)
        monkeypatch.setattr(uninstall.subprocess, "run", lambda *a, **k: type(
            "R", (), {"stdout": "4243 ompwire gateway _run telegram\n"})())
        assert uninstall.running_serves() == [(4243, "ompwire gateway _run telegram")]
