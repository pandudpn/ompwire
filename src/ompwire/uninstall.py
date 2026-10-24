"""`ompwire uninstall`: remove ompwire, and only ompwire.

The agent's own state (`~/.omp`) is never touched — a transport that deleted the
agent's sessions and credentials on the way out would be a trap. What gets removed is
what ompwire itself created: its config (including the bot token), its state, its MCP
installs, and the installed package.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

CONFIG_DIR = Path("~/.config/ompwire")
STATE_DIR = Path("~/.local/state/ompwire")
DATA_DIR = Path("~/.local/share/ompwire")
NEVER_TOUCH = (Path("~/.omp"),)


def _config_dir() -> Path:
    """Honour OMPWIRE_CONFIG_DIR so a sandbox run cannot delete a real install."""
    override = os.environ.get("OMPWIRE_CONFIG_DIR", "").strip()
    return Path(override).expanduser() if override else CONFIG_DIR.expanduser()


def targets(keep_config: bool = False) -> list[tuple[Path, str]]:
    """Everything ompwire owns, with a note on what it holds."""
    plan = [
        (STATE_DIR.expanduser(), "session ids, per-chat prefs, approval override, logs"),
        (DATA_DIR.expanduser(), "MCP server installs created by ompwire"),
    ]
    if not keep_config:
        plan.insert(0, (_config_dir(), "config.toml and .env (bot token)"))
    return [(path, note) for path, note in plan if path.exists()]


def size_of(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                continue
    return total


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def under_systemd(pid: int) -> bool:
    """True when this pid is a supervised gateway rather than a stray one.

    `start` refuses to launch a second poller because two pollers make Telegram answer 409
    Conflict. The process the unit runs matches the same command line, so without this check a
    machine whose gateway is healthy refuses to re-run `start` and tells the user to kill it.
    """
    try:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "/ompwire" in cgroup and ".service" in cgroup


def running_serves() -> list[tuple[int, str]]:
    """Find running gateways without always trusting pgrep to be installed.

    Matches `gateway _run` and the older `gateway run` / `serve` spellings, so an upgrade cannot leave a
    process behind that this code no longer recognises.
    """
    output = ""
    try:
        result = subprocess.run(["pgrep", "-af", "ompwire gateway (_run|run)|ompwire serve"], capture_output=True,
                                text=True, timeout=10, check=False)
        output = result.stdout
    except (OSError, subprocess.SubprocessError):
        proc = Path("/proc")
        if proc.is_dir():
            rows = []
            for entry in proc.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
                except OSError:
                    continue
                rows.append(f"{entry.name} {cmdline}")
            output = "\n".join(rows)
    found = []
    for line in output.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, command = int(parts[0]), parts[1]
        if pid == os.getpid() or "pgrep" in command or "ompwire uninstall" in command:
            continue
        if "ompwire" not in command:
            continue
        if not any(mark in command for mark in ("gateway _run", "gateway run", "serve")):
            continue
        if under_systemd(pid):
            continue
        found.append((pid, command.strip()))
    return found


def stop(pid: int) -> bool:
    try:
        os.kill(pid, 15)
    except OSError:
        return False
    return True


def package_command() -> list[str]:
    """The uninstall command for the installer that owns this copy."""
    from .update import install_mode

    mode = install_mode()
    if mode == "uv":
        return ["uv", "tool", "uninstall", "ompwire"]
    if mode == "pipx":
        return ["pipx", "uninstall", "ompwire"]
    return []


def _protected(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    return any(resolved == guard.expanduser().resolve()
               or guard.expanduser().resolve() in resolved.parents for guard in NEVER_TOUCH)


def run(*, yes: bool = False, keep_config: bool = False, force: bool = False,
        dry_run: bool = False) -> int:
    print("ompwire uninstall\n")
    if not dry_run and not yes:
        print("Pass --yes to actually remove, or --dry-run to only look.\n")

    from . import service

    unit = service.unit_path()
    if unit.is_file():
        if dry_run:
            print(f"--   systemd unit would be disabled and removed: {unit}")
        else:
            service.remove()

    plan = targets(keep_config)
    serves = running_serves()
    if serves:
        print(f"--   {len(serves)} running transport(s):")
        for pid, command in serves:
            print(f"       {pid}  {command[:90]}")
        if dry_run:
            print("     (a dry run only reports; use --force to stop them when removing)")
        elif not force:
            print("\nFAIL stop them first, or re-run with --force to stop them now.\n"
                  "     A running gateway rewrites its state on exit, so removing it underneath\n"
                  "     a live process leaves a half-written session store.")
            return 1

    if not plan:
        print("ok   nothing to remove: no config, state or data directory found")
    total = 0
    for path, note in plan:
        if _protected(path):
            print(f"skip {path} (protected: never touch the agent's own state)")
            continue
        size = size_of(path)
        total += size
        print(f"     {path}  {human(size)}\n       {note}")

    command = package_command()
    print(f"\n     package: {' '.join(command) if command else 'git checkout, not an installed tool'}")

    if dry_run:
        print(f"\ndry run: {human(total)} would be freed. Nothing was removed.")
        return 0

    if not yes:
        try:
            answer = input("\nremove all of the above? [y/N]: ").strip().lower()
        except (EOFError, OSError):
            answer = ""
        if answer not in ("y", "yes"):
            print("nothing removed")
            return 0

    if serves and force:
        for pid, _command in serves:
            print(f"     stopped {pid}" if stop(pid) else f"     could not stop {pid}")

    freed = 0
    for path, _note in plan:
        if _protected(path):
            continue
        size = size_of(path)
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            print(f"FAIL could not remove {path}: {exc}")
            return 1
        freed += size
        print(f"ok   removed {path}")

    if keep_config:
        print(f"ok   kept {_config_dir()} (config and token still on disk)")

    if command:
        try:
            result = subprocess.run(command, timeout=300, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"FAIL package removal failed: {exc}")
            return 1
        if result.returncode == 0:
            print(f"ok   removed the installed package ({' '.join(command)})")
        else:
            print(f"FAIL {' '.join(command)} exited {result.returncode}")
            return 1
    else:
        print("--   running from a checkout: delete the directory yourself once you are done")
        print("     (this process is running from it, so it cannot remove itself)")

    print(f"\n{human(freed)} freed. Left untouched: ~/.omp (agent state) and anything outside "
          "ompwire's own directories.")
    return 0
