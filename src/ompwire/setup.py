"""`ompwire setup`: get a machine from nothing to a runnable transport.

Order follows the dependency chain: the agent must exist before anything else, a
provider must be logged in before a model means anything, the bot token must be
verified before the gateway has an identity, and only then do preferences and MCP
servers make sense.

Secrets go to `.env` (0600), settings to `config.toml`, and MCP servers to omp's own
`~/.omp/agent/mcp.json` — that is the file omp reads, so ompwire does not re-inject
servers omp already knows about.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# same first choice config.py searches, so setup and the loader cannot drift
CONFIG_DIR = Path("~/.config/ompwire")
OMP_INSTALL_CMD = "curl -fsSL https://omp.sh/install | sh"
OMP_MCP_FILE = Path("~/.omp/agent/mcp.json")
MCP_SCHEMA = ("https://raw.githubusercontent.com/can1357/oh-my-pi/main/"
              "packages/coding-agent/src/config/mcp-schema.json")

TEMPLATE = """# ompwire configuration — written by `ompwire setup`
#
# Settings live here; the bot token lives in .env next to this file.
# `ompwire config` prints the merged result.

[acp]
command = "omp"
args = ["--mode", "acp"]
startup_timeout = 60.0
turn_timeout = 900.0

[agent]
cwd = "{cwd}"
model = "{model}"
mode = "{mode}"
thinking = "{thinking}"
yolo = {yolo}
approval_mode = "{approval}"
append_system_prompt = \"\"\"{hint}\"\"\"

[gateway]
platform = "telegram"

[telegram]
allowed_users = [{allowed}]
home_channel = {home}
verbose = true

[stream]
show_tool_calls = true
show_usage = false

[mcp]
source = "{mcp_source}"
hub_path = "{hub_path}"

[approval]
policy = "{approval}"

[logging]
level = "INFO"
file = "~/.local/state/ompwire/ompwire.log"
"""


def _config_dir() -> Path:
    """Honour OMPWIRE_CONFIG_DIR so a sandbox run cannot touch a real install."""
    override = os.environ.get("OMPWIRE_CONFIG_DIR", "").strip()
    return Path(override).expanduser() if override else CONFIG_DIR.expanduser()


def _paths() -> tuple[Path, Path, Path]:
    directory = _config_dir()
    return directory, directory / "config.toml", directory / ".env"


def _confirm(prompt: str) -> str:
    """Yes/no question that survives a pipe or a missing tty (returns "")."""
    try:
        return input(prompt).strip().lower()
    except (EOFError, OSError):
        # pytest capture, a closed pipe, or no tty: treat as "no answer"
        return ""


def _ask(prompt: str, default: str, *, yes: bool) -> str:
    if yes:
        return default
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except (EOFError, OSError):
        return default
    return answer or default


def _ask_secret(prompt: str, yes: bool) -> str:
    if yes:
        return ""
    try:
        return getpass.getpass(f"{prompt}: ").strip()
    except (EOFError, OSError):
        # no tty (piped output, container, CI): treat it as "no token given"
        return ""


def agent_path(command: str = "omp") -> str | None:
    found = shutil.which(command)
    if found:
        return found
    fallback = Path(f"~/.local/bin/{command}").expanduser()
    return str(fallback) if fallback.is_file() else None


def install_agent() -> bool:
    """Run the official installer. Only called after the user says yes."""
    print(f"running: {OMP_INSTALL_CMD}")
    try:
        result = subprocess.run(["sh", "-lc", OMP_INSTALL_CMD], timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"installer failed: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"installer exited {result.returncode}", file=sys.stderr)
        return False
    return True


def bot_identity(token: str, timeout: float = 10.0) -> str:
    """getMe, so setup proves the token works instead of hoping."""
    if not token:
        return ""
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return ""
    if not payload.get("ok"):
        return ""
    result = payload.get("result") or {}
    return str(result.get("username") or "")


def providers() -> dict[str, tuple[str, str]]:
    """Logged-in providers, read from omp's credential store."""
    from .catalog import credential_snapshot

    try:
        return credential_snapshot()
    except Exception:  # noqa: BLE001 - a broken credential db must not stop setup
        return {}


def login_provider(yes: bool) -> bool:
    """Run `omp login` in this terminal, so the OAuth flow happens where the user is."""
    if yes:
        print("--   no provider logged in yet. Run `omp login`, or `/login` in the chat later.")
        return False
    print("--   no provider is logged in yet, and without one the bot cannot answer")
    answer = _confirm("     log in to a provider now? [Y/n]: ")
    if answer not in ("", "y", "yes"):
        return False
    try:
        # inherit the terminal: omp prints a URL and waits for the pasted code
        subprocess.run(["omp", "login"], timeout=900, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"     login failed: {exc}", file=sys.stderr)
        return False
    return True


