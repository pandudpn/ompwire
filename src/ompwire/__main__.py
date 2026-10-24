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
    config = load_config(args.config)
    for warning in config.warnings:
        print(f"warn: {warning}", file=sys.stderr)
    return run_chat(config, key=args.key or _chat_key(), query=args.query, cwd=args.cwd)


def cmd_sessions(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = SessionStore(config.get("session", "store"))
    rows = store.all()
    if not rows:
        print("no sessions recorded")
        return 0
    for row in rows:
        age = int(time.time() - row.updated_at)
        print(f"{row.session_id[:8]}  {age:>7}s ago  {row.cwd}  [{row.key}]")
    store.close()
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

    # the agent comes after the gateway: a mistyped gateway name is the question the
    # user just asked, so answer that before complaining about something else
    from .setup import agent_path

    command = str(config.get("acp", "command", "omp"))
    if agent_path(command) is None:
        print(f"FAIL agent not found: {command}. Run `ompwire setup` first.", file=sys.stderr)
        return 1
    gateways.apply(config, gateway)
    if not str(config.get("telegram", "bot_token", "") or ""):
        print(f"FAIL no credential for gateway {gateway.name!r}. "
              f"Run `ompwire gateway add {gateway.name}` or set {gateway.token_var()}.",
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

    action = getattr(args, "gateway_action", "list") or "list"

    if action == "types":
        for index, kind in enumerate(gateways.KINDS, 1):
            mark = "implemented" if kind in gateways.IMPLEMENTED else "not implemented yet"
            print(f"  {index}. {kind:<9} {mark}")
        return 0

    if action == "list":
        items = gateways.load()
        if not items:
            print("no gateway configured. Run `ompwire gateway add telegram`.")
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

    if action == "add":
        return _gateway_add(args, gateways, service, bot_identity)

    if action in ("remove", "rm"):
        try:
            item = gateways.remove(args.name)
        except gateways.GatewayError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1
        service.remove(item.name, quiet=True)
        print(f"ok   removed gateway {item.name!r} and its unit (credential left in .env: "
              f"{item.token_var()})")
        return 0

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

    if action in ("start", "stop", "restart", "_run") and not gateway.implemented:
        print(f"FAIL the {gateway.kind!r} transport is not implemented yet; "
              f"`ompwire gateway types` shows what can run today", file=sys.stderr)
        return 1
    if action == "_run":
        # what the unit calls. Not a user-facing mode: systemd needs a foreground process,
        # the CLI does not offer one.
        return _run_gateway(args.config, gateway.name, args.force)
    if action == "start":
        return service.start(gateway.name, force=args.force)
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
        print("no gateway configured. Run `ompwire gateway add telegram`.")
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


def _gateway_add(args: argparse.Namespace, gateways, service, bot_identity) -> int:
    from .setup import normalise_token

    kind = getattr(args, "kind", "") or ""
    if not kind:
        if args.yes:
            kind = "telegram"
        else:
            print("gateway type:")
            for index, name in enumerate(gateways.KINDS, 1):
                mark = "" if name in gateways.IMPLEMENTED else "   (not implemented yet)"
                print(f"  {index}. {name}{mark}")
            answer = input("type [1-2, enter for telegram]: ").strip() or "1"
            if answer.isdigit() and 1 <= int(answer) <= len(gateways.KINDS):
                kind = gateways.KINDS[int(answer) - 1]
            else:
                kind = answer
    kind = kind.lower()
    if kind not in gateways.KINDS:
        print(f"FAIL unknown gateway type {kind!r}; known: {', '.join(gateways.KINDS)}",
              file=sys.stderr)
        return 1
    if kind not in gateways.IMPLEMENTED:
        print(f"FAIL the {kind} transport is not implemented yet. The definition would point "
              f"at code that does not exist, so nothing is written.\n"
              f"     telegram is the transport that works today.", file=sys.stderr)
        return 1

    name = (getattr(args, "name", "") or kind).lower()
    try:
        existing = {item.name for item in gateways.load()}
    except gateways.GatewayError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    if name in existing and not args.force:
        print(f"FAIL a gateway named {name!r} already exists (pass --force to rewrite its "
              f"credential)", file=sys.stderr)
        return 1

    token = normalise_token(getattr(args, "token", "") or "")
    if not token and args.token_stdin:
        import sys as _sys

        token = normalise_token(_sys.stdin.readline())
        if not token:
            print("FAIL --token-stdin was given but stdin was empty", file=sys.stderr)
            return 1
    if not token and not args.yes:
        token = normalise_token(_ask_secret_line("telegram bot token from @BotFather"))
    if not token:
        token = gateways.token(gateways.Gateway(name=name, kind=kind))
    if not token:
        print("FAIL no credential given; a gateway cannot start without one", file=sys.stderr)
        return 1

    username = bot_identity(token)
    if not username:
        print("FAIL that token failed getMe: Telegram rejected it. Nothing was written.",
              file=sys.stderr)
        return 1

    if name in existing:
        gateways.save([item if item.name != name else gateways.Gateway(
            name=name, kind=kind, allowed=item.allowed, home=item.home) for item in
            gateways.load()])
        print(f"ok   gateway {name!r} already defined, credential replaced")
    else:
        gateways.add(name, kind, allowed=[], home=None)
        print(f"ok   gateway added: {name} ({kind})")

    env_path = gateways.set_token(gateways.Gateway(name=name, kind=kind), token)
    print(f"ok   credential stored in {env_path} (0600)")
    print(f"ok   token valid: @{username}")

    items = gateways.load()
    item = next(entry for entry in items if entry.name == name)
    if not item.allowed and not args.yes:
        answer = input(f"allowed telegram user ids for @{username} "
                       f"(comma separated, blank = nobody): ").strip()
        ids = [int(part) for part in answer.replace(" ", "").split(",") if part.isdigit()]
        item.allowed = ids
        item.home = ids[0] if ids else None
        gateways.save(items)
        if ids:
            print(f"ok   allowed users: {', '.join(str(user) for user in ids)}")
        else:
            print("warn no allowed users: nobody can talk to this bot yet")

    print(f"\nnext:  ompwire gateway start {name}      # enable and start under systemd")
    return 0


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
    _doctor_units(command)

    store_path = str(config.get("session", "store"))
    try:
        probe = SessionStore(store_path)
        probe.close()
        print(f"ok   session store writable: {store_path}")
    except Exception as exc:
        print(f"FAIL session store: {exc}")
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
        print("--   no gateway configured (run `ompwire gateway add telegram`)")
    from .setup import bot_identity

    for item in items:
        token = gateways.token(item)
        if not token:
            print(f"FAIL gateway {item.name}: no credential "
                  f"(`ompwire gateway add {item.name}` or set {item.token_var()})")
        elif item.kind == "telegram":
            username = bot_identity(token)
            if username:
                print(f"ok   gateway {item.name}: @{username}")
            else:
                print(f"--   gateway {item.name}: Telegram rejected the credential "
                      f"(revoked or rotating; `ompwire gateway add {item.name} --force`)")
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
    from .setup import normalise_token, run

    token = ""
    if args.token_stdin:
        # for terminals where the hidden prompt misbehaves: no echo, no history
        token = normalise_token(sys.stdin.readline())
        if not token:
            print("FAIL --token-stdin was given but stdin was empty", file=sys.stderr)
            return 1
    return run(yes=args.yes, force=args.force, install_omp=args.install_omp, token=token)


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
  ompwire gateway add telegram      add one bot: token, allow list
  ompwire gateway start telegram    enable and start it under systemd --user
  ompwire gateway logs telegram     follow its journal
  ompwire update [--edge]           newest release, or the tip of main (restarts gateways)
  ompwire restart                   restart the gateway without updating
  ompwire status                    settings and whether the gateway is running
  ompwire chat                      chat with the agent from this terminal
  ompwire uninstall --dry-run       see what uninstalling would remove

config: ~/.config/ompwire/config.toml (+ .env for the bot token)
logs:   ~/.local/state/ompwire/ompwire.log
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

    setup = _sub(sub, "setup", "check for omp, then write config and .env",
                 "In order: agent, provider and model, thinking, mode, yolo, cwd, MCP, then the "
                 "gateway with its token and access list.",
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

    gw = _sub(sub, "gateway", "add, start and inspect gateway bots",
              "A gateway is one bot on one platform: its own credential, its own allow list, "
              "its own systemd unit. It only ever runs as that unit, so `start` writes and "
              "enables it. Types: telegram (works today), whatsapp (not yet).",
              "  ompwire gateway add telegram --token-stdin < token.txt\n"
              "  ompwire gateway start telegram\n"
              "  ompwire gateway status")
    gw.add_argument("gateway_action", nargs="?", default="list", metavar="action",
                    choices=["list", "add", "remove", "rm", "start", "stop", "restart",
                             "status", "logs", "types", "_run"])
    gw.add_argument("name", nargs="?", default="", help="gateway name")
    gw.add_argument("--kind", default="", help="add: telegram | whatsapp")
    gw.add_argument("--allowed", default="", help="add: comma separated user ids")
    gw.add_argument("--token", default="", help="add: the bot token itself (prefer --token-stdin)")
    gw.add_argument("--token-stdin", action="store_true",
                    help="add: read the token from stdin")
    gw.add_argument("--force", action="store_true", help="add: replace an existing credential")
    gw.add_argument("--yes", action="store_true", help="add: take the defaults, no prompts")
    gw.set_defaults(func=cmd_gateway)

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
