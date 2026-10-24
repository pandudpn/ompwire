"""`omp login` in the chat: which lines reach the user, and when it counts as done.

The real class opens a pty and a subprocess in __init__, which is exactly what a unit
test should not do. The behaviour under test — line filtering and the verdict — lives in
`_handle` and `_finish`, so instances are built without a child process.
"""

from __future__ import annotations

from ompwire import login as login_module
from ompwire.login import LoginSession


def _session(**overrides) -> tuple[LoginSession, list[str], list[tuple[bool, str]]]:
    lines: list[str] = []
    exits: list[tuple[bool, str]] = []
    session = object.__new__(LoginSession)
    session.target = None
    session.command = "omp"
    session.on_line = lines.append
    session.on_exit = lambda ok, output: exits.append((ok, output))
    session.awaiting_code = False
    session.url = ""
    session.url_announced = False
    session.code_announced = False
    session.started_at = 0.0
    session.exit_code = None
    session._buffer = ""
    session._tail = []
    for key, value in overrides.items():
        setattr(session, key, value)
    return session, lines, exits


class TestLineHandling:
    def test_the_oauth_url_is_captured_and_shown(self):
        session, lines, _exits = _session()
        session._handle("Open this URL: https://auth.example.com/device?code=abc")
        assert session.url == "https://auth.example.com/device?code=abc"
        assert len(lines) == 1

    def test_a_redrawn_url_is_not_repeated_in_the_chat(self):
        session, lines, _exits = _session()
        url_line = "Open this URL: https://auth.example.com/device?code=abc"
        session._handle(url_line)
        session._handle(url_line)
        session._handle(url_line)
        assert len(lines) == 1

    def test_the_code_prompt_is_announced_once_even_when_omp_redraws_it(self):
        session, lines, _exits = _session()
        session._handle("Paste the authorization code:")
        assert session.awaiting_code is True
        session._handle("Paste the authorization code:")
        assert len(lines) == 1

    def test_ordinary_output_passes_through(self):
        session, lines, _exits = _session()
        session._handle("Checking credentials…")
        assert lines == ["Checking credentials…"]

    def test_blank_lines_are_dropped(self):
        session, lines, _exits = _session()
        session._handle("   ")
        assert lines == []
        assert session._tail == []

    def test_the_tail_keeps_only_the_last_forty_lines(self):
        session, _lines, _exits = _session()
        for index in range(60):
            session._handle(f"line {index}")
        assert len(session._tail) == 40
        assert session._tail[-1] == "line 59"


class TestVerdict:
    def _finish(self, session, exit_code: int, tail: list[str]):
        class Proc:
            def wait(self_inner) -> int:
                return exit_code

        session._proc = Proc()
        session._tail = tail
        session._finish()
        return session

    def test_a_success_line_wins_over_a_nonzero_exit(self):
        session, _lines, exits = _session()
        self._finish(session, 1, ["logged in as you@example.com"])
        assert exits == [(True, "logged in as you@example.com")]

    def test_a_failure_line_wins_over_a_zero_exit(self):
        session, _lines, exits = _session()
        self._finish(session, 0, ["login failed: expired code"])
        assert exits[0][0] is False

    def test_a_clean_exit_without_markers_is_a_success(self):
        session, _lines, exits = _session()
        self._finish(session, 0, ["credentials stored in ~/.omp"])
        assert exits[0][0] is True

    def test_a_nonzero_exit_without_markers_is_a_failure(self):
        session, _lines, exits = _session()
        self._finish(session, 2, ["something went wrong"])
        assert exits[0][0] is False

    def test_a_partial_last_line_is_flushed_before_the_verdict(self):
        session, _lines, exits = _session()
        session._buffer = "logged in"
        self._finish(session, 0, [])
        assert exits[0][0] is True
        assert "logged in" in exits[0][1]


class TestSend:
    def test_input_is_written_and_the_prompt_state_resets(self, monkeypatch):
        session, _lines, _exits = _session(awaiting_code=True, code_announced=True)
        written: list[bytes] = []

        class Proc:
            @staticmethod
            def poll():
                return None

        session._proc = Proc()
        session._master = 7
        monkeypatch.setattr(login_module.os, "write", lambda fd, data: written.append(data))

        session.send("  abc-123  ")

        assert written == [b"abc-123\n"]
        assert session.awaiting_code is False
        assert session.code_announced is False

    def test_nothing_is_written_once_the_process_is_gone(self, monkeypatch):
        session, _lines, _exits = _session()

        class Proc:
            @staticmethod
            def poll():
                return 0

        session._proc = Proc()
        session._master = 7
        monkeypatch.setattr(login_module.os, "write",
                            lambda fd, data: _fail("must not write to a dead process"))

        session.send("abc")


def _fail(message: str) -> None:
    raise AssertionError(message)
