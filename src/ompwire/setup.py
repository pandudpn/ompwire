"""`ompwire setup`: get a machine from nothing to a runnable transport.

Two things bite a fresh install: the agent itself is missing, and the config is
hand-written. So setup checks for `omp` first, offers the official installer when
it is absent, then writes both files the transport reads — `config.toml` for
settings and `.env` (0600) for the bot token, which never belongs in config.
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

# same first choice config.py searches, so setup and the loader cannot drift
CONFIG_DIR = Path("~/.config/ompwire")


def _config_dir() -> Path:
    """Honour OMPWIRE_CONFIG_DIR so a sandbox run cannot touch a real install."""
    override = os.environ.get("OMPWIRE_CONFIG_DIR", "").strip()
    return Path(override).expanduser() if override else CONFIG_DIR.expanduser()
CONFIG_FILE = CONFIG_DIR / "config.toml"
ENV_FILE = CONFIG_DIR / ".env"
OMP_INSTALL_CMD = "curl -fsSL https://omp.sh/install | sh"

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
approval_mode = "{approval}"
append_system_prompt = \"\"\"{hint}\"\"\"

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


def _ask(prompt: str, default: str, *, yes: bool) -> str:
    if yes:
        return default
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
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


def default_hint() -> str:
    from .config import DEFAULTS

    return str((DEFAULTS.get("agent") or {}).get("append_system_prompt", ""))


def _paths() -> tuple[Path, Path, Path]:
    directory = _config_dir()
    return directory, directory / "config.toml", directory / ".env"


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
            answer = input("     install omp now? [Y/n]: ").strip().lower()
            wanted = answer in ("", "y", "yes")
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

    # 2. config files
    if config_file.is_file() and not force:
        print(f"ok   config kept: {config_file}  (use --force to rewrite)")
        existing = True
    else:
        existing = False

    if not existing:
        cwd = _ask("agent working directory", "~", yes=yes)
        approval = _ask("approval policy (always-ask|write|yolo)", "always-ask", yes=yes)
        hub = Path("~/mcp-hub/servers.yaml").expanduser()
        default_source = "mcp-hub" if hub.is_file() else "none"
        mcp_source = _ask("mcp servers (none|inline|mcp-hub)", default_source, yes=yes)
        allowed = _ask("allowed telegram user ids (comma separated, empty = nobody)",
                       "", yes=yes)
        allowed_list = ", ".join(part.strip() for part in allowed.split(",") if part.strip().isdigit())
        home = allowed_list.split(",")[0].strip() if allowed_list else "0"
        body = TEMPLATE.format(
            cwd=cwd, approval=approval, hint=default_hint(), allowed=allowed_list, home=home,
            mcp_source=mcp_source, hub_path=str(hub) if mcp_source == "mcp-hub" else "",
        )
        config_dir.mkdir(parents=True, exist_ok=True)
        if config_file.is_file():
            backup = config_file.with_suffix(".toml.bak")
            config_file.replace(backup)
            print(f"     previous config moved to {backup}")
        config_file.write_text(body, encoding="utf-8")
        print(f"ok   config written: {config_file}")

    # 3. the bot token, kept out of config and out of terminal echo
    env_values = _existing_env()
    token = env_values.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        token = _ask_secret("telegram bot token from @BotFather", yes)
    if token:
        username = bot_identity(token)
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(f"TELEGRAM_BOT_TOKEN={token}\n", encoding="utf-8")
        env_file.chmod(0o600)
        print(f"ok   token stored in {env_file} (0600)")
        if username:
            print(f"ok   token valid: @{username}")
        else:
            print("--   token could not be verified with getMe; check it before serving")
    else:
        print(f"--   no token given. Add TELEGRAM_BOT_TOKEN to {env_file} before serving")

    print("\nnext:")
    print("  ompwire doctor     # verify agent, store, token")
    print("  ompwire serve      # start the Telegram transport")
    return 0
