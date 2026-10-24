"""Interactive `omp login` driven through the chat.

The CLI prints an OAuth URL, waits for the browser, then asks for the final
redirect URL or code. A pty is required because omp checks for a terminal; the
session keeps the prompts readable and forwards one line of user input at a time.
"""

from __future__ import annotations

import os
import pty
import re
import subprocess
import threading
import time
from collections.abc import Callable

from .catalog import strip_ansi

URL_RE = re.compile(r"https?://[^\s\"'<>]+")
CODE_PROMPT_RE = re.compile(r"(paste the authorization code|authorization code)", re.IGNORECASE)
SUCCESS_RE = re.compile(r"(logged in|login successful|authenticated|credentials stored)", re.IGNORECASE)
FAILURE_RE = re.compile(r"(login failed|unknown oauth provider|denied|expired)", re.IGNORECASE)


class LoginSession:
    """One `omp login` run, streamed line by line to a callback."""

    def __init__(
        self,
        target: str | None,
        on_line: Callable[[str], None],
        on_exit: Callable[[bool, str], None],
        command: str = "omp",
        menu_index: int | None = None,
    ) -> None:
        self.target = target
        self.on_line = on_line
        self.on_exit = on_exit
        self.command = command
        self.awaiting_code = False
        self.url = ""
        self.started_at = time.time()
        self.exit_code: int | None = None
        self._buffer = ""
        self._tail: list[str] = []
        self._master, slave = pty.openpty()
        argv = [command, "login"] + ([target] if target else [])
        self._proc = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave,
            close_fds=True, start_new_session=True,
        )
        os.close(slave)
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        if menu_index is not None:
            # provider id unknown: pick it from omp's own numbered menu instead
            threading.Thread(target=self._choose_from_menu, args=(menu_index,), daemon=True).start()

    def _choose_from_menu(self, index: int) -> None:
        deadline = time.time() + 20
        while time.time() < deadline and "Select a provider" not in "".join(self._tail):
            time.sleep(0.3)
        time.sleep(0.6)
        self.send(str(index))

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def _pump(self) -> None:
        while True:
            try:
                chunk = os.read(self._master, 8192)
            except OSError:
                break
            if not chunk:
                break
            text = strip_ansi(chunk.decode("utf-8", "replace"))
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._handle(line.rstrip())
        self._finish()

    def _handle(self, line: str) -> None:
        if not line.strip():
            return
        self._tail = (self._tail + [line])[-40:]
        if not self.url:
            match = URL_RE.search(line)
            if match:
                self.url = match.group(0)
        if CODE_PROMPT_RE.search(line):
            self.awaiting_code = True
        if line.strip():
            self.on_line(line)

    def _finish(self) -> None:
        if self._buffer.strip():
            self._handle(self._buffer.strip())
            self._buffer = ""
        self.exit_code = self._proc.wait()
        output = "\n".join(self._tail)
        ok = bool(SUCCESS_RE.search(output)) or (self.exit_code == 0 and not FAILURE_RE.search(output))
        self.on_exit(ok, output)

    def send(self, text: str) -> None:
        """Forward one line of user input (the authorization code, usually)."""
        if not self.alive:
            return
        try:
            os.write(self._master, (text.strip() + "\n").encode())
        except OSError:
            return
        self.awaiting_code = False

    def close(self) -> None:
        if self.alive:
            self._proc.kill()
        for handle in (self._master,):
            try:
                os.close(handle)
            except OSError:
                pass
