"""Entry point: `ompwire [chat|doctor|sessions|config]`."""

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
    from .telegram import serve

    return serve(config)


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
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
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

    print(f"\n{'FAILED' if failures else 'ready'} · ompwire {__version__}")
    return 1 if failures else 0


def cmd_update(args: argparse.Namespace) -> int:
    from .update import run

    return run(edge=args.edge, check=args.check, force=args.force)


def cmd_setup(args: argparse.Namespace) -> int:
    from .setup import run

    return run(yes=args.yes, force=args.force, install_omp=args.install_omp)


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


def cmd_version(_args: argparse.Namespace) -> int:
    from .update import current_commit, current_version, install_mode

    commit = current_commit()
    print(f"ompwire {current_version()}" + (f" ({commit})" if commit else ""))
    print(f"installer: {install_mode()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ompwire", description="Chat harness driving an OMP agent over ACP")
    parser.add_argument("--version", action="version", version=f"ompwire {__version__}")
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser("chat", help="interactive chat (default)")
    chat.add_argument("-q", "--query", help="one-shot prompt, print the answer and exit")
    chat.add_argument("--cwd", help="agent working directory")
    chat.add_argument("--key", help="session key (default: cli:<cwd>)")
    chat.set_defaults(func=cmd_chat)

    doctor = sub.add_parser("doctor", help="verify the agent, the session store and the bot token")
    doctor.set_defaults(func=cmd_doctor)

    sessions = sub.add_parser("sessions", help="list recorded sessions")
    sessions.set_defaults(func=cmd_sessions)

    serve_p = sub.add_parser("serve", help="run the Telegram transport")
    serve_p.set_defaults(func=cmd_serve)

    cfg = sub.add_parser("config", help="show resolved configuration")
    cfg.set_defaults(func=cmd_config)

    upd = sub.add_parser("update", help="update to the newest release (or the tip of main)")
    upd.add_argument("--edge", action="store_true", help="track the latest commit on main")
    upd.add_argument("--stable", action="store_true", help="track the newest release (default)")
    upd.add_argument("--check", action="store_true", help="report what an update would do")
    upd.add_argument("--force", action="store_true", help="for a source checkout, run git checkout")
    upd.set_defaults(func=cmd_update)

    setup = sub.add_parser("setup", help="check for omp, then write config and .env")
    setup.add_argument("--yes", action="store_true", help="accept the defaults, no prompts")
    setup.add_argument("--force", action="store_true", help="rewrite an existing config")
    setup.add_argument("--install-omp", action="store_true",
                       help="run the official omp installer without asking")
    setup.set_defaults(func=cmd_setup)

    scrub = sub.add_parser("scrub-logs", help="replace bot tokens already written to a log file")
    scrub.add_argument("--file", help="log file to clean (default: logging.file from config)")
    scrub.set_defaults(func=cmd_scrub)

    ver = sub.add_parser("version", help="print the installed version and installer")
    ver.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args([*argv, "chat"])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
