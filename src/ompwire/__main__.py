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
from .cli import run_chat
from .config import config_search_paths, load_config
from .gateway_cli import cmd_gateway
from .rpc import AgentClient, AgentError
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


def _doctor_units(agent: str) -> None:
    """Which units would start the gateway and then lose the agent.

    A unit carries the PATH of the shell that generated it. When an agent lives in ~/.local/bin
    and the unit was written before that PATH was recorded, the poller starts, connects, and every
    turn fails with "agent command not found". The unit on disk is compared against what this
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

    from .engine import agent_args, mcp_server_names

    client = AgentClient(
        command=command,
        args=[*agent_args(config), "--no-session"],
        cwd=str(config.get("agent", "cwd") or os.path.expanduser("~")),
        startup_timeout=float(config.get("acp", "startup_timeout", 60.0)),
    )
    try:
        state = client.state()
        model = state.get("model") or {}
        print(f"ok   RPC handshake: session {str(state.get('sessionId'))[:8]} · model "
              f"{model.get('provider')}/{model.get('id')} · thinking {state.get('thinkingLevel') or '-'}")
        print(f"     models available: {len(client.models())} · "
              f"thinking levels: {', '.join(client.thinking_levels()) or '-'}")
    except AgentError as exc:
        print(f"FAIL RPC handshake: {exc}")
        failures += 1
    finally:
        client.close()

    servers = mcp_server_names()
    print(f"ok   mcp: omp loads {', '.join(servers) if servers else 'none'} from its own mcp.json")

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
        if item.kind == "whatsapp":
            number = gateways.identity(item)
            if number:
                print(f"ok   gateway {item.name}: whatsapp {number}")
            else:
                print(f"FAIL gateway {item.name}: no linked WhatsApp device "
                      f"(`ompwire gateway start {item.name}`)")
        elif not gateways.token(item):
            print(f"FAIL gateway {item.name}: no credential "
                  f"(`ompwire gateway start {item.name}` or set {item.token_var()})")
        else:
            username = bot_identity(gateways.token(item))
            if username:
                print(f"ok   gateway {item.name}: @{username}")
            else:
                print(f"--   gateway {item.name}: Telegram rejected the credential "
                      f"(revoked or rotating; `ompwire gateway start {item.name}`)")
        if item.allowed:
            print(f"ok   allowed users for {item.name}: {', '.join(item.allowed)}")
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

    gw = _sub(sub, "gateway", "start, update, remove, list and control gateway bots",
              "A gateway is one bot on one platform: its own credential, its own allow list, "
              "its own systemd unit and its own folders. `start` does everything: pick a "
              "platform, pick or add a profile, check the credential with the platform, write "
              "the unit, start it. Run it again to restart, or to replace a credential. "
              "`update` changes an existing gateway on any platform: its groups (picked by "
              "name from the chats it has seen), the mode it answers in there, and its own "
              "settings over config.toml. A flag set to `inherit` falls back to config.toml. "
              "`remove` asks which gateway and how far: keep its sessions and log, or delete "
              "everything.\n\n"
              "actions:\n"
              "  start [NAME]    add a gateway or replace its credential, then run it\n"
              "  update [NAME]   change groups, group mode and its own settings; a menu, or flags\n"
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
              "  ompwire gateway start --platform whatsapp --allowed 6281234567890\n"
              "  ompwire gateway update work\n"
              "  ompwire gateway update work --thinking high --reasoning on --verbose off\n"
              "  ompwire gateway update work --groups -1001234,-1005678:all --group-mode mention\n"
              "  ompwire gateway update work --model inherit\n"
              "  ompwire gateway remove\n"
              "  ompwire gateway remove work --yes --purge\n"
              "  ompwire gateway list")
    gw.add_argument("gateway_action", nargs="?", default=None, metavar="action",
                    choices=["list", "remove", "rm", "start", "update", "stop", "restart",
                             "status", "logs", "types", "_run"],
                    help="start, update, remove, list, status, stop, restart, logs, types")
    gw.add_argument("name", nargs="?", default="", help="gateway name")
    gw.add_argument("--name", dest="name_flag", default="", metavar="NAME",
                    help="start: gateway name (same as the positional name)")
    gw.add_argument("--platform", default="", help="start: which platform (gateway types lists them)")
    gw.add_argument("--allowed", default="",
                    help="start: who may talk to it, comma separated (Telegram user ids, "
                         "WhatsApp phone numbers)")
    gw.add_argument("--groups", default=None,
                    help="start, update: groups and Telegram channels it listens in, comma "
                         "separated; id:mode gives one its own mode (\"\" lists none)")
    gw.add_argument("--group-mode", default="", choices=["", "mention", "all", "read"],
                    help="start, update: in those chats, answer an @mention or reply "
                         "(mention), every message (all), or nothing (read)")
    gw.add_argument("--relink", action="store_true",
                    help="start, whatsapp: link a new device even when one is linked")
    gw.add_argument("--pair-phone", default="",
                    help="start, whatsapp: link with a pairing code sent to this number "
                         "instead of a QR")
    gw.add_argument("--token", default="", help="start: the credential itself (prefer --token-stdin)")
    gw.add_argument("--token-stdin", action="store_true",
                    help="start: read the credential from stdin")
    gw.add_argument("--model", default="",
                    help="start, update: this gateway's own model (provider/id, or inherit)")
    gw.add_argument("--cwd", default="",
                    help="start, update: this gateway's own working directory (or inherit)")
    gw.add_argument("--approval", default="", choices=["", "write", "yolo", "inherit"],
                    help="start, update: this gateway's own approval mode")
    gw.add_argument("--thinking", default="",
                    choices=["", "off", "auto", "minimal", "low", "medium", "high", "xhigh",
                             "inherit"],
                    help="update: this gateway's thinking level")
    gw.add_argument("--reasoning", default="", choices=["", "on", "off", "inherit"],
                    help="update: show the agent's thinking in direct chats")
    gw.add_argument("--verbose", default="", choices=["", "on", "off", "inherit"],
                    help="update: show activity (trace, todo list, notices) in direct chats")
    gw.add_argument("--purge", action="store_true",
                    help="remove --yes: also delete the gateway's sessions and log")
    gw.add_argument("--no-start", action="store_true",
                    help="start: save the gateway and credential, do not start it; update: "
                         "save, do not restart it")
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


def _join_negative_ids(argv: list[str]) -> list[str]:
    """`--groups -1001,-1002:all` → `--groups=-1001,-1002:all`. A Telegram group id is negative,
    and argparse takes a value starting with "-" for a flag unless it is one plain number."""
    out: list[str] = []
    index = 0
    while index < len(argv):
        if argv[index] == "--groups" and index + 1 < len(argv) and argv[index + 1].startswith("-") \
                and argv[index + 1][1:2].isdigit():
            out.append(f"--groups={argv[index + 1]}")
            index += 2
            continue
        out.append(argv[index])
        index += 1
    return out


def main(argv: list[str] | None = None) -> int:
    argv = _join_negative_ids(list(sys.argv[1:] if argv is None else argv))
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
