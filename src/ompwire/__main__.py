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


def cmd_serve(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    for warning in config.warnings:
        print(f"warn: {warning}", file=sys.stderr)
    from .setup import agent_path

    command = str(config.get("acp", "command", "omp"))
    if agent_path(command) is None:
        print(f"FAIL agent not found: {command}. Run `ompwire setup` first.", file=sys.stderr)
        return 1
    platform = str(config.get("gateway", "platform", "telegram"))
    if platform != "telegram":
        print(f"FAIL gateway platform {platform!r} is not implemented", file=sys.stderr)
        return 1
    if not str(config.get("telegram", "bot_token", "") or ""):
        print("FAIL telegram bot token missing. Run `ompwire setup` or set TELEGRAM_BOT_TOKEN.",
              file=sys.stderr)
        return 1
    from . import lock

    if args.force:
        print("warn: --force skips the single-instance lock; two pollers make Telegram "
              "answer 409 Conflict and drop updates", file=sys.stderr)
        handle = None
    else:
        try:
            handle = lock.acquire()
        except lock.AlreadyRunning as busy:
            print(f"FAIL another gateway is already running (pid {busy.pid}).\n"
                  f"     stop it, or use `ompwire restart`; `ompwire status` shows it.\n"
                  f"     --force starts anyway, at the risk of Telegram 409 Conflict.",
                  file=sys.stderr)
            return 1

    from .telegram import serve

    try:
        return serve(config)
    finally:
        lock.release(handle)


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    failures = 0

    command = str(config.get("acp", "command", "omp"))
    path = shutil.which(command)
    if path is None:
        print(f"FAIL agent command not found on PATH: {command}")
        return 1
    print(f"ok   agent command: {path}")

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

    token = str(config.get("telegram", "bot_token", "") or "")
    print(f"{'ok  ' if token else '--  '} telegram token {'set' if token else 'missing'}")

    allowed = [str(user) for user in (config.get("telegram", "allowed_users") or [])]
    if allowed:
        print(f"ok   allowed users: {', '.join(allowed)}")
    else:
        print("--   allowed users: none (nobody can talk to the bot; run ompwire setup)")

    print(f"\n{'FAILED' if failures else 'ready'} · ompwire {__version__}")
    return 1 if failures else 0


def cmd_update(args: argparse.Namespace) -> int:
    from .update import run

    return run(edge=args.edge, check=args.check, force=args.force,
               restart=not args.no_restart)


def cmd_service(args: argparse.Namespace) -> int:
    from . import service

    action = getattr(args, "service_action", "show")
    if action == "install":
        return service.install(force=args.force, start=not args.no_start)
    if action == "uninstall":
        return service.uninstall(keep_unit_file=args.keep_unit_file)
    return service.show()


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
  ompwire doctor                    verify agent, session store and bot token
  ompwire serve                     run the Telegram transport
  ompwire chat                      chat with the agent from this terminal
  ompwire update [--edge]           newest release, or the tip of main (restarts the gateway)
  ompwire restart                   restart the gateway without updating
  ompwire status                    settings and whether the gateway is running
  ompwire service install           keep the gateway running under systemd --user
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

    serve_p = _sub(sub, "serve", "run the Telegram transport",
                   "Start the gateway. Refuses to start without an agent, a supported platform "
                   "and a verified bot token, and refuses to start twice: a second poller makes "
                   "Telegram answer 409 Conflict and drop updates.",
                   "  ompwire serve\n  ompwire serve --force   # skip the lock, on purpose")
    serve_p.add_argument("--force", action="store_true",
                         help="start even if another gateway holds the lock")
    serve_p.set_defaults(func=cmd_serve)

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
                    help="stop a running `ompwire serve` before removing")
    un.set_defaults(func=cmd_uninstall)

    svc = _sub(sub, "service", "run the gateway under systemd --user",
               "A gateway started from a shell dies with that shell. This writes a user unit "
               "from the paths on this machine, then enables it.",
               "  ompwire service install\n"
               "  ompwire service show\n"
               "  ompwire service uninstall")
    svc.add_argument("service_action", nargs="?", default="show",
                     choices=["install", "uninstall", "show"])
    svc.add_argument("--force", action="store_true", help="rewrite an existing unit file")
    svc.add_argument("--no-start", action="store_true",
                     help="enable the unit without starting it now")
    svc.add_argument("--keep-unit-file", action="store_true",
                     help="uninstall: disable, but leave the unit file in place")
    svc.set_defaults(func=cmd_service)

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
