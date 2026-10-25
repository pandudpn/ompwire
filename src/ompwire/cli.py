"""Interactive/one-shot chat in the terminal, through the engine.

The terminal is just another `engine.Surface`: the agent process, its session, approvals, the
drain and the pacing live in `ChatAgent`, so this and the chat transports cannot drift apart.
What is left here is what a terminal does with a turn: print it.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Any

from . import engine
from .acp import AcpError
from .config import Config

OUTPUT_LINES = 3  # a tool's output here is a hint that it worked, not the report
HELP = "/new  fresh session · /stop  cancel turn · /model <id> · /mode <default|plan> · /exit"


def _dim(text: str) -> str:
    """ANSI dim, only where a terminal is actually looking at it."""
    return f"\033[2m{text}\033[0m" if sys.stdout.isatty() else text


def _index_of(labels: list[str], words: tuple[str, ...]) -> int | None:
    return next((index for index, label in enumerate(labels)
                 if any(word in label.lower() for word in words)), None)


def _picked(choice: str, labels: list[str]) -> int | None:
    """A typed number; empty means the first option, anything else is a refusal."""
    choice = choice.strip() or "1"
    if choice.isdigit():
        index = int(choice) - 1
        if 0 <= index < len(labels):
            return index
    return _index_of(labels, ("deny", "reject"))


class TerminalSurface(engine.Surface):
    """What the terminal shows of a turn: answer text, one line per tool, a file path."""

    label = "cli"
    limit = 100_000  # no rate limit here; the cap only stops one runaway trace

    def __init__(self, config: Config) -> None:
        self.show_reasoning = bool(config.get("stream", "show_reasoning", False))
        # set by run_chat once the agent exists: an approval answer goes to the engine
        self.agent: engine.ChatAgent | None = None
        self.offered: list[str] = []

    # --------------------------------------------------------------- rendering
    def trace(self, t: engine.ToolTrace) -> str:
        detail = t.command or t.title
        head = f"[tool] {t.kind}" + (f" · {detail}" if detail else "")
        if t.status:
            head += f" · {t.status}"
        output = t.output.strip().splitlines()
        if output:
            head += "\n" + "\n".join(f"    {line}" for line in output[:OUTPUT_LINES])
            if len(output) > OUTPUT_LINES:
                head += f"\n    … +{len(output) - OUTPUT_LINES} line(s)"
        return head

    def reasoning(self, text: str) -> str:
        return _dim(f"… reasoning\n{text}")

    def dropped(self, count: int) -> str:
        return f"… {count} tool call(s) not shown"

    def join(self, blocks: list[str]) -> str:
        return "\n".join(blocks)

    # ----------------------------------------------------------------- posting
    async def post(self, body: str, *, kind: str) -> Any | None:
        if not body:
            return None
        if kind == "reasoning" and not self.show_reasoning:
            return None
        # the answer arrives as markdown and is printed as written: the terminal is the renderer
        print(body, flush=True)
        # None means "not delivered" to the engine; a printed line was delivered
        return True

    @contextlib.asynccontextmanager
    async def typing(self):
        yield

    # ---------------------------------------------------------------- buttons
    async def offer_options(self, options: list[str]) -> None:
        """Number the answers and remember them: the next line typed is the answer."""
        self.offered = list(options)
        for index, option in enumerate(options, 1):
            print(f"{index}. {option}")
        print("answer with a number, or type your own reply", flush=True)

    def pick_option(self, line: str) -> str | None:
        """The option a numbered line stands for; the offer is spent either way."""
        offered, self.offered = self.offered, []
        stripped = line.strip()
        if not stripped.isdigit():
            return None
        index = int(stripped) - 1
        return offered[index] if 0 <= index < len(offered) else None

    async def ask_permission(self, token: str, kind: str, detail: str,
                             labels: list[str]) -> Any:
        if not sys.stdin.isatty():
            # one-shot runs cannot answer a prompt; approve rather than hang
            allow = _index_of(labels, ("allow",))
            refuse = _index_of(labels, ("deny", "reject"))
            await self._answer(token, allow if allow is not None else refuse)
            return token
        lines = [f"\napproval needed · {kind}"]
        if detail:
            lines.append(detail)
        lines += [f"  {index}. {label}" for index, label in enumerate(labels, 1)]
        try:
            choice = await asyncio.to_thread(input, "\n".join(lines) + "\nchoose [1]: ")
        except (EOFError, KeyboardInterrupt):
            # nobody is there to decide: refuse, the way a closed stdin always did
            await self._answer(token, _index_of(labels, ("deny", "reject")))
            return token
        await self._answer(token, _picked(choice, labels))
        return token

    async def _answer(self, token: str, index: int | None) -> None:
        """Hand the choice to the engine, which records it and rewrites the prompt."""
        if index is None or self.agent is None:
            return
        label = await self.agent.answer_permission(token, index)
        if label:
            print(f"approval · {label}", flush=True)

    async def settle_permission(self, handle: Any, kind: str, detail: str, outcome: str,
                                expired: bool) -> None:
        print(f"approval expired · {kind}" if expired else f"approval · {outcome}", flush=True)

    # ------------------------------------------------------------------ files
    async def send_file(self, path, as_photo: bool) -> None:
        print(f"[file] {path}", flush=True)

    async def offer_schedule(self, name: str, expression: str, gateway: str,
                             prompt: str) -> None:
        print(f"schedule proposed · {name} · {expression} → {gateway}\n"
              "a schedule is created from a chat transport; nothing is scheduled from here",
              flush=True)

    async def notice(self, text: str) -> Any | None:
        print(text, flush=True)
        return None


async def _repl(agent: engine.ChatAgent, surface: TerminalSurface) -> int:
    while True:
        try:
            line = (await asyncio.to_thread(input, "\n> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        # the offer a turn made is spent by the next line, whatever that line turns out to be
        answer = surface.pick_option(line)
        if line in {"/exit", "/quit"}:
            return 0
        if line == "/new":
            session_id = await agent.new_session()
            print(f"new session {session_id[:8]}", flush=True)
            continue
        if line == "/stop":
            print("cancel sent" if await agent.cancel() else "nothing running", flush=True)
            continue
        if line == "/help":
            print(HELP)
            continue
        if line.startswith("/model ") or line.startswith("/mode "):
            config_id, value = line[1:].split(maxsplit=1)
            try:
                await agent.ensure()
                await asyncio.to_thread(agent.client.set_config_option, agent.session_id,
                                        config_id, value)
            except AcpError as exc:
                print(f"error: {exc}", file=sys.stderr)
            continue
        text = answer or line
        try:
            await agent.ask(text)
        except AcpError as exc:
            print(f"error: {exc}", file=sys.stderr)


async def _chat(agent: engine.ChatAgent, surface: TerminalSurface,
                query: str | None) -> int:
    if query:
        try:
            await agent.ask(query)
        except AcpError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    return await _repl(agent, surface)


def run_chat(config: Config, key: str, query: str | None = None, cwd: str | None = None) -> int:
    surface = TerminalSurface(config)

    async def go() -> int:
        agent = engine.ChatAgent(config, key, surface)
        surface.agent = agent
        if cwd:
            agent.cwd = cwd
        # the terminal does not resume a cut turn: a row left by a crashed CLI is dropped here,
        # and Telegram's resume only ever looks at its own prefix
        agent.store.end_turn(key)
        try:
            await agent.ensure()
            info = agent.info.get("agentInfo") or {}
            print(f"ompwire · {info.get('name', 'omp')} {agent.agent_version or '?'} · "
                  f"session {agent.session_id[:8]} · cwd {agent.cwd}")
            return await _chat(agent, surface, query)
        finally:
            await agent.shutdown()

    return asyncio.run(go())
