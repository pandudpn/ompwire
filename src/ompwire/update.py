"""`ompwire update`: stable by default, `--edge` for the tip of main.

ompwire is distributed as a git package, not from PyPI, so an update means
reinstalling the same tool entry point at another ref. Which installer owns the
command decides how: uv tool, pipx, or a plain checkout that pulls.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = "pandudpn/ompwire"
GIT_URL = f"git+https://github.com/{REPO}.git"
PACKAGE = "ompwire"
STABLE = "stable"
EDGE = "edge"


def current_version() -> str:
    """The version of the code that is running.

    Distribution metadata goes stale on an editable checkout (bump pyproject, ask
    for the version, get the old one), so the module itself wins.
    """
    from . import __version__

    return __version__


def current_commit() -> str:
    root = shutil.which("git")
    if not root:
        return ""
    try:
        out = subprocess.run([root, "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=10, cwd=_checkout_root())
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _checkout_root() -> str:
    from pathlib import Path

    # src/ompwire/update.py -> repo root
    return str(Path(__file__).resolve().parents[2])


def _semver_key(name: str) -> tuple[int, ...]:
    parts = name.lstrip("v").split(".")
    return tuple(int(part) if part.isdigit() else 0 for part in parts)


def _gh_api(path: str) -> dict | list | None:
    """GitHub API through the authenticated gh CLI; the repo is private, so plain
    HTTP answers 404 and an anonymous client cannot tell that from a missing tag."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        out = subprocess.run([gh, "api", f"repos/{REPO}/{path}"], capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def _remote_tags() -> list[str]:
    """Tags straight from git, so a private repo needs no API token."""
    git = shutil.which("git")
    if not git:
        return []
    try:
        out = subprocess.run([git, "ls-remote", "--tags", GIT_URL], capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    names: list[str] = []
    for line in out.stdout.splitlines():
        ref = line.split("refs/tags/")[-1].strip()
        if ref and not ref.endswith("^{}"):
            names.append(ref)
    return names


def latest_stable() -> tuple[str, str]:
    """(ref, description) for the newest release tag."""
    release = _gh_api("releases/latest")
    if isinstance(release, dict) and release.get("tag_name"):
        return str(release["tag_name"]), str(release.get("name") or release["tag_name"])
    tags = _remote_tags()
    if tags:
        best = max(tags, key=_semver_key)
        return best, f"tag {best}"
    return "main", "no release or tag found, falling back to main"


def latest_edge() -> tuple[str, str]:
    commit = _gh_api("commits/main")
    if isinstance(commit, dict) and commit.get("sha"):
        short = str(commit["sha"])[:7]
        message = (commit.get("commit") or {}).get("message", "").splitlines()
        return "main", f"main @ {short} · {message[0] if message else ''}".strip()
    return "main", "main"


def install_mode() -> str:
    """Which tool manager owns the `ompwire` command."""
    uv = shutil.which("uv")
    if uv:
        try:
            listed = subprocess.run([uv, "tool", "list"], capture_output=True, text=True, timeout=15)
            if listed.returncode == 0 and "ompwire" in listed.stdout:
                return "uv"
        except (OSError, subprocess.SubprocessError):
            pass
    pipx = shutil.which("pipx")
    if pipx:
        try:
            listed = subprocess.run([pipx, "list"], capture_output=True, text=True, timeout=15)
            if listed.returncode == 0 and "ompwire" in listed.stdout:
                return "pipx"
        except (OSError, subprocess.SubprocessError):
            pass
    if (Path(_checkout_root()) / ".git").exists():  # a worktree's .git is a file
        return "checkout"
    return "unknown"


def _command_for(mode: str, ref: str) -> list[str]:
    spec = f"{GIT_URL}@{ref}"
    if mode == "uv":
        return ["uv", "tool", "install", "--force", "--from", spec, PACKAGE]
    if mode == "pipx":
        return ["pipx", "install", "--force", spec]
    return []


def serving_processes() -> list[tuple[int, str]]:
    from .uninstall import running_serves

    return running_serves()


def _systemd_active(unit: str) -> bool:
    try:
        result = subprocess.run(["systemctl", "--user", "is-active", unit],
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() == "active"


def _active_units() -> list[str]:
    """Every gateway unit that is running, so an update restarts all of them."""
    units: list[str] = []
    try:
        from . import gateways

        for item in gateways.load():
            name = f"ompwire-{item.name}"
            if name not in units and _systemd_active(name):
                units.append(name)
    except Exception:
        pass
    return units


def _argv_of(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode() for part in raw.split(b"\0") if part]


def _cwd_of(pid: int) -> str | None:
    try:
        return str(Path(f"/proc/{pid}/cwd").resolve())
    except OSError:
        return None


def _transport_command(pid: int, command: str) -> list[str]:
    """The command to respawn.

    A shell wrapper (`zsh -lic ... | tee`) is not what should come back: respawning it
    would stack a second tee. The interpreter line that actually runs the transport is.
    """
    argv = _argv_of(pid)
    if not argv:
        return []
    if argv[0].endswith(("python", "python3")) or "python" in Path(argv[0]).name:
        return argv
    for index, part in enumerate(argv):
        if not part.endswith("ompwire"):
            continue
        rest = argv[index + 1:]
        if rest[:2] == ["gateway", "_run"]:
            return argv[index:]
    return argv if "gateway _run" in command else []


def restart_gateway(timeout: float = 20.0) -> str:
    """Restart a running transport so an update actually takes effect."""
    units = _active_units()
    if units:
        restarted: list[str] = []
        for unit in units:
            try:
                result = subprocess.run(["systemctl", "--user", "restart", unit],
                                        capture_output=True, text=True, timeout=120, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                return f"systemd restart of {unit} failed: {exc}"
            if result.returncode != 0:
                return f"systemd restart of {unit} exited {result.returncode}"
            restarted.append(unit)
        return "restarted via systemd: " + ", ".join(restarted)

    serves = serving_processes()
    if not serves:
        return "no transport running, nothing to restart"

    from .config import load_config

    try:
        log_target = str(load_config().get("logging", "file", "") or "")
    except Exception:
        log_target = ""
    log_path = Path(log_target).expanduser() if log_target else None

    restarted: list[int] = []
    problems: list[str] = []
    deepest = max(serves, key=lambda item: len(_argv_of(item[0])))
    planned = _transport_command(deepest[0], deepest[1])

    for pid, _command in serves:
        try:
            os.kill(pid, 15)
        except OSError:
            continue
    deadline = time.time() + timeout
    for pid, _command in serves:
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)

    if not planned:
        return "stopped the transport but could not tell how to start it again"

    stream = subprocess.DEVNULL
    handle = None
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - lives with the child
            stream = handle
        except OSError:
            stream = subprocess.DEVNULL
    try:
        child = subprocess.Popen(planned, cwd=_cwd_of(deepest[0]) or None, stdin=subprocess.DEVNULL,
                                 stdout=stream, stderr=stream, start_new_session=True)
        restarted.append(child.pid)
    except (OSError, subprocess.SubprocessError) as exc:
        problems.append(str(exc))
    finally:
        if handle is not None:
            handle.close()

    if restarted:
        return f"restarted (pid {', '.join(str(pid) for pid in restarted)})"
    return "stopped the transport but respawn failed: " + "; ".join(problems)


def run(*, edge: bool = False, check: bool = False, force: bool = False,
        restart: bool = True) -> int:
    mode = install_mode()
    ref, description = latest_edge() if edge else latest_stable()
    channel = EDGE if edge else STABLE
    here = current_version()
    commit = current_commit()

    print(f"installed : ompwire {here}" + (f" ({commit})" if commit else ""))
    print(f"installer : {mode}")
    print(f"channel   : {channel} -> {description}")

    if check:
        print("no changes made (--check)")
        return 0

    if mode == "checkout":
        print(f"\nthis is a source checkout, so update it with git:\n"
              f"  git -C {_checkout_root()} fetch --tags && git -C {_checkout_root()} checkout {ref}")
        if not force:
            return 0
        code = _git_checkout(ref)
        if code == 0 and restart:
            print(f"gateway   : {restart_gateway()}")
        return code

    command = _command_for(mode, ref)
    if not command:
        print("\ncould not tell which tool installs ompwire. Reinstall it with:\n"
              f"  uv tool install --from {GIT_URL}@{ref} {PACKAGE}")
        return 1

    print(f"\nrunning: {' '.join(command)}")
    try:
        result = subprocess.run(command, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"update failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        print(f"update failed with exit code {result.returncode}", file=sys.stderr)
        return result.returncode
    print(f"\nupdated to {ref}.")
    if restart:
        print(f"gateway   : {restart_gateway()}")
    else:
        print("gateway   : left running (--no-restart). Restart it with: ompwire restart")
    return 0


def _git_checkout(ref: str) -> int:
    root = _checkout_root()
    try:
        subprocess.run(["git", "-C", root, "fetch", "--tags"], timeout=120, check=True)
        subprocess.run(["git", "-C", root, "checkout", ref], timeout=120, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"git checkout failed: {exc}", file=sys.stderr)
        return 1
    print(f"checkout at {ref}. Reinstall the venv if dependencies changed.")
    return 0
