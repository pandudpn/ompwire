"""Files in both directions.

Out: the agent cannot attach anything through ACP, so it names a path and the transport uploads
it. The marker rides on the same idea as `<<options: ...>>`:

    Here is the chart you asked for.
    <<file: /home/you/report/chart.png>>

The marker never reaches the chat. Anything the agent marks that is missing, outside the working
directory, or too large is reported once as a short line, so a wrong path is visible instead of
silent.

In: a document the chat sends is saved under the state directory and the agent is told the path.
ACP has no generic attachment block, and the agent has a shell, so a path it can read beats a
half-supported content type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

FILE_RE = re.compile(r"<<\s*files?\s*:\s*(?P<body>[^>]+?)\s*>>", re.IGNORECASE | re.DOTALL)
PARTIAL_RE = re.compile(r"<<\s*files?\s*:.*$", re.IGNORECASE | re.DOTALL)

MAX_FILES = 5                  # per turn, so one reply cannot turn into a dump
PHOTO_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
TELEGRAM_PHOTO_MB = 10         # above this a photo has to travel as a document
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Outgoing:
    """One file the agent asked to send, already checked."""

    path: Path
    size: int

    @property
    def as_photo(self) -> bool:
        return self.path.suffix.lower() in PHOTO_SUFFIXES and self.size <= TELEGRAM_PHOTO_MB * 1024 * 1024


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def extract(text: str, root: str | Path, max_mb: float = 20.0) -> tuple[str, list[Outgoing], list[str]]:
    """Return (text without markers, files to send, reasons the rest were dropped).

    `root` is the agent's working directory: a marker pointing outside it is refused, because a
    model that has read `~/.ssh` should not be able to post it into a chat by writing a marker.
    """
    base = Path(root).expanduser().resolve()
    limit = max(0.0, float(max_mb)) * 1024 * 1024
    files: list[Outgoing] = []
    rejected: list[str] = []
    seen: set[Path] = set()

    for match in FILE_RE.finditer(text):
        for raw in re.split(r"[|\n]", match.group("body")):
            candidate = raw.strip().strip("\"'")
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            try:
                path = path.resolve()
            except OSError:
                rejected.append(f"{candidate}: cannot resolve")
                continue
            if path in seen:
                continue
            if not path.exists():
                rejected.append(f"{candidate}: not found")
                continue
            if not path.is_file():
                rejected.append(f"{candidate}: not a file")
                continue
            if not _inside(path, base):
                rejected.append(f"{path.name}: outside {base}")
                continue
            size = path.stat().st_size
            if limit and size > limit:
                rejected.append(f"{path.name}: {size / (1024 * 1024):.1f} MB, over the {max_mb:g} MB limit")
                continue
            if len(files) >= MAX_FILES:
                rejected.append(f"{path.name}: only {MAX_FILES} files per reply")
                continue
            seen.add(path)
            files.append(Outgoing(path=path, size=size))

    cleaned = FILE_RE.sub("", text).strip()
    return cleaned, files, rejected


def hide_partial(text: str, final: bool) -> str:
    """Streaming: a half-written marker must not flash in the message."""
    if final:
        return FILE_RE.sub("", text).strip()
    return PARTIAL_RE.sub("", text).rstrip()


def safe_name(name: str, fallback: str = "file") -> str:
    """A filename from a chat message, reduced to something safe to place on disk."""
    base = Path((name or "").replace("\\", "/")).name
    cleaned = SAFE_NAME.sub("_", base).strip("._-")
    return cleaned[:80] or fallback


def save(raw: bytes, name: str, directory: Path, max_mb: float = 20.0) -> Path:
    """Write an incoming document under `directory` and return where it landed."""
    limit = max(0.0, float(max_mb)) * 1024 * 1024
    if limit and len(raw) > limit:
        raise ValueError(f"{len(raw) / (1024 * 1024):.1f} MB, over the {max_mb:g} MB limit")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / safe_name(name)
    stem, suffix, counter = target.stem, target.suffix, 1
    while target.exists():
        target = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    target.write_bytes(raw)
    return target


def incoming_summary(path: Path, mime: str = "", caption: str = "") -> str:
    """What the agent is told when a document arrives."""
    size = path.stat().st_size if path.exists() else 0
    detail = f"[file] {path.name} ({mime or 'unknown type'}, {size} bytes) saved at {path}"
    if caption.strip():
        detail += f"\ncaption: {caption.strip()}"
    return detail