def agent_catalog(cwd: str, command: str = "omp") -> dict[str, Any]:
    """The options the agent actually offers, so setup picks from facts not guesses."""
    from .acp import AcpClient

    client = AcpClient(command=command, cwd=cwd, startup_timeout=60.0)
    try:
        client.initialize()
        session = client.new_session(cwd, [])
        options = {opt.get("id"): opt for opt in (session.get("configOptions") or [])}
        session_id = str(session.get("sessionId") or "")
        if session_id:
            client.close_session(session_id)
    except Exception:  # noqa: BLE001 - setup degrades to empty choices
        return {"models": [], "modes": [], "thinking": [], "current": {}}
    finally:
        client.close()

    def values(name: str) -> list[str]:
        option = options.get(name) or {}
        return [str(choice.get("value")) for choice in (option.get("options") or [])
                if choice.get("value")]

    return {
        "models": values("model"),
        "modes": values("mode"),
        "thinking": values("thinking"),
        "current": {name: (options.get(name) or {}).get("currentValue") for name in options},
    }


def choose(prompt: str, choices: list[str], default: str, *, yes: bool, limit: int = 30) -> str:
    """Pick one value, showing a numbered window instead of 240 model ids."""
    if not choices:
        return _ask(prompt, default, yes=yes)
    if yes:
        return default if default in choices else choices[0]
    shown = choices[:limit]
    if default and default in choices and default not in shown:
        # the current value must be visible even when the list starts alphabetically
        shown = [default] + [choice for choice in shown if choice != default][: limit - 1]
    print(f"\n{prompt}:")
    for index, choice in enumerate(shown, 1):
        mark = "*" if choice == default else " "
        print(f"  {mark}{index:3d}. {choice}")
    if len(choices) > limit:
        print(f"      … {len(choices) - limit} more, type part of a name to filter")
    try:
        answer = input(f"{prompt} [number, text, or empty for {default}]: ").strip()
    except (EOFError, OSError):
        return default if default in choices else choices[0]
    if not answer:
        return default if default in choices else choices[0]
    if answer.isdigit() and 1 <= int(answer) <= len(shown):
        return shown[int(answer) - 1]
    matches = [choice for choice in choices if answer.lower() in choice.lower()]
    if not matches:
        print(f"      no match for {answer!r}, keeping {default}")
        return default
    return matches[0] if len(matches) == 1 else choose(prompt, matches, default, yes=False,
                                                       limit=limit)


def default_hint() -> str:
    from .config import DEFAULTS

    return str((DEFAULTS.get("agent") or {}).get("append_system_prompt", ""))


def read_mcp_servers(path: Path | None = None) -> dict[str, Any]:
    target = (path or OMP_MCP_FILE).expanduser()
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def add_mcp_server(name: str, command: str, args: list[str],
                   env: dict[str, str] | None = None,
                   path: Path | None = None) -> Path:
    """Write one stdio server into omp's own MCP file, keeping what is already there."""
    target = (path or OMP_MCP_FILE).expanduser()
    existing: dict[str, Any] = {}
    if target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            existing = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    entry: dict[str, Any] = {"type": "stdio", "command": command}
    if args:
        entry["args"] = args
    if env:
        entry["env"] = env
    servers[name] = entry
    existing["mcpServers"] = servers
    existing.setdefault("$schema", MCP_SCHEMA)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return target


def _mcp_wizard(yes: bool, path: Path | None = None) -> Path | None:
    """Collect servers from the terminal, one at a time."""
    if yes:
        return None
    target = (path or OMP_MCP_FILE).expanduser()
    known = read_mcp_servers(target)
    if known:
        print(f"     omp already has {len(known)} server(s): {', '.join(sorted(known))}")
    if _confirm(f"     add an MCP server to {target}? [y/N]: ") not in ("y", "yes"):
        return None
    written: Path | None = None
    while True:
        name = _ask("       server name", "", yes=False).strip()
        command = _ask("       command", "", yes=False).strip()
        args_raw = _ask("       args (space separated)", "", yes=False).strip()
        env_raw = _ask("       env KEY=VAL,KEY2=VAL2", "", yes=False).strip()
        if not name or not command:
            print("       name and command are required, skipped")
        else:
            env = {}
            for pair in (piece.strip() for piece in env_raw.split(",")):
                if pair and "=" in pair:
                    key, value = pair.split("=", 1)
                    env[key.strip()] = value.strip()
            written = add_mcp_server(name, command, args_raw.split() if args_raw else [],
                                     env, path=target)
            print(f"       added {name} to {written}")
        if _confirm("       add another? [y/N]: ") not in ("y", "yes"):
            return written


def _existing_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = _paths()[2]
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _next_steps() -> None:
    print("\nnext:")
    print("  ompwire doctor     # verify agent, store, token")
    print("  ompwire serve      # start the Telegram transport")


