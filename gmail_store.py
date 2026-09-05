# =============================================================
# gmail_store.py — server-side module
#
# Pure storage for a per-license Gmail OAuth token, uploaded once from
# the desktop's already-authorized token.json (the "fast path" decision
# for 2026-09-05's standalone-engine work — see MAILBOT_ROADMAP.md).
# Deliberately knows nothing about how to actually talk to Gmail —
# that's gmail_client.py's job, same separation fleet_store.py
# (storage) already has from parser_core.py (logic).
#
# Same WAL/busy_timeout pattern as fleet_store.py/bid_history.py/
# load_store.py.
# =============================================================

import sqlite3
import os
from typing import Optional
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "gmail_store.db")


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
        conn.execute('''CREATE TABLE IF NOT EXISTS gmail_credentials (
            license_key    TEXT PRIMARY KEY,
            token_json     TEXT NOT NULL,
            connected_email TEXT DEFAULT '',
            status         TEXT DEFAULT 'connected',
            updated_at     TEXT NOT NULL
        )''')
        conn.commit()
    finally:
        conn.close()


def save_token(license_key: str, token_json: str, connected_email: str = "",
               status: str = "connected"):
    """Upsert — used both for the initial upload and for persisting a
    refreshed token back (refresh_token/expiry can rotate on refresh,
    same as the desktop's authenticate_gmail() re-writing token.json
    after every silent refresh)."""
    conn = _connect()
    try:
        conn.execute(
            '''INSERT INTO gmail_credentials (license_key, token_json, connected_email, status, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(license_key) DO UPDATE SET
                   token_json=excluded.token_json,
                   connected_email=CASE WHEN excluded.connected_email != ''
                                        THEN excluded.connected_email
                                        ELSE gmail_credentials.connected_email END,
                   status=excluded.status,
                   updated_at=excluded.updated_at''',
            (license_key, token_json, connected_email, status, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_token(license_key: str) -> Optional[str]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT token_json FROM gmail_credentials WHERE license_key=?", (license_key,)
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_status(license_key: str) -> dict:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT connected_email, status, updated_at FROM gmail_credentials WHERE license_key=?",
            (license_key,),
        )
        row = cur.fetchone()
        if not row:
            return {"connected": False, "connected_email": None, "status": None, "updated_at": None}
        return {"connected": True, "connected_email": row[0] or None,
                "status": row[1], "updated_at": row[2]}
    finally:
        conn.close()


def delete_token(license_key: str):
    conn = _connect()
    try:
        conn.execute("DELETE FROM gmail_credentials WHERE license_key=?", (license_key,))
        conn.commit()
    finally:
        conn.close()
