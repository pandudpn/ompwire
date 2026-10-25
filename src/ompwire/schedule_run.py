"""One scheduled turn, start to finish, in a process that exits when the turn does.

This is what the timer's `oneshot` unit calls. It builds the agent, runs exactly one prompt, sends
the result to the gateway the schedule names, records what happened, and returns. There is no loop
and no daemon: when this function returns, nothing of ompwire is still running.

Sending goes through the target gateway kind's `Delivery`, which talks to the platform directly
with that gateway's credential, so a result still arrives when its poller is stopped. Only the
credential has to be valid.

Nobody is watching, so the defaults are the cautious ones: no session is persisted, a tool that
would need approval is denied rather than waited on, and the turn is bounded by the schedule's
timeout. `--yolo` per schedule opts out of the approval refusal, deliberately and visibly.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from typing import Any

from . import attachments, delivery, gateways, schedules
from .acp import AcpClient, AcpError
from .config import PROTOCOL_HINT, load_config
from .engine import child_env
from .format import now_wib, tokens

log = logging.getLogger("ompwire.schedule")


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
        # an unattended turn is the last place the bot token should be readable from a tool
        env=child_env(),
        startup_timeout=float(config.get("acp", "startup_timeout", 60.0)),
    )


def _meta(item: schedules.Schedule, run: dict[str, Any]) -> list[tuple[str, str]]:
    """What the run cost and what it touched, in the order a person reads it."""
    rows: list[tuple[str, str]] = []
    if run.get("model"):
        rows.append(("model", str(run["model"])))
    if run.get("thinking"):
        rows.append(("thinking", str(run["thinking"])))
    rows.append(("took", f"{run.get('duration', 0.0):.1f}s"))
    if run.get("tools"):
        detail = f"{run['tools']} tool" + ("" if run["tools"] == 1 else "s")
        if run.get("subagents"):
            detail += f" · subagent {run['subagents']}"
        rows.append(("used", detail))
    usage = run.get("usage") or {}
    if usage.get("inputTokens") or usage.get("outputTokens"):
        rows.append(("token", f"{tokens(usage.get('inputTokens') or 0)} in · "
                              f"{tokens(usage.get('outputTokens') or 0)} out"))
    rows.append(("sent to", f"{item.gateway} · {'ephemeral' if item.ephemeral else 'session'}"))
    return rows


async def _deliver(item: schedules.Schedule, text: str, files: list[attachments.Outgoing],
                   rejected: list[str], run: dict[str, Any] | None = None) -> int:
    run = run or {}
    ok = bool(run.get("ok", True))
    return await delivery.for_gateway(gateways.get(item.gateway)).deliver(
        _target(item),
        title=item.name,
        when=f"{schedules.describe(item)} · {item.timezone}",
        ok=ok,
        word="done" if ok else str(run.get("word") or "failed"),
        rows=_meta(item, run),
        body_markdown=text.strip() or "(the agent returned no text)",
        error=str(run.get("error") or ""),
        footer=f"{now_wib()} · {run.get('duration', 0.0):.1f}s · {item.name}",
        files=files,
        rejected=rejected,
    )


def _target(item: schedules.Schedule) -> str:
    """Where the report goes: the gateway's first allowed user, as the transport spells it."""
    gateway = gateways.get(item.gateway)
    allowed = list(gateway.allowed or [])
    if not allowed:
        raise schedules.ScheduleError(
            f"gateway '{item.gateway}' has no allowed users, so there is nowhere to send the result")
    return str(allowed[0])


def _collector(run: dict[str, Any]):
    """Count what the turn actually did, without rendering any of it."""
    def on_update(update) -> None:
        raw = update.raw or {}
        if update.kind == "tool_call":
            run["tools"] = int(run.get("tools") or 0) + 1
            tasks = (raw.get("rawInput") or {}).get("tasks")
            if isinstance(tasks, list) and tasks:
                names = [f"{t.get('name', '?')} ({t.get('agent', 'task')})"
                         for t in tasks if isinstance(t, dict)]
                run["subagents"] = ", ".join(names)[:120]
    return on_update


def run(name: str) -> int:
    item = schedules.get(name)
    config = load_config()
    try:
        target = _target(item)
    except (schedules.ScheduleError, gateways.GatewayError) as exc:
        log.error("schedule %s: %s", name, exc)
        schedules.mark(name, f"failed: {exc}")
        return 2
    log.info("schedule %s: start (gateway=%s target=%s approval=%s)", name, item.gateway, target,
             item.approval)
    client = _client(item, config)
    run_meta: dict[str, Any] = {"ok": True}
    started = time.monotonic()
    try:
        client.initialize()
        working_dir = str(client.cwd or ".")
        session = client.new_session(working_dir, [])
        session_id = str(session.get("sessionId") or session.get("session_id") or "")
        options = {str(option.get("id")): option.get("currentValue")
                   for option in (session.get("configOptions") or [])}
        run_meta["model"] = options.get("model") or config.get("agent", "model") or ""
        run_meta["thinking"] = options.get("thinking") or config.get("agent", "thinking") or ""
        client.on_update = _collector(run_meta)
        result = client.prompt(session_id, item.prompt, timeout=item.timeout)
        run_meta["duration"] = time.monotonic() - started
        run_meta["usage"] = getattr(result, "usage", {}) or {}
        text = getattr(result, "text", "") or ""
        stop = getattr(result, "stop_reason", "") or ""
        _clean, files, rejected = attachments.extract(
            text, root=working_dir, max_mb=float(config.get("files", "max_mb", 20.0)))
        sent = asyncio.run(_deliver(item, _clean, files, rejected, run_meta))
        status = f"ok {stop or 'end_turn'} · {sent} message(s)"
        schedules.mark(name, status)
        log.info("schedule %s: %s", name, status)
        return 0
    except AcpError as exc:
        run_meta.update(ok=False, word="agent failed", error=str(exc),
                        duration=time.monotonic() - started)
        log.error("schedule %s: agent failed: %s", name, exc)
        schedules.mark(name, f"failed: agent: {str(exc)[:120]}")
        _report_failure(item, run_meta)
        return 1
    except Interrupted:
        run_meta.update(ok=False, word="stopped", error="timeout, or systemd stopped the unit",
                        duration=time.monotonic() - started)
        log.warning("schedule %s: stopped mid-turn", name)
        schedules.mark(name, "stopped: timeout or systemd stop")
        _report_failure(item, run_meta)
        return 3
    except Exception as exc:
        run_meta.update(ok=False, word="failed", error=str(exc),
                        duration=time.monotonic() - started)
        log.error("schedule %s: failed: %s", name, exc)
        schedules.mark(name, f"failed: {str(exc)[:120]}")
        _report_failure(item, run_meta)
        return 1
    finally:
        try:
            client.close()
        except Exception:
            pass


def _report_failure(item: schedules.Schedule, run_meta: dict[str, Any]) -> None:
    """A run that fails at 04:00 must say so in the chat, not only in the journal."""
    try:
        asyncio.run(_deliver(item, "", [], [], run_meta))
    except Exception as exc:  # the chat may be unreachable; the journal still has the reason
        log.error("schedule %s: could not report the failure: %s", item.name, exc)


def _terminate(_signum, _frame):
    raise Interrupted()


def main(name: str) -> int:
    signal.signal(signal.SIGTERM, _terminate)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    return run(name)
