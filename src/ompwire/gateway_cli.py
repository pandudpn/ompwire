"""`ompwire gateway ...`: start, remove, list and control the bots.

`start` asks only what it does not know (platform, profile, credential, allow list, own
settings) and is safe to run again; `remove` asks which gateway and how far. `_run` is what a
gateway's systemd unit executes. The platform details come from `gateways.platform()`, so
nothing here is specific to Telegram.
"""

from __future__ import annotations

import argparse
import shutil
import sys

from .config import load_config


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


def cmd_gateway(args: argparse.Namespace) -> int:
    from . import gateways, service
    from .setup import bot_identity

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
