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
        assert "ExecStart=/home/you/.local/bin/ompwire serve --gateway telegram" in body
        assert "Description=ompwire gateway: telegram" in body
        assert "EnvironmentFile=/home/you/.config/ompwire/.env" in body
        assert "WorkingDirectory=/home/you" in body
        assert "Restart=always" in body
        assert "[Install]" in body and "WantedBy=default.target" in body
        # the token belongs in the env file, never in the unit
        assert "TOKEN=" not in body


def test_install_refuses_when_a_gateway_is_running(monkeypatch, capsys):
    monkeypatch.setattr(service, "_systemd_available", lambda: True)
    monkeypatch.setattr(service, "unit_path",
                        lambda gateway="telegram": __import__("pathlib").Path("/tmp/none.service"))
    monkeypatch.setattr("ompwire.update.serving_processes",
                        lambda: [(4242, "python3 ompwire serve")])

    assert service.install("telegram") == 1
    out = capsys.readouterr().out
    assert "already running (pid 4242)" in out
    assert "409" in out


def test_install_refuses_without_systemd(monkeypatch, capsys):
    monkeypatch.setattr(service, "_systemd_available", lambda: False)
    assert service.install("telegram") == 1
    assert "systemd --user is not available" in capsys.readouterr().out


def test_install_writes_the_unit_and_enables_it(monkeypatch, tmp_path, capsys):
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
    monkeypatch.setattr(service.subprocess, "run",
                        lambda command, **kwargs: calls.append(command) or Result())

    assert service.install("telegram") == 0
    body = unit.read_text(encoding="utf-8")
    assert "ExecStart=/home/you/.local/bin/ompwire serve --gateway telegram" in body
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "ompwire-telegram.service"] in calls
    assert "journalctl --user -u ompwire-telegram.service -f" in capsys.readouterr().out


def test_install_refuses_to_clobber_an_existing_unit(monkeypatch, tmp_path, capsys):
    unit = tmp_path / "ompwire.service"
    unit.write_text("mine\n", encoding="utf-8")
    monkeypatch.setattr(service, "_systemd_available", lambda: True)
    monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": unit)

    assert service.install("telegram") == 1
    assert "--force" in capsys.readouterr().out
    assert unit.read_text() == "mine\n"


def test_uninstall_disables_and_removes(monkeypatch, tmp_path, capsys):
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

    assert service.uninstall("telegram") == 0
    assert not unit.exists()
    assert ["systemctl", "--user", "disable", "--now", "ompwire-telegram.service"] in calls
    assert "removed" in capsys.readouterr().out


def test_uninstall_keeps_the_file_when_asked(monkeypatch, tmp_path):
    unit = tmp_path / "ompwire.service"
    unit.write_text("unit\n", encoding="utf-8")
    monkeypatch.setattr(service, "_systemd_available", lambda: False)
    monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": unit)

    assert service.uninstall("telegram", keep_unit_file=True) == 0
    assert unit.exists()


def test_show_says_when_there_is_no_unit(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": tmp_path / "missing.service")
    assert service.show("telegram") == 0
    assert "service install" in capsys.readouterr().out


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

    def test_a_real_uninstall_disables_the_unit_before_deleting_files(
            self, monkeypatch, tmp_path):
        from ompwire import uninstall

        unit = tmp_path / "ompwire.service"
        unit.write_text("unit\n", encoding="utf-8")
        calls: list[str] = []
        monkeypatch.setattr(service, "unit_path", lambda gateway="telegram": unit)
        monkeypatch.setattr(service, "uninstall",
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
        assert "serve --gateway whatsapp" in body
        assert "telegram" not in body
