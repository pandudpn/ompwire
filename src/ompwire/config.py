"""Configuration loading for ompwire.

Precedence: built-in defaults < config.toml < environment variables.
Secrets never live in config.toml — they come from the environment (.env),
which is why every secret key is marked ``secret = true`` in the schema.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATHS = (
    Path("~/.config/ompwire/config.toml"),
    Path("~/.ompwire/config.toml"),
)

# env var -> (section, key) override map. Flat names keep .env readable.
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "OMPWIRE_ACP_COMMAND": ("acp", "command"),
    "OMPWIRE_ACP_ARGS": ("acp", "args"),
    "OMPWIRE_CWD": ("agent", "cwd"),
    "OMPWIRE_MODEL": ("agent", "model"),
    "OMPWIRE_MODE": ("agent", "mode"),
    "OMPWIRE_APPROVAL": ("agent", "approval_mode"),
    "OMPWIRE_STREAM_EDIT_INTERVAL": ("stream", "edit_interval"),
    "OMPWIRE_IDLE_TIMEOUT": ("session", "idle_timeout"),
    "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
}

DEFAULTS: dict[str, dict[str, Any]] = {
    "acp": {
        "command": "omp",
        "args": ["--mode", "acp"],
        "startup_timeout": 60.0,
        "turn_timeout": 900.0,
    },
    "agent": {
        "cwd": "~",
        "model": "",  # empty => whatever the ACP session defaults to
        "mode": "",  # default | plan
        "approval_mode": "always-ask",  # forwarded as --approval-mode when set
        "permission_policy": "allow",  # allow | deny (non-interactive default)
        "extra_instructions": "",
    },
    "session": {
        "store": "~/.local/state/ompwire/sessions.db",
        "idle_timeout": 1800,
    },
    "stream": {
        "enabled": True,
        "edit_interval": 1.0,
        "show_tool_calls": True,
        "show_usage": True,
    },
    "mcp": {
        "servers": [],  # list of {name, command, args, env} passed to session/new
    },
    "telegram": {
        "bot_token": "",  # secret: env only
        "allowed_users": [],
        "home_channel": "",
    },
}


@dataclass(slots=True)
class Config:
    """Resolved configuration plus the path it came from."""

    data: dict[str, dict[str, Any]]
    path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def section(self, name: str) -> dict[str, Any]:
        return self.data.get(name, {})

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.data.get(section, {}).get(key, default)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(raw: str) -> Any:
    """Turn an env string into a bool/int/float/list/str without a schema."""
    low = raw.strip().lower()
    if low in {"true", "false"}:
        return low == "true"
    if low in {"1", "0"} and len(low) == 1:
        return low == "1"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if "," in raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


def find_config_path(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    for candidate in DEFAULT_CONFIG_PATHS:
        path = candidate.expanduser()
        if path.is_file():
            return path
    return None


def load_config(explicit: str | Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    path = find_config_path(explicit)
    warnings: list[str] = []

    file_data: dict[str, Any] = {}
    if path is not None:
        try:
            file_data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"config error: {path} is not valid TOML ({exc})") from exc

    if path is None and explicit:
        warnings.append(f"config file not found: {explicit} (using defaults + env)")

    merged = _deep_merge(DEFAULTS, file_data)

    for env_name, (section, key) in ENV_OVERRIDES.items():
        if env_name in env and env[env_name] != "":
            merged.setdefault(section, {})[key] = _coerce(env[env_name])

    # Scalar env names people already have exported share names with config keys.
    for section, key, env_name in (("telegram", "bot_token", "TELEGRAM_BOT_TOKEN"),):
        if merged.get(section, {}).get(key, "") == "" and env.get(env_name):
            merged.setdefault(section, {})[key] = env[env_name]

    if merged["agent"]["cwd"]:
        merged["agent"]["cwd"] = str(Path(merged["agent"]["cwd"]).expanduser())
    merged["session"]["store"] = str(Path(merged["session"]["store"]).expanduser())

    return Config(data=merged, path=path, warnings=warnings)


def config_search_paths() -> list[str]:
    return [str(p.expanduser()) for p in DEFAULT_CONFIG_PATHS]
