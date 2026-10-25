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


@dataclass(frozen=True)
class Platform:
    """What `gateway start` needs to know to connect one platform.

    `verify` turns a credential into the identity the platform reports for it (a bot username),
    or "" when the platform refuses it. Nothing is written before it answers.
    """

    kind: str
    credential: str  # what the person is asked for
    user_ids: str  # how the allow list is described
    # shown right before each question, so nobody has to leave the terminal to find out how
    credential_steps: tuple[str, ...] = ()
    user_id_steps: tuple[str, ...] = ()


def platform(kind: str) -> Platform:
    if kind == "telegram":
        return Platform(
            kind,
            credential="bot token from @BotFather",
            user_ids="telegram user ids",
            credential_steps=(
                "Open Telegram and start a chat with @BotFather (https://t.me/BotFather).",
                "Send /newbot, then a display name, then a username that ends in \"bot\".",
                "BotFather replies with the token: digits, a colon, then a long string, like "
                "123456789:AAH... Copy all of it.",
                "Already have a bot? Send /mybots, pick it, then API Token. /revoke there gives "
                "a new token and stops the old one.",
            ),
            user_id_steps=(
                "A user id is a number, not an @username.",
                "In Telegram, start a chat with @userinfobot (https://t.me/userinfobot) and send "
                "it any message.",
                "It replies with your Id, a number like 123456789. Copy that.",
                "For other people: ask each of them to do the same and send you their number.",
                "Separate several ids with commas. Leave it blank and nobody can talk to the bot "
                "until you run gateway start again.",
            ),
        )
    raise GatewayError(f"the {kind} transport is not implemented yet")


def verify(kind: str, credential: str) -> str:
    """The identity the platform reports for this credential, "" when it refuses it."""
    if kind == "telegram":
        from .setup import bot_identity

        return bot_identity(credential)
    return ""


def mask(credential: str) -> str:
    """Enough of a credential to recognise it, not enough to use it."""
    value = credential.strip()
    if not value:
        return "none"
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


class GatewayError(RuntimeError):
    """A gateway definition that cannot be used, with the reason in the message."""


# The config sections a gateway may set for itself. Everything else (acp, mcp, logging) is the
# machine's, shared by every gateway; the agent binary and ~/.omp are always shared.
OWN_SECTIONS = ("agent", "stream", "approval", "files", "session")


@dataclass
class Gateway:
    name: str
    kind: str = "telegram"
    enabled: bool = True
    allowed: list[int] = field(default_factory=list)
    home: int | None = None
    # this gateway's own settings, layered over config.toml: {"agent": {"model": ...}, ...}
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)

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
        settings = {section: dict(raw[section]) for section in OWN_SECTIONS
                    if isinstance(raw.get(section), dict) and raw[section]}
        found.append(Gateway(
            name=str(name),
            kind=str(raw.get("type") or raw.get("kind") or "telegram").lower(),
            enabled=bool(raw.get("enabled", True)),
            allowed=allowed,
            home=int(home) if str(home or "").strip().isdigit() else None,
            settings=settings,
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
        for section in OWN_SECTIONS:
            values = item.settings.get(section) or {}
            if not values:
                continue
            lines.append(f"[gateway.{item.name}.{section}]")
            lines.extend(f"{key} = {_toml_value(value)}" for key, value in values.items())
            lines.append("")
    return "\n".join(lines)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def state_dir(name: str) -> Path:
    """Where one gateway keeps its own runtime state: sessions, chat overrides, log.

    Each gateway owns a directory so two bots never share a session, a /reasoning toggle or a
    log line. The first gateway, `telegram`, keeps the paths it has always used: moving a live
    install's sessions.db would cost the conversations in it.
    """
    from .config import default_state_dir

    base = default_state_dir()
    return base if name == "telegram" else base / "gateways" / name


def config_for(gateway: Gateway, explicit: Any = None) -> Any:
    """The full config one gateway runs with: shared config.toml, then its own settings, then its
    own chat overrides, with its own session store and log."""
    from .config import load_config

    home = state_dir(gateway.name)
    layer = {section: dict(values) for section, values in gateway.settings.items()}
    if gateway.name != "telegram":
        layer.setdefault("session", {}).setdefault("store", str(home / "sessions.db"))
    config = load_config(explicit, layer=layer, overrides_path=home / "overrides.json")
    if gateway.name != "telegram":
        shared_log = str(config.get("logging", "file", "") or "")
        if shared_log:
            config.data.setdefault("logging", {})["file"] = str(home / Path(shared_log).name)
    return apply(config, gateway)


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
        raise GatewayError("no gateway configured. Run `ompwire gateway start` first.")
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
