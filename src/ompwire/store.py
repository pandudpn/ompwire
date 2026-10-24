"""SQLite store mapping a chat/transport key to an ACP session id.

The whole point of `session/load` is that history lives inside OMP, so the
harness only has to remember *which* session belongs to a conversation.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SessionRecord:
    key: str
    session_id: str
    cwd: str
    model: str
    mode: str
    updated_at: float


class SessionStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                key        TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                cwd        TEXT NOT NULL,
                model      TEXT NOT NULL DEFAULT '',
                mode       TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._db.commit()

    def get(self, key: str) -> SessionRecord | None:
        row = self._db.execute("SELECT * FROM sessions WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return SessionRecord(
            key=row["key"],
            session_id=row["session_id"],
            cwd=row["cwd"],
            model=row["model"],
            mode=row["mode"],
            updated_at=row["updated_at"],
        )

    def put(self, key: str, session_id: str, cwd: str, model: str = "", mode: str = "") -> None:
        now = time.time()
        self._db.execute(
            """
            INSERT INTO sessions (key, session_id, cwd, model, mode, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                session_id = excluded.session_id,
                cwd        = excluded.cwd,
                model      = excluded.model,
                mode       = excluded.mode,
                updated_at = excluded.updated_at
            """,
            (key, session_id, cwd, model, mode, now, now),
        )
        self._db.commit()

    def touch(self, key: str) -> None:
        self._db.execute("UPDATE sessions SET updated_at = ? WHERE key = ?", (time.time(), key))
        self._db.commit()

    def clear(self, key: str) -> None:
        self._db.execute("DELETE FROM sessions WHERE key = ?", (key,))
        self._db.commit()

    def all(self) -> list[SessionRecord]:
        rows = self._db.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
        return [
            SessionRecord(
                key=r["key"],
                session_id=r["session_id"],
                cwd=r["cwd"],
                model=r["model"],
                mode=r["mode"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def close(self) -> None:
        self._db.close()
