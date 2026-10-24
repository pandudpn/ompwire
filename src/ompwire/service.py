"""`ompwire service`: run each gateway under systemd --user.

One gateway, one unit, one process. A gateway started from a shell dies with that shell,
and `ompwire update` can only restart it through systemd when a unit exists. The unit is
generated from the paths on this machine, because a checked-in one cannot know where the
command or the `.env` live.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

LEGACY_UNIT = "ompwire.service"


def unit_name(gateway: str = "telegram") -> str:
    return f"ompwire-{gateway}.service"


def unit_path(gateway: str = "telegram") -> Path:
    return Path.home() / ".config" / "systemd" / "user" / unit_name(gateway)


def legacy_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / LEGACY_UNIT


def _systemd_available() -> bool:
    if shutil.which("systemctl") is None:
        return False
    try:
        probe = subprocess.run(["systemctl", "--user", "show-environment"],
                               capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def command_path() -> str:
    found = shutil.which("ompwire")
    if found:
        return found
    return str(Path(sys.argv[0]).resolve())


def render_unit(exec_start: str, env_file: Path, workdir: str,
                gateway: str = "telegram") -> str:
    return f"""[Unit]
Description=ompwire gateway: {gateway}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={workdir}
EnvironmentFile={env_file}
ExecStart={exec_start} gateway run {gateway}
# Credentials live in {env_file} (chmod 600), loaded above rather than written
# into this unit. One gateway per unit: stopping this one leaves the others alone.
Restart=always
RestartSec=5
# One agent subprocess per conversation plus the poller; keep the cgroup honest.
MemoryMax=4G

[Install]
WantedBy=default.target
"""


def _env_file() -> Path:
    from .setup import _paths

    return _paths()[2]


def _workdir() -> str:
    from .config import load_config

    cwd = str(load_config().get("agent", "cwd", "") or "")
    return str(Path(cwd).expanduser()) if cwd else str(Path.home())


def _systemctl(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


def install(gateway: str = "telegram", *, force: bool = False, start: bool = True) -> int:
    from .update import serving_processes

    if not _systemd_available():
        print("FAIL systemd --user is not available here; run `ompwire serve --gateway "
              f"{gateway}` under a supervisor of your choice instead")
        return 1

    target = unit_path(gateway)
    if target.is_file() and not force:
        print(f"FAIL {target} already exists. Pass --force to rewrite it.")
        return 1

    running = serving_processes()
    if running:
        pids = ", ".join(str(pid) for pid, _command in running)
        print(f"FAIL a gateway is already running (pid {pids}). Two pollers make Telegram "
              f"answer 409 Conflict and drop updates.\n"
              f"     stop that process first (Ctrl-C in its terminal, or kill the pid),\n"
              f"     then run `ompwire service install {gateway} --force` so systemd owns it")
        return 1

    env_file = _env_file()
    if not env_file.is_file():
        print(f"FAIL {env_file} is missing: add a credential first "
              f"(`ompwire gateway add {gateway}`)")
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_unit(command_path(), env_file, _workdir(), gateway),
                      encoding="utf-8")
    print(f"ok   unit written: {target}")

    steps = (["daemon-reload"], ["enable"] + (["--now"] if start else []) + [unit_name(gateway)])
    for step in steps:
        result = _systemctl(*step)
        if result.returncode != 0:
            print(f"FAIL systemctl --user {' '.join(step)}: "
                  f"{(result.stderr or result.stdout).strip()}")
            return 1
    print(f"ok   {'started and enabled' if start else 'enabled'}: {unit_name(gateway)}")
    print(f"     logs:   journalctl --user -u {unit_name(gateway)} -f")
    print("     state:  ompwire gateway list")

    legacy = legacy_unit_path()
    if legacy.is_file():
        print(f"--   {LEGACY_UNIT} was the single-gateway unit; removing it so only "
              f"per-gateway units remain")
        uninstall(gateway, path=legacy, quiet=True)
    return 0


def uninstall(gateway: str = "telegram", *, keep_unit_file: bool = False,
              path: Path | None = None, quiet: bool = False) -> int:
    target = path or unit_path(gateway)
    name = target.name
    if not target.is_file():
        if not quiet:
            print(f"--   no unit at {target}, nothing to remove")
        return 0
    if _systemd_available():
        result = _systemctl("disable", "--now", name)
        if result.returncode != 0 and not quiet:
            print(f"--   systemctl --user disable --now said: "
                  f"{(result.stderr or result.stdout).strip()}")
    if not keep_unit_file:
        target.unlink(missing_ok=True)
        if not quiet:
            print(f"ok   removed {target}")
    if _systemd_available():
        _systemctl("daemon-reload")
    return 0


def show(gateway: str = "telegram") -> int:
    target = unit_path(gateway)
    if not target.is_file():
        print(f"--   no unit at {target}: `ompwire service install {gateway}` writes one")
        return 0
    print(target.read_text(encoding="utf-8"))
    if _systemd_available():
        print(f"state: {state(gateway)}")
    return 0


def state(gateway: str = "telegram") -> str:
    """What systemd thinks: active, inactive, failed, or no unit at all."""
    if not unit_path(gateway).is_file():
        return "no unit"
    if not _systemd_available():
        return "no systemd"
    result = _systemctl("is-active", unit_name(gateway), timeout=15)
    return result.stdout.strip() or "unknown"


def start(gateway: str = "telegram") -> int:
    return _unit_action("start", gateway)


def stop(gateway: str = "telegram") -> int:
    return _unit_action("stop", gateway)


def restart(gateway: str = "telegram") -> int:
    return _unit_action("restart", gateway)


def _unit_action(action: str, gateway: str) -> int:
    if not _systemd_available():
        print(f"FAIL systemd --user is not available; run `ompwire serve --gateway {gateway}`")
        return 1
    if not unit_path(gateway).is_file():
        print(f"FAIL no unit for {gateway!r} yet. Install it first: "
              f"`ompwire service install {gateway}`")
        return 1
    result = _systemctl(action, unit_name(gateway), timeout=120)
    if result.returncode != 0:
        print(f"FAIL systemctl --user {action} {unit_name(gateway)}: "
              f"{(result.stderr or result.stdout).strip()}")
        return 1
    print(f"ok   {action}: {unit_name(gateway)} is {state(gateway)}")
    return 0
