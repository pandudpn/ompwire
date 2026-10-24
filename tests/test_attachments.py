"""Attachments in both directions: the marker, the guard rails, the saved file."""

from __future__ import annotations

import pytest

from ompwire import attachments


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def test_a_marker_becomes_a_file_and_leaves_the_text_alone(workspace):
    chart = workspace / "chart.png"
    chart.write_bytes(b"x" * 128)
    clean, files, rejected = attachments.extract(
        f"here it is\n<<file: {chart}>>\nthanks", root=workspace)

    assert clean == "here it is\n\nthanks"
    assert [item.path for item in files] == [chart]
    assert rejected == []


def test_several_paths_in_one_marker(workspace):
    one = workspace / "a.pdf"
    two = workspace / "b.csv"
    one.write_bytes(b"1")
    two.write_bytes(b"2")
    _clean, files, _rejected = attachments.extract(
        f"<<file: {one} | {two}>>", root=workspace)
    assert [item.path.name for item in files] == ["a.pdf", "b.csv"]


def test_a_path_outside_the_working_directory_is_refused(workspace, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("nope", encoding="utf-8")
    _clean, files, rejected = attachments.extract(f"<<file: {secret}>>", root=workspace)

    assert files == []
    assert "outside" in rejected[0]


def test_a_missing_file_is_named(workspace):
    _clean, files, rejected = attachments.extract(
        f"<<file: {workspace / 'ghost.png'}>>", root=workspace)
    assert files == []
    assert "not found" in rejected[0]


def test_a_directory_is_not_a_file(workspace):
    _clean, files, rejected = attachments.extract(f"<<file: {workspace}>>", root=workspace)
    assert files == []
    assert "not a file" in rejected[0]


def test_the_size_limit_is_enforced(workspace):
    big = workspace / "big.zip"
    big.write_bytes(b"0" * (2 * 1024 * 1024))
    _clean, files, rejected = attachments.extract(f"<<file: {big}>>", root=workspace, max_mb=1)
    assert files == []
    assert "over the 1 MB limit" in rejected[0]


def test_only_five_files_per_reply(workspace):
    for index in range(7):
        (workspace / f"{index}.txt").write_text(str(index), encoding="utf-8")
    body = " | ".join(str(workspace / f"{index}.txt") for index in range(7))
    _clean, files, rejected = attachments.extract(f"<<file: {body}>>", root=workspace)

    assert len(files) == attachments.MAX_FILES
    assert any("only 5 files" in reason for reason in rejected)


def test_an_image_is_sent_as_a_photo_and_a_big_one_as_a_document(workspace, monkeypatch):
    monkeypatch.setattr(attachments, "TELEGRAM_PHOTO_MB", 1)
    small = workspace / "shot.png"
    small.write_bytes(b"x" * 10)
    large = workspace / "huge.png"
    large.write_bytes(b"x" * (2 * 1024 * 1024))

    _clean, files, _rejected = attachments.extract(
        f"<<file: {small} | {large}>>", root=workspace, max_mb=20)
    kinds = {item.path.name: item.as_photo for item in files}
    assert kinds == {"shot.png": True, "huge.png": False}


def test_a_half_written_marker_is_hidden_while_streaming():
    assert attachments.hide_partial("working <<file: /tmp/rep", final=False) == "working"
    assert attachments.hide_partial("working <<file: /tmp/report.pdf>>", final=False) == "working"


def test_the_final_pass_strips_every_marker():
    text = "a <<file: /tmp/x>> b <<file: /tmp/y>>"
    assert attachments.hide_partial(text, final=True) == "a  b"


@pytest.mark.parametrize(("given", "expected"), [
    ("../../etc/passwd", "passwd"),
    ("/tmp/report final.pdf", "report_final.pdf"),
    ("", "file"),
    ("...", "file"),
])
def test_incoming_names_cannot_escape_the_directory(given, expected):
    assert attachments.safe_name(given) == expected


def test_a_long_name_is_cut():
    assert len(attachments.safe_name("a" * 400 + ".txt")) <= 80


def test_saving_twice_does_not_overwrite(tmp_path):
    first = attachments.save(b"one", "notes.txt", tmp_path)
    second = attachments.save(b"two", "notes.txt", tmp_path)

    assert first != second
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"


def test_saving_over_the_limit_is_refused(tmp_path):
    with pytest.raises(ValueError, match="over the 1 MB limit"):
        attachments.save(b"0" * (2 * 1024 * 1024), "big.bin", tmp_path, max_mb=1)


def test_the_agent_is_told_where_the_file_landed(tmp_path):
    target = attachments.save(b"hello", "data.csv", tmp_path)
    summary = attachments.incoming_summary(target, "text/csv", "monthly numbers")

    assert "data.csv" in summary
    assert str(target) in summary
    assert "text/csv" in summary
    assert "monthly numbers" in summary


def test_a_tilde_path_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "shot.png").write_bytes(b"x")
    _clean, files, rejected = attachments.extract("<<file: ~/shot.png>>", root=tmp_path)
    assert [item.path.name for item in files] == ["shot.png"]
    assert rejected == []


def test_a_leftover_enabled_key_cannot_turn_attachments_off(tmp_path):
    """There is no switch. A config written before the key was removed still sends files."""
    import asyncio
    import types

    from ompwire.config import load_config
    from ompwire.telegram import ChatAgent

    work = tmp_path / "work"
    work.mkdir()
    chart = work / "chart.png"
    chart.write_bytes(b"x" * 64)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[agent]\ncwd = "{work}"\n[files]\nenabled = false\nmax_mb = 20\n'
        f'[session]\nstore = "{tmp_path}/sessions.db"\n', encoding="utf-8")
    config = load_config(str(cfg), env={})

    sent = []

    class Bot:
        async def send_photo(self, chat_id, photo=None, **_kw):
            sent.append("photo")

        async def send_document(self, chat_id, document=None, **_kw):
            sent.append("document")

        async def send_message(self, chat_id, text, **_kw):
            sent.append("message")

    async def run():
        agent = ChatAgent(config, chat_id=1, key="k", app=types.SimpleNamespace(bot=Bot()))
        await agent.deliver_files(f"<<file: {chart}>>")

    asyncio.run(run())
    assert sent == ["photo"]
