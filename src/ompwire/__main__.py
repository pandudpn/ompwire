"""Entry point: `ompwire <command>`.

`ompwire` on its own prints the help; `ompwire chat` starts an interactive session.
Every command carries examples, because the help is the first thing a new machine
shows and it should answer the question without a README open.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

from . import __version__
from .acp import AcpClient, AcpError
from .cli import run_chat
from .config import config_search_paths, load_config
from .store import SessionStore


def _chat_key() -> str:
    return f"cli:{os.getcwd()}"


def cmd_chat(args: argparse.Namespace) -> int:
    from . import gateways

    config = load_config(args.config)
    for warning in config.warnings:
        print(f"warn: {warning}", file=sys.stderr)
    # `ompwire chat` used the shared store; its rows are copied to its own the first time
    gateways.migrate()
    return run_chat(config, key=args.key or _chat_key(), query=args.query, cwd=args.cwd)


def cmd_sessions(args: argparse.Namespace) -> int:
    from . import gateways

    config = load_config(args.config)
    printed = 0
    for owner, path in gateways.stores(config):
        if not path.is_file():
            continue
        store = SessionStore(str(path))
        for row in store.all():
            age = int(time.time() - row.updated_at)
            print(f"{row.session_id[:8]}  {age:>7}s ago  {row.cwd}  [{owner} {row.key}]")
            printed += 1
        store.close()
    if not printed:
        print("no sessions recorded")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(f"config file: {config.path or '(none — defaults + env)'}")
    for candidate in config_search_paths():
        print(f"  search path: {candidate}")
    for section, values in config.data.items():
        if not isinstance(values, dict):
            continue
        print(f"\n[{section}]")
        for key, value in values.items():
            redacted = any(token in key for token in ("token", "api_key", "password", "secret"))
            shown = "***" if (redacted and value) else value
            print(f"  {key} = {shown!r}")
    return 0


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
    if not str(config.get("gateway", "credential", "") or ""):
        print(f"FAIL no credential for gateway {gateway.name!r}. "
              f"Run `ompwire gateway start {gateway.name}` or set {gateway.token_var()}.",
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

    from .telegram import serve

    try:
        return serve(config)
    finally:
        lock.release(handle, lock.lock_path(gateway.name))


def _ask_secret_line(prompt: str) -> str:
    """A gateway credential, read hidden when a terminal is available."""
    from .setup import _ask_secret, normalise_token

    return normalise_token(_ask_secret(prompt, yes=False))


def cmd_schedule(args: argparse.Namespace) -> int:
    from . import schedules

    action = getattr(args, "schedule_action", "list") or "list"

    if action == "_run":  # called by the systemd unit, not by a person
        from . import schedule_run

        return schedule_run.main(args.name)

    if action == "list":
        items = schedules.load()
        if not items:
            print("no schedule configured. Run `ompwire schedule add <name> --cron \"0 7 * * *\" "
                  "--prompt ...`.")
            return 0
        width = max(len(name) for name in items)
        for name in sorted(items):
            item = items[name]
            state = schedules.next_elapse(name) or "-"
            print(f"{name.ljust(width)}  {schedules.describe(item):<34} {item.timezone:<13} "
                  f"-> {item.gateway:<12} next {state:<22} {item.last_status or 'never run'}")
        return 0

    if action == "add":
        try:
            item = schedules.add(
                name=args.name,
                cron=args.cron or "",
                calendar=args.on_calendar or "",
                prompt=args.prompt,
                gateway=args.gateway,
                ephemeral=not args.keep_session,
                approval=args.approval,
                timeout=args.timeout,
                timezone=args.timezone,
            )
            if not args.no_start:
                schedules.install(item)
                schedules.enable(item.name)
        except schedules.ScheduleError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1
        print(f"ok   schedule {item.name}: {item.on_calendar} -> gateway {item.gateway}")
        if not args.no_start:
            print(f"     units: {schedules.unit_names(item.name)[1]}")
            print(f"     next:  {schedules.next_elapse(item.name) or 'see systemctl --user list-timers'}")
        return 0

    if action in ("remove", "rm"):
        try:
            schedules.uninstall(args.name)
            item = schedules.remove(args.name)
        except schedules.ScheduleError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1
        print(f"ok   removed schedule {item.name} and its timer")
        return 0

    if action == "run":
        from . import schedule_run

        print(f"running schedule {args.name} now (this runs one turn in the foreground)")
        return schedule_run.main(args.name)

    if action == "show":
        try:
            item = schedules.get(args.name)
        except schedules.ScheduleError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1
        print(f"name      {item.name}")
        print(f"cron      {item.cron or '-'}")
        print(f"calendar  {item.on_calendar}")
        print(f"gateway   {item.gateway}")
        print(f"prompt    {item.prompt}")
        print(f"session   {'ephemeral' if item.ephemeral else 'persisted'}")
        print(f"approval  {item.approval} (unattended)")
        print(f"timeout   {item.timeout:g}s")
        print(f"last      {item.last_status or 'never run'}")
        print(f"next      {schedules.next_elapse(item.name) or '-'}")
        return 0

    print(f"FAIL unknown action {action}", file=sys.stderr)
    return 1


def cmd_gateway(args: argparse.Namespace) -> int:
    from . import gateways, service
    from .setup import bot_identity

    action = getattr(args, "gateway_action", "") or ""
    if not action:
        args.gateway_help()
        return 0
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
            token = gateways.token(item)
            if not token:
                identity = "no credential"
            else:
                username = bot_identity(token)
                identity = f"@{username}" if username else "credential rejected"
            allowed = ", ".join(str(user) for user in item.allowed) or "none"
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


def _allowed_ids(args: argparse.Namespace) -> list[int] | None:
    """`--allowed` as numbers, or None when the flag was not given."""
    raw = (getattr(args, "allowed", "") or "").replace(",", " ").split()
    if not raw:
        return None
    try:
        return [int(part) for part in raw]
    except ValueError:
        return []


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

    home = gateways.state_dir(name)
    # the first gateway keeps the shared state directory; it is never deleted with it
    own_state = home != gateways.state_dir("telegram") and home.is_dir()
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
        return _new_name(args, gateways, kind, items), True
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
        answer = input(f"name for the new {kind} gateway [{suggestion}]: ").strip().lower()
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


def _start_allowed(args: argparse.Namespace, gateway, spec) -> list[int]:
    """The allow list: --allowed wins, otherwise the current one, asked for when empty."""
    given = _allowed_ids(args)
    if given is not None:
        return given
    if gateway.allowed or args.yes:
        return list(gateway.allowed)
    _print_steps(f"who may talk to it: {spec.user_ids}", spec.user_id_steps)
    answer = input(f"allowed {spec.user_ids} (comma separated, blank = nobody): ").strip()
    return [int(part) for part in answer.replace(" ", "").split(",") if part.isdigit()]


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
    settings = {section: dict(values) for section, values in gateway.settings.items()}
    flags = {("agent", "model"): getattr(args, "model", ""),
             ("agent", "cwd"): getattr(args, "cwd", ""),
             ("agent", "approval_mode"): getattr(args, "approval", "")}
    for (section, key), value in flags.items():
        if value:
            settings.setdefault(section, {})[key] = value
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

    credential = _start_credential(args, gateways, gateway, is_new)
    if credential is None:
        return 1
    allowed = _start_allowed(args, gateway, spec)
    settings = _start_settings(args, gateway, is_new)

    changed_credential = credential != ("" if is_new else gateways.token(gateway))
    if not is_new and changed_credential and service.state(name) == "active":
        # one poller per credential: the old token must stop before the new one is stored
        service.stop(name, disable=False)

    # home is where schedules report; it follows the allow list only when that list changed
    home = gateway.home if allowed == gateway.allowed else (allowed[0] if allowed else None)
    updated = gateways.Gateway(name=name, kind=kind, enabled=gateway.enabled, allowed=allowed,
                               home=home, settings=settings)
    gateways.save([updated if item.name == name else item for item in items]
                  + ([updated] if is_new else []))
    if changed_credential:
        env_path = gateways.set_token(updated, credential)
        print(f"ok   credential stored in {env_path} (0600)")
    print(f"ok   gateway {'added' if is_new else 'updated'}: {name} ({kind})")
    if allowed:
        print(f"ok   allowed users: {', '.join(str(user) for user in allowed)}")
    else:
        print("warn no allowed users: nobody can talk to this gateway yet")
    own = ", ".join(f"{key}={value}" for values in settings.values() for key, value in values.items())
    print(f"ok   settings: {own or 'shared config.toml'} · state in {gateways.state_dir(name)}")

    if args.no_start:
        print(f"--   not started (--no-start); `ompwire gateway start {name}` starts it")
        return 0
    if service.state(name) == "active":
        return service.restart(name)
    return service.start(name, force=args.force)


def _doctor_units(agent: str) -> None:
    """Which units would start the gateway and then lose the agent.

    A unit carries the PATH of the shell that generated it. When an agent lives in ~/.local/bin
    and the unit was written before that PATH was recorded, the poller starts, connects, and every
    turn fails with "ACP command not found". The unit on disk is compared against what this
    version would write, so a stale unit is named here instead of being discovered in the chat.
    """
    from pathlib import Path

    # service_path() is what a freshly written unit carries; a unit on disk that lacks it is old
    directory = str(Path(shutil.which(agent) or "").parent)
    for unit in sorted((Path.home() / ".config" / "systemd" / "user").glob("ompwire-*.service")):
        text = unit.read_text(encoding="utf-8", errors="replace")
        if "Environment=PATH=" not in text:
            print(f"--   {unit.name} predates PATH in its unit: "
                  f"`ompwire gateway restart {unit.name.removeprefix('ompwire-').removesuffix('.service')}`")
        elif directory and f":{directory}:" not in f":{text.split('Environment=PATH=', 1)[1].splitlines()[0]}:":
            print(f"--   {unit.name} does not have {directory} on its PATH: "
                  f"restart the gateway to rewrite it")


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    failures = 0

    command = str(config.get("acp", "command", "omp"))
    path = shutil.which(command)
    if path is None:
        print(f"FAIL agent command not found on PATH: {command}")
        return 1
    print(f"ok   agent command: {path}")
    # The config file is not the whole truth: /yolo and /approval are per chat and live in the
    # store, so a doctor that only reads the file reports a gate that is not there.
    from . import gateways

    open_chats: list[str] = []
    for owner, store_file in gateways.stores(config):
        if not store_file.is_file():
            continue
        try:
            probe = SessionStore(str(store_file))
            for chat, prefs in probe.all_prefs().items():
                if str(prefs.get("yolo", "")).lower() in {"1", "true", "on"}:
                    open_chats.append(f"{owner} chat {chat} (yolo on)")
                if str(prefs.get("approval", "")) == "allow":
                    open_chats.append(f"{owner} chat {chat} (approval allow)")
            probe.close()
        except Exception as exc:
            print(f"WARN could not read per-chat preferences in {store_file}: {exc}")
    file_yolo = bool(config.get("agent", "yolo", False))
    if file_yolo or open_chats:
        print("WARN no gate: " + (", ".join(open_chats) if open_chats
                                  else "yolo in the config file")
              + " · the agent can write and run anything inside its cwd")
    else:
        print(f"ok   approvals: {config.get('agent', 'approval_mode', 'write')} "
              f"· a shell command asks in the chat")
    _doctor_units(command)

    for owner, store_file in gateways.stores(config):
        if not store_file.is_file():
            print(f"--   session store {owner}: none yet ({store_file})")
            continue
        try:
            probe = SessionStore(str(store_file))
            probe.close()
            print(f"ok   session store writable: {owner} {store_file}")
        except Exception as exc:
            print(f"FAIL session store {owner}: {exc}")
            failures += 1

    client = AcpClient(
        command=command,
        args=list(config.get("acp", "args") or []),
        cwd=str(config.get("agent", "cwd") or os.path.expanduser("~")),
        startup_timeout=float(config.get("acp", "startup_timeout", 60.0)),
    )
    try:
        info = client.initialize()
        agent = info.get("agentInfo") or {}
        caps = info.get("agentCapabilities") or {}
        print(f"ok   ACP handshake: {agent.get('name')} {agent.get('version')} (protocol {info.get('protocolVersion')})")
        print(f"     capabilities: loadSession={caps.get('loadSession')} sessions={sorted((caps.get('sessionCapabilities') or {}).keys())}")
        session = client.new_session(str(config.get("agent", "cwd") or os.path.expanduser("~")), [])
        session_id = session.get("sessionId")
        options = {opt.get("id"): opt for opt in (session.get("configOptions") or [])}
        print(f"ok   session/new: {str(session_id)[:8]} · config options {sorted(options)}")
        if "model" in options:
            print(f"     model now: {options['model'].get('currentValue')} ({len(options['model'].get('options') or [])} available)")
        if session_id:
            client.close_session(str(session_id))
    except AcpError as exc:
        print(f"FAIL ACP handshake: {exc}")
        failures += 1
    finally:
        client.close()

    if config.get("mcp", "servers"):
        names = [str(server.get("name", "?")) for server in config.get("mcp", "servers", [])]
        print(f"      mcp servers passed to session/new: {', '.join(names)}")

    from . import gateways

    try:
        items = gateways.load(config)
    except gateways.GatewayError as exc:
        print(f"FAIL gateways: {exc}")
        failures += 1
        items = []
    if not items:
        print("--   no gateway configured (run `ompwire gateway start`)")
    from .setup import bot_identity

    for item in items:
        token = gateways.token(item)
        if not token:
            print(f"FAIL gateway {item.name}: no credential "
                  f"(`ompwire gateway start {item.name}` or set {item.token_var()})")
        elif item.kind == "telegram":
            username = bot_identity(token)
            if username:
                print(f"ok   gateway {item.name}: @{username}")
            else:
                print(f"--   gateway {item.name}: Telegram rejected the credential "
                      f"(revoked or rotating; `ompwire gateway start {item.name}`)")
        else:
            print(f"ok   gateway {item.name}: credential set")
        if item.allowed:
            print(f"ok   allowed users for {item.name}: "
                  f"{', '.join(str(user) for user in item.allowed)}")
        else:
            print(f"--   allowed users for {item.name}: none (nobody can talk to this bot)")

    print(f"\n{'FAILED' if failures else 'ready'} · ompwire {__version__}")
    return 1 if failures else 0


def cmd_update(args: argparse.Namespace) -> int:
    from .update import run

    return run(edge=args.edge, check=args.check, force=args.force,
               restart=not args.no_restart)


def cmd_status(args: argparse.Namespace) -> int:
    from .status import run

    return run(args.config)


def cmd_restart(_args: argparse.Namespace) -> int:
    from .update import restart_gateway

    print(f"gateway: {restart_gateway()}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from .setup import run

    return run(yes=args.yes, force=args.force, install_omp=args.install_omp)


def cmd_uninstall(args: argparse.Namespace) -> int:
    from .uninstall import run

    return run(yes=args.yes, keep_config=args.keep_config, force=args.force,
               dry_run=args.dry_run)


def cmd_scrub(args: argparse.Namespace) -> int:
    from .logs import scrub_file

    config = load_config(args.config)
    target = args.file or str(config.get("logging", "file", ""))
    if not target:
        print("no log file configured")
        return 1
    replaced = scrub_file(target)
    print(f"redacted {replaced} token occurrence(s) in {target}")
    return 0


def cmd_help(args: argparse.Namespace) -> int:
    parser = build_parser()
    topic = getattr(args, "topic", None)
    if not topic:
        parser.print_help()
        return 0
    subparsers = next(action for action in parser._actions
                      if isinstance(action, argparse._SubParsersAction))
    chosen = subparsers.choices.get(topic)
    if chosen is None:
        print(f"unknown command: {topic}\n", file=sys.stderr)
        parser.print_help()
        return 2
    chosen.print_help()
    return 0


def cmd_version(_args: argparse.Namespace) -> int:
    from .update import current_commit, current_version, install_mode

    commit = current_commit()
    print(f"ompwire {current_version()}" + (f" ({commit})" if commit else ""))
    print(f"installer: {install_mode()}")
    return 0


TOP_EPILOG = """\
common flows:
  ompwire setup                     first machine: check the agent, write config and .env
  ompwire doctor                    verify agent, store and each gateway credential
  ompwire gateway list              bots, credentials and whether they run
  ompwire gateway start             pick a platform, add or update a bot, run it
  ompwire gateway remove            remove a bot; keep or delete its sessions and log
  ompwire gateway logs telegram     follow its journal
  ompwire update [--edge]           newest release, or the tip of main (restarts gateways)
  ompwire restart                   restart the gateway without updating
  ompwire status                    settings and whether the gateway is running
  ompwire chat                      chat with the agent from this terminal
  ompwire uninstall --dry-run       see what uninstalling would remove

