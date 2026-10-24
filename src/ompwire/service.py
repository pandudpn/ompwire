"""`ompwire service`: run the gateway under systemd --user.

A gateway started from a shell dies with that shell, and `ompwire update` can only
restart it through systemd when a unit exists. The unit is generated from the paths on
this machine, because a checked-in one cannot know where the command or the `.env` live.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "ompwire.service"


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


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


def render_unit(exec_start: str, env_file: Path, workdir: str) -> str:
    return f"""[Unit]
Description=ompwire — agent over ACP for chat platforms
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={workdir}
EnvironmentFile={env_file}
ExecStart={exec_start} serve
# The bot token lives in {env_file} (chmod 600), loaded above rather than
# written into this unit.
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


def install(*, force: bool = False, start: bool = True) -> int:
    from .update import serving_processes

    if not _systemd_available():
        print("FAIL systemd --user is not available here; run the gateway with "
              "`ompwire serve` instead (a supervisor of your choice can hold it)")
        return 1

    target = unit_path()
    if target.is_file() and not force:
        print(f"FAIL {target} already exists. Pass --force to rewrite it.")
        return 1

    running = serving_processes()
    if running:
        pids = ", ".join(str(pid) for pid, _command in running)
        print(f"FAIL a gateway is already running (pid {pids}). Two pollers make Telegram "
              f"answer 409 Conflict and drop updates.\n"
              f"     stop that process first (Ctrl-C in its terminal, or kill the pid),\n"
              f"     then run `ompwire service install --force` so systemd owns it from now on")
        return 1

    env_file = _env_file()
    if not env_file.is_file():
        print(f"FAIL {env_file} is missing: run `ompwire setup` so the unit has a token file")
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    body = render_unit(command_path(), env_file, _workdir())
    target.write_text(body, encoding="utf-8")
    print(f"ok   unit written: {target}")

    steps = (["daemon-reload"], ["enable"] + (["--now"] if start else []) + [UNIT_NAME])
    for step in steps:
        result = subprocess.run(["systemctl", "--user", *step],
                               capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            print(f"FAIL systemctl --user {' '.join(step)}: "
                  f"{(result.stderr or result.stdout).strip()}")
            return 1
    print(f"ok   {'started and enabled' if start else 'enabled'}: {UNIT_NAME}")
    print("     logs:  journalctl --user -u ompwire -f")
    print("     status: ompwire status")
    return 0


def uninstall(*, keep_unit_file: bool = False) -> int:
    target = unit_path()
    if not target.is_file():
        print(f"--   no unit at {target}, nothing to remove")
        return 0
    if _systemd_available():
        for step in (["disable", "--now", UNIT_NAME], ):
            result = subprocess.run(["systemctl", "--user", *step],
                                   capture_output=True, text=True, timeout=60, check=False)
            if result.returncode != 0:
                print(f"--   systemctl --user {' '.join(step)} said: "
                      f"{(result.stderr or result.stdout).strip()}")
    if not keep_unit_file:
        target.unlink(missing_ok=True)
        print(f"ok   removed {target}")
    if _systemd_available():
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, text=True, timeout=60, check=False)
    return 0


def show() -> int:
    target = unit_path()
    if not target.is_file():
        print(f"--   no unit at {target}: `ompwire service install` writes one")
        return 0
    print(target.read_text(encoding="utf-8"))
    if _systemd_available():
        state = subprocess.run(["systemctl", "--user", "is-active", UNIT_NAME],
                              capture_output=True, text=True, timeout=15, check=False)
        print(f"state: {state.stdout.strip() or state.stderr.strip()}")
    return 0
