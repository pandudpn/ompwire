"""Message formatting for the chat transports.

Telegram HTML is used instead of MarkdownV2: HTML needs escaping only for three
characters, so dynamic values (paths, model ids) survive without a minefield of
backslash rules. Cards are a title line plus a monospace key/value grid, which
keeps columns aligned no matter how long an id is.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))
BAR = "─" * 18


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def uptime(seconds: float) -> str:
    """Compact, human duration: 8s, 4m 12s, 2h 05m, 3d 04h."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def now_wib() -> str:
    return datetime.now(WIB).strftime("%H:%M:%S WIB")


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


TOOL_OUTPUT_LINES = 8
TOOL_LINE_WIDTH = 120


def _clip(text: str, lines: int = TOOL_OUTPUT_LINES, width: int = TOOL_LINE_WIDTH) -> str:
    """Keep a tool trace readable: clip long lines, keep the first N lines."""
    parts = []
    for line in str(text).splitlines():
        parts.append(line if len(line) <= width else line[:width] + "…")
    if len(parts) > lines:
        parts = parts[:lines] + [f"… +{len(parts) - lines} more line(s)"]
    return "\n".join(parts)


def tool_block(kind: str, title: str, command: str = "", output: str = "", status: str = "",
               subagent: str = "") -> str:
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
