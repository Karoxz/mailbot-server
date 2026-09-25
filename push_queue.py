# =============================================================
# push_queue.py — server-side module, added 2026-09-25
#
# Was a plain in-process collections.deque in main.py — invisible across
# uvicorn's 4 worker processes (Gmail's Pub/Sub webhook lands on whichever
# worker Google happens to hit; a poll from GET /webhook/poll landing on
# any OTHER worker saw an empty queue even though a push genuinely
# arrived seconds earlier). ~75% of push notifications were silently
# dropped. Same SQLite/WAL pattern as load_store.py/route_calibration.py
# — proven safe across the 4 worker processes — fixes this the same way
# LOAD_STORE/BID_TEMPLATE were already fixed.
# =============================================================

import sqlite3
import os
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "push_queue.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    conn = _connect()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS pending_pushes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            history_id  TEXT NOT NULL,
            pushed_at   REAL NOT NULL
        )''')
        conn.commit()
    finally:
        conn.close()


def push(history_id: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO pending_pushes (history_id, pushed_at) VALUES (?, ?)",
            (history_id, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def drain_all() -> list:
    """Returns every pending (history_id, pushed_at) pair and clears the
    queue, same "read once, gone" semantics the in-memory deque had."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, history_id, pushed_at FROM pending_pushes ORDER BY id"
        ).fetchall()
        if rows:
            conn.execute("DELETE FROM pending_pushes WHERE id <= ?", (rows[-1][0],))
            conn.commit()
        return [(r[1], r[2]) for r in rows]
    finally:
        conn.close()
