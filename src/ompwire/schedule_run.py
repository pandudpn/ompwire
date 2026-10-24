"""One scheduled turn, start to finish, in a process that exits when the turn does.

This is what the timer's `oneshot` unit calls. It builds the agent, runs exactly one prompt, sends
the result to the gateway the schedule names, records what happened, and returns. There is no loop
and no daemon: when this function returns, nothing of ompwire is still running.

Sending goes straight to the Bot API with the target gateway's token, so a result still arrives
when that gateway's poller is stopped. Only the token has to be valid.

Nobody is watching, so the defaults are the cautious ones: no session is persisted, a tool that
would need approval is denied rather than waited on, and the turn is bounded by the schedule's
timeout. `--yolo` per schedule opts out of the approval refusal, deliberately and visibly.
"""

from __future__ import annotations

import logging
import signal
import sys
from typing import Any

from . import attachments, gateways, schedules
from .acp import AcpClient, AcpError
from .config import PROTOCOL_HINT, load_config

log = logging.getLogger("ompwire.schedule")

MAX_MESSAGE = 4000  # Telegram allows 4096; leave room for the prefix


class Interrupted(Exception):
    """The unit was stopped mid-turn, most likely by the systemd timeout."""


def _client(item: schedules.Schedule, config: Any) -> AcpClient:
    args = list(config.get("acp", "args") or ["--mode", "acp"])
    if item.ephemeral and "--no-session" not in args:
        args.append("--no-session")
    # omp accepts always-ask | always-ask-cached | write | yolo. This client installs no
    # permission handler, so acp answers "cancelled" to every request it cannot answer: whatever
    # the mode still asks for is refused at once instead of waiting for a human who is asleep.
    args.append(f"--approval-mode={schedules.agent_approval(item.approval)}")
    hint = str(config.get("agent", "append_system_prompt", "") or "").strip()
    args += ["--append-system-prompt", f"{hint}\n\n{PROTOCOL_HINT}".strip()]
    cwd = str(config.get("agent", "cwd", "") or "") or "~"
    from pathlib import Path

    return AcpClient(
        command=str(config.get("acp", "command", "omp")),
        args=args,
        cwd=str(Path(cwd).expanduser()),
        startup_timeout=float(config.get("acp", "startup_timeout", 60.0)),
    )


def _chunks(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Telegram rejects long messages, so a report is split on line boundaries."""
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                parts.append(current)
            while len(line) > limit:  # a single very long line still has to be cut
                parts.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return parts or [""]


async def _deliver(item: schedules.Schedule, text: str, files: list[attachments.Outgoing],
                   rejected: list[str]) -> int:
    from telegram import Bot

    token = gateways.token(gateways.get(item.gateway))
    bot = Bot(token)
    header = f"schedule · {item.name}"
    body = text.strip() or "(no output)"
    chat = _chat_id(item)
    chunks = _chunks(body)
    sent = 0
    for index, chunk in enumerate(chunks):
        await bot.send_message(chat_id=chat, text=f"{header}\n\n{chunk}" if index == 0 else chunk)
        sent += 1
    for outgoing in files:
        with outgoing.path.open("rb") as handle:
            if outgoing.as_photo:
                await bot.send_photo(chat_id=chat, photo=handle)
            else:
                await bot.send_document(chat_id=chat, document=handle)
        sent += 1
    if rejected:
        await bot.send_message(chat_id=chat, text="skipped: " + " · ".join(rejected[:4]))
    return sent


def _chat_id(item: schedules.Schedule) -> int:
    gateway = gateways.get(item.gateway)
    allowed = list(gateway.allowed or [])
    if not allowed:
        raise schedules.ScheduleError(
            f"gateway '{item.gateway}' has no allowed users, so there is nowhere to send the result")
    return int(allowed[0])


def run(name: str) -> int:
    item = schedules.get(name)
    config = load_config()
    try:
        chat = _chat_id(item)
    except (schedules.ScheduleError, gateways.GatewayError) as exc:
        log.error("schedule %s: %s", name, exc)
        schedules.mark(name, f"failed: {exc}")
        return 2
    log.info("schedule %s: start (gateway=%s chat=%s)", name, item.gateway, chat)
    client = _client(item, config)
    try:
        client.initialize()
        working_dir = str(client.cwd or ".")
        session = client.new_session(working_dir, [])
        session_id = str(session.get("sessionId") or session.get("session_id") or "")
        result = client.prompt(session_id, item.prompt, timeout=item.timeout)
        text = getattr(result, "text", "") or ""
        stop = getattr(result, "stop_reason", "") or ""
        _clean, files, rejected = attachments.extract(
            text, root=working_dir, max_mb=float(config.get("files", "max_mb", 20.0)))
        import asyncio

        sent = asyncio.run(_deliver(item, _clean, files, rejected))
        status = f"ok {stop or 'end_turn'} · {sent} message(s)"
        schedules.mark(name, status)
        log.info("schedule %s: %s", name, status)
        return 0
    except AcpError as exc:
        log.error("schedule %s: agent failed: %s", name, exc)
        schedules.mark(name, f"failed: agent: {str(exc)[:120]}")
        return 1
    except Interrupted:
        log.warning("schedule %s: stopped mid-turn", name)
        schedules.mark(name, "stopped: timeout or systemd stop")
        return 3
    except Exception as exc:  # delivery problems must be visible, not swallowed
        log.error("schedule %s: failed: %s", name, exc)
        schedules.mark(name, f"failed: {str(exc)[:120]}")
        return 1
    finally:
        try:
            client.close()
        except Exception:
            pass


def _terminate(_signum, _frame):
    raise Interrupted()


def main(name: str) -> int:
    signal.signal(signal.SIGTERM, _terminate)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    return run(name)
