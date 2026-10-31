"""`ompwire gateway ...`: start, update, remove, list and control the bots.

`start` asks only what it does not know (platform, profile, credential, allow list, own
settings) and is safe to run again; `update` changes an existing gateway (gateway_update.py);
`remove` asks which gateway and how far. `_run` is what a gateway's systemd unit executes. The
platform details come from `gateways.platform()`, so nothing here is specific to Telegram.
"""

from __future__ import annotations

import argparse
import shutil
import sys

from .config import load_config
from .gateway_update import parse_groups


def _run_gateway(config_path: str | None, name: str, force: bool) -> int:
    from . import gateways

    config = load_config(config_path)
    for warning in config.warnings:
        print(f"warn: {warning}", file=sys.stderr)

    try:
        gateway = gateways.selected(config, name)
    except gateways.GatewayError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    if not gateway.implemented:
        print(f"FAIL the {gateway.kind!r} transport is not implemented yet; "
              f"`ompwire gateway list` shows what can run today", file=sys.stderr)
        return 1
    # before the lock: the old state is only moved while no process holds it
    from .config import migrate_state_dir

    for line in migrate_state_dir():
        print(f"migrated {line}", file=sys.stderr)
    for line in gateways.migrate(state_for=gateway.name):
        print(f"migrated {line}", file=sys.stderr)

    # the agent comes after the gateway: a mistyped gateway name is the question the
    # user just asked, so answer that before complaining about something else
    from .setup import agent_path

    command = str(config.get("acp", "command", "omp"))
    if agent_path(command) is None:
        print(f"FAIL agent not found: {command}. Run `ompwire setup` first.", file=sys.stderr)
        return 1
    # this gateway's own config: its settings over config.toml, its own sessions, overrides, log
    config = gateways.config_for(gateway, config_path)
    if not gateways.ready(gateway):
        print(f"FAIL gateway {gateway.name!r} is not connected to {gateway.kind}. Run "
              f"`ompwire gateway start {gateway.name}`"
              + (f" or set {gateway.token_var()}." if gateway.kind == "telegram" else "."),
              file=sys.stderr)
        return 1
    from . import lock

    if force:
        print("warn: --force skips the single-instance lock; two pollers make Telegram "
              "answer 409 Conflict and drop updates", file=sys.stderr)
        handle = None
    else:
        try:
            handle = lock.acquire(lock.lock_path(gateway.name))
        except lock.AlreadyRunning as busy:
            print(f"FAIL gateway {gateway.name!r} is already running (pid {busy.pid}).\n"
                  f"     stop it, or use `ompwire gateway restart {gateway.name}`;\n"
                  f"     `ompwire gateway list` shows it. --force starts anyway, at the "
                  f"risk of Telegram 409 Conflict.", file=sys.stderr)
            return 1

    if gateway.kind == "whatsapp":
        from .whatsapp import serve
    else:
        from .telegram import serve

    try:
        return serve(config)
    finally:
        lock.release(handle, lock.lock_path(gateway.name))


def _ask_secret_line(prompt: str) -> str:
    """A gateway credential, read hidden when a terminal is available."""
    from .setup import _ask_secret, normalise_token

    return normalise_token(_ask_secret(prompt, yes=False))


