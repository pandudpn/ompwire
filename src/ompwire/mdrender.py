"""Markdown to Telegram-HTML rendering.

Telegram renders HTML for a small tag set (`<b>`, `<i>`, `<s>`, `<code>`,
`<pre>`, `<a>`, `<blockquote>`), so agent markdown is converted rather than
posted verbatim. Two constraints shape this module:

* Streaming: a half-received message can contain an unterminated fence or an
  unclosed `**`, so every emitted fragment must still be valid HTML. Unterminated
  fences are closed at the end of the text instead of swallowing the rest.
* Escaping order: the source is HTML-escaped first, then inline markers are
  replaced, otherwise model output containing `<` or `&` breaks parsing.
"""

from __future__ import annotations

import html
import re

FENCE_RE = re.compile(r"(```[^\n]*\n.*?(?:```|\Z))", re.DOTALL)
INLINE_CODE_RE = re.compile(r"(`[^`\n]+`)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\*)(?=\S)(.+?)(?<=\S)\*(?!\*)", re.DOTALL)
STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
BULLET_RE = re.compile(r"^[ \t]*[-+*][ \t]+", re.MULTILINE)
QUOTE_RE = re.compile(r"^[ \t]*(?:&gt;|>)[ \t]?(.+)$", re.MULTILINE)
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")
BAR_CHARS = "█▓▒░▏▎▍▌▋▊▉"
BAR_LINE_RE = re.compile(r"^\s*\S.*[" + BAR_CHARS + r"]")


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _fence(block: str) -> str:
    body = block[3:]
    language = ""
    first_line, _, rest = body.partition("\n")
    if first_line.strip() and " " not in first_line.strip() and not first_line.strip().startswith("```"):
        language = first_line.strip()
        body = rest
    if body.endswith("```"):
        body = body[:-3]
    body = body.rstrip("\n")
    tag = f'<pre><code class="language-{_escape(language)}">' if language else "<pre>"
    closing = "</code></pre>" if language else "</pre>"
    return f"{tag}{_escape(body)}{closing}"


def _plain_cell(cell: str) -> str:
    return re.sub(r"\*\*|~~|`", "", cell).strip()


def _table_block(rows: list[str]) -> str:
    """A markdown table becomes a fenced block: pipes and padding need a mono font."""
    grid: list[list[str]] = []
    for row in rows:
        if TABLE_SEP_RE.match(row):
            continue
        cells = [_plain_cell(cell) for cell in row.strip().strip("|").split("|")]
        grid.append(cells)
    if not grid:
        return ""
    width = max(len(cells) for cells in grid)
    grid = [cells + [""] * (width - len(cells)) for cells in grid]
    sizes = [max(len(cells[i]) for cells in grid) for i in range(width)]
    lines = []
    for index, cells in enumerate(grid):
        line = "  ".join(cell.ljust(sizes[i]) for i, cell in enumerate(cells)).rstrip()
        lines.append(line)
        if index == 0:
            lines.append("  ".join("-" * sizes[i] for i in range(width)))
    return "```text\n" + "\n".join(lines) + "\n```"


def _tables_to_fences(text: str) -> str:
    """Rewrite tables as fences before inline handling, so `code` inside a cell survives."""
    lines = text.split("\n")
    out: list[str] = []
    block: list[str] = []
    for line in lines:
        if TABLE_ROW_RE.match(line):
            block.append(line)
            continue
        if block:
            out.append(_table_block(block) if len(block) > 1 else block[0])
            block = []
        out.append(line)
    if block:
        out.append(_table_block(block) if len(block) > 1 else block[0])
    return "\n".join(out)


def _monospace_runs(text: str) -> str:
    """Keep bar charts and aligned blocks in a pre block, since they need a monospace font."""
    lines = text.split("\n")
    out: list[str] = []
    block: list[str] = []
    for line in lines:
        if BAR_LINE_RE.match(line):
            block.append(line)
            continue
        if block:
            out.append("<pre>" + "\n".join(block) + "</pre>")
            block = []
        out.append(line)
    if block:
        out.append("<pre>" + "\n".join(block) + "</pre>")
    return "\n".join(out)


def _inline(text: str) -> str:
    out: list[str] = []
    for piece in INLINE_CODE_RE.split(text):
        if piece.startswith("`") and piece.endswith("`") and len(piece) > 1:
            out.append(f"<code>{_escape(piece[1:-1])}</code>")
        else:
            escaped = _escape(piece)
            escaped = HEADING_RE.sub(lambda m: f"<b>{m.group(2)}</b>", escaped)
            escaped = LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', escaped)
            escaped = BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", escaped)
            escaped = ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", escaped)
            escaped = STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", escaped)
            escaped = BULLET_RE.sub("• ", escaped)
            escaped = QUOTE_RE.sub(lambda m: f"<blockquote>{m.group(1)}</blockquote>", escaped)
            escaped = _monospace_runs(escaped)
            out.append(escaped)
    return "".join(out)


def to_html(text: str) -> str:
    """Convert agent markdown to Telegram-safe HTML."""
    if not text:
        return ""
    text = _tables_to_fences(text)
    parts: list[str] = []
    for chunk in FENCE_RE.split(text):
        if not chunk:
            continue
        if chunk.startswith("```"):
            parts.append(_fence(chunk))
        else:
            parts.append(_inline(chunk))
    return "".join(parts)


def strip_html(text: str) -> str:
    """Plain-text fallback: drop tags and unescape entities."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# omp injects MCP connect failures into the agent's own message stream; they are
# diagnostics for us, not something to read in the chat.
MCP_NOISE_RE = re.compile(
    r"MCP (?:srv|server)s?\s+[\"']?[^\"'.:]+[\"']?\s+failed to connect:.*?"
    r"(?:restarts the session\.|$)",
    re.DOTALL,
)


def strip_agent_noise(text: str) -> str:
    return MCP_NOISE_RE.sub("", text).strip()


def to_plain(text: str) -> str:
    """Strip the markdown markers that look like noise in a plain-text fallback."""
    text = FENCE_RE.sub(lambda m: m.group(0).strip("`"), text)
    return re.sub(r"\*\*|~~|`", "", text)
