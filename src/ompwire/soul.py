"""SOUL.md: the persona one gateway (or `ompwire chat`) speaks with.

The wizard asks what persona the person wants, has the agent draft SOUL.md from that one line,
prints the draft, and writes the file only after the person accepts it. Generation runs `omp -p`
with every tool disabled and stdin closed, so it can never stop to ask for an approval.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from .config import SOUL_FILENAME

# a measured draft on omniroute/chat took about 140s; a slow model must not fall back to the
# template just for being slow
GENERATE_TIMEOUT = 300.0

TEMPLATE = """<!--
SOUL.md: the persona this bot speaks with. Whatever you write below this comment is added to the
agent's system prompt, ahead of the chat-format rules. HTML comments like this one are dropped.
An empty file means no persona. Changes apply when the agent process next starts.
-->
"""

PROMPT = """Write a SOUL.md file: the persona a chat assistant speaks with. It is appended to the
assistant's system prompt, so write it in second person ("You are ...").

The person describes the persona they want as: {description}

Cover, in short markdown sections: who the assistant is, its tone, how it handles questions and
disagreement, and what it avoids. Keep it under 250 words. Do not describe output formatting
(code blocks, markdown), another rule handles that. Reply with the file content only, no preamble.
"""

_FENCE = re.compile(r"^\s*```[\w-]*\s*\n(?P<body>.*?)\n\s*```\s*$", re.DOTALL)


def strip_fences(text: str) -> str:
    """Drop the ```markdown fence a chat model tends to wrap a whole file in."""
    text = text.strip()
    match = _FENCE.match(text)
    return (match.group("body") if match else text).strip()


def generate(description: str, model: str = "", command: str = "omp",
             timeout: float = GENERATE_TIMEOUT) -> str:
    """One read-only agent turn that drafts SOUL.md. Raises RuntimeError on any failure."""
    args = [command, "-p", "--no-tools", "--no-lsp", "--no-session"]
    if model:
        args.append(f"--model={model}")
    args.append(PROMPT.format(description=description.strip()))
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"no answer within {int(timeout)}s") from exc
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc
    draft = strip_fences(result.stdout or "")
    if result.returncode != 0 or not draft:
        detail = (result.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"exit code {result.returncode}")
    return draft


def write(state_dir: Path, text: str) -> Path:
    path = Path(state_dir).expanduser() / SOUL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


def wizard(state_dir: Path, model: str = "", *,
           ask: Callable[[str], str] = input,
           draft_with: Callable[[str, str], str] | None = None) -> Path | None:
    """Ask for a persona, show the drafted SOUL.md, write it only once accepted.

    y writes the draft, r redrafts (optionally with a correction), n writes the empty template.
    A failed draft also falls back to the template, so the gateway is never blocked on it.
    An existing SOUL.md is left alone unless the person asks to replace it.
    """
    draft_with = draft_with or (lambda text, chosen: generate(text, chosen))
    raw_ask = ask

    def ask(prompt: str) -> str:
        # no terminal (a pipe, a test, a service): every question reads as "no answer"
        try:
            return raw_ask(prompt)
        except (EOFError, OSError):
            return ""

    path = Path(state_dir).expanduser() / SOUL_FILENAME
    keep = path.is_file() and ask(
        f"\n{path} already exists. replace it? [y/N]: ").strip().lower() not in ("y", "yes")
    if keep:
        print(f"ok   keeping {path}")
        return path
    description = ask("\npersona for this bot, in your own words (empty = skip): ").strip()
    if not description:
        return _template(state_dir, "skipped")
    while True:
        print(f"--   drafting SOUL.md with {model or 'the agent default model'}...")
        try:
            draft = draft_with(description, model)
        except RuntimeError as exc:
            print(f"warn could not draft SOUL.md: {exc}")
            return _template(state_dir, "draft failed")
        print("\n" + "-" * 60 + f"\n{draft}\n" + "-" * 60)
        choice = ask("write this SOUL.md? [y]es / [r]edraft / [n]o: ").strip().lower()
        if choice in ("y", "yes"):
            written = write(state_dir, draft)
            print(f"ok   persona written: {written}")
            return written
        if choice in ("r", "redraft"):
            fix = ask("what should change? (empty = just try again): ").strip()
            if fix:
                description = f"{description}\nRevision requested: {fix}"
            continue
        return _template(state_dir, "not accepted")


def _template(state_dir: Path, reason: str) -> Path:
    written = write(state_dir, TEMPLATE)
    print(f"--   {reason}: empty template written to {written}, edit it any time")
    return written
