"""Gateway definitions: one bot, one service, one process each.

A gateway is a chat platform connection with its own credential and its own allow list.
Keeping them separate is the whole point: a WhatsApp gateway that breaks should not take
the Telegram one down with it, and each gets its own systemd unit.

Definitions live in `gateways.toml` — a file ompwire owns, so a rewrite cannot chew
through the comments in `config.toml`. A legacy `[telegram]` section in `config.toml`
still works: it is read as a gateway named `telegram`.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("telegram", "whatsapp")
IMPLEMENTED = ("telegram",)
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


class GatewayError(RuntimeError):
    """A gateway definition that cannot be used, with the reason in the message."""


@dataclass
class Gateway:
    name: str
    kind: str = "telegram"
    enabled: bool = True
    allowed: list[int] = field(default_factory=list)
    home: int | None = None

    @property
    def implemented(self) -> bool:
        return self.kind in IMPLEMENTED

    def token_var(self) -> str:
        """The .env key for this gateway's credential."""
        if self.name == "telegram":
            return "TELEGRAM_BOT_TOKEN"      # the name every existing machine already has
        return f"OMPWIRE_GATEWAY_{self.name.upper().replace('-', '_')}_TOKEN"

    def unit(self) -> str:
        from .service import unit_name

        return unit_name(self.name)


def gateways_file() -> Path:
    from .setup import _paths

    return _paths()[0] / "gateways.toml"


def _parse(data: dict[str, Any]) -> list[Gateway]:
    found: list[Gateway] = []
    for name, raw in (data.get("gateway") or {}).items():
        if not isinstance(raw, dict):
            continue
        allowed: list[int] = []
        for value in raw.get("allowed_users") or []:
            try:
                allowed.append(int(value))
            except (TypeError, ValueError):
                continue
        home = raw.get("home_channel")
        found.append(Gateway(
            name=str(name),
            kind=str(raw.get("type") or raw.get("kind") or "telegram").lower(),
            enabled=bool(raw.get("enabled", True)),
            allowed=allowed,
            home=int(home) if str(home or "").strip().isdigit() else None,
        ))
    return sorted(found, key=lambda gw: gw.name)


def load(config: Any = None) -> list[Gateway]:
    """Every configured gateway, newest file first, legacy section second."""
    path = gateways_file()
    if path.is_file():
        try:
            found = _parse(tomllib.loads(path.read_text(encoding="utf-8")))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise GatewayError(f"{path} is not readable TOML: {exc}") from exc
        if found:
            return found
    return _from_config(config)


def _from_config(config: Any) -> list[Gateway]:
    """A `[telegram]` block in config.toml is a gateway named `telegram`."""
    if config is None:
        from .config import load_config

        try:
            config = load_config()
        except SystemExit:
            return []
    raw_allowed = config.get("telegram", "allowed_users") or []
    home = config.get("telegram", "home_channel")
    token = str(config.get("telegram", "bot_token") or "")
    if not raw_allowed and not token:
        return []
    allowed: list[int] = []
    for value in raw_allowed:
        try:
            allowed.append(int(value))
        except (TypeError, ValueError):
            continue
    return [Gateway(name="telegram", kind="telegram",
                    allowed=allowed,
                    home=int(home) if str(home or "").strip().isdigit() else None)]


def render(items: list[Gateway]) -> str:
    lines: list[str] = ["# Written by `ompwire gateway`. One section per bot.",
                        "# Credentials stay in .env, never in this file.", ""]
    for item in items:
        lines.append(f"[gateway.{item.name}]")
        lines.append(f'type = "{item.kind}"')
        lines.append(f"enabled = {'true' if item.enabled else 'false'}")
        if item.allowed:
            lines.append("allowed_users = [" + ", ".join(str(user) for user in item.allowed) + "]")
        else:
            lines.append("allowed_users = []")
        if item.home:
            lines.append(f"home_channel = {item.home}")
        lines.append("")
    return "\n".join(lines)


def save(items: list[Gateway], path: Path | None = None) -> Path:
    target = path or gateways_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(items), encoding="utf-8")
    return target


def validate_name(name: str) -> str:
    value = str(name or "").strip().lower()
    if not NAME_RE.match(value):
        raise GatewayError(f"invalid gateway name {name!r}: use lowercase letters, digits and "
                           f"dashes, starting with a letter")
    return value


def add(name: str, kind: str = "telegram", allowed: list[int] | None = None,
        home: int | None = None, *, enabled: bool = True) -> Gateway:
    name = validate_name(name)
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise GatewayError(f"unknown gateway type {kind!r}; known types: {', '.join(KINDS)}")
    items = load()
    if any(item.name == name for item in items):
        raise GatewayError(f"a gateway named {name!r} already exists")
    gateway = Gateway(name=name, kind=kind, enabled=enabled,
                      allowed=list(allowed or []), home=home)
    save(items + [gateway])
    return gateway


def remove(name: str) -> Gateway:
    items = load()
    keep = [item for item in items if item.name != name]
    if len(keep) == len(items):
        raise GatewayError(f"no gateway named {name!r}")
    save(keep)
    return next(item for item in items if item.name == name)


def get(name: str, config: Any = None) -> Gateway:
    for item in load(config):
        if item.name == name:
            return item
    known = ", ".join(item.name for item in load(config)) or "none configured"
    raise GatewayError(f"no gateway named {name!r} (configured: {known})")


def selected(config: Any, name: str = "") -> Gateway:
    """The gateway a command should act on: the named one, or the only one there is."""
    items = load(config)
    if name:
        for item in items:
            if item.name == name:
                return item
        known = ", ".join(item.name for item in items) or "none configured"
        raise GatewayError(f"no gateway named {name!r} (configured: {known})")
    if not items:
        raise GatewayError("no gateway configured. Run `ompwire gateway add telegram` first.")
    if len(items) == 1:
        return items[0]
    names = ", ".join(item.name for item in items)
    raise GatewayError(f"several gateways are configured ({names}); name one")


# ------------------------------------------------------------------ credentials


def _env_file() -> Path:
    from .setup import _paths

    return _paths()[2]


def read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    path = _env_file()
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def token(gateway: Gateway) -> str:
    """This gateway's credential: the environment wins, then .env."""
    name = gateway.token_var()
    value = os.environ.get(name) or read_env_file().get(name) or ""
    if not value and gateway.name == "telegram":
        # older machines keep the token under the platform name only
        value = os.environ.get("TELEGRAM_BOT_TOKEN") or read_env_file().get(
            "TELEGRAM_BOT_TOKEN") or ""
    return value


def set_token(gateway: Gateway, value: str) -> Path:
    """Write one credential into .env, leaving the other keys alone."""
    path = _env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = gateway.token_var()
    lines: list[str] = []
    replaced = False
    for raw in (path.read_text(encoding="utf-8").splitlines() if path.is_file() else []):
        if raw.strip().startswith(f"{key}="):
            lines.append(f"{key}={value}")
            replaced = True
        else:
            lines.append(raw)
    if not replaced:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def apply(config: Any, gateway: Gateway) -> Any:
    """Lay this gateway's credential and allow list onto the config the transport reads."""
    section = dict(config.section("telegram"))
    section["bot_token"] = token(gateway)
    section["allowed_users"] = list(gateway.allowed)
    if gateway.home:
        section["home_channel"] = gateway.home
    config.data["telegram"] = section
    config.data["_gateway"] = {"name": gateway.name, "type": gateway.kind}
    return config
