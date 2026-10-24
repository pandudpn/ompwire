"""Minimal, dependency-free Agent Client Protocol (ACP) client over stdio.

Wire facts this module encodes were verified against omp 18.12.6 (`omp --mode
acp`), which answers JSON-RPC 2.0 as newline-delimited JSON:

    initialize        -> agentInfo.name == "omp"
    session/new       -> {sessionId, configOptions[], modes}
    session/load      -> same shape, conversation resumes across processes, and the
                         transcript is REPLAYED as session/update frames (mute them)
    session/prompt    -> {stopReason, usage{inputTokens,outputTokens,...}}
    session/update    -> notifications carrying agent_message_chunk / tool_call /
                         usage_update / available_commands_update / session_info_update
    session/request_permission (server -> client) -> {outcome:{outcome,optionId}}

`--mode acp` is not listed in `omp --help`, so this contract is what pins it:
tests/test_acp_contract.py asserts today's shapes and fails loudly on upstream drift.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1


class AcpError(RuntimeError):
    """Transport or protocol failure."""


class AcpTimeout(AcpError):
    """The agent stayed silent past the deadline; ompwire gave up on it, the agent did not die."""


@dataclass(slots=True)
class Update:
    """One `session/update` notification, flattened for consumers."""

    kind: str
    session_id: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        # agent_thought_chunk carries the same content shape; it used to be dropped here, which
        # made the reasoning display look broken for a reason that had nothing to do with display
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


class AcpClient:
    """One ACP agent subprocess. Owns its pipes, pending requests and callbacks."""

    def __init__(
        self,
        command: str = "omp",
        args: Iterable[str] = ("--mode", "acp"),
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        on_update: Callable[[Update], None] | None = None,
        on_permission: Callable[[dict[str, Any]], str | None] | None = None,
        startup_timeout: float = 60.0,
    ) -> None:
        self.command = command
        self.args = list(args)
        self.cwd = cwd
        self.on_update = on_update
        self.on_permission = on_permission
        self.startup_timeout = startup_timeout

        self._proc: subprocess.Popen[str] | None = None
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._stderr_tail: list[str] = []
        self._reader: threading.Thread | None = None
        self._env = env
        self._last_activity = time.monotonic()

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
            raise AcpError(f"ACP command not found: {self.command}") from exc

        self._reader = threading.Thread(target=self._read_loop, name="acp-reader", daemon=True)
        self._reader.start()
        threading.Thread(target=self._stderr_loop, name="acp-stderr", daemon=True).start()

    def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr_tail.append(line.rstrip())
            del self._stderr_tail[:-40]

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            self._last_activity = time.monotonic()
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" in msg and "id" in msg:
                self._handle_server_request(msg)
            elif "method" in msg:
                self._handle_notification(msg)
            else:
                slot = self._pending.get(msg.get("id"))
                if slot is not None:
                    slot.put(msg)

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        if msg.get("method") != "session/update":
            return
        params = msg.get("params") or {}
        update = params.get("update") or {}
        kind = str(update.get("sessionUpdate") or "")
        if self.on_update is None or not kind:
            return
        try:
            self.on_update(Update(kind=kind, session_id=str(params.get("sessionId") or ""), raw=update))
        except Exception:  # a broken callback must not kill the reader thread
            pass

    def _handle_server_request(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "session/request_permission":
            # The answer can take minutes (a person taps a button). Waiting here would stall the
            # reader, and every reply behind it: a steer sent during an approval never returned.
            threading.Thread(target=self._answer_permission, args=(msg["id"], params),
                             name="acp-permission", daemon=True).start()
            return
        self._respond(msg["id"], None, error={"code": -32601, "message": f"unsupported client method {method}"})

    def _answer_permission(self, msg_id: Any, params: dict[str, Any]) -> None:
        option_id = None
        if self.on_permission is not None:
            option_id = self.on_permission(params)
        if option_id is None:
            result: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}
        else:
            result = {"outcome": {"outcome": "selected", "optionId": option_id}}
        try:
            self._respond(msg_id, result)
        except AcpError:
            pass  # the agent went away while the person was deciding
    # ------------------------------------------------------------------- rpc
    def _respond(self, msg_id: Any, result: Any, error: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        self._write(msg)

    def _write(self, msg: dict[str, Any]) -> None:
        with self._lock:
            # read under the lock: close() can null _proc between a check and the write
            proc = self._proc
            if proc is None or proc.stdin is None:
                raise AcpError("ACP process is not running")
            try:
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise AcpError(f"ACP process died: {self._stderr_hint()}") from exc

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any], timeout: float = 120.0,
                idle: bool = False) -> dict[str, Any]:
        """Send one request and wait for its reply.

        With `idle`, `timeout` counts silence, not total time: any line from the agent (a chunk,
        a tool update) resets it. A long turn that is still working is not a hung turn, and
        abandoning it left omp running while the chat treated it as failed.
        """
        self.start()
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            slot: queue.Queue = queue.Queue(maxsize=1)
            self._pending[msg_id] = slot
        self._last_activity = time.monotonic()
        self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        try:
            while True:
                remaining = timeout - (time.monotonic() - self._last_activity) if idle else timeout
                try:
                    reply = slot.get(timeout=max(remaining, 0.05))
                    break
                except queue.Empty as exc:
                    if idle and time.monotonic() - self._last_activity < timeout:
                        continue
                    kind = "silent for" if idle else "timed out after"
                    raise AcpTimeout(f"{method} {kind} {timeout}s ({self._stderr_hint()})") from exc
        finally:
            self._pending.pop(msg_id, None)
        if "error" in reply:
            err = reply["error"]
            detail = err.get("data")
            if isinstance(detail, dict):
                detail = detail.get("details") or detail
            suffix = f" ({detail})" if detail else ""
            raise AcpError(f"{method} failed: {err.get('message', err)}{suffix}")
        return reply.get("result") or {}

    def _stderr_hint(self) -> str:
        return " | ".join(self._stderr_tail[-3:]) if self._stderr_tail else "no stderr"

    # --------------------------------------------------------------- surface
    def initialize(self, client_capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": client_capabilities or {}},
            timeout=self.startup_timeout,
        )

    def new_session(self, cwd: str, mcp_servers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return self.request("session/new", {"cwd": cwd, "mcpServers": mcp_servers or []})

    def load_session(
        self, session_id: str, cwd: str, mcp_servers: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        return self.request(
            "session/load",
            {"sessionId": session_id, "cwd": cwd, "mcpServers": mcp_servers or []},
            timeout=self.startup_timeout,
        )

    def list_sessions(self, cwd: str) -> dict[str, Any]:
        return self.request("session/list", {"cwd": cwd})

    def close_session(self, session_id: str) -> dict[str, Any]:
        return self.request("session/close", {"sessionId": session_id})

    def set_config_option(self, session_id: str, config_id: str, value: str) -> dict[str, Any]:
        return self.request(
            "session/set_config_option",
            {"sessionId": session_id, "configId": config_id, "value": value},
        )

    def cancel(self, session_id: str) -> None:
        self.notify("session/cancel", {"sessionId": session_id})

    def steer(self, session_id: str, text: str, images: list[dict[str, Any]] | None = None,
              timeout: float = 30.0) -> tuple[str, str]:
        """Inject a message into the running turn; returns omp's (outcome, reason).

        `injected` means the turn will read it. `promptRequired` means no turn was running, so the
        caller sends it as a normal prompt. `failed` carries omp's reason. It must be a request:
        omp's `extNotification` handler is empty, so the same payload sent as a notification is
        dropped without a word.
        """
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}, *(images or [])]
        result = self.request("_session/steering", {"sessionId": session_id, "prompt": blocks},
                              timeout=timeout)
        return str(result.get("outcome") or "failed"), str(result.get("reason") or "")

    def prompt(
        self,
        session_id: str,
        text: str,
        images: list[dict[str, Any]] | None = None,
        timeout: float = 900.0,
        collect: bool = True,
    ) -> PromptResult:
        """Run one turn, streaming `session/update` through on_update as it arrives."""
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
        blocks.extend(images or [])

        seen: list[Update] = []
        chunks: list[str] = []
        previous = self.on_update

        def _capture(update: Update) -> None:
            if collect:
                seen.append(update)
                if update.kind == "agent_message_chunk":
                    chunks.append(update.text)
            if previous is not None:
                previous(update)

        self.on_update = _capture
        try:
            result = self.request(
                "session/prompt", {"sessionId": session_id, "prompt": blocks}, timeout=timeout,
                idle=True,
            )
        finally:
            self.on_update = previous
        return PromptResult(
            stop_reason=str(result.get("stopReason") or ""),
            usage=result.get("usage") or {},
            text="".join(chunks),
            updates=seen,
        )

    # ---------------------------------------------------------------- teardown
    def close(self, session_id: str | None = None, grace: float = 2.0) -> None:
        if self._proc is None:
            return
        if session_id:
            try:
                self.close_session(session_id)
            except AcpError:
                pass
        proc = self._proc
        self._proc = None
        proc.terminate()
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            time.sleep(0.1)

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def __enter__(self) -> AcpClient:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def pick_permission_option(params: dict[str, Any], policy: str = "allow") -> str | None:
    """Choose an optionId for a `session/request_permission` request."""
    options = params.get("options") or []
    if policy == "deny":
        wanted = ("reject", "deny", "cancel")
    else:
        wanted = ("allow", "accept", "approve")
    for prefix in wanted:
        for option in options:
            kind = str(option.get("kind") or "").lower()
            if kind.startswith(prefix):
                return option.get("optionId")
    return None
