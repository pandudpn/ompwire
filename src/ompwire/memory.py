"""Optional Honcho prefetch.

OMP has no external-memory hook (memory.backend accepts only mnemopi, hindsight
or local), so the harness injects recalled context itself: one Honcho context
call before a turn, rendered as a text preamble. Failures are swallowed — a
memory outage must never break a conversation, it just loses the preamble.

The response shape is read defensively because the call is not yet exercised
against a live key: any dict is searched for the first non-empty of a known set
of fields, and an unexpected payload yields "" instead of raising.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

PREFETCH_FIELDS = (
    "summary",
    "peer_representation",
    "representation",
    "context",
    "card",
    "content",
)


def _extract(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    for field in PREFETCH_FIELDS:
        value = payload.get(field)
        text = _extract(value)
        if text:
            return text
    for value in payload.values():
        text = _extract(value)
        if len(text) > 40:
            return text
    return ""


def fetch_prefetch(
    base_url: str,
    workspace: str,
    observer: str,
    target: str,
    api_key: str,
    timeout: float = 8.0,
) -> str:
    """GET /v3/workspaces/<ws>/peers/<observer>/context?target=<target>."""
    url = (
        f"{base_url.rstrip('/')}/v3/workspaces/{workspace}/peers/{observer}/context"
        f"?target={target}&summary=true&limit_to_session=false"
    )
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
        request.add_header("X-Api-Key", api_key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local base_url from config
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return ""
    return _extract(payload)


def build_preamble(text: str, max_chars: int = 4000) -> str:
    """Wrap recalled context so the agent treats it as background, not a request."""
    text = text.strip()
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + " …"
    return (
        "<recalled_context>\n"
        "Background from long-term memory. Use it if relevant; it is not a user request.\n"
        f"{text}\n"
        "</recalled_context>\n\n"
    )