def cmd_gateway(args: argparse.Namespace) -> int:
    from . import gateways, service

    action = getattr(args, "gateway_action", "") or ""
    if not action:
        args.gateway_help()
        return 0
    if action != "_run":
        # an older install's definitions and credentials, so list/start/remove see its bots;
        # `_run` migrates for itself, state included, before it takes its lock
        for line in gateways.migrate():
            print(f"migrated {line}")
    if getattr(args, "name_flag", ""):
        args.name = args.name_flag

    if action == "types":
        for index, kind in enumerate(gateways.KINDS, 1):
            mark = "implemented" if kind in gateways.IMPLEMENTED else "not implemented yet"
            print(f"  {index}. {kind:<9} {mark}")
        return 0

    if action == "list":
        items = gateways.load(load_config(args.config))
        if not items:
            print("no gateway configured. Run `ompwire gateway start`.")
            return 0
        width = max(len(item.name) for item in items)
        for item in items:
            if not gateways.ready(item):
                identity = "not linked" if item.kind == "whatsapp" else "no credential"
            else:
                identity = gateways.identity(item) or "credential rejected"
            allowed = ", ".join(item.allowed) or "none"
            print(f"{item.name.ljust(width)}  {item.kind:<9} {identity:<24} "
                  f"users {allowed:<12} {service.state(item.name)}")
        return 0


    if action in ("remove", "rm"):
        try:
            return _gateway_remove(args, gateways, service)
        except EOFError:
            print("\nFAIL input ended before the removal was confirmed; nothing was removed. "
                  "Without a terminal, pass the name and --yes.", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\n--   cancelled; nothing was removed")
            return 130

    if action == "start":
        return _gateway_start(args, gateways, service)
    if action == "update":
        from . import gateway_update

        try:
            return gateway_update.run(args, gateways, service)
        except EOFError:
            print("\nFAIL input ended before the change was saved; nothing was written. "
                  "Without a terminal, pass the name and flags.", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\n--   cancelled; nothing was written")
            return 130
    if action == "status":
        return _gateway_status(args, gateways, service)
    if action == "logs":
        try:
            item = gateways.selected(load_config(args.config), getattr(args, "name", "") or "")
        except gateways.GatewayError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1
        return _gateway_logs(item, service)
    try:
        gateway = gateways.selected(load_config(args.config), getattr(args, "name", "") or "")
    except gateways.GatewayError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1

    if action in ("stop", "restart", "_run") and not gateway.implemented:
        print(f"FAIL the {gateway.kind!r} transport is not implemented yet; "
              f"`ompwire gateway types` shows what can run today", file=sys.stderr)
        return 1
    if action == "_run":
        # what the unit calls. Not a user-facing mode: systemd needs a foreground process,
        # the CLI does not offer one.
        return _run_gateway(args.config, gateway.name, args.force)
    return getattr(service, action)(gateway.name)


def _gateway_status(args: argparse.Namespace, gateways, service) -> int:
    """One line per gateway: what runs it, and where its logs are."""
    try:
        items = gateways.load(load_config(args.config))
    except gateways.GatewayError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    wanted = getattr(args, "name", "") or ""
    if wanted:
        items = [item for item in items if item.name == wanted]
        if not items:
            print(f"FAIL no gateway named {wanted!r}", file=sys.stderr)
            return 1
    if not items:
        print("no gateway configured. Run `ompwire gateway start`.")
        return 0
    for item in items:
        state = service.state(item.name)
        unit = service.unit_name(item.name)
        print(f"{item.name}  {item.kind}  {state}")
        print(f"  unit   {service.unit_path(item.name)}")
        print(f"  logs   journalctl --user -u {unit} -f")
        if state in ("inactive", "failed", "no unit"):
            print(f"  start  ompwire gateway start {item.name}")
    return 0


def _gateway_logs(item, service) -> int:
    import subprocess as _subprocess

    unit = service.unit_name(item.name)
    result = _subprocess.run(["journalctl", "--user", "-u", unit, "-n", "40", "--no-pager"],
                             capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"FAIL journalctl: {(result.stderr or result.stdout).strip()}", file=sys.stderr)
        return 1
    print(result.stdout.rstrip())
    return 0


def _allowed_ids(args: argparse.Namespace) -> list[str] | None:
    """`--allowed` as ids (digits only), or None when the flag was not given."""
    from .gateways import user_id

    raw = (getattr(args, "allowed", "") or "").split(",")
    if not "".join(raw).strip():
        return None
    return [user for user in map(user_id, raw) if user]


def _gateway_remove(args: argparse.Namespace, gateways, service) -> int:
    """Take one gateway away: its unit, its definition, its credential, and (if asked) its state.

    It asks which gateway when no name is given, refuses while a schedule still reports to it,
    and wants the name typed back before deleting anything.
    """
    import shutil as _shutil

    from . import schedules

    try:
        items = gateways.load(load_config(args.config))
    except (gateways.GatewayError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    if not items:
        print("no gateway configured; nothing to remove")
        return 0

    name = (getattr(args, "name", "") or "").strip().lower()
    if not name:
        if args.yes:
            print("FAIL name the gateway to remove; --yes never picks one for you", file=sys.stderr)
            return 1
        print("gateways:")
        picked = _choose("which", [f"{item.name}   ({item.kind}, {service.state(item.name)})"
                                   for item in items])
        if picked is None:
            print("FAIL that is not one of the gateways listed", file=sys.stderr)
            return 1
        name = items[picked].name
    item = next((entry for entry in items if entry.name == name), None)
    if item is None:
        known = ", ".join(entry.name for entry in items)
        print(f"FAIL no gateway named {name!r} (configured: {known})", file=sys.stderr)
        return 1

    try:
        reporting = sorted(entry.name for entry in schedules.load().values()
                           if entry.gateway == name)
    except schedules.ScheduleError:
        reporting = []
    if reporting and not args.force:
        print(f"FAIL schedules still report to {name!r}: {', '.join(reporting)}. Remove them "
              f"(`ompwire schedule remove <name>`) or point them elsewhere first; --force removes "
              f"the gateway anyway and their next run fails.", file=sys.stderr)
        return 1

    # the gateway's folder holds its definition and credential too; state is everything else
    home = gateways.state_dir(name)
    own_state = home.is_dir() and any(path.name not in ("gateway.toml", ".env")
                                      for path in home.iterdir())
    keep_state = True
    if args.yes:
        keep_state = not getattr(args, "purge", False)
    else:
        print(f"remove {name!r}:")
        choice = _choose("how", [
            "remove the gateway: unit, definition and credential; keep its sessions and log",
            "full delete: also its sessions, chat toggles and log" + (
                "" if own_state else " (none of its own to delete)"),
        ])
        if choice is None:
            print("FAIL that is not one of the choices listed", file=sys.stderr)
            return 1
        keep_state = choice == 0
        typed = input(f"type {name} to confirm: ").strip()
        if typed != name:
            print("--   not confirmed; nothing was removed")
            return 1

    service.remove(name, quiet=True)
    gateways.remove(name)
    print(f"ok   removed gateway {name!r} and its unit")
    if gateways.unset_token(item):
        print(f"ok   credential removed from .env ({item.token_var()})")
    if own_state and not keep_state:
        _shutil.rmtree(home)
        print(f"ok   removed its sessions and log: {home}")
    elif own_state:
        print(f"--   kept its sessions and log: {home}")
    if reporting:
        print(f"warn schedules that still report to {name!r} will fail: {', '.join(reporting)}")
    return 0


def _choose(prompt: str, options: list[str], default: int = 1) -> int | None:
    """A numbered menu; the chosen index, or None for an answer that is not on it."""
    for index, label in enumerate(options, 1):
        print(f"  {index}. {label}")
    answer = input(f"{prompt} [1-{len(options)}, enter for {default}]: ").strip() or str(default)
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return int(answer) - 1
    return None


def _start_platform(args: argparse.Namespace, gateways) -> str | None:
    """The platform to start, from --platform or a menu of the implemented ones."""
    kind = (getattr(args, "platform", "") or "").strip().lower()
    if not kind:
        if args.yes:
            kind = gateways.IMPLEMENTED[0]
        else:
            print("platform:")
            labels = [kind if kind in gateways.IMPLEMENTED else f"{kind}   (not implemented yet)"
                      for kind in gateways.KINDS]
            picked = _choose("platform", labels)
            if picked is None:
                print("FAIL that is not one of the platforms listed", file=sys.stderr)
                return None
            kind = gateways.KINDS[picked]
    if kind not in gateways.KINDS:
        print(f"FAIL unknown platform {kind!r}; known: {', '.join(gateways.KINDS)}",
              file=sys.stderr)
        return None
    if kind not in gateways.IMPLEMENTED:
        print(f"FAIL the {kind} transport is not implemented yet, so nothing is written. "
              f"`ompwire gateway types` shows what can run today.", file=sys.stderr)
        return None
    return kind


def _start_profile(args: argparse.Namespace, gateways, kind: str, items) -> tuple[str, bool] | None:
    """Which gateway of this platform to start: (name, is_new). A menu when several exist."""
    named = (getattr(args, "name", "") or "").strip().lower()
    mine = [item for item in items if item.kind == kind]
    if named:
        clash = next((item for item in items if item.name == named and item.kind != kind), None)
        if clash is not None:
            print(f"FAIL {named!r} is a {clash.kind} gateway, not {kind}", file=sys.stderr)
            return None
        try:
            return gateways.validate_name(named), all(item.name != named for item in items)
        except gateways.GatewayError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return None
    if not mine:
        # the first gateway of a platform gets a name too: it names the unit and the logs, and
        # renaming later means a new gateway, so the question belongs here and not after
        name = _new_name(args, gateways, kind, items, ask=True)
        return (name, True) if name else None
    if args.yes:
        if len(mine) == 1:
            return mine[0].name, False
        print(f"FAIL several {kind} gateways exist ({', '.join(item.name for item in mine)}); "
              f"name one", file=sys.stderr)
        return None
    labels = [f"{item.name}   ({_identity_line(gateways, item)})" for item in mine]
    labels.append("add a new profile")
    print(f"{kind} gateways:")
    picked = _choose("which", labels)
    if picked is None:
        print("FAIL that is not one of the gateways listed", file=sys.stderr)
        return None
    if picked < len(mine):
        return mine[picked].name, False
    name = _new_name(args, gateways, kind, items, ask=True)
    return (name, True) if name else None


def _identity_line(gateways, item) -> str:
    credential = gateways.token(item)
    return f"credential {gateways.mask(credential)}" if credential else "no credential"


def _new_name(args, gateways, kind: str, items, ask: bool = False) -> str | None:
    taken = {item.name for item in items}
    suggestion = kind
    counter = 2
    while suggestion in taken:
        suggestion = f"{kind}-{counter}"
        counter += 1
    if not ask or args.yes:
        return suggestion
    while True:
        answer = input(f"name for the new {kind} gateway, used for its service and logs "
                       f"[{suggestion}]: ").strip().lower()
        name = answer or suggestion
        if name in taken:
            print(f"     {name!r} already exists; pick another name")
            continue
        try:
            return gateways.validate_name(name)
        except gateways.GatewayError as exc:
            print(f"     {exc}")


def _start_credential(args: argparse.Namespace, gateways, gateway, is_new: bool) -> str | None:
    """A verified credential for this gateway, asking for one when there is none or it is
    rejected, or when the person chose to replace it."""
    spec = gateways.platform(gateway.kind)
    given = _given_credential(args)
    if given is None:
        return None
    current = "" if is_new else gateways.token(gateway)
    if given:
        candidate = given
    elif current and not is_new and not args.yes and _wants_new_credential(current, spec):
        candidate = ""
    else:
        candidate = current
    shown_steps = False
    for attempt in range(3):
        if not candidate:
            if args.yes:
                print(f"FAIL no {spec.credential} given; pass --token-stdin", file=sys.stderr)
                return None
            if not shown_steps:
                # once, before the first question, also when a stored credential was rejected
                _print_steps(f"how to get a {spec.credential}", spec.credential_steps)
                shown_steps = True
            candidate = _ask_secret_line(spec.credential)
            if not candidate:
                print("FAIL no credential given; nothing was written", file=sys.stderr)
                return None
        identity = gateways.verify(gateway.kind, candidate)
        if identity:
            print(f"ok   credential valid: @{identity}")
            return candidate
        print(f"warn {gateway.kind} rejected that credential")
        if given or args.yes or attempt == 2:
            print("FAIL no valid credential; nothing was written", file=sys.stderr)
            return None
        candidate = ""
    return None


def _start_link(args: argparse.Namespace, gateways, service, gateway, is_new: bool) -> str | None:
    """A linked WhatsApp device for this gateway: kept when WhatsApp still knows it, linked by QR
    (or a pairing code) when there is none, the phone removed it, or --relink asks for a new one.
    Returns the number."""
    import asyncio

    from .wa_client import check_link, link, linked_number, session_path, unlink

    path = session_path(gateway.name)
    current = linked_number(path)
    if current and service.state(gateway.name) == "active":
        # one device, one connection: the running gateway must let go before anything connects
        service.stop(gateway.name, disable=False)
    if current and not getattr(args, "relink", False):
        # the file says linked; only WhatsApp knows whether the phone still lists the device
        print(f"--   checking the link to +{current} with WhatsApp…")
        try:
            alive = asyncio.run(check_link(path))
        except TimeoutError:
            print("FAIL WhatsApp did not answer within 30s; check the network and run again",
                  file=sys.stderr)
            return None
        if alive:
            print(f"ok   linked as +{alive}")
            return alive
        print(f"--   the phone no longer lists this device (+{current}); linking again")
    if args.yes and not getattr(args, "pair_phone", ""):
        print("FAIL a WhatsApp link needs the phone in hand; run without --yes, or pass "
              "--pair-phone <number>", file=sys.stderr)
        return None
    if current:
        unlink(path)
        if getattr(args, "relink", False):
            print(f"--   forgot the link to +{current} on this machine; remove it on the phone "
                  f"too (Linked devices)")
    spec = gateways.platform("whatsapp")
    _print_steps("link the bot's WhatsApp number", spec.credential_steps)
    phone = gateways.user_id(getattr(args, "pair_phone", "") or "")

    import tempfile
    from pathlib import Path

    # Linking happens in a scratch directory and only a finished link is moved into the
    # gateway's folder. whatsmeow holds the thread in Go while it waits for the scan, so a closed
    # terminal or a kill cannot be relied on to run any cleanup; this way there is none to run.
    scratch = Path(tempfile.mkdtemp(prefix="ompwire-link-"))
    draft = scratch / path.name
    qr_png = scratch / "link-qr.png"

    def show_qr(data: str) -> None:
        import segno

        code = segno.make_qr(data)
        code.terminal(compact=True)
        code.save(str(qr_png), scale=8, border=4)
        print(f"     scan within ~20s; a new one follows if it expires (also saved: {qr_png})")

    def show_code(code: str) -> None:
        print(f"\n     on +{phone}: tap the WhatsApp notification, or Linked devices > Link with "
              f"phone number instead, and type:  {code}\n")

    import asyncio
    import shutil as _shutil

    try:
        number = asyncio.run(link(draft, on_qr=show_qr, phone=phone, on_code=show_code))
        path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            part = draft.with_name(draft.name + suffix)
            if part.exists():
                _shutil.move(str(part), str(path.with_name(path.name + suffix)))
    except TimeoutError:
        print("FAIL no device was linked in time; nothing was written", file=sys.stderr)
        return None
    finally:
        _shutil.rmtree(scratch, ignore_errors=True)
    print(f"ok   linked as +{number}")
    return number


def _start_groups(args: argparse.Namespace, gateway, is_new: bool
                  ) -> tuple[list[str], str, dict[str, str]]:
    """The groups (and, on Telegram, channels) a gateway listens in, and when it answers there.

    `--groups` wins: ids separated by commas, each optionally `id:mode` for its own mode.
    Otherwise a new gateway, or one with none yet, is asked: WhatsApp lists the groups its
    number is in, Telegram takes ids (the bot sends one to its owner when it is added to a chat).
    An existing gateway keeps what it has. Without a terminal nothing is asked.
    """
    from .gateways import GROUP_MODES, chat_id

    groups = list(gateway.groups)
    modes = dict(gateway.group_modes)
    given = getattr(args, "groups", None)
    mode = (getattr(args, "group_mode", "") or gateway.group_mode or "mention").lower()
    if given is not None:
        groups, modes = parse_groups(given, gateway.kind)
    elif not args.yes and (is_new or not groups) and sys.stdin.isatty():
        if gateway.kind == "whatsapp":
            from . import service

            if service.state(gateway.name) == "active":
                # one linked device, one connection: reading its groups would knock it off
                service.stop(gateway.name, disable=False)
            groups = _pick_groups(gateway, groups)
        else:
            groups, modes = _ask_telegram_chats(groups)
        if groups and not getattr(args, "group_mode", ""):
            print("\nin those chats, when should it answer?")
            labels = ["mention: an @mention of the bot or a reply to it (groups)",
                      "all: every message from an allowed member, every channel post",
                      "read: never, it only reads"]
            picked = _choose("mode", labels, default=GROUP_MODES.index(mode) + 1
                             if mode in GROUP_MODES else 1)
            mode = GROUP_MODES[picked] if picked is not None else "mention"
    if mode not in GROUP_MODES:
        print(f"warn unknown group mode {mode!r}; using mention")
        mode = "mention"
    groups = [group for group in map(chat_id, groups) if group]
    return groups, mode, {chat: value for chat, value in modes.items() if chat in groups}


def _ask_telegram_chats(current: list[str]) -> tuple[list[str], dict[str, str]]:
    """Telegram has no call that lists a bot's chats, so the ids are typed in."""
    print("\ngroups and channels (optional): the bot answers only in chats listed here.\n"
          "  1. Easiest: leave this blank, add the bot to the group or channel once it runs, then\n"
          "     `ompwire gateway update` lists that chat by name. The bot also sends you its id.\n"
          "  2. In a group, to see an @mention of it, make the bot an admin, or turn privacy\n"
          "     mode off in @BotFather (/setprivacy) and add it again. A reply to the bot and\n"
          "     /command@the_bot reach it either way.\n"
          "  3. In a channel the bot must be an admin. A channel has no sender: anyone who can\n"
          "     post there can talk to the agent, so list only channels you control.")
    answer = input("chat ids (comma separated, id:mode for its own mode, blank = "
                   f"{'keep' if current else 'none'}): ").strip()
    if not answer:
        return current, {}
    return parse_groups(answer, "telegram")


def _pick_groups(gateway, current: list[str]) -> list[str]:
    """Numbers from the list of groups the linked number is in; blank keeps it out of groups."""
    import asyncio

    from .wa_client import joined_groups, session_path

    print("\ngroups: the bot answers only in groups you list here (and only allowed numbers)")
    try:
        found = asyncio.run(joined_groups(session_path(gateway.name)))
    except Exception as exc:
        print(f"warn could not read its groups ({exc}); add them later with --groups")
        return current
    if not found:
        print("--   this number is in no group yet; add it to one, then run gateway start again")
        return current
    for index, (jid, name) in enumerate(found, 1):
        mark = " (listed)" if jid in current else ""
        print(f"  {index}. {name}  {jid}{mark}")
    answer = input("which groups (numbers, comma separated, blank = none): ").strip()
    picked = [found[int(part) - 1][0] for part in answer.replace(" ", "").split(",")
              if part.isdigit() and 1 <= int(part) <= len(found)]
    return picked


def _print_steps(title: str, steps: tuple[str, ...]) -> None:
    """Numbered how-to before a question, wrapped to the terminal."""
    if not steps:
        return
    import textwrap

    width = min(shutil.get_terminal_size((88, 20)).columns, 88)
    print(f"\n{title}:")
    for index, step in enumerate(steps, 1):
        print(textwrap.fill(step, width=width, initial_indent=f"  {index}. ",
                            subsequent_indent="     "))
    print()


def _given_credential(args: argparse.Namespace) -> str | None:
    """--token or --token-stdin, "" when neither, None when stdin was asked for but empty."""
    from .setup import normalise_token

    token = normalise_token(getattr(args, "token", "") or "")
    if not token and getattr(args, "token_stdin", False):
        token = normalise_token(sys.stdin.readline())
        if not token:
            print("FAIL --token-stdin was given but stdin was empty", file=sys.stderr)
            return None
    return token


def _wants_new_credential(current: str, spec) -> bool:
    from .gateways import mask

    print(f"current {spec.credential}: {mask(current)}")
    answer = input("enter a new one? [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def _start_allowed(args: argparse.Namespace, gateway, spec) -> list[str]:
    """The allow list: --allowed wins, otherwise the current one, asked for when empty."""
    given = _allowed_ids(args)
    if given is not None:
        return given
    if gateway.allowed or args.yes:
        return list(gateway.allowed)
    _print_steps(f"who may talk to it: {spec.user_ids}", spec.user_id_steps)
    from .gateways import user_id

    answer = input(f"allowed {spec.user_ids} (comma separated, blank = nobody): ").strip()
    return [user for user in map(user_id, answer.split(",")) if user]


# Settings a gateway is asked about on creation, as (section, key, question). Anything else can
# still be set by hand in its [gateway.<name>.<section>] table in gateways.toml.
_OWN_QUESTIONS = (
    ("agent", "model", "model (provider/id)"),
    ("agent", "cwd", "working directory"),
    ("agent", "approval_mode", "approval mode (write, yolo)"),
)


def _start_settings(args: argparse.Namespace, gateway, is_new: bool) -> dict[str, dict]:
    """This gateway's own settings over config.toml. Flags set them; a new profile is asked
    whether to use the shared config; an existing one keeps what it has."""
    from .gateway_update import UpdateError, apply_value, parse_value

    settings = {section: dict(values) for section, values in gateway.settings.items()}
    flags = {field: getattr(args, field, "") or "" for field in ("model", "cwd", "approval")}
    for field, raw in flags.items():
        if raw:
            try:
                apply_value(settings, field, parse_value(field, raw))
            except UpdateError as exc:
                print(f"warn {exc}; not set")
    if not is_new or args.yes or any(flags.values()):
        return settings
    shared = load_config()
    print("\nthis gateway can share config.toml with the others, or use its own model, working "
          "directory and approval mode. Sessions, chat toggles and the log are its own either way.")
    if _choose("settings", ["share config.toml", "set its own"]) != 1:
        return settings
    for section, key, question in _OWN_QUESTIONS:
        current = str(shared.get(section, key, "") or "")
        answer = input(f"{question} [{current or 'agent default'}]: ").strip()
        if answer:
            settings.setdefault(section, {})[key] = answer
    return settings


def _gateway_start(args: argparse.Namespace, gateways, service) -> int:
    """One command for the first start, a token reset and a plain restart.

    It asks only what it does not know: the platform, which profile when there are several, a
    credential when there is none or the platform rejects it, and the allow list when empty.
    Nothing is written until the credential is verified. A running gateway is stopped before
    its credential changes, so the old token is never polling next to the new one.
    """
    try:
        return _gateway_start_steps(args, gateways, service)
    except EOFError:
        print("\nFAIL input ended before every question was answered. Without a terminal, "
              "pass --yes with --platform, --token-stdin and --allowed.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n--   cancelled; nothing was written")
        return 130


def _gateway_start_steps(args: argparse.Namespace, gateways, service) -> int:
    config = load_config(args.config)
    try:
        items = gateways.load(config)
    except (gateways.GatewayError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1

    named = (getattr(args, "name", "") or "").strip().lower()
    existing = next((item for item in items if item.name == named), None) if named else None
    if existing is not None and not getattr(args, "platform", ""):
        args.platform = existing.kind
    kind = _start_platform(args, gateways)
    if kind is None:
        return 1
    profile = _start_profile(args, gateways, kind, items)
    if profile is None:
        return 1
    name, is_new = profile
    gateway = next((item for item in items if item.name == name), None) or gateways.Gateway(
        name=name, kind=kind)
    spec = gateways.platform(kind)

    if kind == "whatsapp":
        linked = _start_link(args, gateways, service, gateway, is_new)
        if linked is None:
            return 1
        credential = ""
    else:
        credential = _start_credential(args, gateways, gateway, is_new)
        if credential is None:
            return 1
    allowed = _start_allowed(args, gateway, spec)
    groups, group_mode, group_modes = _start_groups(args, gateway, is_new)
    settings = _start_settings(args, gateway, is_new)

    changed_credential = kind != "whatsapp" and credential != ("" if is_new else gateways.token(gateway))
    if not is_new and changed_credential and service.state(name) == "active":
        # one poller per credential: the old token must stop before the new one is stored
        service.stop(name, disable=False)

    # home is where schedules report; it follows the allow list only when that list changed
    home = gateway.home if allowed == gateway.allowed else (allowed[0] if allowed else None)
    updated = gateways.Gateway(name=name, kind=kind, enabled=gateway.enabled, allowed=allowed,
                               home=home, groups=groups, group_mode=group_mode,
                               group_modes=group_modes, settings=settings)
    gateways.save([updated if item.name == name else item for item in items]
                  + ([updated] if is_new else []))
    if changed_credential:
        env_path = gateways.set_token(updated, credential)
        print(f"ok   credential stored in {env_path} (0600)")
    print(f"ok   gateway {'added' if is_new else 'updated'}: {name} ({kind})")
    if allowed:
        print(f"ok   allowed {spec.user_ids}: {', '.join(allowed)}")
    else:
        print("warn no allowed users: nobody can talk to this gateway yet")
    own = "".join(f" ({chat}: {mode})" for chat, mode in group_modes.items())
    print(f"ok   groups and channels: {', '.join(groups) or 'none'} · mode {group_mode}{own}")
    own = ", ".join(f"{key}={value}" for values in settings.values() for key, value in values.items())
    print(f"ok   settings: {own or 'shared config.toml'} · state in {gateways.state_dir(name)}")
    if is_new and not args.yes:
        from . import soul

        model = str((settings.get("agent") or {}).get("model") or config.get("agent", "model", "")
                    or "")
        soul.wizard(gateways.state_dir(name), model)

    if args.no_start:
        print(f"--   not started (--no-start); `ompwire gateway start {name}` starts it")
        return 0
    if service.state(name) == "active":
        return service.restart(name)
    return service.start(name, force=args.force)
