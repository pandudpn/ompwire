"""`ompwire gateway update [NAME]`: change what an existing gateway does, on any platform.

`start` connects a bot; `update` changes the gateway it already is: its groups and the mode it
answers in there, and its own settings over config.toml (model, working directory, approval
mode, thinking level, reasoning and activity in direct chats). Nothing here knows a platform:
groups come from the chats the running gateway has recorded (`gateways.seen_chats`), so a group
is picked by name and nobody has to fish for an id.

Without flags it is a menu that loops until "save"; with flags it changes exactly those and asks
nothing. Either way the gateway restarts once at the end when it was running.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import clear_override, load_config

# The agent's levels as omp reports them; the live list replaces this when the agent answers.
THINKING_LEVELS = ("off", "auto", "minimal", "low", "medium", "high", "xhigh")
APPROVAL_MODES = ("write", "yolo")
INHERIT = "inherit"  # a flag value that drops the gateway's own value

# (section, key) of every setting `update` owns, with how it is shown and set.
FIELDS: dict[str, tuple[str, str]] = {
    "model": ("agent", "model"),
    "cwd": ("agent", "cwd"),
    "approval": ("agent", "approval_mode"),
    "thinking": ("agent", "thinking"),
    "reasoning": ("stream", "show_reasoning"),
    "verbose": ("stream", "show_tool_calls"),
}
LABELS = {
    "model": "model",
    "cwd": "working directory",
    "approval": "approval mode",
    "thinking": "thinking level",
    "reasoning": "reasoning in direct chats",
    "verbose": "activity (trace, todo list) in direct chats",
}
# the per-chat toggle a direct chat stores under the same name; it would hide a new default
CHAT_PREF = {"thinking": "thinking", "model": "model", "reasoning": "reasoning",
             "verbose": "verbose"}


class UpdateError(ValueError):
    """A value `update` refuses, with the reason in the message."""


def _bool(value: str, field: str) -> bool:
    low = value.strip().lower()
    if low in {"on", "true", "yes", "1"}:
        return True
    if low in {"off", "false", "no", "0"}:
        return False
    raise UpdateError(f"{field}: {value!r} is not on or off")


def parse_value(field: str, raw: str, thinking_levels: tuple[str, ...] = THINKING_LEVELS) -> Any:
    """The typed value a flag or an answer stands for, or `INHERIT` to drop the gateway's own."""
    value = raw.strip()
    if value.lower() == INHERIT:
        return INHERIT
    if not value:
        raise UpdateError(f"{field}: empty; use {INHERIT} to fall back to config.toml")
    if field == "approval":
        if value.lower() not in APPROVAL_MODES:
            raise UpdateError(f"approval: {value!r}; choose {', '.join(APPROVAL_MODES)}")
        return value.lower()
    if field == "thinking":
        if value.lower() not in thinking_levels:
            raise UpdateError(f"thinking: {value!r}; choose {', '.join(thinking_levels)}")
        return value.lower()
    if field in ("reasoning", "verbose"):
        return _bool(value, field)
    if field == "cwd":
        path = Path(value).expanduser()
        if not path.is_dir():
            raise UpdateError(f"cwd: {path} is not a directory")
        return str(path)
    return value


def apply_value(settings: dict[str, dict[str, Any]], field: str, value: Any) -> None:
    section, key = FIELDS[field]
    if value == INHERIT:
        values = settings.get(section) or {}
        values.pop(key, None)
        if not values:
            settings.pop(section, None)
        return
    settings.setdefault(section, {})[key] = value


