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
from typing import Any

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
model = "{model}"
mode = "{mode}"
thinking = "{thinking}"
yolo = {yolo}
approval_mode = "{approval}"
append_system_prompt = \"\"\"{hint}\"\"\"

[gateway]
platform = "{platform}"

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
    answer = input(f"{prompt} [number, text, or empty for {default}]: ").strip()
    if not answer:
        return default if default in choices else choices[0]
    if answer.isdigit() and 1 <= int(answer) <= len(shown):
        return shown[int(answer) - 1]
    matches = [choice for choice in choices if answer.lower() in choice.lower()]
    if not matches:
        print(f"      no match for {answer!r}, keeping {default}")
        return default
    return matches[0] if len(matches) == 1 else choose(prompt, matches, default, yes=False, limit=limit)


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
        cwd = _ask("agent working directory", os.path.expanduser("~"), yes=yes)
        approval = _ask("approval policy when yolo is off (always-ask|write|yolo)",
                        "always-ask", yes=yes)
        catalog_values: dict[str, Any] = {"models": [], "modes": [], "thinking": [], "current": {}}
        if agent_path():
            print("\nreading the agent's model/mode/thinking options…")
            catalog_values = agent_catalog(os.path.expanduser(cwd))
        current = catalog_values.get("current") or {}
        model = choose("model", catalog_values.get("models") or [],
                       str(current.get("model") or ""), yes=yes)
        mode = choose("mode", catalog_values.get("modes") or [],
                      str(current.get("mode") or "default"), yes=yes)
        thinking = choose("thinking", catalog_values.get("thinking") or [],
                          str(current.get("thinking") or "xhigh"), yes=yes)
        yolo_answer = _ask("yolo mode (approvals bypassed)", "yes", yes=yes)
        yolo = yolo_answer.strip().lower() in ("yes", "y", "true", "on", "1")
        hub = Path("~/mcp-hub/servers.yaml").expanduser()
        default_source = "mcp-hub" if hub.is_file() else "none"
        mcp_source = _ask("mcp servers (none|inline|mcp-hub)", default_source, yes=yes)
        platform = _ask("gateway platform (telegram)", "telegram", yes=yes).strip().lower()
        if platform != "telegram":
            # never echo the answer back: a token pasted into the wrong prompt is a secret
            shown = "that value looks like a secret, not a platform name" \
                if ":" in platform or len(platform) > 24 else repr(platform)
            print(f"--   {shown}; only telegram is implemented")
            return 1
        allowed = _ask("allowed telegram user ids (comma separated, empty = nobody)",
                       "", yes=yes)
        allowed_list = ", ".join(part.strip() for part in allowed.split(",") if part.strip().isdigit())
        home = allowed_list.split(",")[0].strip() if allowed_list else "0"
        body = TEMPLATE.format(
            cwd=cwd, approval=approval, hint=default_hint(), allowed=allowed_list, home=home,
            mcp_source=mcp_source, hub_path=str(hub) if mcp_source == "mcp-hub" else "",
            model=model, mode=mode, thinking=thinking, yolo="true" if yolo else "false",
            platform=platform,
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

    print("\nnext:")
    print("  ompwire doctor     # verify agent, store, token")
    print("  ompwire serve      # start the Telegram transport")
    return 0
