"""Gateways under systemd --user: one gateway, one unit, one process.

A gateway only ever runs as a supervised service. There is no foreground mode to babysit,
so `start` writes the unit if it is missing, enables it, and starts it in one step; `stop`
turns it off again; `remove` takes the unit away. The unit is generated from the paths on
this machine, because a checked-in one cannot know where the command or the `.env` live.
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


def agent_command() -> str:
    """The agent this machine is configured to launch."""
    from .config import load_config

    return str(load_config().get("acp", "command", "omp"))


def agent_binary(command: str = "") -> str | None:
    """Where the agent actually is, on PATH or where the installer puts it.

    A fresh install adds ~/.local/bin to the shell's rc files, which the current shell has not
    read yet, so `which omp` answers nothing while omp sits right there. Probing the install
    location is what makes `gateway start` work in the same shell that ran install.sh.
    """
    from .setup import agent_path

    return agent_path(command or agent_command())


def service_path(agent: str = "") -> str:
    """PATH for the unit.

    A systemd user unit does not inherit the interactive shell's PATH. Without this the gateway
    starts, then fails its first turn with "ACP command not found": the poller runs, and the agent
    binary sits in ~/.local/bin, which systemd never had on its path. The directories holding the
    binaries this install resolved come first, so the unit finds exactly what this shell found.
    """
    dirs: list[str] = []
    # The agent's directory first: ExecStart is already an absolute path to ompwire, so the agent
    # is the binary this PATH exists for.
    for binary in (agent_binary(agent) or "", command_path()):
        if not binary:
            continue
        parent = str(Path(binary).parent)
        if parent not in dirs:
            dirs.append(parent)
    for directory in (Path.home() / ".local" / "bin", Path.home() / ".bun" / "bin",
                      Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin")):
        if str(directory) not in dirs:
            dirs.append(str(directory))
    return ":".join(dirs)


def render_unit(exec_start: str, env_file: Path, workdir: str,
                gateway: str = "telegram", path: str = "") -> str:
    return f"""[Unit]
Description=ompwire gateway: {gateway}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={workdir}
EnvironmentFile={env_file}
# The agent is not on systemd's default path, so the unit carries the one this install used.
Environment=PATH={path or service_path()}
ExecStart={exec_start} gateway _run {gateway}
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


def _credential_works(gateway: str) -> bool:
    """A unit that restarts a rejected token every 5 seconds is worse than no unit."""
    from . import gateways

    try:
        items = {item.name: item for item in gateways.load()}
    except gateways.GatewayError:
        return True          # nothing readable to check: do not block on our own bug
    item = items.get(gateway)
    if item is None or item.kind != "telegram":
        return True          # not a telegram gateway, or not defined here at all
    token = gateways.token(item)
    if not token:
        return False
    from .setup import bot_identity

    return bool(bot_identity(token))


