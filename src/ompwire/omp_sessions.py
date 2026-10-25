"""Session-level token numbers, read from omp's own transcript.

The ACP turn result only carries one turn's usage and dies with the process, so totals kept in
memory reset on every gateway restart. omp writes every assistant message with its provider
usage to `~/.omp/agent/sessions/<cwd-slug>/<timestamp>_<session-id>.jsonl`; summing that file
gives totals that survive restarts and /resume. The file is append-only and grows to megabytes,
so it is read incrementally from the last offset.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# overridable in tests
SESSIONS_DIR = Path("~/.omp/agent/sessions")

# A message whose context is below this share of the last good one, with no compaction in
# between, is a report omp could not anchor (a resumed turn after a restart reads ~400 tokens
# against a 700k context). Real compactions keep far more than 5%, and they leave an entry.
COLLAPSE_RATIO = 0.05


@dataclass
class SessionUsage:
    calls: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    context: int = 0  # tokens the last trustworthy call carried
    _path: Path | None = field(default=None, repr=False)
    _offset: int = field(default=0, repr=False)
    _compacted: bool = field(default=False, repr=False)

    def _add(self, entry: dict) -> None:
        if entry.get("type") == "compaction":
            self._compacted = True
            return
        message = entry.get("message") if entry.get("type") == "message" else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        counts = {key: int(usage.get(key) or 0)
                  for key in ("input", "output", "cacheRead", "cacheWrite")}
        self.calls += 1
        self.input += counts["input"]
        self.output += counts["output"]
        self.cache_read += counts["cacheRead"]
        self.cache_write += counts["cacheWrite"]
        carried = sum(counts.values())
        if carried <= 0:
            return  # an aborted call reports zeros
        if self._compacted or plausible_context(carried, self.context):
            self.context = carried
            self._compacted = False


def plausible_context(used: int, last_good: int) -> bool:
    """False for a report that collapsed to a sliver of the last good one."""
    return not (last_good > 0 and used < COLLAPSE_RATIO * last_good)


def transcript(session_id: str) -> Path | None:
    if not session_id:
        return None
    root = Path(os.path.expanduser(str(SESSIONS_DIR)))
    matches = sorted(root.glob(f"*/*_{session_id}.jsonl"))
    return matches[-1] if matches else None


_cache: dict[str, SessionUsage] = {}


def usage(session_id: str) -> SessionUsage | None:
    """Totals for one omp session, None when its transcript cannot be found."""
    state = _cache.get(session_id)
    path = state._path if state is not None and state._path and state._path.exists() \
        else transcript(session_id)
    if path is None:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if state is None or state._path != path or size < state._offset:
        state = SessionUsage(_path=path)
        _cache[session_id] = state
    if size == state._offset:
        return state
    with path.open("rb") as handle:
        handle.seek(state._offset)
        chunk = handle.read()
    # a line still being written has no newline yet; leave it for the next read
    end = chunk.rfind(b"\n") + 1
    for line in chunk[:end].splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            state._add(entry)
    state._offset += end
    return state