config: ~/.config/ompwire/config.toml, one folder per bot in ~/.config/ompwire/gateways/
state:  ~/.local/state/ompwire/gateways/<bot>/ (sessions, log), cli/ for ompwire chat
"""


def _sub(sub, name: str, help_text: str, description: str, examples: str):
    parser = sub.add_parser(name, help=help_text, description=description,
                            epilog=f"examples:\n{examples}", formatter_class=argparse.RawDescriptionHelpFormatter)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ompwire",
        description="Chat harness driving an OMP agent over ACP",
        epilog=TOP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"ompwire {__version__}")
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", metavar="command")

    help_p = _sub(sub, "help", "show help for a command",
                  "Show the command list, or the help of one command.",
                  "  ompwire help\n  ompwire help setup")
    help_p.add_argument("topic", nargs="?", help="command to explain")
    help_p.set_defaults(func=cmd_help)

    chat = _sub(sub, "chat", "talk to the agent from this terminal",
                "Interactive chat in this terminal. The session key defaults to cli:<cwd>, so a "
                "directory keeps its own conversation.",
                "  ompwire chat\n  ompwire chat -q \"summarise this repo\"\n  ompwire chat --cwd ~/proj")
    chat.add_argument("-q", "--query", help="one-shot prompt, print the answer and exit")
    chat.add_argument("--cwd", help="agent working directory")
    chat.add_argument("--key", help="session key (default: cli:<cwd>)")
    chat.set_defaults(func=cmd_chat)

    doctor = _sub(sub, "doctor", "verify the agent, the session store and the bot token",
                  "Checks every dependency the transport needs and prints one line per check.",
                  "  ompwire doctor")
    doctor.set_defaults(func=cmd_doctor)

    sessions = _sub(sub, "sessions", "list recorded sessions",
                    "Show the chat_key to sessionId mapping and how long ago each was used.",
                    "  ompwire sessions")
    sessions.set_defaults(func=cmd_sessions)

    cfg = _sub(sub, "config", "show resolved configuration",
               "Print the merged config with secrets masked, plus the search paths.",
               "  ompwire config")
    cfg.set_defaults(func=cmd_config)

    upd = _sub(sub, "update", "update to the newest release (or the tip of main)",
               "Uses whichever installer owns this copy (uv tool, pipx, or a git checkout).",
               "  ompwire update\n  ompwire update --edge\n  ompwire update --check")
    upd.add_argument("--edge", action="store_true", help="track the latest commit on main")
    upd.add_argument("--stable", action="store_true", help="track the newest release (default)")
    upd.add_argument("--check", action="store_true", help="report what an update would do")
    upd.add_argument("--force", action="store_true", help="for a source checkout, run git checkout")
    upd.add_argument("--no-restart", action="store_true",
                     help="leave a running gateway alone instead of restarting it")
    upd.set_defaults(func=cmd_update)

    setup = _sub(sub, "setup", "check for omp, then write the agent config",
                 "In order: agent, provider and model, thinking, mode, yolo, cwd, MCP. The "
                 "gateway and its token are not part of setup: use `ompwire gateway start`.",
                 "  ompwire setup\n  ompwire setup --yes\n  ompwire setup --force --install-omp")
    setup.add_argument("--yes", action="store_true", help="accept the defaults, no prompts")
    setup.add_argument("--force", action="store_true", help="rewrite an existing config")
    setup.add_argument("--install-omp", action="store_true",
                       help="run the official omp installer without asking")
    setup.set_defaults(func=cmd_setup)

    un = _sub(sub, "uninstall", "remove ompwire: config, state, data, package",
              "Removes only what ompwire created. ~/.omp (agent sessions, credentials, MCP "
              "config) is never touched.",
              "  ompwire uninstall --dry-run\n  ompwire uninstall\n  ompwire uninstall --keep-config")
    un.add_argument("--yes", action="store_true", help="remove without asking")
    un.add_argument("--dry-run", action="store_true", help="show what would be removed")
    un.add_argument("--keep-config", action="store_true",
                    help="leave config.toml and .env (bot token) in place")
    un.add_argument("--force", action="store_true",
                    help="stop a running gateway before removing")
    un.set_defaults(func=cmd_uninstall)

    sc = _sub(sub, "schedule", "run one turn on a timer, unattended",
              "A schedule is a cron entry plus a systemd timer: no daemon, no cost between runs. "
              "The result goes to the gateway named in the schedule, so an unattended turn never "
              "picks its own destination. Runs are ephemeral and refuse approval-needing tools "
              "unless --yolo is given.",
              "  ompwire schedule add morning --cron \"0 7 * * *\" --prompt \"ringkas repo semalam\"\n"
              "  ompwire schedule list\n"
              "  ompwire schedule run morning")
    sc.add_argument("schedule_action", nargs="?", default="list", metavar="action",
                    choices=["list", "add", "remove", "rm", "run", "show", "_run"])
    sc.add_argument("name", nargs="?", default="", help="schedule name")
    sc.add_argument("--cron", default="", help="add: five cron fields, e.g. \"0 7 * * *\"")
    sc.add_argument("--on-calendar", dest="on_calendar", default="",
                    help="add: a systemd expression for what cron cannot say, e.g. \"Mon..Fri 07:00\"")
    sc.add_argument("--prompt", default="", help="add: what the agent is asked each time")
    sc.add_argument("--gateway", default="telegram", help="add: which bot receives the result")
    sc.add_argument("--timezone", default="Asia/Jakarta",
                    help="add: the clock the cron fields are read in")
    sc.add_argument("--keep-session", action="store_true",
                    help="add: persist the session instead of running ephemeral")
    sc.add_argument("--approval", default="yolo", choices=["ask", "write", "yolo"],
                    help="add: what the run may do unattended. ask=reads only, "
                         "write=reads and file writes, yolo=everything (default)")
    sc.add_argument("--timeout", type=float, default=1800.0, help="add: seconds per run")
    sc.add_argument("--no-start", action="store_true",
                    help="add: write the schedule without installing the timer")
    sc.set_defaults(func=cmd_schedule)

    gw = _sub(sub, "gateway", "start, remove, list and control gateway bots",
              "A gateway is one bot on one platform: its own credential, its own allow list, "
              "its own systemd unit and its own folders. `start` does everything: pick a "
              "platform, pick or add a profile, check the credential with the platform, write "
              "the unit, start it. Run it again to restart, or to replace a credential. "
              "`remove` asks which gateway and how far: keep its sessions and log, or delete "
              "everything.\n\n"
              "actions:\n"
              "  start [NAME]    add or update a gateway, then run it\n"
              "  remove [NAME]   remove a gateway; asks which, and confirms by name\n"
              "  list            gateways, bot identity, allow list, unit state\n"
              "  status [NAME]   unit path, state and where its logs are\n"
              "  stop NAME       stop it and keep it down after a reboot\n"
              "  restart NAME    rewrite the unit, then restart it\n"
              "  logs NAME       its last 40 journal lines\n"
              "  types           platforms this version knows, and which run today",
              "  ompwire gateway start\n"
              "  ompwire gateway start --platform telegram --name work\n"
              "  ompwire gateway start work --token-stdin < token.txt --yes\n"
              "  ompwire gateway remove\n"
              "  ompwire gateway remove work --yes --purge\n"
              "  ompwire gateway list")
    gw.add_argument("gateway_action", nargs="?", default=None, metavar="action",
                    choices=["list", "remove", "rm", "start", "stop", "restart",
                             "status", "logs", "types", "_run"],
                    help="start, remove, list, status, stop, restart, logs, types")
    gw.add_argument("name", nargs="?", default="", help="gateway name")
    gw.add_argument("--name", dest="name_flag", default="", metavar="NAME",
                    help="start: gateway name (same as the positional name)")
    gw.add_argument("--platform", default="", help="start: which platform (gateway types lists them)")
    gw.add_argument("--allowed", default="", help="start: comma separated user ids")
    gw.add_argument("--token", default="", help="start: the credential itself (prefer --token-stdin)")
    gw.add_argument("--token-stdin", action="store_true",
                    help="start: read the credential from stdin")
    gw.add_argument("--model", default="", help="start: this gateway's own model")
    gw.add_argument("--cwd", default="", help="start: this gateway's own working directory")
    gw.add_argument("--approval", default="", choices=["", "write", "yolo"],
                    help="start: this gateway's own approval mode")
    gw.add_argument("--purge", action="store_true",
                    help="remove --yes: also delete the gateway's sessions and log")
    gw.add_argument("--no-start", action="store_true",
                    help="start: save the gateway and credential, do not start it")
    gw.add_argument("--force", action="store_true",
                    help="start: write the unit even when a check fails; remove: even when a "
                         "schedule reports to it")
    gw.add_argument("--yes", action="store_true",
                    help="start, remove: no prompts; fail instead of asking")
    gw.set_defaults(func=cmd_gateway, gateway_help=gw.print_help)

    st = _sub(sub, "status", "what this installation is set to, and whether it is running",
              "One screen: version and how it was installed, agent, model/mode/thinking, "
              "approvals, cwd, allowed users, token, mcp, gateway state, sessions, log.",
              "  ompwire status")
    st.set_defaults(func=cmd_status)

    rst = _sub(sub, "restart", "restart a running gateway so new code is used",
               "Restarts the systemd user unit when it is active, otherwise respawns the "
               "transport process it finds.",
               "  ompwire restart")
    rst.set_defaults(func=cmd_restart)

    scrub = _sub(sub, "scrub-logs", "replace bot tokens already written to a log file",
                 "New log lines are redacted as they are written; this cleans files written "
                 "before that filter existed.",
                 "  ompwire scrub-logs\n  ompwire scrub-logs --file /tmp/ompwire-serve.log")
    scrub.add_argument("--file", help="log file to clean (default: logging.file from config)")
    scrub.set_defaults(func=cmd_scrub)

    ver = _sub(sub, "version", "print the installed version and installer",
               "Shows the version of the running code, its commit, and which installer owns it.",
               "  ompwire version")
    ver.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # no arguments is a question about this tool, not a request for a chat
        parser.print_help()
        print("\nRun `ompwire chat` to talk to the agent.")
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
