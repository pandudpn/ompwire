"""Bridge an mcp-hub `servers.yaml` into ACP `mcpServers` entries.

omp 18.12.6 is strict about array-typed fields: a stdio entry without `env` and
an http entry without `headers` both fail `session/new` with an opaque internal
error. Every entry is therefore normalised to the full shape before it is sent.

PyYAML is only needed when this module actually loads a hub file, so it stays an
optional dependency (`pip install ompwire[hub]`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("ompwire.mcp_hub")


def _env_list(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, dict):
        return [{"name": str(k), "value": str(v)} for k, v in raw.items()]
    if isinstance(raw, list):
        out: list[dict[str, str]] = []
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                out.append({"name": str(item["name"]), "value": str(item.get("value", ""))})
        return out
    return []


def _header_list(raw: Any) -> list[dict[str, str]]:
    """omp wants headers as an array; mcp-hub writes them as a mapping."""
    if isinstance(raw, dict):
        return [{"name": str(k), "value": str(v)} for k, v in raw.items()]
    if isinstance(raw, list):
        return [{"name": str(i["name"]), "value": str(i.get("value", ""))}
                for i in raw if isinstance(i, dict) and i.get("name")]
    return []


def _split_command(raw: Any) -> tuple[str, list[str]]:
    """Hub files write either 'command: npx' + args, or the whole argv as a list."""
    if isinstance(raw, (list, tuple)):
        parts = [str(part) for part in raw]
        return (parts[0], parts[1:]) if parts else ("", [])
    if isinstance(raw, str) and raw.strip():
        return raw.strip(), [str(a) for a in ([] if raw else [])]
    return "", []


def hub_servers(
    path: str | Path,
    include: list[str] | tuple[str, ...] = (),
    exclude: list[str] | tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Return ACP-ready entries for the enabled servers in an mcp-hub file."""
    target = Path(path).expanduser()
    if not target.is_file():
        log.warning("mcp hub file not found: %s", target)
        return []
    try:
        import yaml  # noqa: PLC0415 - optional dependency, only needed here
    except ImportError:
        log.warning("PyYAML missing: install ompwire[hub] to load %s", target)
        return []
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("could not read %s: %s", target, exc)
        return []

    raw_servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(raw_servers, dict):
        raw_servers = data if isinstance(data, dict) else {}

    entries: list[dict[str, Any]] = []
    for name, raw in raw_servers.items():
        if not isinstance(raw, dict) or raw.get("disabled") or raw.get("enabled") is False:
            continue
        if include and name not in include:
            continue
        if name in exclude:
            continue
        transport = str(raw.get("type") or raw.get("transport") or "stdio").lower()
        if transport in {"remote", "http", "streamable-http", "sse"}:
            url = str(raw.get("url") or "")
            if not url:
                continue
            kind = "sse" if transport == "sse" else "http"
            entries.append({
                "name": name,
                "type": kind,
                "url": url,
                # required array: omitting it breaks session/new, and dropping the
                # hub's auth headers leaves servers answering 401
                "headers": _header_list(raw.get("headers")),
            })
            continue
        command, args = _split_command(raw.get("command"))
        if not command:
            continue
        entries.append({
            "name": name,
            "type": "stdio",
            "command": command,
            "args": args + [str(a) for a in (raw.get("args") or [])],
            "env": _env_list(raw.get("env")),
        })
    log.info("mcp hub: %d of %d servers enabled from %s", len(entries),
             len(raw_servers), target)
    return entries


def merge_entry(entry: dict[str, Any], patch: dict[str, Any]) -> None:
    """Apply an override, merging env/headers by name so auth is never dropped."""
    for key, value in (patch or {}).items():
        if value in (None, ""):
            continue
        if key in {"env", "headers"}:
            merged = {item["name"]: item["value"] for item in _env_list(entry.get(key))}
            merged.update({item["name"]: item["value"] for item in _env_list(value)})
            entry[key] = [{"name": name, "value": val} for name, val in merged.items()]
            continue
        entry[key] = value


def normalize(servers: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Fill the array fields omp requires so session/new cannot fail on shape."""
    out: list[dict[str, Any]] = []
    for server in servers or []:
        transport = str(server.get("type") or "stdio").lower()
        if transport in {"remote", "http", "streamable-http", "sse"}:
            out.append({
                "name": str(server.get("name", "mcp")),
                "type": "sse" if transport == "sse" else "http",
                "url": str(server.get("url", "")),
                "headers": _header_list(server.get("headers")),
            })
            continue
        command, args = _split_command(server.get("command"))
        out.append({
            "name": str(server.get("name", "mcp")),
            "type": "stdio",
            "command": command,
            "args": args + [str(a) for a in (server.get("args") or [])],
            "env": _env_list(server.get("env")),
        })
    return out
