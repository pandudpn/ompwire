"""What arrives in a chat that is not text: a clip, a voice note, a sticker, a pin.

A video sent to a gateway used to reach no handler at all. Telegram accepted it, the chat showed
it as delivered, and nothing ever came back — no answer, no error, no log line beyond the generic
`update ... text=''`. These pin the reply that replaces that silence.
"""

from __future__ import annotations

import asyncio
import types

from telegram.error import TelegramError

from ompwire import telegram
from ompwire.config import load_config


class Bot:
    """The Bot API, as far as a media message touches it: nothing is sent, everything is kept."""

    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, _chat_id, text, **_kw):
        self.sent.append(text)
        return types.SimpleNamespace(edit_text=self._record, reply_text=self._record)

    async def _record(self, body, **_kw):
        self.sent.append(body)


def _config(tmp_path):
    config = load_config(env={})
    config.data["session"]["store"] = str(tmp_path / "s.db")
    config.data["gateway"] = {"name": "telegram", "allowed_users": [7]}
    config.data["files"]["incoming_dir"] = str(tmp_path / "incoming")
    return config


def _transport(config):
    transport = telegram.Transport(config)
    transport.app = types.SimpleNamespace(bot=Bot())
    return transport


class Media:
    """A Telegram media object: file_size, mime, and get_file returning a file with bytes."""

    def __init__(self, raw: bytes = b"clip-bytes", mime: str = "video/mp4", size: int | None = None,
                 **extra):
        self.raw = raw
        self.mime_type = mime
        self.file_size = len(raw) if size is None else size
        for key, value in extra.items():
            setattr(self, key, value)

    async def get_file(self, **_kw):
        raw = self.raw

        class File:
            async def download_as_bytearray(self, **_kw):
                return bytearray(raw)

        return File()


def _message(**attach):
    """A chat message carrying one attachment, plus everything `_refusal` reads from it."""
    base = dict(text=None, caption=None, entities=None, caption_entities=None,
                reply_to_message=None, from_user=types.SimpleNamespace(id=7),
                photo=None, video=None, video_note=None, animation=None, voice=None,
                audio=None, sticker=None, document=None, location=None, venue=None,
                contact=None, poll=None, dice=None, story=None, paid_media=None,
                reply_text=None)
    base.update(attach)
    return types.SimpleNamespace(**base)


def _update(transport, message, chat_id: int = 7):
    async def reply_text(body, **_kw):
        transport.app.bot.sent.append(body)

    message.reply_text = reply_text
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=chat_id),
        effective_chat=types.SimpleNamespace(id=chat_id, type="private"),
        effective_message=message, callback_query=None)


def _run_media(transport, message, monkeypatch, chat_id: int = 7):
    """Hand one message to the handler; the prompt it would have submitted is the observation."""
    captured: list[str] = []

    async def fake_submit(_chat_id, payload, _message):
        captured.append(payload.text)

    monkeypatch.setattr(transport, "submit", fake_submit)
    asyncio.run(transport.on_media(_update(transport, message, chat_id), None))
    return captured


def test_a_video_is_saved_and_handed_to_the_agent_as_a_path(tmp_path, monkeypatch):
    """The silence that started this: a clip arrived, no handler took it, nothing was answered."""
    transport = _transport(_config(tmp_path))
    captured = _run_media(transport, _message(video=Media(duration=7, width=1920, height=1080)),
                          monkeypatch)
    assert len(captured) == 1
    prompt = captured[0]
    assert prompt.startswith("[video] ")
    assert "video/mp4" in prompt
    assert "7s" in prompt and "1920x1080" in prompt
    saved = tmp_path / "incoming" / "7"
    files = list(saved.iterdir())
    assert len(files) == 1 and files[0].read_bytes() == b"clip-bytes"
    assert str(files[0]) in prompt


def test_a_voice_note_and_a_sticker_are_read_as_their_own_kind(tmp_path, monkeypatch):
    transport = _transport(_config(tmp_path))
    for attach, kind, mime in (({"voice": Media(mime="audio/ogg", duration=3)}, "voice note",
                                "audio/ogg"),
                               ({"sticker": Media(mime="image/webp", emoji="🔥", width=512,
                                                  height=512, is_animated=False, is_video=False)},
                                "sticker", "image/webp")):
        transport.app.bot.sent.clear()
        captured = _run_media(transport, _message(**attach), monkeypatch)
        assert captured and captured[0].startswith(f"[{kind}] "), captured
        assert mime in captured[0]


def test_a_sticker_keeps_the_extension_its_format_needs(tmp_path, monkeypatch):
    """A .webp sticker saved as an opaque name cannot be opened by the shell the agent has."""
    transport = _transport(_config(tmp_path))
    for extra, expected in (({"is_animated": True}, ".tgs"), ({"is_video": True}, ".webm"),
                            ({"is_animated": False, "is_video": False}, ".webp")):
        media = Media(mime="image/webp", width=1, height=1, **extra)
        _run_media(transport, _message(sticker=media), monkeypatch)
        names = [path.name for path in (tmp_path / "incoming" / "7").iterdir()]
        assert any(name.endswith(expected) for name in names), (expected, names)


