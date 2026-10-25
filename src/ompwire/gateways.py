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
IMPLEMENTED = ("telegram", "whatsapp")
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
    if kind == "whatsapp":
        return Platform(
            kind,
            credential="linked WhatsApp number",
            user_ids="phone numbers",
            credential_steps=(
                "Use a number of its own for the bot, not your daily one: anyone who messages "
                "that number reaches the bot's gate, and WhatsApp may restrict an unofficial "
                "client.",
                "On the phone with that number: WhatsApp > Settings > Linked devices > Link a "
                "device, then scan the QR shown here.",
                "No camera at hand? Pass --pair-phone <number> and type the 8-character code "
                "WhatsApp shows instead of scanning.",
            ),
            user_id_steps=(
                "Phone numbers with the country code, digits only: 6281234567890, not "
                "081234567890 or +62 812-3456-7890.",
                "Separate several with commas. Only these numbers get an answer, in a direct "
                "chat and in a group.",
            ),
        )
    raise GatewayError(f"the {kind} transport is not implemented yet")


def verify(kind: str, credential: str) -> str:
    """The identity the platform reports for this credential, "" when it refuses it."""
    if kind == "telegram":
        from .setup import bot_identity

        return bot_identity(credential)
    return ""


def ready(gateway: Gateway) -> bool:
    """Whether the gateway has what it needs to connect: a token, or a linked WhatsApp device."""
    if gateway.kind == "whatsapp":
        from .wa_client import linked_number, session_path

        return bool(linked_number(session_path(gateway.name)))
    return bool(token(gateway))


def identity(gateway: Gateway) -> str:
    """Who this gateway is on its platform, for `list`, `status` and `doctor`; "" when unknown."""
    if gateway.kind == "whatsapp":
        from .wa_client import linked_number, session_path

        number = linked_number(session_path(gateway.name))
        return f"+{number}" if number else ""
    credential = token(gateway)
    if not credential:
        return ""
    username = verify(gateway.kind, credential)
    return f"@{username}" if username else ""


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


GROUP_MODES = ("mention", "all", "read")


@dataclass
class Gateway:
    name: str
    kind: str = "telegram"
    enabled: bool = True
    # who may talk to it, as the platform names a person: Telegram user ids, WhatsApp phone
    # numbers (digits, country code first). Kept as text so neither platform's id is mangled.
    allowed: list[str] = field(default_factory=list)
    home: str | None = None
    # the groups (and Telegram channels) it listens in, and how it answers there
    groups: list[str] = field(default_factory=list)
    group_mode: str = "mention"
    # a mode for one group or channel, over group_mode: {"-1001234": "all"}
    group_modes: dict[str, str] = field(default_factory=dict)
    # this gateway's own settings, layered over config.toml: {"agent": {"model": ...}, ...}
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # every constructor (a parsed file, `gateway start`, a test) ends up with the same shape
        self.allowed = [user for user in map(user_id, self.allowed) if user]
        self.home = chat_id(self.home) or None
        self.groups = [group for group in map(chat_id, self.groups) if group]
        self.group_modes = {chat_id(key): str(value).lower() for key, value in self.group_modes.items()
                            if chat_id(key) and str(value).lower() in GROUP_MODES}

    def mode_for(self, chat: str) -> str:
        return self.group_modes.get(chat_id(chat), self.group_mode)

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


def user_id(value: Any) -> str:
    """One allow-list entry as text: digits only, so "+62 812-2227" and 628122227 match."""
    return re.sub(r"\D", "", str(value or ""))


def chat_id(value: Any) -> str:
    """A chat a gateway sends to or listens in: a person, a Telegram group or channel (a
    negative id, -100...), or a WhatsApp group (...@g.us). The sign and the server are kept."""
    text = str(value or "").strip()
    if "@" in text:
        return text
    digits = user_id(text)
    return f"-{digits}" if digits and text.startswith("-") else digits


def _parse(data: dict[str, Any]) -> list[Gateway]:
    found: list[Gateway] = []
    for name, raw in (data.get("gateway") or {}).items():
        if not isinstance(raw, dict):
            continue
        mode = str(raw.get("group_mode") or "mention").lower()
        settings = {section: dict(raw[section]) for section in OWN_SECTIONS
                    if isinstance(raw.get(section), dict) and raw[section]}
        modes = raw.get("group_modes") if isinstance(raw.get("group_modes"), dict) else {}
        found.append(Gateway(
            name=str(name),
            kind=str(raw.get("type") or raw.get("kind") or "telegram").lower(),
            enabled=bool(raw.get("enabled", True)),
            allowed=list(raw.get("allowed_users") or []),
            home=raw.get("home_channel"),
            groups=list(raw.get("groups") or []),
            group_mode=mode if mode in GROUP_MODES else "mention",
            group_modes=dict(modes),
            settings=settings,
        ))
    return sorted(found, key=lambda gw: gw.name)


