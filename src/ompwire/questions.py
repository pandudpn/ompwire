"""Optional answers the agent offers at the end of a turn.

omp has no interactive picker in ACP mode (`ask` is interactive-mode only), so the
transport asks the model for a machine-readable tail instead:

    <<options: postgres | mysql | sqlite>>

The marker is stripped from the message the chat sees and turned into buttons. Any
wording the model forgets to mark still arrives as plain text, so this degrades to
today's behaviour instead of losing the question.
"""

from __future__ import annotations

import re

# A marker counts only when it starts a line, as the protocol hint asks: text that mentions one
# (`<<options: …>>` inside a sentence) used to become a "…" button. Its body cannot contain `>>`,
# so two markers in one turn (a steered turn holds two answers) never merge into one, and text
# glued after the closing `>>` stays text.
OPTIONS_RE = re.compile(r"^[ \t]*<<\s*options?\s*:(?P<body>(?:(?!>>).)+?)>>",
                        re.IGNORECASE | re.DOTALL | re.MULTILINE)
PARTIAL_RE = re.compile(r"^[ \t]*<<\s*options?\s*:(?:(?!>>).)*\Z",
                        re.IGNORECASE | re.DOTALL | re.MULTILINE)
MAX_OPTIONS = 6


def extract(text: str) -> tuple[str, list[str]]:
    """Return (message without the marker, options in order)."""
    matches = list(OPTIONS_RE.finditer(text))
    if not matches:
        return text, []
    match = matches[-1]  # the question is asked at the end of the turn
    # an option with no letter or digit ("…", "-") is a placeholder, not a choice to offer
    options = [part.strip() for part in re.split(r"\||\n", match.group("body"))
               if re.search(r"\w", part)]
    # every marker leaves the text, not only the one that became buttons
    cleaned = OPTIONS_RE.sub("", text).strip()
    deduped: list[str] = []
    for option in options:
        if option not in deduped:
            deduped.append(option)
    return cleaned, deduped[:MAX_OPTIONS]


def hide_partial(text: str, final: bool) -> str:
    """Streaming: don't show half a marker while the model is still writing it."""
    if final:
        return extract(text)[0]
    return PARTIAL_RE.sub("", OPTIONS_RE.sub("", text)).rstrip()
