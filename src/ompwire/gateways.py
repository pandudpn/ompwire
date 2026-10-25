"""Gateway definitions: one bot, one service, one process each.

A gateway is a chat platform connection with its own credential and its own allow list.
Keeping them separate is the whole point: a WhatsApp gateway that breaks should not take
the Telegram one down with it, and each gets its own systemd unit.

Each gateway owns two directories and nothing outside them:

    ~/.config/ompwire/gateways/<name>/gateway.toml   platform, allow list, its own settings
    ~/.config/ompwire/gateways/<name>/.env           its credential (0600)
    ~/.local/state/ompwire/gateways/<name>/          sessions.db, overrides.json, log, lock

so removing one is removing two directories. `config.toml` stays the shared default, and the
agent itself (omp, ~/.omp) is shared by all. `migrate()` moves an install from the old layout:
one gateways.toml, one .env, and the first gateway's state at the root of the state directory.
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
    """The old single definitions file, read only to migrate from it."""
    from .setup import _paths

    return _paths()[0] / "gateways.toml"


def gateways_dir() -> Path:
    from .setup import _paths

    return _paths()[0] / "gateways"


def gateway_file(name: str) -> Path:
    return gateways_dir() / name / "gateway.toml"


def env_file(name: str) -> Path:
    return gateways_dir() / name / ".env"


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


def _read(path: Path) -> list[Gateway]:
    try:
        return _parse(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GatewayError(f"{path} is not readable TOML: {exc}") from exc


def load(config: Any = None) -> list[Gateway]:
    """Every configured gateway: one directory each, then the old gateways.toml an install has
    not migrated yet, then a `[telegram]` block in config.toml."""
    root = gateways_dir()
    found: list[Gateway] = []
    if root.is_dir():
        for path in sorted(root.glob("*/gateway.toml")):
            found.extend(item for item in _read(path) if item.name == path.parent.name)
    if found:
        return sorted(found, key=lambda gw: gw.name)
    legacy = gateways_file()
    if legacy.is_file():
        found = _read(legacy)
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
    lines: list[str] = ["# Written by `ompwire gateway`.",
                        "# The credential stays in the .env next to this file.", ""]
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
    """Where one gateway keeps its runtime state: sessions, chat overrides, log, lock. Two bots
    never share a session, a /reasoning toggle or a log line."""
    from .config import default_state_dir

    return default_state_dir() / "gateways" / name


LOG_NAME = "ompwire.log"


def stores(config: Any = None) -> list[tuple[str, Path]]:
    """Every session store on this machine as (owner, path): the CLI's, then each gateway's."""
    from .config import load_config

    shared = config if config is not None else load_config()
    found = [("cli", Path(str(shared.get("session", "store"))).expanduser())]
    for item in load(shared):
        store = item.settings.get("session", {}).get("store") or state_dir(item.name) / "sessions.db"
        found.append((item.name, Path(str(store)).expanduser()))
    return found


def config_for(gateway: Gateway, explicit: Any = None) -> Any:
    """The full config one gateway runs with: shared config.toml, then its own settings, then its
    own chat overrides, with its own session store and log."""
    from .config import load_config

    home = state_dir(gateway.name)
    layer = {section: dict(values) for section, values in gateway.settings.items()}
    layer.setdefault("session", {}).setdefault("store", str(home / "sessions.db"))
    config = load_config(explicit, layer=layer, overrides_path=home / "overrides.json")
    if str(config.get("logging", "file", "") or ""):
        config.data.setdefault("logging", {})["file"] = str(home / LOG_NAME)
    return apply(config, gateway)


def save(items: list[Gateway]) -> Path:
    """Write each gateway to its own directory; a gateway missing from `items` loses its file."""
    root = gateways_dir()
    keep = {item.name for item in items}
    for item in items:
        target = gateway_file(item.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render([item]), encoding="utf-8")
    if root.is_dir():
        for path in root.glob("*/gateway.toml"):
            if path.parent.name not in keep:
                path.unlink()
                _prune(path.parent)
    return root


def _prune(directory: Path) -> None:
    """Remove a gateway directory once nothing is left in it."""
    try:
        directory.rmdir()
    except OSError:
        pass


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


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def token(gateway: Gateway) -> str:
    """This gateway's credential: the environment wins, then its own .env, then the shared .env
    an install has not migrated yet."""
    name = gateway.token_var()
    return (os.environ.get(name) or _read_env(env_file(gateway.name)).get(name)
            or read_env_file().get(name) or "")


def set_token(gateway: Gateway, value: str) -> Path:
    """Write this gateway's credential into its own .env, leaving any other keys alone."""
    path = env_file(gateway.name)
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