def _read(path: Path) -> list[Gateway]:
    try:
        return _parse(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GatewayError(f"{path} is not readable TOML: {exc}") from exc


def load(config: Any = None) -> list[Gateway]:
    """Every configured gateway: one directory each, or the gateways.toml of a v0.14 install
    that has not been migrated yet. `config` is accepted for callers that have one; the
    definitions do not depend on it."""
    root = gateways_dir()
    found: list[Gateway] = []
    if root.is_dir():
        for path in sorted(root.glob("*/gateway.toml")):
            found.extend(item for item in _read(path) if item.name == path.parent.name)
    if found:
        return sorted(found, key=lambda gw: gw.name)
    legacy = gateways_file()
    if legacy.is_file():
        return _read(legacy)
    return []


def render(items: list[Gateway]) -> str:
    lines: list[str] = ["# Written by `ompwire gateway`.",
                        "# The credential stays in the .env next to this file.", ""]
    for item in items:
        lines.append(f"[gateway.{item.name}]")
        lines.append(f'type = "{item.kind}"')
        lines.append(f"enabled = {'true' if item.enabled else 'false'}")
        # numbers stay numbers in the file, so a hand-edited v0.14 file keeps its shape
        lines.append("allowed_users = [" + ", ".join(item.allowed) + "]")
        if item.home:
            lines.append(f"home_channel = {_toml_value(item.home) if '@' in item.home else item.home}")
        lines.append("groups = [" + ", ".join(_toml_value(group) for group in item.groups) + "]")
        lines.append(f'group_mode = "{item.group_mode}"')
        if item.group_modes:
            lines.append("group_modes = { " + ", ".join(
                f"{_toml_value(chat)} = {_toml_value(mode)}"
                for chat, mode in sorted(item.group_modes.items())) + " }")
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


def add(name: str, kind: str = "telegram", allowed: list[Any] | None = None,
        home: Any = None, *, enabled: bool = True) -> Gateway:
    name = validate_name(name)
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise GatewayError(f"unknown gateway type {kind!r}; known types: {', '.join(KINDS)}")
    items = load()
    if any(item.name == name for item in items):
        raise GatewayError(f"a gateway named {name!r} already exists")
    gateway = Gateway(name=name, kind=kind, enabled=enabled,
                      allowed=[user for user in map(user_id, allowed or []) if user],
                      home=user_id(home) or None)
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
    """This gateway's credential: the environment wins, then its own .env. The shared .env of an
    older install is read only by migrate(), which every `gateway` command runs first."""
    name = gateway.token_var()
    return os.environ.get(name) or _read_env(env_file(gateway.name)).get(name) or ""


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
    """Remove this gateway's credential. Its own .env goes when nothing else is left in it, and
    the gateway's config directory with it when that is empty too."""
    key = gateway.token_var()
    own = env_file(gateway.name)
    removed = _drop_key(own, key)
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
    moved.extend(_adopt_telegram_block())
    legacy = gateways_file()
    if legacy.is_file() and not any(gateways_dir().glob("*/gateway.toml")):
        items = _read(legacy)
        if items:
            save(items)
            legacy.rename(legacy.with_name("gateways.toml.migrated"))
            moved.append(f"definitions: {legacy} -> {gateways_dir()}/<name>/gateway.toml")
    shared = read_env_file()
    took = False
    for item in load():
        key = item.token_var()
        if key in shared and not _read_env(env_file(item.name)).get(key):
            set_token(item, shared[key])
            _drop_key(_env_file(), key)
            moved.append(f"credential {key}: shared .env -> {env_file(item.name)}")
            took = True
    moved.extend(_retire_shared_env(announce=took))

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


def _retire_shared_env(announce: bool) -> list[str]:
    """The shared .env held every bot's token. Once each gateway has its own, nothing reads it:
    delete it when only comments are left, and say once what is still in it otherwise."""
    path = _env_file()
    if not path.is_file():
        return []
    left = read_env_file()
    if left:
        if not announce:
            return []
        return [f"note: {path} still holds {', '.join(sorted(left))}; nothing reads it any more"]
    path.unlink()
    return [f"removed {path}: every credential now lives in its gateway's folder"]


def _adopt_telegram_block() -> list[str]:
    """Before gateways.toml (v0.9), the one bot lived in config.toml's [telegram] block and its
    token in TELEGRAM_BOT_TOKEN, and v0.14 still read that as a gateway named `telegram`. Turn it
    into a gateway directory once, when no gateway is defined any other way."""
    from .setup import _paths

    if any(gateways_dir().glob("*/gateway.toml")) or gateways_file().is_file():
        return []
    config_file = _paths()[1]
    try:
        block = tomllib.loads(config_file.read_text(encoding="utf-8")).get("telegram") or {}
    except (OSError, tomllib.TOMLDecodeError):
        return []
    allowed = [user for user in map(user_id, block.get("allowed_users") or []) if user]
    has_token = bool(os.environ.get("TELEGRAM_BOT_TOKEN") or read_env_file().get("TELEGRAM_BOT_TOKEN"))
    if not allowed and not has_token:
        return []
    save([Gateway(name="telegram", kind="telegram", allowed=allowed,
                  home=user_id(block.get("home_channel")) or None)])
    return [f"definitions: [telegram] in {config_file} -> {gateway_file('telegram')}"]


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
    """Put this gateway's identity in the config its transport reads, under `[gateway]` whatever
    the platform: a transport reads its credential and allow list from there, and keeps its own
    platform section (`[telegram]`) for settings that only make sense on that platform."""
    config.data["gateway"] = {
        "name": gateway.name,
        "type": gateway.kind,
        "credential": token(gateway),
        "allowed_users": list(gateway.allowed),
        "home_channel": gateway.home,
        "groups": list(gateway.groups),
        "group_mode": gateway.group_mode,
        "group_modes": dict(gateway.group_modes),
    }
    return config
