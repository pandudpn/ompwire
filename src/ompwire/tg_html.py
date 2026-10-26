"""Telegram HTML formatting: cards, tool blocks, help and schedule run cards.

Every string produced here is Telegram HTML (`esc` for values, `<pre>` grids for alignment).
It exists so the rest of the code can stay platform-neutral.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from typing import Any

from .format import TOOL_LINE_WIDTH, _clip, tokens


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


def stats_rows(stats: dict[str, Any]) -> list[tuple[str, str]]:
    """omp's SessionStats as card rows: messages, tool calls, tokens, cost, context."""
    counts = stats.get("tokens") or {}
    rows = [
        ("messages", f"{stats.get('userMessages', 0)} you · "
                     f"{stats.get('assistantMessages', 0)} agent · "
                     f"{stats.get('totalMessages', 0)} total"),
        ("tools", f"{stats.get('toolCalls', 0)} calls"),
        ("tokens", f"in {tokens(counts.get('input'))} · out {tokens(counts.get('output'))}"),
        ("cache", f"{tokens(counts.get('cacheRead'))} read · "
                  f"{tokens(counts.get('cacheWrite'))} written"),
        ("total", tokens(counts.get("total"))),
    ]
    cost = stats.get("cost")
    if isinstance(cost, (int, float)):
        rows.append(("cost", f"${cost:.4f}"))
    premium = stats.get("premiumRequests")
    if premium:
        rows.append(("premium", f"{premium} requests"))
    usage = stats.get("contextUsage") or {}
    window = usage.get("contextWindow")
    if window:
        used = int(usage.get("tokens") or 0)
        rows.append(("context", f"{tokens(used)} / {tokens(window)} "
                                f"({used * 100 // int(window)}%)"))
    return rows


# What the tool is for, at a glance; anything not listed is a plugin or MCP tool
TOOL_ICONS = {
    "bash": "💻", "eval": "🐍", "read": "📖", "write": "✍️", "edit": "✏️", "delete": "🗑️",
    "move": "📦", "grep": "🔍", "glob": "🔍", "find": "🔍", "ast_grep": "🔍", "ast_edit": "🧬",
    "lsp": "🧭", "web_search": "🌐", "fetch": "🌐", "browser": "🌐", "github": "🐙",
    "task": "🤖", "subagent": "🤖", "wait": "⏳", "todo": "📋", "ask": "❓", "debug": "🐞",
    "checkpoint": "📍", "rewind": "⏪", "recall": "🧠", "retain": "🧠", "reflect": "🧠",
    "memory_edit": "🧠", "learn": "🎓", "manage_skill": "🎓", "security_scan": "🛡️",
    "image": "🖼️", "ida": "🔬", "computer": "🖱️",
}


def tool_icon(tool: str) -> str:
    if tool.startswith("mcp__"):
        return "🔌"
    return TOOL_ICONS.get(tool, "🔧")


def tool_label(tool: str) -> str:
    """An MCP tool arrives as mcp__<server>_<tool>; the reader wants server and tool."""
    return tool[len("mcp__"):] if tool.startswith("mcp__") else tool


def tool_block(tool: str, title: str, command: str = "", output: str = "", status: str = "",
               subagent: str = "", steps: Iterable[str] = (), brief: str = "") -> str:
    """One tool call as a fenced block: the command and its output in one <pre>."""
    tool = tool or "tool"
    head = f"{tool_icon(tool)} <code>{esc(tool_label(tool))}</code>"
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


PLAN_MARKS = {"completed": "☑️", "in_progress": "▶️"}


def plan_card(items: Iterable[Any]) -> str:
    """The agent's todo list: done items struck through, the current one in bold."""
    items = list(items)
    done = sum(1 for item in items if item.status == "completed")
    lines = [f"📋 <b>Todo</b> · <i>{done}/{len(items)} done</i>"]
    for item in items:
        text = esc(item.content)
        if item.status == "completed":
            text = f"<s>{text}</s>"
        elif item.status == "in_progress":
            text = f"<b>{text}</b>"
        lines.append(f"{PLAN_MARKS.get(item.status, '⬜')} {text}")
    return "\n".join(lines)


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


def paginate(items: list, page: int, per_page: int) -> tuple[list, int, int]:
    """Clamp a page index and slice it; page is zero-based here."""
    pages = max(1, -(-len(items) // per_page))
    page = max(0, min(page, pages - 1))
    return items[page * per_page:(page + 1) * per_page], page, pages


def two_columns(buttons: list[Any]) -> list[list[Any]]:
    """Inline keyboard rows, two buttons each."""
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
