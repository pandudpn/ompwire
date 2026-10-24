"""Restarting the transport after an update, without guessing at a shell wrapper."""

from __future__ import annotations

from ompwire import update


def test_transport_command_prefers_the_python_line(monkeypatch):
    """A `zsh -lic ... | tee` wrapper must not be respawned: it would stack a second tee."""
    shell_line = ["/usr/bin/zsh", "-lic",
                  "cd /repo && exec .venv/bin/ompwire gateway _run telegram | tee log"]
    python_line = ["/repo/.venv/bin/python3", ".venv/bin/ompwire", "gateway", "_run", "telegram"]
    monkeypatch.setattr(update, "_argv_of",
                        lambda pid: python_line if pid == 2 else shell_line)

    assert update._transport_command(2, "ompwire gateway _run telegram") == python_line
    assert update._transport_command(1, "/usr/bin/zsh -lic ... ompwire gateway _run telegram") \
        == shell_line
    # a process started before this change (still spelled `serve`) is recognised too
    assert update._transport_command(2, "ompwire serve") == python_line


def test_no_transport_means_nothing_to_restart(monkeypatch):
    monkeypatch.setattr(update, "serving_processes", lambda: [])
    monkeypatch.setattr(update, "_systemd_active", lambda: False)
    assert "nothing to restart" in update.restart_gateway()


def test_systemd_unit_is_used_when_active(monkeypatch):
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    monkeypatch.setattr(update, "_systemd_active", lambda: True)
    monkeypatch.setattr(update.subprocess, "run",
                        lambda command, **kwargs: calls.append(command) or Result())

    assert "systemd" in update.restart_gateway()
    assert calls and calls[0][:3] == ["systemctl", "--user", "restart"]


def test_a_bare_process_is_stopped_and_respawned(monkeypatch):
    killed: list[tuple[int, int]] = []
    spawned: list[list[str]] = []

    class Child:
        pid = 999

    monkeypatch.setattr(update, "serving_processes",
                        lambda: [(4242, "python3 .venv/bin/ompwire serve")])
    monkeypatch.setattr(update, "_argv_of",
                        lambda pid: ["/repo/.venv/bin/python3", ".venv/bin/ompwire", "serve"])
    monkeypatch.setattr(update, "_cwd_of", lambda pid: "/repo")
    def fake_kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError(pid)      # the process is gone, so the wait ends

    monkeypatch.setattr(update.os, "kill", fake_kill)
    monkeypatch.setattr(update.subprocess, "Popen",
                        lambda argv, **kwargs: spawned.append(argv) or Child())
    monkeypatch.setattr(update, "_systemd_active", lambda: False)

    message = update.restart_gateway(timeout=0.2)

    assert "restarted" in message and "999" in message
    assert killed[0] == (4242, 15)
    assert all(sig == 0 for _pid, sig in killed[1:])
    assert spawned == [["/repo/.venv/bin/python3", ".venv/bin/ompwire", "serve"]]


def test_check_never_restarts(monkeypatch, capsys):
    monkeypatch.setattr(update, "install_mode", lambda: "uv")
    monkeypatch.setattr(update, "latest_stable", lambda: ("v9.9.9", "ompwire 9.9.9"))
    monkeypatch.setattr(update, "latest_edge", lambda: ("main", "main"))
    monkeypatch.setattr(update, "current_commit", lambda: "")
    monkeypatch.setattr(update, "restart_gateway",
                        lambda: (_ for _ in ()).throw(AssertionError("--check restarted")))

    assert update.run(check=True) == 0
    assert "no changes made" in capsys.readouterr().out


def test_no_restart_leaves_the_gateway_alone(monkeypatch, capsys):
    class Result:
        returncode = 0

    monkeypatch.setattr(update, "install_mode", lambda: "uv")
    monkeypatch.setattr(update, "latest_stable", lambda: ("v9.9.9", "ompwire 9.9.9"))
    monkeypatch.setattr(update, "latest_edge", lambda: ("main", "main"))
    monkeypatch.setattr(update, "current_commit", lambda: "")
    monkeypatch.setattr(update, "_command_for", lambda mode, ref: ["true"])
    monkeypatch.setattr(update.subprocess, "run", lambda command, **kwargs: Result())
    monkeypatch.setattr(update, "restart_gateway",
                        lambda: (_ for _ in ()).throw(AssertionError("--no-restart restarted")))

    assert update.run(restart=False) == 0
    out = capsys.readouterr().out
    assert "left running" in out and "ompwire restart" in out


def test_every_active_gateway_unit_is_restarted(monkeypatch):
    """One gateway one unit means an update has to restart all of them, not just one."""
    from ompwire import gateways

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    restarted: list[str] = []
    monkeypatch.setattr(gateways, "load", lambda config=None: [
        gateways.Gateway(name="telegram"), gateways.Gateway(name="telegram-2")])
    monkeypatch.setattr(update, "_systemd_active",
                        lambda unit="ompwire": unit in {"ompwire-telegram", "ompwire-telegram-2"})
    monkeypatch.setattr(update, "serving_processes", lambda: [])
    monkeypatch.setattr(update.subprocess, "run",
                        lambda command, **kwargs: restarted.append(command[-1]) or Result())

    message = update.restart_gateway()

    assert restarted == ["ompwire-telegram", "ompwire-telegram-2"]
    assert "ompwire-telegram-2" in message
