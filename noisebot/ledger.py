"""One action per issue, plus a daily cap.

SQLite is the local stand-in. A later DynamoDB store can implement ``try_begin``
and ``release`` with a conditional put and a per-day counter.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utc_day(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).date().isoformat()


class SqliteLedger:
    def __init__(self, path: Path | str, daily_cap: int):
        self.path = Path(path)
        self.daily_cap = daily_cap
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    issue_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    day TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS actions_day ON actions(day)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def try_begin(self, issue_id: str, action: str = "discard") -> str:
        """Reserve the issue. Returns ``ok``, ``duplicate``, or ``capped``."""

        issue_id = str(issue_id)
        day = utc_day()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM actions WHERE issue_id = ?", (issue_id,)
            ).fetchone()
            if existing:
                conn.rollback()
                return "duplicate"
            count = conn.execute("SELECT COUNT(*) FROM actions WHERE day = ?", (day,)).fetchone()[0]
            if count >= self.daily_cap:
                conn.rollback()
                return "capped"
            conn.execute(
                "INSERT INTO actions (issue_id, action, day, created_at) VALUES (?, ?, ?, ?)",
                (issue_id, action, day, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return "ok"

    def release(self, issue_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM actions WHERE issue_id = ?", (str(issue_id),))
            conn.commit()

    def daily_count(self, day: str | None = None) -> int:
        day = day or utc_day()
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM actions WHERE day = ?", (day,)).fetchone()
            return int(row[0])