def _systemctl(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


def start(gateway: str = "telegram", *, force: bool = False, now: bool = True) -> int:
    """Systemd owns the gateway: write the unit if needed, enable it, start it."""
    from .update import serving_processes

    if not _systemd_available():
        print("FAIL systemd --user is not available on this machine, and a gateway only "
              "runs as a service.\n"
              "     On a machine without systemd --user, run it under whatever supervisor "
              "you do have:\n"
              f"       ompwire gateway _run {gateway}   # foreground, for a container or a "
              f"process manager")
        return 1

    target = unit_path(gateway)

    running = serving_processes()
    if running:
        pids = ", ".join(str(pid) for pid, _command in running)
        print(f"FAIL a gateway process is already running outside systemd (pid {pids}). Two "
              f"pollers make Telegram answer 409 Conflict and drop updates.\n"
              f"     stop it first (kill the pid), then `ompwire gateway start {gateway}`")
        return 1

    env_file = _env_file()
    if not env_file.is_file():
        print(f"FAIL {env_file} is missing: add a credential first "
              f"(`ompwire gateway add {gateway}`)")
        return 1
    agent = agent_binary()
    if agent is None:
        command = agent_command()
        print(f"FAIL the agent ({command}) was not found, so the unit would start and then fail "
              f"every turn.\n"
              f"     install it:  curl -fsSL https://omp.sh/install | sh\n"
              f"     then:        ompwire gateway start {gateway}")
        if not force:
            return 1
        print("     --force given: writing the unit anyway")

    if not force and not _credential_works(gateway):
        print(f"FAIL the credential for {gateway!r} is not accepted by the platform. "
              f"Restart=always would hammer it every 5 seconds.\n"
              f"     replace it: ompwire gateway add {gateway} --force")
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_unit(command_path(), env_file, _workdir(), gateway),
                      encoding="utf-8")
    print(f"ok   unit written: {target}")

    steps = (["daemon-reload"], ["enable"] + (["--now"] if now else []) + [unit_name(gateway)])
    for step in steps:
        result = _systemctl(*step)
        if result.returncode != 0:
            print(f"FAIL systemctl --user {' '.join(step)}: "
                  f"{(result.stderr or result.stdout).strip()}")
            return 1
    print(f"ok   {'started and enabled' if now else 'enabled'}: {unit_name(gateway)}")
    print(f"     logs:   journalctl --user -u {unit_name(gateway)} -f")
    print("     state:  ompwire gateway list")

    legacy = legacy_unit_path()
    if legacy.is_file():
        print(f"--   {LEGACY_UNIT} was the single-gateway unit; removing it so only "
              f"per-gateway units remain")
        remove(gateway, path=legacy, quiet=True)
    return 0


def remove(gateway: str = "telegram", *, keep_unit_file: bool = False,
           path: Path | None = None, quiet: bool = False) -> int:
    """Take the unit away: stop it, disable it, delete the file."""
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


def state(gateway: str = "telegram") -> str:
    """What systemd thinks: active, inactive, failed, or no unit at all."""
    if not unit_path(gateway).is_file():
        return "no unit"
    if not _systemd_available():
        return "no systemd"
    result = _systemctl("is-active", unit_name(gateway), timeout=15)
    return result.stdout.strip() or "unknown"


def stop(gateway: str = "telegram", *, disable: bool = True) -> int:
    """Off now, and off at the next boot: a gateway that comes back by itself is a surprise."""
    if not _systemd_available():
        print("FAIL systemd --user is not available on this machine; nothing is running "
              "under it either")
        return 1
    if not unit_path(gateway).is_file():
        print(f"--   no unit for {gateway!r}, nothing to stop")
        return 0
    steps = [["stop", unit_name(gateway)]]
    if disable:
        steps.append(["disable", unit_name(gateway)])
    for step in steps:
        result = _systemctl(*step, timeout=120)
        if result.returncode != 0:
            print(f"FAIL systemctl --user {' '.join(step)}: "
                  f"{(result.stderr or result.stdout).strip()}")
            return 1
    print(f"ok   stopped{' and disabled' if disable else ''}: {unit_name(gateway)}")
    return 0


def restart(gateway: str = "telegram") -> int:
    if not _systemd_available():
        print("FAIL systemd --user is not available on this machine")
        return 1
    target = unit_path(gateway)
    if not target.is_file():
        return start(gateway)
    # Rewrite the unit before restarting it. The unit is generated from this machine's paths, so a
    # plain `systemctl restart` would keep running an old PATH or an old ExecStart written by a
    # previous version: the process restarts and the same bug restarts with it.
    refreshed = render_unit(command_path(), _env_file(), _workdir(), gateway)
    if target.read_text(encoding="utf-8") != refreshed:
        target.write_text(refreshed, encoding="utf-8")
        print(f"ok   unit refreshed: {target}")
        reload_result = _systemctl("daemon-reload")
        if reload_result.returncode != 0:
            print(f"FAIL systemctl --user daemon-reload: "
                  f"{(reload_result.stderr or reload_result.stdout).strip()}")
            return 1
    result = _systemctl("restart", unit_name(gateway), timeout=120)
    if result.returncode != 0:
        print(f"FAIL systemctl --user restart {unit_name(gateway)}: "
              f"{(result.stderr or result.stdout).strip()}")
        return 1
    print(f"ok   restarted: {unit_name(gateway)} is {state(gateway)}")
    return 0
