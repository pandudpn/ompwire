"""Telegram HTML formatting: cards, tool blocks, help and schedule run cards.

Every string produced here is Telegram HTML (`esc` for values, `<pre>` grids for alignment).
It exists so the rest of the code can stay platform-neutral.
"""

from __future__ import annotations

import html
from collections.abc import Iterable

from .format import TOOL_LINE_WIDTH, _clip


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def grid(rows: Iterable[tuple[str, object]]) -> str:
    """Monospace, column-aligned key/value block."""
    pairs = [(str(key), str(value)) for key, value in rows if value not in (None, "")]
    if not pairs:
        return ""
    width = max(len(key) for key, _ in pairs)
    body = "\n".join(f"{key.ljust(width)}  {value}" for key, value in pairs)
    return f"<pre>{esc(body)}</pre>"


def card(title: str, subtitle: str = "", rows: Iterable[tuple[str, object]] = (), footer: str = "") -> str:
    """A status card: title line, aligned grid, optional footer."""
    parts = [f"<b>{esc(title)}</b>"]
    if subtitle:
        parts[0] += f" · <i>{esc(subtitle)}</i>"
    block = grid(rows)
    if block:
        parts.append(block)
    if footer:
        parts.append(f"<i>{esc(footer)}</i>")
    return "\n".join(parts)


def change(label: str, old: object, new: object, note: str = "") -> str:
    """Config-change notice: one line, old value struck through to the new one."""
    line = f"⚙️ <b>{esc(label)}</b>  <s>{esc(old)}</s> → <code>{esc(new)}</code>"
    return f"{line}\n<i>{esc(note)}</i>" if note else line


def tool_block(kind: str, title: str, command: str = "", output: str = "", status: str = "",
               subagent: str = "", steps: Iterable[str] = (), brief: str = "") -> str:
    """One tool call as a fenced block: the command and its output in one <pre>."""
    head = f"🔧 <code>{esc(kind or 'tool')}</code>"
    if status == "in_progress":
        head += " · <i>running</i>"
    elif status and status not in {"completed", "pending"}:
        head += f" · <i>{esc(status)}</i>"

    lines: list[str] = []
    if command:
        # inside a fence the command can keep its line breaks, so a heredoc or a
        # chained pipeline stays readable instead of collapsing into one long line
        head_lines = command.splitlines()[:6]
        lines.append(f"$ {_clip(head_lines[0], lines=1, width=TOOL_LINE_WIDTH + 40)}")
        lines.extend(f"  {_clip(line, lines=1, width=TOOL_LINE_WIDTH + 40)}" for line in head_lines[1:])
        if len(command.splitlines()) > 6:
            lines.append("  …")
    elif title:
        lines.append(_clip(title, lines=1, width=TOOL_LINE_WIDTH + 40))
    if subagent:
        lines.append(_clip(f"subagent: {subagent}", lines=1, width=TOOL_LINE_WIDTH + 40))
    if brief:
        lines.append(_clip(f"task: {brief}", lines=1, width=TOOL_LINE_WIDTH + 40))
    for step in list(steps)[-4:]:
        lines.append(_clip(f"  · {step}", lines=1, width=TOOL_LINE_WIDTH + 40))
    if output.strip():
        lines.append(_clip(output))
    if not lines:
        return head
    return f"{head}\n<pre>{esc(chr(10).join(lines))}</pre>"


def help_sections(sections: Iterable[tuple[str, Iterable[tuple[str, str]]]]) -> str:
    """Grouped command list: heading, then `command` with its description."""
    out: list[str] = []
    for heading, items in sections:
        out.append(f"<b>{esc(heading)}</b>")
        out.extend(f"/{name} · <i>{esc(desc)}</i>" for name, desc in items)
        out.append("")
    return "\n".join(out).rstrip()


def run_card(title: str, when: str, status: str, rows: Iterable[tuple[str, object]] = (),
             body_html: str = "", footer: str = "") -> str:
    """One scheduled run, as the chat should see it.

    A bare "schedule · name" line above an unformatted answer reads like a log tail. The same turn
    shown as a card, with what it cost and what it touched, is something worth waking up to.
    """
    head = f"🗓️ <b>{esc(title)}</b> · <i>{esc(when)}</i> · {status}"
    parts = [head]
    block = grid(rows)
    if block:
        parts.append(block)
    if body_html.strip():
        parts.append(body_html.strip())
    if footer:
        parts.append(f"<i>{esc(footer)}</i>")
    return "\n\n".join(parts)


def run_status(ok: bool, word: str) -> str:
    """A status badge that survives being read at a glance."""
    return f"{'✅' if ok else '⛔'} <b>{esc(word)}</b>"
