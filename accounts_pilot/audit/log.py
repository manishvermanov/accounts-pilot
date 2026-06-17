"""Append-only audit log.

Every field submitted, every gate, every screenshot, with timestamps. This is
your answer the first time an OTA disputes a listing, and your debugging trail
when a wizard changes and an adapter step breaks. Append-only by design — rows
are never updated or deleted.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


class AuditLog:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    step TEXT,
                    action TEXT NOT NULL,
                    detail TEXT
                )"""
            )

    def record(self, job_id: str, step: str | None, action: str, detail: str = "") -> None:
        at = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                "INSERT INTO audit_events (at, job_id, step, action, detail) VALUES (?,?,?,?,?)",
                (at, job_id, step, action, detail),
            )

    def for_job(self, job_id: str) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT at, step, action, detail FROM audit_events WHERE job_id=? ORDER BY id",
                (job_id,),
            ).fetchall()
        return [dict(r) for r in rows]
