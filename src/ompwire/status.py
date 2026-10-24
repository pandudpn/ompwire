"""`ompwire status`: what this installation is doing right now.

doctor answers "will it work?" and fails loudly. status answers "what is it set to, and
is it running?" — one screen, no handshake, so it stays fast enough to run casually.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import __version__
from .config import approval_policy, load_config


def _config_file() -> Path:
    from .setup import _paths

    return _paths()[1]


def _agent_version(command: str, timeout: float = 6.0) -> str:
    path = shutil.which(command)
    if path is None:
        return "not found"
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return f"{path} (version unknown)"
    line = (result.stdout or result.stderr).strip().splitlines()
    return f"{line[0]} ({path})" if line else f"{path} (no version output)"


def _gateway_state() -> tuple[str, str]:
    """(state, detail) — systemd first, then a bare process, then nothing running."""
    from .update import SYSTEMD_UNIT, _systemd_active, serving_processes

    if _systemd_active():
        return "systemd", f"user unit {SYSTEMD_UNIT} is active"
    serves = serving_processes()
    if serves:
        pids = ", ".join(str(pid) for pid, _command in serves)
        return "process", f"running as pid {pids} (no systemd unit)"
    return "stopped", "not running"


def _sessions(store_path: str) -> str:
    from .store import SessionStore

    try:
        store = SessionStore(store_path)
    except Exception as exc:  # noqa: BLE001 - status must not raise
        return f"{store_path} (unreadable: {exc})"
    try:
        records = store.all()
    finally:
        store.close()
    if not records:
        return f"{store_path} (no sessions yet)"
    return f"{len(records)} session(s) in {store_path}"


def _log_line(config) -> str:
    target = str(config.get("logging", "file", "") or "")
    if not target:
        return "no log file configured"
    path = Path(target).expanduser()
    if not path.is_file():
        return f"{path} (not created yet)"
    return f"{path} ({path.stat().st_size / 1024:.0f} KiB)"


def collect(config_path: str | None = None) -> list[tuple[str, str]]:
    config = load_config(config_path)
    rows: list[tuple[str, str]] = []

    rows.append(("version", f"ompwire {__version__}"))
    try:
        from .update import install_mode

        rows.append(("installed", install_mode()))
    except Exception:  # noqa: BLE001
        rows.append(("installed", "unknown"))

    config_file = Path(config_path) if config_path else _config_file()
    rows.append(("config", str(config_file) if config_file.is_file()
                 else f"{config_file} (missing: run ompwire setup)"))

    command = str(config.get("acp", "command", "omp"))
    rows.append(("agent", _agent_version(command)))

    model = str(config.get("agent", "model", "") or "") or "agent default"
    rows.append(("model", model))
    rows.append(("mode", str(config.get("agent", "mode", "") or "") or "agent default"))
    rows.append(("thinking", str(config.get("agent", "thinking", "") or "") or "agent default"))

    yolo = bool(config.get("agent", "yolo", True))
    if yolo:
        rows.append(("approvals", "yolo (bypassed)"))
    else:
        rows.append(("approvals", f"ask · agent mode "
                                  f"{config.get('agent', 'approval_mode', 'always-ask')}"
                                  f" · ompwire policy {approval_policy(config)}"))
    rows.append(("cwd", str(config.get("agent", "cwd", "") or "") or os.path.expanduser("~")))

    allowed = [str(user) for user in (config.get("telegram", "allowed_users") or [])]
    rows.append(("allowed users", ", ".join(allowed) if allowed
                 else "none (nobody can talk to the bot)"))

    token = str(config.get("telegram", "bot_token", "") or "")
    rows.append(("telegram token", "set" if token else "missing (run ompwire setup)"))

    source = str(config.get("mcp", "source", "none") or "none")
    servers = [str(server.get("name", "?")) for server in config.get("mcp", "servers") or []]
    excludes = [str(name) for name in config.get("mcp", "exclude") or []]
    detail = f"source {source}"
    if servers:
        detail += f" · {len(servers)} server(s) in config"
    if source == "mcp-hub":
        try:
            from .mcp_hub import hub_servers

            merged = hub_servers(
                config.get("mcp", "hub_path", "~/mcp-hub/servers.yaml"),
                include=config.get("mcp", "include", []) or [],
                exclude=config.get("mcp", "exclude", []) or [],
            )
            detail += f" · {len(merged)} server(s) from the hub"
        except Exception as exc:  # noqa: BLE001 - a broken hub is a status fact, not a crash
            detail += f" · hub unreadable: {exc}"
    if excludes:
        detail += f" · excluded: {', '.join(excludes)}"
    rows.append(("mcp", detail))

    state, detail = _gateway_state()
    rows.append(("gateway", f"{state} · {detail}"))
    rows.append(("sessions", _sessions(str(config.get("session", "store")))))
    rows.append(("log", _log_line(config)))
    return rows


def render(rows: list[tuple[str, str]]) -> str:
    width = max((len(label) for label, _ in rows), default=0)
    return "\n".join(f"{label.ljust(width)}  {value}" for label, value in rows)


def run(config_path: str | None = None) -> int:
    """Always exits 0: status reports, doctor judges."""
    print(render(collect(config_path)))
    return 0
