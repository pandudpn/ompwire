"""Interactive/one-shot chat against a single ACP session."""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass

from .acp import AcpClient, AcpError, Update, pick_permission_option
from .config import Config
from .store import SessionStore


def _env_for_child() -> dict[str, str]:
    """Pass the current environment but drop our own secrets from the child."""
    env = dict(os.environ)
    for name in ("TELEGRAM_BOT_TOKEN", "HONCHO_API_KEY", "OMPWIRE_HONCHO_API_KEY"):
        env.pop(name, None)
    return env


def _acp_args(config: Config) -> list[str]:
    raw = config.get("acp", "args", ["--mode", "acp"])
    if isinstance(raw, str):
        return shlex.split(raw)
    return [str(part) for part in (raw or [])]


@dataclass
class Printer:
    show_tool_calls: bool = True
    show_usage: bool = True
    stream: bool = True
    started: bool = False

    def on_update(self, update: Update) -> None:
        if update.kind == "agent_message_chunk" and update.text:
            if self.stream:
                sys.stdout.write(update.text)
                sys.stdout.flush()
            self.started = True
        elif update.kind == "tool_call" and self.show_tool_calls:
            title = update.tool_title or "tool"
            sys.stdout.write(f"\n  [tool] {title}\n" if self.started else f"  [tool] {title}\n")
            sys.stdout.flush()

    def end_turn(self) -> None:
        if self.started:
            sys.stdout.write("\n")
            sys.stdout.flush()


class ChatSession:
    def __init__(self, config: Config, key: str, cwd: str | None = None) -> None:
        self.config = config
        self.key = key
        self.cwd = cwd or config.get("agent", "cwd") or os.path.expanduser("~")
        self.store = SessionStore(config.get("session", "store"))
        self.printer = Printer(
            show_tool_calls=bool(config.get("stream", "show_tool_calls", True)),
            show_usage=bool(config.get("stream", "show_usage", True)),
            stream=bool(config.get("stream", "enabled", True)),
        )
        self.client = AcpClient(
            command=str(config.get("acp", "command", "omp")),
            args=_acp_args(config),
            cwd=self.cwd,
            env=_env_for_child(),
            on_update=self.printer.on_update,
            on_permission=self.ask_permission,
            startup_timeout=float(config.get("acp", "startup_timeout", 60.0)),
        )
        self.session_id: str = ""
        self.mcp_servers: list[dict] = list(config.get("mcp", "servers", []) or [])
        self.info: dict = {}

    # ------------------------------------------------------------- lifecycle
    def open(self) -> None:
        self.client.start()
        self.info = self.client.initialize()
        agent = (self.info.get("agentInfo") or {}).get("name", "?")
        version = (self.info.get("agentInfo") or {}).get("version", "?")
        record = self.store.get(self.key)
        resumed = False
        # session/load REPLAYS the existing transcript as session/update
        # notifications. Left unhandled, a resumed chat would dump every prior
        # assistant message into the room, so updates are muted while loading.
        previous_callback = self.client.on_update
        self.client.on_update = None
        try:
            if record is not None:
                try:
                    result = self.client.load_session(record.session_id, self.cwd, self.mcp_servers)
                    self.session_id = record.session_id
                    resumed = True
                except AcpError:
                    result = self.client.new_session(self.cwd, self.mcp_servers)
                    self.session_id = str(result.get("sessionId") or "")
            else:
                result = self.client.new_session(self.cwd, self.mcp_servers)
                self.session_id = str(result.get("sessionId") or "")
        finally:
            self.client.on_update = previous_callback
        if not self.session_id:
            raise AcpError("agent did not return a sessionId")
        self.options = {opt.get("id"): opt for opt in (result.get("configOptions") or [])}
        self.store.put(
            self.key,
            self.session_id,
            self.cwd,
            model=str(self.config.get("agent", "model", "")),
            mode=str(self.config.get("agent", "mode", "")),
        )
        self._apply_preferences()
        state = "resumed" if resumed else "new"
        print(f"ompwire · {agent} {version} · session {state} {self.session_id[:8]} · cwd {self.cwd}")

    def _apply_preferences(self) -> None:
        model = str(self.config.get("agent", "model", "") or "")
        mode = str(self.config.get("agent", "mode", "") or "")
        for config_id, value in (("model", model), ("mode", mode)):
            if not value:
                continue
            try:
                self.client.set_config_option(self.session_id, config_id, value)
            except AcpError as exc:
                print(f"warn: could not set {config_id}={value}: {exc}")

    def ask_permission(self, params: dict) -> str | None:
        policy = str(self.config.get("agent", "permission_policy", "ask"))
        if policy in {"allow", "deny"}:
            return pick_permission_option(params, policy)
        if not sys.stdin.isatty():
            # one-shot runs cannot answer a prompt; approve rather than hang
            return pick_permission_option(params, "allow")
        options = params.get("options") or []
        title = params.get("title") or (params.get("toolCall") or {}).get("title") or "tool call"
        print(f"\npermission requested: {title}")
        for index, option in enumerate(options, 1):
            print(f"  {index}. {option.get('name') or option.get('kind')}")
        try:
            choice = input("choose [1]: ").strip() or "1"
            return options[int(choice) - 1].get("optionId")
        except (EOFError, ValueError, IndexError):
            return None

    # ------------------------------------------------------------------ turn
    def ask(self, text: str) -> str:
        result = self.client.prompt(
            self.session_id,
            text,
            timeout=float(self.config.get("acp", "turn_timeout", 900.0)),
        )
        self.printer.end_turn()
        self.store.touch(self.key)
        if self.printer.show_usage and result.usage:
            usage = result.usage
            print(
                f"  [turn] {result.stop_reason} · in {usage.get('inputTokens', '?')}"
                f" out {usage.get('outputTokens', '?')} cached {usage.get('cachedReadTokens', 0)}"
            )
        return result.text

    def reset(self) -> None:
        self.client.close(self.session_id)
        self.store.clear(self.key)
        self.client = AcpClient(
            command=str(self.config.get("acp", "command", "omp")),
            args=_acp_args(self.config),
            cwd=self.cwd,
            env=_env_for_child(),
            on_update=self.printer.on_update,
            on_permission=self.ask_permission,
        )
        self.open()

    def close(self) -> None:
        self.client.close(self.session_id)
        self.store.close()


def run_chat(config: Config, key: str, query: str | None = None, cwd: str | None = None) -> int:
    session = ChatSession(config, key=key, cwd=cwd)
    session.open()
    if query:
        try:
            session.ask(query)
        except AcpError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            session.close()
        return 0

    try:
        while True:
            try:
                line = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in {"/exit", "/quit"}:
                break
            if line == "/new":
                session.reset()
                continue
            if line == "/help":
                print("/new  fresh session · /stop  cancel turn · /model <id> · /mode <default|plan> · /exit")
                continue
            if line.startswith("/model "):
                value = line.split(maxsplit=1)[1]
                session.client.set_config_option(session.session_id, "model", value)
                continue
            if line.startswith("/mode "):
                value = line.split(maxsplit=1)[1]
                session.client.set_config_option(session.session_id, "mode", value)
                continue
            try:
                session.ask(line)
            except AcpError as exc:
                print(f"error: {exc}", file=sys.stderr)
    finally:
        session.close()
    return 0
