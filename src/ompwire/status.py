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
    """(state, detail) — gateway units first, then a bare process, then nothing running."""
    from .update import _active_units, serving_processes

    units = _active_units()
    if units:
        return "systemd", f"{len(units)} gateway unit(s) active: {', '.join(units)}"
    serves = serving_processes()
    if serves:
        pids = ", ".join(str(pid) for pid, _command in serves)
        return "process", f"running as pid {pids} (no systemd unit)"
    return "stopped", "not running"


def _gateway_rows() -> list[tuple[str, str]]:
    """One row per configured gateway: identity, allow list, service state."""
    from . import gateways, service

    try:
        items = gateways.load()
    except gateways.GatewayError as exc:
        return [("gateways", f"unreadable: {exc}")]
    if not items:
        return [("gateways", "none (run `ompwire gateway start`)")]
    rows: list[tuple[str, str]] = []
    for item in items:
        if not gateways.ready(item):
            identity = "not linked" if item.kind == "whatsapp" else "no credential"
        else:
            try:
                identity = gateways.identity(item) or "credential rejected"
            except Exception:
                identity = "credential rejected"
        allowed = ", ".join(item.allowed) or "no users"
        rows.append((f"gateway {item.name}",
                     f"{item.kind} · {identity} · users {allowed} · {service.state(item.name)}"))
    return rows


def _sessions(store_path: str) -> str:
    from .store import SessionStore

    if not Path(store_path).expanduser().is_file():
        return f"{store_path} (none yet)"
    try:
        store = SessionStore(store_path)
    except Exception as exc:
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
    except Exception:
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

    yolo = bool(config.get("agent", "yolo", False))
    if yolo:
        rows.append(("approvals", "yolo (bypassed)"))
    else:
        rows.append(("approvals", f"ask · agent mode "
                                  f"{config.get('agent', 'approval_mode', 'always-ask')}"
                                  f" · ompwire policy {approval_policy(config)}"))
    rows.append(("cwd", str(config.get("agent", "cwd", "") or "") or os.path.expanduser("~")))

    rows.extend(_gateway_rows())

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
        except Exception as exc:
            detail += f" · hub unreadable: {exc}"
    if excludes:
        detail += f" · excluded: {', '.join(excludes)}"
    rows.append(("mcp", detail))

    rows.append(("processes", _gateway_state()[1]))
    from . import gateways

    for owner, store_file in gateways.stores(config):
        rows.append((f"sessions {owner}", _sessions(str(store_file))))
    rows.append(("log", _log_line(config)))
    return rows


def render(rows: list[tuple[str, str]]) -> str:
    width = max((len(label) for label, _ in rows), default=0)
    return "\n".join(f"{label.ljust(width)}  {value}" for label, value in rows)


def run(config_path: str | None = None) -> int:
    """Always exits 0: status reports, doctor judges."""
    print(render(collect(config_path)))
    return 0