def run(*, yes: bool = False, force: bool = False, install_omp: bool = False) -> int:
    config_dir, config_file, env_file = _paths()
    print("ompwire setup\n")

    # 1. the agent comes first: the transport has nothing to drive without it
    found = agent_path()
    if found:
        print(f"ok   agent found: {found}")
    else:
        print("--   agent not found: `omp` is required before ompwire can run")
        print(f"     official installer: {OMP_INSTALL_CMD}")
        wanted = install_omp
        if not wanted and not yes:
            wanted = _confirm("     install omp now? [Y/n]: ") in ("", "y", "yes")
        if wanted:
            if not install_agent():
                return 1
            found = agent_path()
            if found:
                print(f"ok   agent installed: {found}")
            else:
                print("--   installer finished but `omp` is not on PATH yet; "
                      "open a new shell and re-run `ompwire setup`")
                return 1
        else:
            print("     skipping. Install omp, then re-run `ompwire setup`.")
            return 1

    # 2. a provider has to be logged in before a model means anything
    logged_in = providers()
    if logged_in:
        detail = ", ".join(f"{name} ({kind})" for name, (kind, _) in sorted(logged_in.items()))
        print(f"ok   providers logged in: {detail}")
    else:
        login_provider(yes)
        logged_in = providers()
        if logged_in:
            print(f"ok   providers logged in: {', '.join(sorted(logged_in))}")
        else:
            print("warn no provider logged in: the bot starts but cannot answer until you "
                  "run `omp login` or `/login` in the chat")

    # 3. the gateway identity, verified before anything depends on it
    env_values = _existing_env()
    token = env_values.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    username = ""
    for attempt in range(1, 4):
        if not token:
            token = _ask_secret("telegram bot token from @BotFather", yes)
        if not token:
            break
        username = bot_identity(token)
        if username:
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text(f"TELEGRAM_BOT_TOKEN={token}\n", encoding="utf-8")
            env_file.chmod(0o600)
            print(f"ok   token stored in {env_file} (0600)")
            print(f"ok   token valid: @{username}")
            break
        print(f"--   that token failed getMe (attempt {attempt}/3)")
        token = ""
    else:
        print("--   token could not be verified; check it in @BotFather and re-run setup")

    if not token:
        print(f"\nFAIL no usable TELEGRAM_BOT_TOKEN. The telegram gateway cannot start without it.\n"
              f"     get one from @BotFather, then either re-run `ompwire setup` or write\n"
              f"     TELEGRAM_BOT_TOKEN=… into {env_file} (chmod 600)")
        return 1

    # 4. settings, now that the bot has an identity to hand out
    if config_file.is_file() and not force:
        print(f"ok   config kept: {config_file}  (use --force to rewrite)")
        _next_steps()
        return 0

    allowed = _ask(f"allowed telegram user ids for @{username or 'the bot'} "
                   "(message @userinfobot to get yours, blank = nobody)", "", yes=yes)
    allowed_list = ", ".join(part.strip() for part in allowed.split(",") if part.strip().isdigit())
    home = allowed_list.split(",")[0].strip() if allowed_list else "0"
    if not allowed_list:
        print("warn no allowed users: nobody can talk to the bot until you add one")

    cwd = _ask("agent working directory", os.path.expanduser("~"), yes=yes)
    print("\nreading the agent's model/mode/thinking options…")
    catalog_values = agent_catalog(os.path.expanduser(cwd))
    current = catalog_values.get("current") or {}
    model = choose("model", catalog_values.get("models") or [],
                   str(current.get("model") or ""), yes=yes)
    mode = choose("mode", catalog_values.get("modes") or [],
                  str(current.get("mode") or "default"), yes=yes)
    thinking = choose("thinking", catalog_values.get("thinking") or [],
                      str(current.get("thinking") or "xhigh"), yes=yes)

    # 5. one question, not two: yolo decides whether an approval policy is needed
    yolo_answer = _ask("yolo mode (approvals bypassed)", "yes", yes=yes)
    yolo = yolo_answer.strip().lower() in ("yes", "y", "true", "on", "1")
    approval = "always-ask"
    if not yolo:
        approval = _ask("approval policy (always-ask|write)", "always-ask", yes=yes)

    # 6. MCP belongs to omp; ompwire only merges a hub when explicitly asked
    print(f"\nmcp: server definitions live in omp itself ({OMP_MCP_FILE})")
    _mcp_wizard(yes)
    hub = Path("~/mcp-hub/servers.yaml").expanduser()
    mcp_source = "none"
    if not yes and hub.is_file():
        if _confirm(f"     merge servers from {hub} at runtime too? [y/N]: ") in ("y", "yes"):
            mcp_source = "mcp-hub"

    body = TEMPLATE.format(
        cwd=cwd, approval=approval, hint=default_hint(), allowed=allowed_list, home=home,
        mcp_source=mcp_source, hub_path=str(hub) if mcp_source == "mcp-hub" else "",
        model=model, mode=mode, thinking=thinking, yolo="true" if yolo else "false",
    )
    config_dir.mkdir(parents=True, exist_ok=True)
    if config_file.is_file():
        backup = config_file.with_suffix(".toml.bak")
        config_file.replace(backup)
        print(f"     previous config moved to {backup}")
    config_file.write_text(body, encoding="utf-8")
    print(f"ok   config written: {config_file}")

    _next_steps()
    return 0
