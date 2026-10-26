"""The WhatsApp link: one linked device per gateway, through neonize (whatsmeow).

There is no token. Linking scans a QR (or types a pairing code) on the phone that owns the
number, and whatsmeow keeps the device keys in a SQLite file. That file is the credential:
whoever holds it acts as this linked device, so it lives in the gateway's own state directory,
mode 0600, and nothing logs its contents.

Everything platform-shaped about WhatsApp that is not a chat lives here: where the session is,
linking it, recognising who sent a message, and turning agent markdown into WhatsApp markup.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger("ompwire.whatsapp")

SESSION_FILE = "whatsapp.db"
PHONE_SERVER = "s.whatsapp.net"
GROUP_SERVER = "g.us"
LID_SERVER = "lid"


def session_path(gateway: str) -> Path:
    from .gateways import state_dir

    return state_dir(gateway) / SESSION_FILE


def linked_number(path: Path) -> str:
    """The phone number this session is linked as, "" when it holds no device.

    Read straight from whatsmeow's table so `gateway list` and `doctor` need no connection.
    """
    if not path.is_file():
        return ""
    import sqlite3

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        row = connection.execute("SELECT jid FROM whatsmeow_device LIMIT 1").fetchone()
    except sqlite3.Error:
        return ""
    finally:
        connection.close()
    return user_part(str(row[0])) if row and row[0] else ""


def user_part(jid: str) -> str:
    """The person inside a JID string: "62812:39@s.whatsapp.net" -> "62812"."""
    return jid.split("@", 1)[0].split(":", 1)[0]


# ------------------------------------------------------------------ the client


def new_client(path: Path):
    """A neonize client bound to this session file, created 0600 before whatsmeow opens it.

    Call it inside the event loop that will run it. neonize keeps the loop it delivers events to
    in a module global and sets it only once, so a second `asyncio.run` in the same process (link,
    then list groups) would have its events sent to the first, closed loop.
    """
    from neonize.aioze import events
    from neonize.aioze.client import NewAClient

    events.event_global_loop = asyncio.get_running_loop()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    os.chmod(path, 0o600)
    return NewAClient(str(path))


async def link(path: Path, *, on_qr: Callable[[str], Awaitable[None] | None],
               phone: str = "", on_code: Callable[[str], None] | None = None,
               timeout: float = 180.0) -> str:
    """Link a new device and return the phone number it was linked as.

    With `phone`, WhatsApp shows a "link with phone number" prompt on that phone and the
    returned code is typed there instead of scanning. Otherwise every QR rotation reaches
    `on_qr` as the text to encode.
    """
    from neonize.aioze.events import ConnectedEv, PairStatusEv

    client = new_client(path)
    linked: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def qr(_client, data: bytes) -> None:
        if phone:
            return
        result = on_qr(data.decode())
        if asyncio.iscoroutine(result):
            await result

    client.event.qr(qr)

    @client.event(PairStatusEv)
    async def paired(_client, event) -> None:
        if not linked.done():
            linked.set_result(user_part(f"{event.ID.User}@{event.ID.Server}"))

    @client.event(ConnectedEv)
    async def connected(_client, _event) -> None:
        if linked.done():
            return
        number = linked_number(path)
        if number:  # already linked: nothing to pair
            linked.set_result(number)

    runner = asyncio.create_task(client.connect())
    try:
        if phone:
            # the socket has to be up before a pairing code can be requested
            await asyncio.sleep(3)
            code = await client.PairPhone(phone, show_push_notification=True)
            if on_code is not None:
                on_code(code)
        return await asyncio.wait_for(linked, timeout)
    finally:
        try:
            await client.disconnect()
        except Exception as exc:
            log.debug("disconnect after linking: %s", exc)
        runner.cancel()


async def joined_groups(path: Path, timeout: float = 30.0) -> list[tuple[str, str]]:
    """The groups this linked number is in, as (jid, name), read over a short connection.

    Run only while the gateway is stopped: one session holds one connection.
    """
    from neonize.aioze.events import ConnectedEv

    client = new_client(path)
    ready = asyncio.Event()

    @client.event(ConnectedEv)
    async def connected(_client, _event) -> None:
        ready.set()

    async def no_qr(_client, _data: bytes) -> None:
        ready.set()  # not linked after all: nothing to list

    client.event.qr(no_qr)
    runner = asyncio.create_task(client.connect())
    try:
        await asyncio.wait_for(ready.wait(), timeout)
        groups = await client.get_joined_groups()
        return sorted(((f"{group.JID.User}@{GROUP_SERVER}", group.GroupName.Name or group.JID.User)
                       for group in groups), key=lambda item: item[1].lower())
    finally:
        try:
            await client.disconnect()
        except Exception as exc:
            log.debug("disconnect after listing groups: %s", exc)
        runner.cancel()


async def check_link(path: Path, timeout: float = 30.0) -> str:
    """The number this session is still linked as, asked of WhatsApp itself; "" when it is not.

    The session file outlives the link: removing the device on the phone (Linked devices) does
    not touch it, so reading the file alone says "linked" for a link that is gone.
    """
    from neonize.aioze.events import ConnectedEv, LoggedOutEv

    client = new_client(path)
    verdict: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def decide(alive: bool) -> None:
        if not verdict.done():
            verdict.set_result(alive)

    @client.event(ConnectedEv)
    async def connected(_client, _event) -> None:
        decide(True)

    @client.event(LoggedOutEv)
    async def logged_out(_client, _event) -> None:
        decide(False)

    async def asks_qr(_client, _data: bytes) -> None:
        decide(False)  # a new QR means WhatsApp no longer knows this device

    client.event.qr(asks_qr)
    runner = asyncio.create_task(client.connect())
    try:
        alive = await asyncio.wait_for(verdict, timeout)
        return linked_number(path) if alive else ""
    finally:
        try:
            await client.disconnect()
        except Exception as exc:
            log.debug("disconnect after the link check: %s", exc)
        runner.cancel()

def unlink(path: Path) -> bool:
    """Forget the linked device on this machine. The phone still lists it until removed there."""
    removed = False
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if candidate.exists():
            candidate.unlink()
            removed = True
    return removed


# ------------------------------------------------------------------ markup

_CODE_RE = re.compile(r"(`[^`\n]+`)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_BOLD_UNDER_RE = re.compile(r"__(?=\S)(.+?)(?<=\S)__", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?!\*)(?=\S)(.+?)(?<=\S)\*(?!\*)", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BULLET_RE = re.compile(r"^([ \t]*)[-+*][ \t]+", re.MULTILINE)


def to_whatsapp(text: str) -> str:
    """Agent markdown as WhatsApp markup: *bold*, _italic_, ~strike~, `code`, ```blocks```.

    Code spans and fences are left exactly as written; WhatsApp renders both. Everything else
    is rewritten outside them only, so a `**` inside a command is never touched.
    """
    from .mdrender import segments

    out: list[str] = []
    for kind, part, _language in segments(text.replace("\r\n", "\n")):
        if kind == "code":
            # WhatsApp shows a language tag after the fence as text, so it is dropped; the
            # block keeps its own lines, the markdown indent and fence width taken off
            out.append(("\n" if out else "") + "```" + part.rstrip("\n") + "\n```")
            continue
        if out:
            part = "\n" + part
        pieces = _CODE_RE.split(part)
        for index, piece in enumerate(pieces):
            if index % 2 == 1:
                out.append(piece)
                continue
            # a heading becomes markdown bold first: the italic pass below rewrites single
            # stars, and the bold pass then turns ** into WhatsApp's one-star bold
            piece = _HEADING_RE.sub(lambda m: f"**{m.group(1).replace('**', '').strip()}**", piece)
            piece = _ITALIC_STAR_RE.sub(r"_\1_", piece)
            piece = _BOLD_RE.sub(r"*\1*", piece)
            piece = _BOLD_UNDER_RE.sub(r"*\1*", piece)
            piece = _STRIKE_RE.sub(r"~\1~", piece)
            piece = _LINK_RE.sub(r"\1 (\2)", piece)
            piece = _BULLET_RE.sub(r"\1• ", piece)
            out.append(piece)
    return "".join(out).strip()


def chunks(text: str, limit: int) -> list[str]:
    """Cut on blank lines, then lines, so a code block is cut at a line, never mid-word."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def strip_mention(text: str, ids: set[str]) -> str:
    """Drop the bot's own @mention from an incoming message before the agent reads it."""
    for user in ids:
        if user:
            text = re.sub(rf"@{re.escape(user)}\b", "", text)
    return " ".join(text.split()) if "\n" not in text else text.strip()