def _drop_key(path: Path, key: str) -> bool:
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [raw for raw in lines if not raw.strip().startswith(f"{key}=")]
    if len(kept) == len(lines):
        return False
    path.write_text("\n".join(kept).rstrip("\n") + ("\n" if kept else ""), encoding="utf-8")
    path.chmod(0o600)
    return True


def unset_token(gateway: Gateway) -> bool:
    """Remove this gateway's credential, wherever it is stored. Its own .env goes when nothing
    else is left in it, and the gateway's config directory with it when that is empty too."""
    key = gateway.token_var()
    own = env_file(gateway.name)
    removed = _drop_key(own, key)
    removed = _drop_key(_env_file(), key) or removed
    if own.is_file() and not _read_env(own):
        own.unlink()
    _prune(own.parent)
    return removed


# ------------------------------------------------------------------ migration


def migrate(state_for: str = "") -> list[str]:
    """Move an install from the old layout; returns one line per thing moved.

    Definitions and credentials move whenever this runs: a running gateway already holds its
    credential in its environment, and `token()` still finds a credential in either place.
    State moves only for `state_for`, the gateway about to start, and only while nothing holds its
    old lock, so no running process is writing the files being moved. Safe to run again.
    """
    from .config import default_state_dir

    moved: list[str] = []
    legacy = gateways_file()
    if legacy.is_file() and not any(gateways_dir().glob("*/gateway.toml")):
        items = _read(legacy)
        if items:
            save(items)
            legacy.rename(legacy.with_name("gateways.toml.migrated"))
            moved.append(f"definitions: {legacy} -> {gateways_dir()}/<name>/gateway.toml")
    shared = read_env_file()
    for item in load():
        key = item.token_var()
        if key in shared and not _read_env(env_file(item.name)).get(key):
            set_token(item, shared[key])
            _drop_key(_env_file(), key)
            moved.append(f"credential {key}: shared .env -> {env_file(item.name)}")

    root = default_state_dir()
    old_store = root / "sessions.db"
    if old_store.is_file():
        moved.extend(_copy_cli_rows(old_store, root / "cli" / "sessions.db"))
    if state_for == "telegram" and old_store.is_file():
        moved.extend(_move_root_state(root))
    if state_for:
        old_lock = root / f"serve-{state_for}.lock"
        if old_lock.is_file() and not _held(old_lock):
            old_lock.unlink()
    return moved


def _has_sessions(store: Path) -> bool:
    import sqlite3

    if not store.is_file():
        return False
    connection = sqlite3.connect(store)
    try:
        return any(connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                   for table in ("sessions", "inflight"))
    except sqlite3.Error:
        return True  # unreadable: do not overwrite it
    finally:
        connection.close()


def _held(lock: Path) -> bool:
    from .lock import holder

    return holder(lock) is not None


def _move_root_state(root: Path) -> list[str]:
    """The first gateway kept its state at the root of the state directory."""
    target = state_dir("telegram")
    if _held(root / "serve-telegram.lock") or _has_sessions(target / "sessions.db"):
        return []
    if (target / "sessions.db").exists():
        (target / "sessions.db").unlink()  # an empty store something opened before the move
    target.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    names = ["sessions.db", "sessions.db-journal", "overrides.json", LOG_NAME]
    names += [f"{LOG_NAME}.{index}" for index in range(1, 10)]
    for name in names:
        source = root / name
        if source.is_file():
            source.rename(target / name)
            moved.append(f"state: {source} -> {target / name}")
    return moved


def _copy_cli_rows(old_store: Path, cli_store: Path) -> list[str]:
    """`ompwire chat` kept its sessions in the same store; copy its rows to its own, once."""
    import sqlite3

    if cli_store.exists():
        return []
    from .store import SessionStore

    SessionStore(str(cli_store)).close()  # the schema
    source = sqlite3.connect(old_store)
    target = sqlite3.connect(cli_store)
    try:
        copied = 0
        for table in ("sessions", "prefs"):
            columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
            if not columns:
                continue
            names = ", ".join(columns)
            marks = ", ".join("?" for _ in columns)
            rows = source.execute(f"SELECT {names} FROM {table} WHERE key LIKE 'cli:%'").fetchall()
            target.executemany(f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({marks})", rows)
            copied += len(rows)
        target.commit()
    finally:
        source.close()
        target.close()
    return [f"cli sessions: {copied} row(s) -> {cli_store}"] if copied else []


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
