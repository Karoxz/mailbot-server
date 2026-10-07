# =============================================================
# phone_relay_store.py — server-side module, added 2026-10-07
#
# BID PHONE's desktop relay can't use a Telegram message as the hand-
# back channel: a bot's own sendMessage never generates an incoming
# update for THAT SAME bot's getUpdates — Telegram updates represent
# events directed AT the bot, never its own outgoing sends. Confirmed
# live 2026-10-07: the desktop's own getUpdates loop never once saw
# the "##PHONEBID##" marker message it had just received via
# sendMessage, across every rebuild and process check — a hard
# platform rule, not a bug in that loop.
#
# A tiny SQLite-backed queue instead, same shape as push_queue.py's
# fix for the identical class of problem ("invisible across uvicorn's
# 4 worker processes" — an in-memory dict here would have the exact
# same bug push_queue.py already found and fixed once). The desktop
# polls for (and atomically clears) its own pending entries on its
# existing callback-polling loop, keyed by (license_key, machine_id)
# since this queue serves every desktop installation, not just one.
# =============================================================

import sqlite3
import os
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "phone_relay.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    conn = _connect()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS relays (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT NOT NULL,
            machine_id  TEXT NOT NULL,
            order_id    TEXT NOT NULL,
            truck_idx   TEXT,
            price       REAL NOT NULL,
            rate        REAL,
            created_at  REAL NOT NULL
        )''')
        conn.execute('''CREATE INDEX IF NOT EXISTS idx_relays_lookup
                        ON relays(license_key, machine_id)''')
        conn.commit()
    finally:
        conn.close()


def enqueue(license_key: str, machine_id: str, order_id: str, truck_idx, price: float, rate) -> None:
    conn = _connect()
    try:
        conn.execute(
            '''INSERT INTO relays (license_key, machine_id, order_id, truck_idx, price, rate, created_at)
               VALUES (?,?,?,?,?,?,?)''',
            (license_key, machine_id, order_id,
             str(truck_idx) if truck_idx is not None else "", price, rate, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def poll_and_clear(license_key: str, machine_id: str) -> list:
    """Every pending relay for this exact (license_key, machine_id),
    atomically removed — same "read once, gone" semantics push_queue.py's
    drain_all() uses, just scoped to one desktop instead of global."""
    conn = _connect()
    try:
        rows = conn.execute(
            '''SELECT id, order_id, truck_idx, price, rate FROM relays
               WHERE license_key=? AND machine_id=? ORDER BY id''',
            (license_key, machine_id),
        ).fetchall()
        if rows:
            ids = [r[0] for r in rows]
            conn.execute(f'DELETE FROM relays WHERE id IN ({",".join("?" * len(ids))})', ids)
            conn.commit()
        return [
            {"order_id": r[1], "truck_idx": (int(r[2]) if r[2] else None), "price": r[3], "rate": r[4]}
            for r in rows
        ]
    finally:
        conn.close()
