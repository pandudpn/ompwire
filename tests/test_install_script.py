"""The installer is the first thing a stranger runs, so its surface is tested too."""

from __future__ import annotations

import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "install.sh"


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", *args], input=stdin, capture_output=True, text=True,
                          timeout=60, check=False)


def test_help_works_as_a_file():
    result = _run(str(SCRIPT), "--help")
    assert result.returncode == 0
    assert "raw.githubusercontent.com/pandudpn/ompwire/main/install.sh" in result.stdout
    assert "--no-setup" in result.stdout


def test_help_works_when_piped():
    """`curl … | bash -s -- --help` must behave: $0 is 'bash', not a path."""
    result = _run("-s", "--", "--help", stdin=SCRIPT.read_text())
    assert result.returncode == 0
    assert "Install ompwire" in result.stdout
    assert "can't read bash" not in result.stderr


def test_unknown_flag_is_an_error():
    result = _run(str(SCRIPT), "--bogus")
    assert result.returncode == 2
    assert "unknown argument: --bogus" in result.stderr


def test_ref_without_a_value_is_an_error():
    result = _run(str(SCRIPT), "--ref")
    assert result.returncode != 0
    assert "--ref needs a value" in result.stderr


def test_the_script_is_posix_shell_clean():
    """It is invoked as bash, but the body stays sh-compatible for `sh install.sh`."""
    result = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stderr
