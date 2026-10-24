"""Update plumbing: channel resolution, installer detection, command building."""

from __future__ import annotations

import subprocess

from ompwire import update


def test_semver_ordering_beats_string_ordering():
    names = ["v0.9.9", "v0.10.0", "v0.2.1"]
    assert max(names, key=update._semver_key) == "v0.10.0"


def test_command_for_each_installer():
    uv_cmd = update._command_for("uv", "v1.2.3")
    assert uv_cmd[:5] == ["uv", "tool", "install", "--force", "--from"]
    assert uv_cmd[5] == f"{update.GIT_URL}@v1.2.3"
    assert uv_cmd[6] == "ompwire"

    pipx_cmd = update._command_for("pipx", "main")
    assert pipx_cmd[:3] == ["pipx", "install", "--force"]
    assert pipx_cmd[3] == f"{update.GIT_URL}@main"

    assert update._command_for("unknown", "main") == []


def test_stable_falls_back_to_tags_then_main(monkeypatch):
    monkeypatch.setattr(update, "_gh_api", lambda path: None)
    monkeypatch.setattr(update, "_remote_tags", lambda: ["v0.1.0", "v0.2.0", "v0.10.0"])
    assert update.latest_stable() == ("v0.10.0", "tag v0.10.0")

    monkeypatch.setattr(update, "_remote_tags", lambda: [])
    ref, description = update.latest_stable()
    assert ref == "main" and "no release" in description


def test_stable_prefers_a_published_release(monkeypatch):
    monkeypatch.setattr(update, "_gh_api",
                        lambda path: {"tag_name": "v9.9.9", "name": "ompwire 9.9.9"})
    monkeypatch.setattr(update, "_remote_tags", lambda: ["v0.1.0"])
    assert update.latest_stable() == ("v9.9.9", "ompwire 9.9.9")


def test_edge_reports_the_main_commit(monkeypatch):
    monkeypatch.setattr(update, "_gh_api", lambda path: {
        "sha": "abcdef1234567890",
        "commit": {"message": "feat: something\n\nbody"},
    })
    ref, description = update.latest_edge()
    assert ref == "main"
    assert "abcdef1" in description and "feat: something" in description


def test_install_mode_detects_a_checkout(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    assert update.install_mode() == "checkout"


def test_install_mode_detects_uv_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(update.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(command, **kwargs):
        stdout = "ompwire v0.2.0\n- ompwire\n" if command[1:3] == ["tool", "list"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    assert update.install_mode() == "uv"


def test_run_check_changes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(update, "install_mode", lambda: "uv")
    monkeypatch.setattr(update, "latest_stable", lambda: ("v0.2.0", "ompwire 0.2.0"))
    monkeypatch.setattr(update, "latest_edge", lambda: ("main", "main"))
    monkeypatch.setattr(update, "current_commit", lambda: "")

    def boom(*args, **kwargs):
        raise AssertionError("an update must not run with --check")

    monkeypatch.setattr(update.subprocess, "run", boom)
    assert update.run(check=True) == 0
    out = capsys.readouterr().out
    assert "channel   : stable -> ompwire 0.2.0" in out
    assert "no changes made" in out


def test_current_version_comes_from_the_code_not_stale_metadata(monkeypatch):
    """An editable checkout keeps old dist metadata; the running module wins."""
    from ompwire import __version__

    assert update.current_version() == __version__


def test_declared_version_matches_the_running_code():
    """Two version strings drift silently; this is the check that catches it."""
    import pathlib
    import tomllib

    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert update.current_version() == declared
