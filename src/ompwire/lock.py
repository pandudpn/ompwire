"""One transport per bot.

Telegram answers a second poller with 409 Conflict and drops updates, so a second
A second gateway process for the same bot is not a harmless mistake: the bot goes quiet while both processes look
healthy. The lock is an flock, which means the kernel releases it the moment the holder
dies — a stale file is never a reason to refuse to start.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    """Another serve holds the lock."""

    def __init__(self, pid: int, path: Path) -> None:
        self.pid = pid
        self.path = path
        super().__init__(f"another gateway is running (pid {pid}, lock {path})")


def lock_path(gateway: str = "") -> Path:
    """Per gateway: two gateways may run side by side, the same one may not run twice."""
    from .config import default_state_dir

    name = f"-{gateway}" if gateway else ""
    return default_state_dir() / f"serve{name}.lock"


def holder(path: Path | None = None) -> int | None:
    """The pid holding the lock right now, or None when nobody holds it."""
    target = path or lock_path()
    if not target.is_file():
        return None
    try:
        raw = target.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return None
    if not raw:
        return None
    try:
        pid = int(raw[0].split()[0])
    except (IndexError, ValueError):
        return None
    return pid if _alive(pid) else None


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire(path: Path | None = None) -> int:
    """Take the lock, or raise AlreadyRunning with the pid that holds it."""
    target = path or lock_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(target, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pid = holder(target) or -1
        os.close(handle)
        raise AlreadyRunning(pid, target) from None
    os.truncate(handle, 0)
    os.write(handle, f"{os.getpid()}\n".encode())
    os.fsync(handle)
    return handle


def release(handle: int | None, path: Path | None = None) -> None:
    if handle is None:
        return
    target = path or lock_path()
    try:
        os.truncate(handle, 0)
    except OSError:
        pass
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(handle)
    except OSError:
        pass
    try:
        if target.exists() and target.stat().st_size == 0:
            target.unlink()
    except OSError:
        pass
