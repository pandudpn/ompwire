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

# A fence opens on its own line: up to three spaces of indent (more under a list item), then three
# or more backticks or tildes and an optional language. It closes on a line of the same character
# at least as long, so a ```` block can show a ``` one inside it.
FENCE_OPEN_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>[^`\n]*)$")
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


def _escape_url(url: str) -> str:
    return html.escape(url, quote=True)


def _fence(language: str, body: str) -> str:
    language = language.strip().split()[0] if language.strip() else ""
    tag = f'<pre><code class="language-{_escape(language)}">' if language else "<pre>"
    closing = "</code></pre>" if language else "</pre>"
    return f"{tag}{_escape(body.rstrip(chr(10)))}{closing}"


def segments(text: str) -> list[tuple[str, str, str]]:
    """Split text into ("text", body, "") and ("code", body, language) runs.

    The fence's own indent is taken off every line of its body: a fence under a bullet is
    indented as markdown, not as code. An unterminated fence (a reply still streaming) runs to
    the end of the text, so the HTML stays valid."""
    out: list[tuple[str, str, str]] = []
    prose: list[str] = []
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        opened = FENCE_OPEN_RE.match(lines[index])
        if opened is None:
            prose.append(lines[index])
            index += 1
            continue
        if prose:
            out.append(("text", "\n".join(prose), ""))
            prose = []
        indent, fence = opened.group("indent"), opened.group("fence")
        body: list[str] = []
        index += 1
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                index += 1
                break
            body.append(line[len(indent):] if line.startswith(indent) else line.lstrip(" \t"))
            index += 1
        out.append(("code", "\n".join(body), opened.group("info")))
    if prose:
        out.append(("text", "\n".join(prose), ""))
    return out


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
    # Inline code is stashed first so a bold span can wrap it: `**not `x` here**`
    # used to break because the code split landed between the asterisks.
    codes: dict[str, str] = {}

    def stash(match: re.Match[str]) -> str:
        key = f"\x00{len(codes)}\x00"
        codes[key] = f"<code>{_escape(match.group(1))}</code>"
        return key

    work = INLINE_CODE_RE.sub(lambda m: stash(re.match(r"`([^`\n]+)`", m.group(0))), text)
    work = _escape(work)
    work = HEADING_RE.sub(lambda m: f"<b>{m.group(2)}</b>", work)
    work = LINK_RE.sub(lambda m: f'<a href="{_escape_url(m.group(2))}">{m.group(1)}</a>', work)
    work = BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", work)
    work = ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", work)
    work = STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", work)
    work = BULLET_RE.sub("• ", work)
    work = QUOTE_RE.sub(lambda m: f"<blockquote>{m.group(1)}</blockquote>", work)
    work = _monospace_runs(work)
    for key, rendered in codes.items():
        work = work.replace(key, rendered)
    return work


def to_html(text: str) -> str:
    """Convert agent markdown to Telegram-safe HTML."""
    if not text:
        return ""
    text = _tables_to_fences(text.replace("\r\n", "\n"))
    parts: list[str] = []
    for kind, body, language in segments(text):
        if kind == "code":
            # the newlines around a block are layout, already given by <pre>
            if parts and parts[-1].endswith("\n"):
                parts[-1] = parts[-1][:-1]
            parts.append(_fence(language, body))
        else:
            parts.append(_inline(body if not parts else "\n" + body))
    return "".join(parts)


def split_markdown(text: str, limit: int) -> list[str]:
    """Cut a long answer into messages at line breaks. A cut inside a code block closes the
    block in this message and opens it again, same fence and language, in the next, so every
    message renders its code as code instead of one unclosed block and bare lines after it.
    Splitting happens on the markdown, before any transport converts it."""

    out: list[str] = []
    current: list[str] = []
    size = 0
    opener = marker = ""  # the open fence ("```bash", "```"), or "" outside a code block

    def cut() -> None:
        nonlocal current, size
        if opener and current and current[-1] == opener and len(current) > 1:
            current.pop()  # a fence that just opened moves with its code, not left empty here
            out.append("\n".join(current).rstrip("\n"))
        else:
            out.append("\n".join(current + ([marker] if marker else [])))
        current, size = ([opener], len(opener) + 1) if opener else ([], 0)

    for line in text.split("\n"):
        closing = len(marker) + 1 if marker else 0
        while size + len(line) + closing > limit:
            if current and current != [opener]:
                cut()
                continue
            room = max(1, limit - size - closing)  # one line too long even alone
            current.append(line[:room])
            size += room + 1
            line = line[room:]
            cut()
        current.append(line)
        size += len(line) + 1
        stripped = line.strip()
        if marker:
            if stripped and set(stripped) == {marker[0]} and len(stripped) >= len(marker):
                opener = marker = ""
        elif (opened := FENCE_OPEN_RE.match(line)) is not None:
            marker = opened.group("fence")
            opener = marker + opened.group("info")
    if current:
        out.append("\n".join(current))
    return out or [""]



def split_blocks(text: str, limit: int = 3200) -> list[str]:
    """Split on blank lines so a table or a fence is never cut in half.

    Rendering happens after splitting: a table that lost its header row would be
    just a line of pipes again.
    """
    blocks: list[str] = []
    current: list[str] = []
    for block in re.split(r"\n{2,}", text):
        block = block.strip("\n")
        if not block:
            continue
        candidate = "\n\n".join(current + [block])
        if len(candidate) > limit and current:
            blocks.append("\n\n".join(current))
            current = [block]
        else:
            current.append(block)
    if current:
        blocks.append("\n\n".join(current))

    out: list[str] = []
    for block in blocks:
        while len(block) > limit:
            out.append(block[:limit])
            block = block[limit:]
        if block:
            out.append(block)
    return out


def strip_html(text: str) -> str:
    """Plain-text fallback: drop tags and unescape entities."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# What Telegram counts is the message after entities are parsed, in UTF-16 units: `&lt;` is one
# character there, and an emoji outside the BMP is two. A message over 4096 dies on
# "Message is too long" whether it was sent as HTML or as plain text.
RENDERED_TOKEN_RE = re.compile(r"<[^>]+>|&#?[0-9a-zA-Z]+;|.", re.DOTALL)
RENDERED_TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)[^>]*?(/?)>")


def visible_length(html_text: str) -> int:
    """The size Telegram measures: the text the reader gets, in UTF-16 units."""
    return len(strip_html(html_text).encode("utf-16-le")) // 2


def _token_units(token: str) -> int:
    """What one token of rendered HTML adds to the message Telegram measures."""
    if token.startswith("<"):
        return 0  # markup is not text
    if len(token) > 2 and token.startswith("&") and token.endswith(";"):
        return 1  # an entity is one character once Telegram parses it
    return 2 if ord(token) > 0xFFFF else 1


def split_html(text: str, limit: int) -> list[str]:
    """Cut pre-rendered Telegram HTML into messages of at most `limit` visible characters.

    A card (a plan checklist, a tool block) is rendered before the transport sees it, so no
    splitter upstream can be trusted to have kept it short. The cut lands just after a line
    break where one is within reach, never inside a tag or an entity, and every tag still open
    at the cut is closed here and reopened in the next message, so each message is valid HTML.
    """
    limit = max(2, limit)  # two units is one emoji: below that a message could not advance
    if visible_length(text) <= limit:
        return [text]
    tokens = RENDERED_TOKEN_RE.findall(text)
    out: list[str] = []
    prefix: list[tuple[str, str]] = []  # the tags open at this message's start, outermost first
    start = 0
    while start < len(tokens):
        stack = list(prefix)
        body: list[str] = []
        used = 0
        index = start
        break_at, break_used, break_stack = -1, 0, list(prefix)
        while index < len(tokens):
            token = tokens[index]
            units = _token_units(token)
            if used + units > limit:
                break
            body.append(token)
            used += units
            if token.startswith("<"):
                tag = RENDERED_TAG_RE.match(token)
                if tag is not None:
                    closing, name, empty = tag.group(1), tag.group(2), tag.group(3)
                    if closing:
                        for position in range(len(stack) - 1, -1, -1):
                            if stack[position][0] == name:
                                del stack[position:]
                                break
                    elif not empty:
                        stack.append((name, token))
            index += 1
            if token == "\n":
                break_at, break_used, break_stack = len(body), used, list(stack)
        if break_at > 0 and break_used * 2 >= limit:  # a break too near the start is no break
            index, stack, body = start + break_at, break_stack, body[:break_at]
        out.append("".join(markup for _name, markup in prefix) + "".join(body)
                   + "".join(f"</{name}>" for name, _markup in reversed(stack)))
        prefix = stack
        start = index
    return out


def fit_markdown(text: str, limit: int) -> list[str]:
    """Split agent markdown so every rendered message fits `limit` visible characters.

    split_markdown counts the markdown; the transport counts the rendered message, where an
    emoji is two units and a padded table is longer than its source. A part that still renders
    too long is split again with a budget scaled by how far over it went, which ends because
    every round leaves a shorter part behind.
    """
    pending, out = [text], []
    while pending:
        part = pending.pop(0)
        rendered = visible_length(to_html(part))
        if rendered <= limit or len(part) < 2:
            out.append(part)
            continue
        budget = max(1, len(part) * limit // rendered)
        pieces = split_markdown(part, min(budget, len(part) - 1))
        pending[:0] = pieces if len(pieces) > 1 else split_markdown(part, max(1, len(part) // 2))
    return out


# omp injects MCP connect failures into the agent's own message stream; they are
# diagnostics for us, not something to read in the chat. A failure ends at its
# line, or at the restart sentence, so the agent's own answer after it survives.
MCP_NOISE_RE = re.compile(
    r"MCP (?:srv|server)s?\s+[\"']?[^\n\"\'.:]+[\"']?\s+failed to connect:"
    # a failure runs to the restart sentence, the end of its line, or the end of the text
    r".*?(?:restarts the session\.|(?=\n)|$)",
    re.DOTALL,
)


def strip_agent_noise(text: str) -> str:
    return MCP_NOISE_RE.sub("", text).strip()


def to_plain(text: str) -> str:
    """Strip the markdown markers that look like noise in a plain-text fallback."""
    text = "\n".join(body for _kind, body, _language in segments(text.replace("\r\n", "\n")))
    return re.sub(r"\*\*|~~|`", "", text)
