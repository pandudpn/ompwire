"""omp's RPC-UI mode (`omp --mode rpc-ui`) over stdio, shaped for the engine.

omp's RPC protocol is newline-delimited JSON, not JSON-RPC: commands carry an `id`, replies come
back as `{"type": "response", "id", "command", "success", "data"|"error"}`, and everything else is
an event frame. Facts this module relies on, measured against omp 18.12.6:

    ready                       first frame; advertises protocol v2 (chunked frames)
    prompt                      acknowledged on admission; `prompt_result` with the same id ends it
    steer                       queued into the running turn; `queue_update` shows it, then empties
    abort                       ends the run; its prompt_result says status "aborted"
    extension_ui_request        approvals arrive as `select`; `ask` carries the ask tool's questions
    --resume <sessionId>        reopens a transcript written by an earlier process (ACP's included)
    get_state                   sessionId, sessionFile, contextUsage, todoPhases, model, thinking

The engine was written against ACP updates and its rendering is tested in that shape, so this
client translates RPC events into the same `Update` kinds (agent_message_chunk, tool_call, plan,
usage_update, config_option_update). Everything protocol-specific stays in this file.

A plain `--mode rpc-ui` reads the agent's own `mcp.json`, so MCP servers are omp's business here:
the lazy hub starts what a turn calls instead of every server per chat.
"""

from __future__ import annotations

import base64
import contextlib
import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 2


class AgentError(RuntimeError):
    """Transport or protocol failure."""


class AgentTimeout(AgentError):
    """The agent stayed silent past the deadline; ompwire gave up on it, the agent did not die."""


@dataclass(slots=True)
class Update:
    """One event from the agent, in the shape the engine renders."""

    kind: str
    session_id: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        if self.kind in {"agent_message_chunk", "agent_thought_chunk"}:
            content = self.raw.get("content") or {}
            if content.get("type") == "text":
                return content.get("text") or ""
        return ""

    @property
    def tool_title(self) -> str:
        return str(self.raw.get("title") or self.raw.get("toolName") or "")


@dataclass(slots=True)
class PromptResult:
    stop_reason: str
    usage: dict[str, Any]
    text: str
    updates: list[Update]


# omp's ask/approval dialogs. An approval is a `select` whose options are exactly these.
APPROVE, DENY = "Approve", "Deny"


def _blocks_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(str(item.get("text") or "") for item in content
                     if isinstance(item, dict) and item.get("type") == "text")


def _usage(raw: dict[str, Any]) -> dict[str, Any]:
    """omp's per-message usage in the names the engine reports (ACP's)."""
    return {
        "inputTokens": int(raw.get("input") or 0),
        "outputTokens": int(raw.get("output") or 0),
        "cachedReadTokens": int(raw.get("cacheRead") or 0),
        "cachedWriteTokens": int(raw.get("cacheWrite") or 0),
    }


