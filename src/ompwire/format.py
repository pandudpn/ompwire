"""Shared formatting helpers: time and the tool-trace clip used by every surface.

Telegram HTML helpers live in `tg_html.py`; nothing here knows a platform.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))


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


def wib_time(iso: str) -> str:
    """An ISO UTC timestamp from omp's transcript as a WIB clock time; today drops the date."""
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(WIB)
    except ValueError:
        return "?"
    if moment.date() == datetime.now(WIB).date():
        return moment.strftime("%H:%M WIB")
    return moment.strftime("%d %b %H:%M WIB")


def tokens(count: object) -> str:
    """A token count a person can read at a glance: 940, 58.6k, 1.05M."""
    try:
        n = int(count)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    if abs(n) < 1_000:
        return str(n)
    value = n / 1_000
    for unit in ("k", "M"):
        text = f"{value:.{2 if abs(value) < 10 else 1}f}"
        if abs(float(text)) < 1_000:
            return f"{text.rstrip('0').rstrip('.')}{unit}"
        value /= 1_000
    text = f"{value:.{2 if abs(value) < 10 else 1}f}"
    return f"{text.rstrip('0').rstrip('.')}G"


# omp reports a compaction as "Tokens: 94611 -> 58555 (saved 36056)"; raw counts of that size
# are not readable in a chat, so they are rewritten with the same units as everything else.
_RAW_TOKENS = re.compile(r"Tokens: (\d+) -> (\d+) \(saved (\d+)\)")


def humanize_token_report(text: str) -> str:
    return _RAW_TOKENS.sub(
        lambda m: f"Tokens: {tokens(m[1])} → {tokens(m[2])} (saved {tokens(m[3])})", text)



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


def percent(part: int, whole: int) -> str:
    """Share of `whole` as a whole-number percentage, for the context meter."""
    return f"{part * 100 // whole}%"
