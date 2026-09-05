# =============================================================
# load_store.py — server-side module
#
# SQLite-backed replacement for what used to be two in-process globals
# in parser_core.py: LOAD_STORE (a plain dict) and BID_TEMPLATE (a
# plain string). Both were invisible across uvicorn's 4 worker
# processes — a load matched by worker A was never visible to a
# /api/web/feed request served by worker B, and a bid-template edit
# via the web only ever updated whichever single worker handled that
# POST (found 2026-09-05, previously undocumented — same bug as
# LOAD_STORE, just never noticed since the default rarely gets edited).
# This is also a hard prerequisite for the standalone poller (a 5th
# process, see poller.py) to make its matches visible to the 4 API
# workers at all.
#
# Pattern mirrors fleet_store.py/bid_history.py: module-level DB_PATH,
# _connect() per call, WAL mode, try/finally close — already proven
# safe across the 4 uvicorn workers today.
# =============================================================

import sqlite3
import os
import json
from typing import Optional
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "load_store.db")

MAX_LOADS = 500  # same cap the old in-memory dict enforced

_DEFAULT_BID_TEMPLATE = """Rate: $
{vehicle_type}
Dims: {truck_dimensions}
MC#

Truck is {google_deadhead} miles out
{truck_equipment}

ETA to PU: {deadhead_eta_str}

ALL BIDS ARE VALID 15 MIN"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    conn = _connect()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS loads (
            order_id   TEXT PRIMARY KEY,
            data_json  TEXT NOT NULL,
            created_at TEXT NOT NULL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS bid_template (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            template   TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        # poller.py's heartbeat — single row, overwritten every outer-loop
        # tick regardless of per-license outcomes. This is the direct fix
        # for "I can't tell if it's working": the Settings UI polls this
        # to show "poller last ran Xs ago" instead of silence.
        conn.execute('''CREATE TABLE IF NOT EXISTS poller_heartbeat (
            id                 INTEGER PRIMARY KEY CHECK (id = 1),
            last_run_at        TEXT,
            licenses_processed INTEGER DEFAULT 0,
            last_error         TEXT
        )''')
        # Seed the single bid_template row with the historical default,
        # exactly once — matches what BID_TEMPLATE used to default to as
        # a plain Python string before this migration. Never overwrites
        # an existing row (so a real edit is never clobbered on restart).
        cur = conn.execute("SELECT COUNT(*) FROM bid_template")
        if cur.fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO bid_template (id, template, updated_at) VALUES (1, ?, ?)",
                (_DEFAULT_BID_TEMPLATE, _now()),
            )
        conn.commit()
    finally:
        conn.close()


def get_load(order_id: str) -> Optional[dict]:
    conn = _connect()
    try:
        cur = conn.execute("SELECT data_json FROM loads WHERE order_id=?", (order_id,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def get_recent_loads(limit: int = 30) -> list:
    """Newest first — what /api/web/feed wants directly (the old dict
    version had to do items[-limit:][::-1] itself; ORDER BY does it here)."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT data_json FROM loads ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [json.loads(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()


def put_load(order_id: str, data: dict):
    """Upsert one load, then evict everything past MAX_LOADS oldest-first
    — same 500-entry cap the old in-memory dict enforced (there it was
    Python dict insertion order; here it's created_at, same practical
    effect)."""
    try:
        payload = json.dumps(data)
    except TypeError:
        # Defensive only — every field in this dict has always had to be
        # JSON-safe already (the same dict is returned verbatim as part
        # of the /api/parse JSON response), but never let a stray
        # non-serializable value crash the whole write.
        payload = json.dumps({k: v for k, v in data.items()
                               if k != "original_msg_full"})
    conn = _connect()
    try:
        now = _now()
        conn.execute(
            '''INSERT INTO loads (order_id, data_json, created_at) VALUES (?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET data_json=excluded.data_json,
                                                    created_at=excluded.created_at''',
            (order_id, payload, now),
        )
        conn.execute('''
            DELETE FROM loads WHERE order_id NOT IN (
                SELECT order_id FROM loads ORDER BY created_at DESC LIMIT ?
            )''', (MAX_LOADS,))
        conn.commit()
    finally:
        conn.close()


def get_bid_template() -> str:
    conn = _connect()
    try:
        cur = conn.execute("SELECT template FROM bid_template WHERE id=1")
        row = cur.fetchone()
        return row[0] if row else _DEFAULT_BID_TEMPLATE
    finally:
        conn.close()


def set_bid_template(template: str):
    conn = _connect()
    try:
        conn.execute(
            "UPDATE bid_template SET template=?, updated_at=? WHERE id=1",
            (template, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def write_poller_heartbeat(licenses_processed: int, last_error: Optional[str] = None):
    conn = _connect()
    try:
        conn.execute(
            '''INSERT INTO poller_heartbeat (id, last_run_at, licenses_processed, last_error)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET last_run_at=excluded.last_run_at,
                                              licenses_processed=excluded.licenses_processed,
                                              last_error=excluded.last_error''',
            (_now(), licenses_processed, last_error),
        )
        conn.commit()
    finally:
        conn.close()


def get_poller_heartbeat() -> Optional[dict]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT last_run_at, licenses_processed, last_error FROM poller_heartbeat WHERE id=1"
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"last_run_at": row[0], "licenses_processed": row[1], "last_error": row[2]}
    finally:
        conn.close()
