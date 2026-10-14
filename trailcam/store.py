"""SQLite store for motion events and sightings."""

from __future__ import annotations

import sqlite3
import threading
import time

from .config import settings

DB_PATH = settings.data_dir / "trailcam.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    request_id  TEXT PRIMARY KEY,      -- Ring meta.request_id, the idempotency key
    event_id    TEXT,
    device_id   TEXT,
    account_id  TEXT,
    ts_ms       INTEGER,
    sub_type    TEXT,
    status      TEXT,                  -- queued, working, skipped, no_animal, sighting, unavailable, error
    detail      TEXT,
    source      TEXT,                  -- ring (webhook) or playground (Event History polling)
    received_at REAL
);
CREATE TABLE IF NOT EXISTS sightings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT,
    device_id   TEXT,
    device_name TEXT,
    ts_ms       INTEGER,
    label       TEXT,
    scientific  TEXT,
    score       REAL,
    clip        TEXT,
    thumb       TEXT,
    first_visit INTEGER,
    country     TEXT,
    state       TEXT,
    media       TEXT,                  -- clip, snapshot, or simulated (Playground motion pool)
    source      TEXT,                  -- ring or playground
    ring_subtype TEXT,                 -- Ring's own detection from Event History, e.g. animal
    feedback    TEXT,                  -- confirmed or rejected by the user
    second      TEXT,                  -- SpeciesNet's second answer, e.g. great blue heron
    second_score REAL
);
"""

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


with _lock:
    _db = _conn()
    _db.executescript(SCHEMA)
    # Databases from before a column was added get it here.
    cols = {r[1] for r in _db.execute("PRAGMA table_info(sightings)")}
    for col, kind in (("ring_subtype", "TEXT"), ("feedback", "TEXT"), ("second", "TEXT"), ("second_score", "REAL")):
        if col not in cols:
            _db.execute(f"ALTER TABLE sightings ADD COLUMN {col} {kind}")
    _db.commit()


def add_event(request_id: str, event_id: str, device_id: str, account_id: str, ts_ms: int,
              sub_type: str, status: str, detail: str = "", source: str = "ring") -> bool:
    """Insert an event; False when this request_id was already seen (a Ring retry)."""
    with _lock:
        try:
            _db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (request_id, event_id, device_id, account_id, ts_ms, sub_type, status, detail, source, time.time()),
            )
            _db.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def set_event(request_id: str, status: str, detail: str = "") -> None:
    """Update an event's progress. A "Ring tags: ..." note from intake stays on the end."""
    with _lock:
        row = _db.execute("SELECT detail FROM events WHERE request_id=?", (request_id,)).fetchone()
        old = (row["detail"] if row else "") or ""
        if "Ring tags:" in old:
            tags = old[old.index("Ring tags:"):]
            detail = f"{detail}. {tags}" if detail else tags
        _db.execute("UPDATE events SET status=?, detail=? WHERE request_id=?", (status, detail, request_id))
        _db.commit()


def species_seen(label: str) -> bool:
    with _lock:
        return _db.execute("SELECT 1 FROM sightings WHERE label=? LIMIT 1", (label,)).fetchone() is not None


def add_sighting(**row) -> int:
    cols = ",".join(row)
    with _lock:
        cur = _db.execute(f"INSERT INTO sightings ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        _db.commit()
        return cur.lastrowid


def set_feedback(sighting_id: int, feedback: str | None) -> None:
    with _lock:
        _db.execute("UPDATE sightings SET feedback=? WHERE id=?", (feedback, sighting_id))
        _db.commit()


def sightings(limit: int = 200) -> list[dict]:
    with _lock:
        rows = _db.execute("SELECT * FROM sightings ORDER BY ts_ms DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def species_counts() -> list[dict]:
    with _lock:
        rows = _db.execute(
            "SELECT label, scientific, COUNT(*) AS visits, MIN(ts_ms) AS first_ts, MAX(ts_ms) AS last_ts "
            "FROM sightings GROUP BY label ORDER BY visits DESC, label"
        ).fetchall()
    return [dict(r) for r in rows]


def events(limit: int = 50) -> list[dict]:
    with _lock:
        rows = _db.execute("SELECT * FROM events ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def event_totals() -> dict:
    with _lock:
        rows = _db.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def clear() -> None:
    with _lock:
        _db.execute("DELETE FROM events")
        _db.execute("DELETE FROM sightings")
        _db.commit()