class AgentClient:
    """One `omp --mode rpc-ui` process: one session, its pipes, pending commands and callbacks."""

    def __init__(
        self,
        command: str = "omp",
        args: Iterable[str] = ("--mode", "rpc-ui"),
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        on_update: Callable[[Update], None] | None = None,
        on_dialog: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        startup_timeout: float = 60.0,
    ) -> None:
        self.command = command
        self.args = list(args)
        self.cwd = cwd
        self.on_update = on_update
        # a dialog the agent opens (an approval, an ask); returns the extension_ui_response body
        self.on_dialog = on_dialog
        self.startup_timeout = startup_timeout

        self._proc: subprocess.Popen[str] | None = None
        self._pending: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._stderr_tail: list[str] = []
        self._env = env
        self._last_activity = time.monotonic()
        self._ready = threading.Event()
        self._chunks: dict[str, list[str]] = {}
        # the prompt currently running and what it collected
        self._turn: dict[str, Any] | None = None
        self.session_id = ""
        self.session_file = ""

    # ---------------------------------------------------------------- process
    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.cwd,
                env=self._env,
            )
        except FileNotFoundError as exc:
            raise AgentError(f"agent command not found: {self.command}") from exc
        threading.Thread(target=self._read_loop, name="rpc-reader", daemon=True).start()
        threading.Thread(target=self._stderr_loop, name="rpc-stderr", daemon=True).start()
        if not self._ready.wait(self.startup_timeout):
            hint = self._stderr_hint()
            self.close()
            raise AgentError(f"agent did not start within {self.startup_timeout:.0f}s ({hint})")
        if self._proc is None or self._proc.poll() is not None:
            raise AgentError(f"agent exited at startup ({self._stderr_hint()})")
        # v2 splits an oversized frame into chunks instead of truncating it
        with contextlib.suppress(AgentError):
            self.request("negotiate_protocol", {"protocolVersion": PROTOCOL_VERSION}, timeout=10)
        # the ask tool speaks through `ask` dialogs; without this the model is told it is missing
        with contextlib.suppress(AgentError):
            self.request("set_ask_dialog", {"enabled": True}, timeout=10)

    def _stderr_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for line in proc.stderr:
            self._stderr_tail.append(line.rstrip())
            del self._stderr_tail[:-40]

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            self._last_activity = time.monotonic()
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict):
                self._dispatch(frame)
        # stdout closed: the process is gone; wake every waiter instead of letting it time out
        self._ready.set()
        for slot in list(self._pending.values()):
            slot.put({"type": "response", "success": False, "error": "agent process exited"})
        self._finish_turn("error", f"agent process exited ({self._stderr_hint()})")

    def _dispatch(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "rpc_chunk":
            frame = self._reassemble(frame)
            if frame is None:
                return
            kind = frame.get("type")
        if kind == "ready":
            self._ready.set()
            return
        if kind == "response":
            slot = self._pending.get(str(frame.get("id") or ""))
            if slot is not None:
                slot.put(frame)
            return
        if kind == "extension_ui_request":
            method = frame.get("method")
            if method in {"select", "confirm", "input", "editor", "ask"}:
                # a person may take minutes to answer; never block the reader on it
                threading.Thread(target=self._answer_dialog, args=(frame,),
                                 name="rpc-dialog", daemon=True).start()
            return
        if kind == "prompt_result":
            turn = self._turn
            if turn is not None and frame.get("id") == turn["id"]:
                error = frame.get("error") or {}
                self._finish_turn(str(frame.get("status") or "completed"),
                                  str(error.get("message") or "") if isinstance(error, dict) else "")
            return
        self._event(frame)

    def _reassemble(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        chunk_id = str(frame.get("chunkId") or "")
        parts = self._chunks.setdefault(chunk_id, [])
        if int(frame.get("index") or 0) != len(parts):
            self._chunks.pop(chunk_id, None)  # interleaved or interrupted: drop the whole frame
            return None
        parts.append(str(frame.get("data") or ""))
        if len(parts) < int(frame.get("count") or 0):
            return None
        self._chunks.pop(chunk_id, None)
        try:
            return json.loads(b"".join(base64.b64decode(part) for part in parts).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    # ------------------------------------------------------------- events
    def _emit(self, kind: str, raw: dict[str, Any]) -> None:
        update = Update(kind=kind, session_id=self.session_id, raw=raw)
        turn = self._turn
        if turn is not None:
            turn["updates"].append(update)
            if kind == "agent_message_chunk":
                turn["text"].append(update.text)
        if self.on_update is None:
            return
        try:
            self.on_update(update)
        except Exception:  # a broken callback must not kill the reader thread
            pass

    def _event(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "message_update":
            event = frame.get("assistantMessageEvent") or {}
            sub = event.get("type")
            delta = str(event.get("delta") or "")
            message_id = str(frame.get("messageId") or "")
            if sub == "text_delta" and delta:
                self._emit("agent_message_chunk", {"content": {"type": "text", "text": delta},
                                                   "messageId": message_id})
            elif sub == "thinking_delta" and delta:
                self._emit("agent_thought_chunk", {"content": {"type": "text", "text": delta},
                                                   "messageId": message_id})
        elif kind == "message_end":
            message = frame.get("message") or {}
            if message.get("role") == "assistant" and self._turn is not None:
                usage = message.get("usage") or {}
                if usage:
                    self._turn["usage"] = _usage(usage)
        elif kind == "tool_execution_start":
            args = frame.get("args") if isinstance(frame.get("args"), dict) else {}
            name = str(frame.get("toolName") or "")
            self._emit("tool_call", {
                "toolCallId": frame.get("toolCallId"),
                "toolName": name,
                "kind": name,
                "title": str(frame.get("intent") or name),
                "rawInput": args,
                "status": "in_progress",
            })
        elif kind == "tool_execution_update":
            partial = _blocks_text((frame.get("partialResult") or {}).get("content"))
            self._emit("tool_call_update", {
                "toolCallId": frame.get("toolCallId"),
                "toolName": frame.get("toolName"),
                "status": "in_progress",
                "rawOutput": {"content": [{"type": "text", "text": partial}]} if partial else {},
            })
        elif kind == "tool_execution_end":
            result = frame.get("result") or {}
            name = str(frame.get("toolName") or "")
            self._emit("tool_call_update", {
                "toolCallId": frame.get("toolCallId"),
                "toolName": name,
                "status": "failed" if frame.get("isError") else "completed",
                "rawOutput": {"content": [{"type": "text",
                                            "text": _blocks_text(result.get("content"))}]},
            })
            phases = (result.get("details") or {}).get("phases") if name == "todo" else None
            if isinstance(phases, list):
                # the todo tool carries the whole list in its result; ACP sent it as `plan`
                entries = [{"content": task.get("content"), "status": task.get("status")}
                           for phase in phases if isinstance(phase, dict)
                           for task in (phase.get("tasks") or []) if isinstance(task, dict)]
                self._emit("plan", {"entries": entries})
        elif kind in {"model_changed", "thinking_level_changed"}:
            self._emit("config_option_update", {"event": kind, **frame})
        elif kind == "command_output" and frame.get("text"):
            # a local slash command (/compact, /usage) answers here and starts no turn
            self._emit("agent_message_chunk", {"content": {"type": "text",
                                                           "text": str(frame["text"])}})
            turn = self._turn
            if turn is not None:
                turn["output"] = True
                if turn.get("local"):
                    turn["done"].set()
        elif kind == "notice":
            # info notices ("xd://: mounted …") are noise; the chat only hears problems
            if str(frame.get("level") or "info") in {"warning", "error"}:
                self._emit("notice", frame)
        elif kind == "extension_error":
            self._emit("notice", frame)
        elif kind in {"auto_retry_start", "auto_retry_end", "retry_fallback_applied",
                      "retry_fallback_succeeded"}:
            self._emit("retry", frame)
        elif kind in {"auto_compaction_start", "auto_compaction_end"}:
            self._emit("compaction", frame)
        elif kind in {"btw_delta", "btw_record"}:
            self._emit("btw", frame)
        elif kind in {"queue_update", "available_commands_update", "agent_end", "session_settled"}:
            self._emit(str(kind), frame)

    def _finish_turn(self, status: str, error: str = "") -> None:
        turn = self._turn
        if turn is None:
            return
        turn["status"], turn["error"] = status, error
        turn["done"].set()

    # ------------------------------------------------------------- dialogs
    def _answer_dialog(self, frame: dict[str, Any]) -> None:
        answer: dict[str, Any] | None = None
        if self.on_dialog is not None:
            try:
                answer = self.on_dialog(frame)
            except Exception:
                answer = None
        body = answer if answer is not None else {"cancelled": True}
        with contextlib.suppress(AgentError):
            self._write({"type": "extension_ui_response", "id": frame.get("id"), **body})

    # ------------------------------------------------------------------- wire
    def _write(self, msg: dict[str, Any]) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.stdin is None:
                raise AgentError("agent process is not running")
            try:
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise AgentError(f"agent process died: {self._stderr_hint()}") from exc

    def request(self, command: str, params: dict[str, Any] | None = None,
                timeout: float = 120.0) -> Any:
        """Send one command and wait for its response; the response's `data`."""
        self.start()
        with self._lock:
            self._next_id += 1
            msg_id = f"ow{self._next_id}"
            slot: queue.Queue = queue.Queue(maxsize=1)
            self._pending[msg_id] = slot
        try:
            self._write({"id": msg_id, "type": command, **(params or {})})
            try:
                reply = slot.get(timeout=timeout)
            except queue.Empty as exc:
                raise AgentTimeout(f"{command} timed out after {timeout:.0f}s "
                                 f"({self._stderr_hint()})") from exc
        finally:
            self._pending.pop(msg_id, None)
        if not reply.get("success"):
            raise AgentError(f"{command} failed: {reply.get('error') or 'unknown error'}")
        return reply.get("data")

    def _stderr_hint(self) -> str:
        return " | ".join(self._stderr_tail[-3:]) if self._stderr_tail else "no stderr"

    # ---------------------------------------------------------------- surface
    def state(self) -> dict[str, Any]:
        data = self.request("get_state") or {}
        self.session_id = str(data.get("sessionId") or self.session_id)
        self.session_file = str(data.get("sessionFile") or self.session_file)
        return data

    def models(self) -> list[dict[str, Any]]:
        data = self.request("get_available_models", timeout=60) or {}
        return list(data.get("models") or []) if isinstance(data, dict) else list(data or [])

    def thinking_levels(self) -> list[str]:
        data = self.request("get_available_thinking_levels") or {}
        return [str(level) for level in (data.get("levels") or [])]

    def set_model(self, provider: str, model_id: str) -> dict[str, Any]:
        return self.request("set_model", {"provider": provider, "modelId": model_id}, timeout=60) or {}

    def set_thinking(self, level: str) -> None:
        self.request("set_thinking_level", {"level": level})

    def new_session(self) -> str:
        data = self.request("new_session") or {}
        if data.get("cancelled"):
            raise AgentError("new_session was cancelled")
        return str(self.state().get("sessionId") or "")

    def fork(self) -> str:
        data = self.request("fork", timeout=60) or {}
        if data.get("cancelled"):
            raise AgentError("fork was cancelled (is the session persisted?)")
        return str(self.state().get("sessionId") or "")

    def compact(self, instructions: str = "") -> dict[str, Any]:
        params = {"customInstructions": instructions} if instructions else {}
        return self.request("compact", params, timeout=600) or {}

    def abort(self) -> None:
        with contextlib.suppress(AgentError):
            self._write({"id": "abort", "type": "abort"})

    def steer(self, text: str, images: list[dict[str, Any]] | None = None,
              timeout: float = 30.0) -> tuple[str, str]:
        """Inject a message into the running turn: ("injected" | "promptRequired" | "failed", reason)."""
        turn = self._turn
        if turn is None or turn["done"].is_set():
            return "promptRequired", ""
        params: dict[str, Any] = {"message": text}
        if images:
            params["images"] = images
        try:
            self.request("steer", params, timeout=timeout)
        except AgentError as exc:
            return "failed", str(exc)
        return "injected", ""

    def follow_up(self, text: str, images: list[dict[str, Any]] | None = None,
                  timeout: float = 30.0) -> tuple[str, str]:
        """Queue a message to run after the current turn: same contract as steer."""
        turn = self._turn
        if turn is None or turn["done"].is_set():
            return "promptRequired", ""
        params: dict[str, Any] = {"message": text}
        if images:
            params["images"] = images
        try:
            self.request("follow_up", params, timeout=timeout)
        except AgentError as exc:
            return "failed", str(exc)
        return "injected", ""

    def session_stats(self) -> dict[str, Any]:
        return self.request("get_session_stats") or {}

    def set_session_name(self, name: str) -> None:
        self.request("set_session_name", {"name": name})

    def export_html(self, path: str) -> str:
        """Export the session as HTML to an absolute `path`; returns the file written."""
        data = self.request("export_html", {"outputPath": path}, timeout=120) or {}
        return str(data.get("path") or path)

    def btw(self, question: str) -> dict[str, Any]:
        """Ask a side question that does not enter the main context; returns its record."""
        return self.request("btw", {"question": question}) or {}

    def handoff(self, instructions: str = "") -> dict[str, Any]:
        params = {"customInstructions": instructions} if instructions else {}
        return self.request("handoff", params, timeout=120) or {}

    def prompt(self, text: str, images: list[dict[str, Any]] | None = None,
               timeout: float = 900.0) -> PromptResult:
        """Run one turn, streaming updates through on_update; `timeout` counts silence."""
        self.start()
        with self._lock:
            self._next_id += 1
            msg_id = f"ow{self._next_id}"
        turn: dict[str, Any] = {"id": msg_id, "updates": [], "text": [], "usage": {},
                                "done": threading.Event(), "status": "", "error": ""}
        self._turn = turn
        slot: queue.Queue = queue.Queue(maxsize=1)
        self._pending[msg_id] = slot
        params: dict[str, Any] = {"message": text}
        if images:
            params["images"] = images
        self._last_activity = time.monotonic()
        try:
            self._write({"id": msg_id, "type": "prompt", **params})
            # the acknowledgement: refused prompts end here, admitted ones run to prompt_result
            try:
                ack = slot.get(timeout=max(timeout, self.startup_timeout))
            except queue.Empty as exc:
                raise AgentTimeout(f"prompt was not admitted ({self._stderr_hint()})") from exc
            if not ack.get("success"):
                raise AgentError(f"prompt failed: {ack.get('error') or 'refused'}")
            if (ack.get("data") or {}).get("agentInvoked") is False:
                # a local command (/compact, /context) is acknowledged first and answers after,
                # in a command_output frame; ending here would leak that into the next turn
                turn["local"] = True
                turn["status"] = "completed"
                if turn.get("output"):  # the reader may already have passed the output on
                    turn["done"].set()
                if not turn["done"].wait(timeout):
                    raise AgentTimeout(f"{text.split()[0]} gave no output in {timeout:.0f}s")
            while not turn["done"].wait(0.25):
                if time.monotonic() - self._last_activity > timeout:
                    raise AgentTimeout(f"prompt silent for {timeout:.0f}s ({self._stderr_hint()})")
        finally:
            self._pending.pop(msg_id, None)
            if self._turn is turn:
                self._turn = None
        status = str(turn["status"] or "completed")
        if status == "error":
            raise AgentError(turn["error"] or "the turn failed")
        return PromptResult(
            stop_reason={"completed": "end_turn", "aborted": "cancelled"}.get(status, status),
            usage=turn["usage"],
            text="".join(turn["text"]),
            updates=turn["updates"],
        )

    # ---------------------------------------------------------------- teardown
    def close(self, grace: float = 3.0) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        # closing stdin is omp's clean shutdown: queued work is drained, the session flushed
        for stream in (proc.stdin,):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=grace)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def __enter__(self) -> AgentClient:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def is_approval(frame: dict[str, Any]) -> bool:
    """omp's tool approval: a `select` titled "Allow tool: …" with Approve/Deny."""
    options = [str(option) for option in (frame.get("options") or [])]
    return frame.get("method") == "select" and APPROVE in options and DENY in options


def approval_detail(frame: dict[str, Any], limit: int = 600) -> tuple[str, str]:
    """(tool, what it will do) from the approval's title. omp writes it as lines:
    "Allow tool: bash\\nReason: …\\nCommand: rm -rf …" or
    "Allow tool: write\\nPath: /x\\nContent:\\n<the whole file>"."""
    tool, command, path, rest = "tool", "", "", []
    for line in str(frame.get("title") or "").splitlines():
        if line.startswith("Allow tool:"):
            tool = line.split(":", 1)[1].strip() or tool
        elif line.startswith("Command:"):
            command = line.split(":", 1)[1].strip()
        elif line.startswith("Path:"):
            path = line.split(":", 1)[1].strip()
        else:
            rest.append(line)
    if command:
        return tool, command
    body = "\n".join(rest).strip()
    if len(body) > limit:
        body = body[:limit].rstrip() + f"\n… ({len(body) - limit} more characters)"
    return tool, "\n".join(part for part in (path, body) if part)
