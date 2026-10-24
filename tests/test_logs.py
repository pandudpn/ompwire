"""The token must never reach a log line."""

from __future__ import annotations

import logging

from ompwire import logs

TOKEN = "123456789:AAtest-only-not-a-real-token_abcdefghij"
LINE = ("HTTP Request: POST https://api.telegram.org/bot"
        + TOKEN + "/getUpdates \"HTTP/1.1 200 OK\"")


def test_redact_replaces_the_secret_and_keeps_the_bot_id():
    cleaned = logs.redact(LINE)
    assert TOKEN not in cleaned
    assert "bot123456789:***" in cleaned


def test_filter_cleans_both_message_and_arguments():
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1,
                               "POST %s", (f"https://api.telegram.org/bot{TOKEN}/getMe",),
                               None)
    logs.RedactTokens().filter(record)
    rendered = record.getMessage()
    assert TOKEN not in rendered and "bot123456789:***" in rendered


def test_scrub_rewrites_an_existing_file(tmp_path):
    path = tmp_path / "ompwire.log"
    path.write_text("ok\n" + LINE + "\n" + LINE + "\n", encoding="utf-8")
    assert logs.scrub_file(path) == 2
    text = path.read_text(encoding="utf-8")
    assert TOKEN not in text
    assert text.count("bot123456789:***") == 2
    assert logs.scrub_file(path) == 0          # idempotent
    assert logs.scrub_file(tmp_path / "missing.log") == 0
