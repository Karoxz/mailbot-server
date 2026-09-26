# =============================================================
# activity_log.py — server-side module, added 2026-09-25
#
# A live, per-license activity feed for the web dashboard — didn't exist
# in any form before this. Every other "log" in this project (Python's
# logging module in main.py/poller.py, bare print() in thread_learner.py)
# only ever reaches journalctl/console, nothing queryable by the API or
# visible to a dispatcher. This module persists a small, curated set of
# already-loggable moments (bid recorded, broker blacklisted, load
# matched, etc. — see the call sites that write here) so they can be
# shown live on activity.html. Not a replacement for the existing
# logger.info()/print() calls — those stay, this is additive.
#
# Same SQLite/WAL pattern as load_store.py/route_calibration.py.
# =============================================================

import sqlite3
import os
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "activity_log.db")
_KEEP_PER_LICENSE = 3000


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    conn = _connect()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            message     TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )''')
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_license_created "
            "ON events (license_key, created_at DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def log_event(license_key: str, event_type: str, message: str) -> None:
    if not license_key:
        return
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO events (license_key, event_type, message, created_at) "
            "VALUES (?, ?, ?, ?)",
            (license_key, event_type, message, datetime.now(timezone.utc).isoformat()),
        )
        # The standalone poller now logs every parsed email (including
        # skipped ones — the web equivalent of the desktop's live log
        # panel), which is hundreds of rows an hour. Keep only the newest
        # _KEEP_PER_LICENSE per license; checked once per ~100 inserts so
        # it costs nothing on the hot path.
        if cur.lastrowid and cur.lastrowid % 100 == 0:
            conn.execute(
                "DELETE FROM events WHERE license_key=? AND id NOT IN "
                "(SELECT id FROM events WHERE license_key=? ORDER BY id DESC LIMIT ?)",
                (license_key, license_key, _KEEP_PER_LICENSE),
            )
        conn.commit()
    finally:
        conn.close()


def get_recent_events(license_key: str, limit: int = 100) -> list:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, event_type, message, created_at FROM events "
            "WHERE license_key=? ORDER BY id DESC LIMIT ?",
            (license_key, limit),
        ).fetchall()
        return [
            {"id": r[0], "event_type": r[1], "message": r[2], "created_at": r[3]}
            for r in rows
        ]
    finally:
        conn.close()