def test_a_caption_travels_with_the_clip(tmp_path, monkeypatch):
    transport = _transport(_config(tmp_path))
    captured = _run_media(transport, _message(video=Media(), caption="cek detik ke-3"),
                          monkeypatch)
    assert captured and "cek detik ke-3" in captured[0]


def test_a_timedelta_duration_is_read_as_seconds(tmp_path, monkeypatch):
    """PTB_TIMEDELTA, set by this module, hands duration over as a timedelta, not an int."""
    from datetime import timedelta

    transport = _transport(_config(tmp_path))
    captured = _run_media(transport, _message(video=Media(duration=timedelta(seconds=12))),
                          monkeypatch)
    assert captured and "12s" in captured[0]
    assert "0:00:12" not in captured[0]


def test_a_clip_over_the_telegram_limit_is_named_not_dropped(tmp_path, monkeypatch):
    """getFile tops out at 20 MB; saying so beats a download that dies without a word."""
    transport = _transport(_config(tmp_path))
    media = Media(size=(telegram.TELEGRAM_GETFILE_MB + 5) * 1024 * 1024)
    captured = _run_media(transport, _message(video=media), monkeypatch)
    assert captured == []
    assert transport.app.bot.sent and "too large" in transport.app.bot.sent[-1]


def test_a_failed_download_still_answers(tmp_path, monkeypatch):
    transport = _transport(_config(tmp_path))
    media = Media()

    async def boom(**_kw):
        raise TelegramError("file is too big")

    media.get_file = boom
    captured = _run_media(transport, _message(video=media), monkeypatch)
    assert captured == []
    assert transport.app.bot.sent and "could not fetch the video" in transport.app.bot.sent[-1]


def test_a_pin_poll_or_contact_gets_a_reply_instead_of_silence(tmp_path, monkeypatch):
    transport = _transport(_config(tmp_path))
    for attach, word in (({"location": object()}, "location pin"), ({"poll": object()}, "poll"),
                         ({"contact": object()}, "contact")):
        transport.app.bot.sent.clear()
        message = _message(**attach)
        asyncio.run(transport.on_unsupported(_update(transport, message), None))
        assert transport.app.bot.sent, attach
        assert word in transport.app.bot.sent[-1]


def test_the_update_log_names_what_arrived(tmp_path, monkeypatch, caplog):
    """`text=''` read the same as an empty message; the kind is what made this hard to spot."""
    import logging

    transport = _transport(_config(tmp_path))
    with caplog.at_level(logging.INFO, logger="ompwire.telegram"):
        asyncio.run(transport.log_update(
            _update(transport, _message(video=Media())), None))
    assert any("attachment=video" in record.getMessage() for record in caplog.records)


def test_none_of_these_handlers_take_a_plain_text_message(tmp_path):
    """The text path must not be stolen: on_media and on_unsupported both decline plain prose."""
    from telegram import Chat, Message, Update

    transport = _transport(_config(tmp_path))
    text_message = Message(message_id=1, date=None, chat=Chat(id=7, type="private"),
                           from_user=types.SimpleNamespace(id=7), text="halo")
    assert not telegram.MEDIA_FILTER.check_update(Update(update_id=1, message=text_message))
    assert transport._media(types.SimpleNamespace(
        video=None, video_note=None, animation=None, voice=None, audio=None, sticker=None)) == (
        None, "")


def test_every_new_handler_ignores_an_edited_message(tmp_path):
    """An edit must not run a turn a second time; docs/guide.md says so, and a rewrap broke it.

    `new & filters.PHOTO | filters.Document.IMAGE` parses as `(new & PHOTO) | Document.IMAGE`
    because `&` binds tighter, so an edited image document escaped the `new` scope and matched
    on_photo. Probed here through the real registered handlers, not a re-typed filter.
    """
    from telegram import Chat, Document, Message, PhotoSize, Update
    from telegram.ext import Application

    transport = _transport(_config(tmp_path))
    app = Application.builder().token("1:x").build()
    transport.register(app)
    # Every handler in group 0, whatever its type: picking the MessageHandlers by class would
    # leave a future kind (a CallbackQueryHandler, a new update type) silently unguarded.
    # Group -1 is `log_update`, which is meant to see edits — it only logs.
    ours = [handler for group, handlers in app.handlers.items() if group != -1
            for handler in handlers]
    assert len(ours) >= 4, [type(handler).__name__ for handler in ours]
    assert transport.log_update in [handler.callback for handler in app.handlers[-1]]

    chat, user = Chat(id=7, type="private"), types.SimpleNamespace(id=7)
    image_doc = Message(message_id=1, date=None, chat=chat, from_user=user, caption="x",
                        document=Document(file_id="d", file_unique_id="u", mime_type="image/png"))
    video = Message(message_id=2, date=None, chat=chat, from_user=user, video=Media())
    photo = Message(message_id=3, date=None, chat=chat, from_user=user,
                    photo=(PhotoSize(file_id="p", file_unique_id="p", width=9, height=9),))

    for message, label in ((image_doc, "image document"), (video, "video"), (photo, "photo")):
        assert any(handler.check_update(Update(update_id=9, message=message)) for handler in ours), \
            f"a new {label} message must still be handled"
        assert not any(handler.check_update(Update(update_id=9, edited_message=message))
                       for handler in ours), f"an edited {label} message must not run a turn"
