"""Optional answers the agent offers at the end of a turn.

omp has no picker a chat can answer (its `ask` dialog is TUI-only), so the
transport asks the model for a machine-readable tail instead:

    <<options: postgres | mysql | sqlite>>

The marker is stripped from the message the chat sees and turned into buttons. Any
wording the model forgets to mark still arrives as plain text, so this degrades to
today's behaviour instead of losing the question.
"""

from __future__ import annotations

import re

# A marker counts when it starts a line, as the protocol hint asks, or when it closes the whole
# message: models often end the last sentence with it ("Mau yang mana? <<options: a | b>>").
# Anywhere else it is a mention (`<<options: …>>` explained in a sentence) and stays text; that
# used to become a "…" button. Its body cannot contain `>>`, so two markers in one turn (a steered
# turn holds two answers) never merge into one.
_MARKER = r"<<\s*options?\s*:(?P<body>(?:(?!>>).)+?)>>"
OPTIONS_RE = re.compile(rf"^[ \t]*{_MARKER}|(?<!`)[ \t]*{_MARKER.replace('body', 'tail')}[ \t]*\Z",
                        re.IGNORECASE | re.DOTALL | re.MULTILINE)
PARTIAL_RE = re.compile(r"(?:^|(?<=[^\s`]))[ \t]*<<\s*options?\s*:(?:(?!>>).)*\Z",
                        re.IGNORECASE | re.DOTALL | re.MULTILINE)
MAX_OPTIONS = 6


def extract(text: str) -> tuple[str, list[str]]:
    """Return (message without the marker, options in order)."""
    matches = list(OPTIONS_RE.finditer(text))
    if not matches:
        return text, []
    match = matches[-1]  # the question is asked at the end of the turn
    body = match.group("body") or match.group("tail")
    # an option with no letter or digit ("…", "-") is a placeholder, not a choice to offer
    options = [part.strip() for part in re.split(r"\||\n", body) if re.search(r"\w", part)]
    # every marker leaves the text, not only the one that became buttons
    cleaned = OPTIONS_RE.sub("", text).strip()
    deduped: list[str] = []
    for option in options:
        if option not in deduped:
            deduped.append(option)
    return cleaned, deduped[:MAX_OPTIONS]


def question_of(text: str) -> str:
    """The question an answer's options reply to, for the heading of their card.

    The marker carries only the choices, so the card used to say "pick an answer" and the
    reader scrolled up to find what was asked. The last line ending in "?" is the question;
    without one, the closing paragraph stands in, since that is what the choices follow.
    """
    cleaned, _ = extract(text)
    paragraphs = [block for block in cleaned.split("\n\n") if block.strip()]
    if not paragraphs:
        return ""
    lines = [_unmark(line) for line in paragraphs[-1].splitlines() if line.strip()]
    asked = next((line for line in reversed(lines) if line.endswith("?")), "")
    if not asked:
        asked = " ".join(lines)
    return _headline(asked)


_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_CODE_RE = re.compile(r"`([^`]*)`")
_EMPHASIS_RE = re.compile(r"(\*\*|__)(?=\S)|(?<=\S)(\*\*|__)")


def _unmark(line: str) -> str:
    """Plain text for a card heading: a list marker and markdown go, the words stay.

    Stripping every `*` and `_` used to turn `quiet_minutes` into `quietminutes`, so the
    heading said something the answer never did. Code spans keep their content untouched and
    only their backticks leave.
    """
    text = _BULLET_RE.sub("", line.strip())
    text = _CODE_RE.sub(lambda m: m.group(1), text)  # keep the identifier, drop the backticks
    text = _EMPHASIS_RE.sub("", text)
    return text.strip()


def _headline(text: str) -> str:
    """The heading, whole. A question cut mid-sentence is a question the reader has to go
    looking for; the card is allowed the room to say it in full."""
    return " ".join(text.split())


def hide_partial(text: str, final: bool) -> str:
    """Streaming: don't show half a marker while the model is still writing it."""
    if final:
        return extract(text)[0]
    return PARTIAL_RE.sub("", OPTIONS_RE.sub("", text)).rstrip()
