"""Discovery for the two catalogs the chat exposes: login providers and skills.

Both are read from omp itself rather than duplicated here, because omp owns the
list: providers come from its interactive login menu, authentication state from
`omp auth export` (provider names only, never the secrets), and skills from the
SKILL.md files it loads.
"""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
MENU_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$", re.MULTILINE)

# menu label -> oauth provider id, for the ones omp documents as ids. Anything not
# listed is still selectable; it is driven through the menu instead of by id.
PROVIDER_IDS = {
    "ChatGPT Plus/Pro (Codex Subscription)": "openai-codex",
    "Anthropic (Claude Pro/Max)": "anthropic",
    "Z.AI (GLM Coding Plan)": "zai",
    "Z.AI (GLM Coding Plan · Sign in)": "zai",
    "Kimi Code": "kimi-code",
    "OpenRouter": "openrouter",
    "GitHub Copilot": "github-copilot",
    "Cursor (Claude, GPT, etc.)": "cursor",
    "Devin": "devin",
    "xAI API": "xai",
    "xAI Grok OAuth (SuperGrok or X Premium+)": "xai-oauth",
}

SKILL_DIRS = ("~/.omp/agent/skills", "~/.omp/skills", ".omp/skills")


@dataclass(slots=True)
class Provider:
    index: int
    label: str
    provider_id: str
    logged_in: bool


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    path: Path


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def run_pty(command: list[str], timeout: float, cwd: str | None = None) -> str:
    """Run a command on a pty and return what it printed before `timeout`."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                            cwd=cwd, close_fds=True, start_new_session=True)
    os.close(slave)
    out: list[str] = []
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 8192)
            except OSError:
                break
            if not chunk:
                break
            out.append(chunk.decode("utf-8", "replace"))
    finally:
        proc.kill()
        proc.wait(timeout=5)
        os.close(master)
    return strip_ansi("".join(out))


def parse_menu(text: str) -> list[tuple[int, str]]:
    """Pull the numbered provider list out of `omp login`'s first screen."""
    items: list[tuple[int, str]] = []
    seen: set[int] = set()
    for match in MENU_RE.finditer(text):
        index, label = int(match.group(1)), match.group(2).strip()
        if index in seen:
            continue
        seen.add(index)
        items.append((index, label))
    return items


def logged_in_providers(command: str = "omp", timeout: float = 30.0) -> set[str]:
    """Provider ids with stored credentials. Secrets stay inside omp."""
    try:
        result = subprocess.run([command, "auth", "export"], capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()
    import json

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()
    return {str(item.get("provider")) for item in (payload.get("credentials") or [])
            if item.get("provider")}


def credentials_in_db(db_path: str = "~/.omp/agent/agent.db") -> dict[str, str]:
    """provider -> credential type, read straight from omp's own store.

    `omp login` is what writes here; this only confirms the write landed, so it
    opens the database read-only and never selects the credential payload.
    """
    import sqlite3

    path = Path(db_path).expanduser()
    if not path.is_file():
        return {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    try:
        rows = con.execute("SELECT provider, credential_type FROM auth_credentials").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return {str(provider): str(kind) for provider, kind in rows}


def credential_snapshot(db_path: str = "~/.omp/agent/agent.db") -> dict[str, tuple[str, str]]:
    """provider -> (type, updated_at). Lets a re-login show up even when the name exists."""
    import sqlite3

    path = Path(db_path).expanduser()
    if not path.is_file():
        return {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    try:
        rows = con.execute(
            "SELECT provider, credential_type, COALESCE(updated_at, '') FROM auth_credentials"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return {str(provider): (str(kind), str(updated)) for provider, kind, updated in rows}


def list_providers(command: str = "omp") -> list[Provider]:
    """The login menu plus which entries already hold credentials."""
    menu = parse_menu(run_pty([command, "login"], timeout=9.0))
    signed_in = logged_in_providers(command)
    providers: list[Provider] = []
    for index, label in menu:
        provider_id = PROVIDER_IDS.get(label, "")
        providers.append(Provider(
            index=index,
            label=label,
            provider_id=provider_id,
            logged_in=bool(provider_id) and provider_id in signed_in,
        ))
    return providers


def _frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fields: dict[str, str] = {}
    for line in text[3:end].splitlines():
        key, _, value = line.partition(":")
        if key.strip() and value.strip():
            fields[key.strip().lower()] = value.strip().strip('"\'')
    return fields


def list_skills(dirs: tuple[str, ...] = SKILL_DIRS, cwd: str | None = None) -> list[Skill]:
    """Every SKILL.md omp can load, sorted by name."""
    found: dict[str, Skill] = {}
    for raw in dirs:
        base = Path(raw).expanduser()
        if not base.is_absolute():
            base = Path(cwd or ".") / base
        if not base.is_dir():
            continue
        for path in base.glob("*/SKILL.md"):
            meta = _frontmatter(path)
            name = meta.get("name") or path.parent.name
            if name in found:
                continue
            found[name] = Skill(name=name, description=meta.get("description", ""), path=path)
    return sorted(found.values(), key=lambda skill: skill.name.lower())


def skill_body(skill: Skill, limit: int = 12000) -> str:
    """SKILL.md text without its frontmatter, clipped to a chat-sized chunk."""
    try:
        text = skill.path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"could not read {skill.path}: {exc}"
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    text = text.strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n\n… truncated"
    return text
