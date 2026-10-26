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


def hide_partial(text: str, final: bool) -> str:
    """Streaming: don't show half a marker while the model is still writing it."""
    if final:
        return extract(text)[0]
    return PARTIAL_RE.sub("", OPTIONS_RE.sub("", text)).rstrip()
