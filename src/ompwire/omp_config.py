"""Read and change omp's own config.yml — the compaction settings only.

omp reads ~/.omp/agent/config.yml itself, with comments and a deliberate key order. Rewriting
the whole file with a YAML dumper would throw both away, so a change is a targeted text edit of
the `compaction:` block: every other byte stays as it was. Writes are atomic and the first one
leaves a backup next to the file.
"""

from __future__ import annotations

import contextlib
import copy
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

# overridable in tests: monkeypatch this before calling read_compaction / set_compaction
CONFIG_PATH = Path("~/.omp/agent/config.yml")

MIN_PERCENT, MAX_PERCENT = 10, 95


def path() -> Path:
    return Path(os.path.expanduser(str(CONFIG_PATH)))


def read_compaction() -> dict[str, Any]:
    """omp's compaction block, {} when the file or the block is missing."""
    try:
        data = yaml.safe_load(path().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    block = data.get("compaction") if isinstance(data, dict) else None
    return dict(block) if isinstance(block, dict) else {}


def set_compaction(**changes: Any) -> dict[str, Any]:
    """Edit the compaction block in place. Keys: thresholdPercent (10..95), enabled (bool)."""
    if set(changes) - {"thresholdPercent", "enabled"}:
        raise ValueError(f"unknown compaction keys: {sorted(set(changes) - {'thresholdPercent', 'enabled'})}")
    if "thresholdPercent" in changes:
        value = changes["thresholdPercent"]
        if not isinstance(value, int) or isinstance(value, bool) or not MIN_PERCENT <= value <= MAX_PERCENT:
            raise ValueError(f"thresholdPercent must be an int in {MIN_PERCENT}..{MAX_PERCENT}")
    if "enabled" in changes and not isinstance(changes["enabled"], bool):
        raise ValueError("enabled must be a bool")

    file = path()
    try:
        original = file.read_text(encoding="utf-8")
    except OSError:
        original = None

    updated = copy.deepcopy(read_compaction())
    updated.update(changes)
    rendered = _render_block(original, changes, updated)
    _write(file, rendered, original)
    return updated


def _render_block(original: str | None, changes: dict[str, Any],
                  updated: dict[str, Any]) -> str:
    """The new file text: only the lines for the changed keys move."""
    if original is None:
        header = ""
        body = "compaction:\n" + _key_lines(updated, indent="  ")
        return header + body

    lines = original.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(r"^compaction:\s*(#.*)?$", line)), None)
    if start is None:
        if not lines or lines[-1].endswith("\n"):
            if lines and lines[-1].strip():
                lines.append("\n")
        else:
            lines[-1] = lines[-1] + "\n"
        lines.append("compaction:\n")
        lines.extend(_key_lines(updated, indent="  ", keepends=True))
        return "".join(lines)

    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
        if not lines[end].strip():
            break
        end += 1
    block = lines[start + 1:end]
    rewritten: list[str] = []
    remaining = dict(changes)
    for line in block:
        m = re.match(r"^(\s*)([A-Za-z_][\w-]*)(\s*:.*)$", line)
        if m and m.group(2) in remaining:
            key = m.group(2)
            rewritten.append(f"{m.group(1)}{key}: {updated[key]}\n")
            del remaining[key]
        else:
            rewritten.append(line)
    rewritten.extend(_key_lines(remaining, indent="  ", keepends=True))
    return "".join(lines[:start + 1] + rewritten + lines[end:])


def _key_lines(values: dict[str, Any], *, indent: str, keepends: bool = False) -> list[str]:
    order = ["enabled", "thresholdPercent"]
    keys = [k for k in order if k in values] + sorted(set(values) - set(order))
    def fmt(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
    lines = [f"{indent}{key}: {fmt(values[key])}" for key in keys]
    return [line + "\n" for line in lines] if keepends else lines


def _write(file: Path, text: str, original: str | None) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    backup = file.with_name(file.name + ".ompwire-bak")
    if original is not None and not backup.exists():
        backup.write_text(original, encoding="utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=file.parent, prefix=file.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, file)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