def show(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def effective(gateway: Any, field: str) -> tuple[str, str]:
    """(value, where it comes from) for one field: the gateway's own, or config.toml's."""
    section, key = FIELDS[field]
    own = (gateway.settings.get(section) or {}).get(key)
    if own is not None and own != "":
        return show(own), "own"
    shared = load_config().get(section, key, "")
    if field in ("reasoning", "verbose"):
        return show(bool(shared)), "config.toml"
    return (show(shared) if shared not in (None, "") else "agent default"), "config.toml"


# ------------------------------------------------------------------ groups
def parse_groups(given: str, kind: str) -> tuple[list[str], dict[str, str]]:
    """`-1001,-1002:all` or `1203...@g.us:read`: the chats, and the ones with a mode of their own."""
    from .gateways import GROUP_MODES, chat_id

    groups: list[str] = []
    modes: dict[str, str] = {}
    for part in (piece.strip() for piece in given.split(",")):
        if not part:
            continue
        head, _, tail = part.rpartition(":")
        chat, own = (head, tail) if head and tail.lower() in GROUP_MODES else (part, "")
        if kind == "whatsapp" and "@" not in chat:
            chat = f"{chat}@g.us"
        chat = chat_id(chat)
        if not chat:
            continue
        groups.append(chat)
        if own:
            modes[chat] = own.lower()
    return groups, modes


def chat_label(chat: str, seen: dict[str, dict[str, str]]) -> str:
    entry = seen.get(chat) or {}
    title = entry.get("title") or ""
    kind = entry.get("kind") or "group"
    return f"{title}  ({kind} {chat})" if title else f"{kind} {chat}"


def _pick_groups(gateway: Any, groups: list[str]) -> list[str]:
    """Toggle groups by number from every chat the gateway has seen, plus the listed ones."""
    from .gateways import chat_id, seen_chats

    seen = seen_chats(gateway.name)
    known = sorted(set(seen) | set(groups),
                   key=lambda chat: ((seen.get(chat) or {}).get("title") or "~", chat))
    if not known:
        print("--   this gateway has seen no group or channel yet. Add the bot to one while the\n"
              "     gateway runs (on WhatsApp, its number must be in the group), then run update\n"
              "     again; it will be listed by name. An id you already have works too.")
    for index, chat in enumerate(known, 1):
        mark = "x" if chat in groups else " "
        print(f"  [{mark}] {index}. {chat_label(chat, seen)}")
    answer = input("numbers to switch on/off, or ids to add (comma separated, blank = keep): ").strip()
    if not answer:
        return groups
    picked = list(groups)
    for part in (piece.strip() for piece in answer.split(",")):
        if part.isdigit() and 1 <= int(part) <= len(known):
            chat = known[int(part) - 1]
            picked = [c for c in picked if c != chat] if chat in picked else picked + [chat]
            continue
        added, _ = parse_groups(part, gateway.kind)
        picked += [chat for chat in added if chat not in picked]
    return [chat for chat in map(chat_id, picked) if chat]


def _pick_mode(prompt: str, current: str, allow_default: bool = False) -> str | None:
    from .gateways import GROUP_MODES

    labels = ["mention: an @mention of the bot or a reply to it",
              "all: every message from an allowed member, every channel post",
              "read: never answer, only read"]
    if allow_default:
        labels.append("same as the gateway's default")
    print(f"\n{prompt}:")
    for index, label in enumerate(labels, 1):
        mark = "*" if (GROUP_MODES + ("",))[index - 1] == current else " "
        print(f"  {mark}{index}. {label}")
    answer = input(f"{prompt} [1-{len(labels)}, blank = keep]: ").strip()
    if not answer:
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(labels):
        return (GROUP_MODES + ("",))[int(answer) - 1]
    print(f"warn {answer!r} is not on the list; kept {current or 'default'}")
    return None


# ------------------------------------------------------------------ the agent's choices
def _agent_choices(cwd: str) -> dict[str, list[str]]:
    """The models and thinking levels the agent offers, asked once, or empty when it is down."""
    from .setup import agent_catalog

    try:
        catalog = agent_catalog(cwd or str(Path.home()))
    except Exception as exc:
        # an empty list here read as "the agent offers nothing" and hid a broken agent
        print(f"warn could not ask the agent for its models: {exc}; type the value instead")
        return {}
    return {"models": catalog.get("models") or [], "thinking": catalog.get("thinking") or []}


def _ask_field(field: str, gateway: Any, choices: dict[str, list[str]]) -> Any | None:
    """One setting from the menu: the new value, `INHERIT`, or None to keep it."""
    from .setup import choose

    value, source = effective(gateway, field)
    print(f"\n{LABELS[field]}: {value} ({source})")
    options: list[str] = []
    if field == "thinking":
        options = list(choices.get("thinking") or THINKING_LEVELS)
    elif field == "approval":
        options = list(APPROVAL_MODES)
    elif field in ("reasoning", "verbose"):
        options = ["on", "off"]
    elif field == "model":
        options = list(choices.get("models") or [])
    if options:
        shared = load_config()
        section, key = FIELDS[field]
        base = shared.get(section, key, "")
        inherit = f"{INHERIT} ({show(base) if base not in (None, '') else 'agent default'} " \
                  f"from config.toml)"
        options.append(inherit)
        shown = value if source == "own" else inherit
        if shown not in options:
            options.insert(0, shown)  # a model the agent no longer lists is still the current one
        picked = choose(LABELS[field], options, shown, yes=False)
        if picked == shown:
            return None
        raw = INHERIT if picked == inherit else picked
    else:
        raw = input(f"{LABELS[field]} (blank = keep, {INHERIT} = use config.toml): ").strip()
        if not raw:
            return None
    try:
        return parse_value(field, raw, tuple(choices.get("thinking") or THINKING_LEVELS))
    except UpdateError as exc:
        print(f"warn {exc}; kept {value}")
        return None


# ------------------------------------------------------------------ command
def _select(args: argparse.Namespace, gateways: Any, service: Any, items: list[Any]) -> Any | None:
    name = (getattr(args, "name", "") or "").strip().lower()
    if not name:
        if len(items) == 1:
            return items[0]
        if args.yes:
            print("FAIL name the gateway to update; --yes never picks one for you", file=sys.stderr)
            return None
        from .gateway_cli import _choose

        print("gateways:")
        picked = _choose("which", [f"{item.name}   ({item.kind}, {service.state(item.name)})"
                                   for item in items])
        if picked is None:
            print("FAIL that is not one of the gateways listed", file=sys.stderr)
            return None
        return items[picked]
    found = next((item for item in items if item.name == name), None)
    if found is None:
        known = ", ".join(item.name for item in items)
        print(f"FAIL no gateway named {name!r} (configured: {known})", file=sys.stderr)
    return found


def _flag_changes(args: argparse.Namespace, gateway: Any) -> tuple[Any, list[str]] | None:
    """Apply every flag given; (updated gateway, what changed) or None after an error."""
    settings = {section: dict(values) for section, values in gateway.settings.items()}
    groups, modes, mode = list(gateway.groups), dict(gateway.group_modes), gateway.group_mode
    changed: list[str] = []
    try:
        for field in FIELDS:
            raw = getattr(args, field, "") or ""
            if raw:
                value = parse_value(field, raw)
                apply_value(settings, field, value)
                changed.append(field)
    except UpdateError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return None
    if getattr(args, "groups", None) is not None:
        groups, modes = parse_groups(args.groups, gateway.kind)
        changed.append("groups")
    if getattr(args, "group_mode", ""):
        mode = args.group_mode
        changed.append("group mode")
    modes = {chat: value for chat, value in modes.items() if chat in groups}
    return replace(gateway, settings=settings, groups=groups, group_modes=modes,
                   group_mode=mode), changed


def _menu(gateway: Any) -> tuple[Any, list[str]] | None:
    """Change fields one at a time until "save"; None when it is left without saving."""
    from .gateways import seen_chats

    settings = {section: dict(values) for section, values in gateway.settings.items()}
    current = replace(gateway, settings=settings, groups=list(gateway.groups),
                      group_modes=dict(gateway.group_modes))
    changed: list[str] = []
    choices: dict[str, list[str]] | None = None
    while True:
        seen = seen_chats(current.name)
        listed = ", ".join(
            chat_label(chat, seen) + (f" [{current.group_modes[chat]}]"
                                      if chat in current.group_modes else "")
            for chat in current.groups) or "none"
        rows = [("groups and channels", listed),
                ("group mode", current.group_mode)]
        rows += [(LABELS[field], "{} ({})".format(*effective(current, field))) for field in FIELDS]
        print(f"\n{current.name} ({current.kind}):")
        for index, (label, value) in enumerate(rows, 1):
            print(f"  {index}. {label}: {value}")
        print(f"  {len(rows) + 1}. save" + (f" ({', '.join(changed)})" if changed else ""))
        print(f"  {len(rows) + 2}. quit without saving")
        answer = input(f"change [1-{len(rows) + 2}, blank = save]: ").strip()
        if not answer:
            return current, changed
        if not answer.isdigit() or not 1 <= int(answer) <= len(rows) + 2:
            print(f"warn {answer!r} is not on the list")
            continue
        pick = int(answer)
        if pick == len(rows) + 1:
            return current, changed
        if pick == len(rows) + 2:
            return None
        if pick == 1:
            before = list(current.groups)
            groups = _pick_groups(current, before)
            if groups == before:
                continue
            modes = {chat: value for chat, value in current.group_modes.items() if chat in groups}
            current = replace(current, groups=groups, group_modes=modes)
            changed.append("groups")
            for chat in [chat for chat in groups if chat not in before]:
                own = _pick_mode(f"mode in {chat_label(chat, seen)}", "", allow_default=True)
                if own:
                    current = replace(current, group_modes={**current.group_modes, chat: own})
            continue
        if pick == 2:
            mode = _pick_mode("default mode in groups and channels", current.group_mode)
            if mode and mode != current.group_mode:
                current = replace(current, group_mode=mode)
                changed.append("group mode")
            continue
        field = list(FIELDS)[pick - 3]
        if field in ("model", "thinking") and choices is None:
            print("--   asking the agent what it offers…")
            choices = _agent_choices(effective(current, "cwd")[0])
        value = _ask_field(field, current, choices or {})
        if value is None:
            continue
        settings = {section: dict(values) for section, values in current.settings.items()}
        apply_value(settings, field, value)
        current = replace(current, settings=settings)
        changed.append(field)


def _clear_chat_choices(gateway: Any, fields: list[str]) -> list[str]:
    """A setting changed here is the new default; a value stored for one direct chat (from
    /model, /thinking, /reasoning, /verbose, or an overrides.json toggle) would hide it."""
    from .gateways import state_dir
    from .store import SessionStore

    cleared: list[str] = []
    home = state_dir(gateway.name)
    overrides = home / "overrides.json"
    for field in fields:
        if field not in FIELDS:
            continue
        section, key = FIELDS[field]
        if overrides.is_file() and clear_override(section, key, overrides):
            cleared.append(f"{field} (overrides.json)")
    if field_names := [CHAT_PREF[field] for field in fields if field in CHAT_PREF]:
        store_path = (gateway.settings.get("session") or {}).get("store") or home / "sessions.db"
        if Path(str(store_path)).expanduser().is_file():
            store = SessionStore(str(store_path))
            try:
                for key, prefs in store.all_prefs().items():
                    chat = key.split(":", 1)[-1]
                    if chat.startswith("-") or chat.endswith("@g.us"):
                        continue  # a group's own choice stays: it was made for that group
                    for name in field_names:
                        if name in prefs:
                            store.delete_pref(key, name)
                            cleared.append(f"{name} ({key})")
            finally:
                store.close()
    return cleared


def run(args: argparse.Namespace, gateways: Any, service: Any) -> int:
    try:
        items = gateways.load(load_config(args.config))
    except (gateways.GatewayError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    if not items:
        print("no gateway configured. Run `ompwire gateway start` first.")
        return 1
    gateway = _select(args, gateways, service, items)
    if gateway is None:
        return 1

    flagged = any(getattr(args, field, "") for field in FIELDS) or getattr(args, "groups", None) \
        is not None or getattr(args, "group_mode", "")
    if flagged:
        result = _flag_changes(args, gateway)
    elif args.yes or not sys.stdin.isatty():
        print("FAIL nothing to change: pass a flag (ompwire gateway update --help), or run it in a "
              "terminal for the menu", file=sys.stderr)
        return 1
    else:
        result = _menu(gateway)
    if result is None:
        if not flagged:
            print("--   nothing saved")
        return 1 if flagged else 0
    updated, changed = result
    if not changed:
        print(f"--   {gateway.name}: nothing changed")
        return 0

    gateways.save([updated if item.name == updated.name else item for item in items])
    print(f"ok   {updated.name} updated: {', '.join(dict.fromkeys(changed))}")
    for line in _clear_chat_choices(updated, changed):
        print(f"ok   cleared a per-chat choice that would hide it: {line}")
    if service.state(updated.name) != "active":
        print(f"--   {updated.name} is not running; the change applies when it starts")
        return 0
    if args.no_start:
        print(f"--   not restarted (--no-start); `ompwire gateway restart {updated.name}` applies it")
        return 0
    print(f"--   restarting {updated.name} to apply it")
    return service.restart(updated.name)
