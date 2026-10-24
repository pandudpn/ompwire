"""Keep the bot token out of log files.

httpx logs every request URL at INFO, and the Telegram API embeds the token in the
path (`/bot<id>:<secret>/getUpdates`), so a plain log setup writes the secret to
disk on every poll. This filter rewrites it before anything reaches a handler.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

TOKEN_RE = re.compile(r"(bot\d{6,}:)[A-Za-z0-9_-]{20,}")

# libraries that log request URLs and add nothing but noise at INFO
NOISY = ("httpx", "httpcore", "telegram.ext.Updater", "urllib3")


def redact(text: str) -> str:
    return TOKEN_RE.sub(r"\1***", text)


class RedactTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(arg) if isinstance(arg, str) else arg
                                for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact(value) if isinstance(value, str) else value
                           for key, value in record.args.items()}
        return True


def install(level: int = logging.INFO) -> None:
    """Attach the filter to every handler on the root logger, then quiet the noisy ones."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        if not any(isinstance(existing, RedactTokens) for existing in handler.filters):
            handler.addFilter(RedactTokens())
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


def scrub_file(path: str | Path) -> int:
    """Rewrite an existing log in place, replacing any token it already captured."""
    target = Path(path).expanduser()
    if not target.is_file():
        return 0
    text = target.read_text(encoding="utf-8", errors="replace")
    cleaned = redact(text)
    if cleaned == text:
        return 0
    replaced = len(TOKEN_RE.findall(text))
    temp = target.with_suffix(target.suffix + ".scrub")
    temp.write_text(cleaned, encoding="utf-8")
    temp.chmod(target.stat().st_mode & 0o777)
    temp.replace(target)
    return replaced
