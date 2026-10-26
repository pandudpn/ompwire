"""A scheduled run's report for a WhatsApp gateway, sent by the gateway that is running.

A timer runs `ompwire schedule _run` as its own process. On Telegram that process calls the Bot
API directly. A WhatsApp link is one device with one connection, and a second process opening
the same session would knock the running gateway off. So the scheduled run writes its report to
the gateway's outbox directory and the running gateway, which already holds the connection,
sends it within a couple of seconds and deletes it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .attachments import Outgoing
from .delivery import Delivery
from .wa_client import PHONE_SERVER, chunks, markdown_messages
from .wa_surface import WHATSAPP_LIMIT, card

log = logging.getLogger("ompwire.whatsapp")

# how long a report may wait for a gateway that is down before it is dropped as stale
MAX_AGE = 6 * 3600


def outbox_dir(gateway: str) -> Path:
    from .gateways import state_dir

    return state_dir(gateway) / "outbox"


@dataclass
class Report:
    path: Path
    chat: str
    messages: list[str]
    files: list[dict[str, Any]]


def render(*, title: str, when: str, ok: bool, word: str, rows: Iterable[tuple[str, str]],
           body_markdown: str, error: str, footer: str) -> list[str]:
    """The report as WhatsApp messages: the card, the answer cut on its markdown so a code
    block split across messages stays code, and the footer."""
    status = f"{'✅' if ok else '⛔'} *{word}*"
    messages = [card(f"🗓️ {title}", f"{when} · {status}", list(rows))]
    if error:
        body = [f"```{error[:400]}```"]
    else:
        body = markdown_messages(body_markdown, WHATSAPP_LIMIT) if body_markdown.strip() else []
    for part in body + ([f"_{footer}_"] if footer else []):
        # parts share a message while they fit, as the single text they used to be
        if len(messages[-1]) + 2 + len(part) <= WHATSAPP_LIMIT:
            messages[-1] += "\n\n" + part
        else:
            messages.append(part)
    return messages


class WhatsAppDelivery(Delivery):
    """Queues the report for the running gateway; returns how many messages it will send."""

    def __init__(self, gateway: str) -> None:
        self.gateway = gateway

    async def deliver(self, target: str, *, title: str, when: str, ok: bool, word: str,
                      rows: Iterable[tuple[str, str]], body_markdown: str, error: str,
                      footer: str, files: list[Outgoing], rejected: list[str]) -> int:
        from . import service

        if service.state(self.gateway) != "active":
            # nothing would send it; say so on the schedule instead of piling up reports
            raise RuntimeError(f"gateway '{self.gateway}' is not running, so the report "
                               f"cannot be sent (start it: ompwire gateway start {self.gateway})")
        messages = render(title=title, when=when, ok=ok, word=word, rows=rows,
                          body_markdown=body_markdown, error=error, footer=footer)
        if rejected:
            messages[-1] += "\n\nskipped: " + " · ".join(rejected[:4])
        directory = outbox_dir(self.gateway)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"chat": f"{target}@{PHONE_SERVER}", "messages": messages, "at": time.time(),
                   "files": [{"path": str(item.path), "photo": item.as_photo} for item in files]}
        # written aside, then renamed: the gateway never reads half a report
        name = f"{time.time_ns()}-{os.getpid()}.json"
        partial = directory / f".{name}"
        partial.write_text(json.dumps(payload), encoding="utf-8")
        partial.rename(directory / name)
        return len(messages) + len(files)


def pending(gateway: str) -> list[Report]:
    directory = outbox_dir(gateway)
    if not directory.is_dir():
        return []
    reports: list[Report] = []
    for path in sorted(directory.glob("[0-9]*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("unreadable outbox report %s: %s", path.name, exc)
            path.rename(path.with_suffix(".bad"))
            continue
        if time.time() - float(data.get("at") or 0) > MAX_AGE:
            log.warning("outbox report %s is older than %ss, dropped", path.name, MAX_AGE)
            path.unlink(missing_ok=True)
            continue
        # "text" is the shape written before reports were pre-split; still sent as before
        messages = list(data.get("messages") or chunks(str(data.get("text") or ""), WHATSAPP_LIMIT))
        reports.append(Report(path, str(data["chat"]), messages, list(data.get("files") or [])))
    return reports


async def send(transport: Any, report: Report) -> None:
    for part in report.messages:
        if await transport.send_text(report.chat, part) is None:
            raise RuntimeError("WhatsApp refused the message")
    for item in report.files:
        path = Path(item["path"])
        if path.is_file():
            await transport.send_file(report.chat, path, bool(item.get("photo")))
    report.path.unlink(missing_ok=True)
    log.info("outbox report sent chat=%s", report.chat)


def fail(report: Report, reason: str) -> None:
    """Keep a report that could not be sent out of the loop, with the reason next to it."""
    try:
        report.path.rename(report.path.with_suffix(".failed"))
        report.path.with_suffix(".reason").write_text(reason, encoding="utf-8")
    except OSError:
        report.path.unlink(missing_ok=True)
