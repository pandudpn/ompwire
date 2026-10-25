"""Shared formatting helpers: time and the tool-trace clip used by every surface.

Telegram HTML helpers live in `tg_html.py`; nothing here knows a platform.
"""

from __future__ import annotations

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
