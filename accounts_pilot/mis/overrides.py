"""Local persistence for operator edits to a hotel profile.

The MIS (Metabase) is READ-ONLY — we can't push changes back. So when the operator
fills in fields the DB lacks (room counts, star rating, KYC, payout, …) or fixes data,
those edits are saved HERE, keyed by property_id, in a small SQLite table next to the
job store. Next time the hotel is opened, the saved edits are merged back on top of the
fresh MIS data (operator edits win), so nothing is lost across restarts.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path

from accounts_pilot.config import settings


def _conn() -> sqlite3.Connection:
    p = Path(settings.db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.execute(
        "CREATE TABLE IF NOT EXISTS profile_overrides ("
        "  property_id TEXT PRIMARY KEY,"
        "  profile_json TEXT NOT NULL,"
        "  updated_at  TEXT NOT NULL)"
    )
    return c


def save_override(property_id: str, profile: dict) -> None:
    """Upsert the operator's edited profile for this hotel."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO profile_overrides(property_id, profile_json, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(property_id) DO UPDATE SET "
            "  profile_json = excluded.profile_json, updated_at = excluded.updated_at",
            (str(property_id), json.dumps(profile, ensure_ascii=False), ts),
        )


def get_override(property_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT profile_json FROM profile_overrides WHERE property_id = ?",
            (str(property_id),),
        ).fetchone()
    return json.loads(row[0]) if row else None


def delete_override(property_id: str) -> None:
    """Forget the saved edits — next open re-pulls fresh from the MIS."""
    with _conn() as c:
        c.execute("DELETE FROM profile_overrides WHERE property_id = ?", (str(property_id),))


def list_overrides() -> list[str]:
    with _conn() as c:
        return [r[0] for r in c.execute("SELECT property_id FROM profile_overrides").fetchall()]
