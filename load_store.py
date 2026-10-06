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
#
# Per-license isolation, added 2026-09-25 — before this, `loads` was
# keyed on order_id ALONE, which is NOT globally unique (a real
# broker-assigned order number, or an epoch-second synthetic fallback
# — confirmed by reading parser_core.py, neither carries any account
# context) — two different real accounts' loads could silently
# overwrite each other. The primary key is now (license_key, order_id).
# bid_template similarly goes from one global row to one row per
# license, with license_key='' kept as the true fallback default (used
# when a license — e.g. a brand-new standalone account — hasn't set
# its own yet). poller_heartbeat stays a single global row on purpose:
# it's one poller process's own liveness signal, not per-account data.
# =============================================================

import sqlite3
import os
import json
from typing import Optional
from datetime import datetime, timezone

from migration_defaults import LEGACY_DATA_LICENSE_KEY

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "load_store.db")

MAX_LOADS = 500  # same cap the old in-memory dict enforced, now per-license

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
        # Primary key becomes (license_key, order_id) — see module
        # docstring for why order_id alone isn't safe. SQLite can't
        # ALTER a primary key, so this recreates the table (same
        # create/copy/drop/rename approach used elsewhere in this
        # migration round) rather than just adding a filter column.
        loads_cols = {row[1] for row in conn.execute('PRAGMA table_info(loads)')}
        if "license_key" not in loads_cols:
            conn.execute('''CREATE TABLE loads_new (
                license_key TEXT NOT NULL,
                order_id    TEXT NOT NULL,
                data_json   TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (license_key, order_id)
            )''')
            conn.execute(
                '''INSERT INTO loads_new (license_key, order_id, data_json, created_at)
                   SELECT ?, order_id, data_json, created_at FROM loads''',
                (LEGACY_DATA_LICENSE_KEY,)
            )
            conn.execute('DROP TABLE loads')
            conn.execute('ALTER TABLE loads_new RENAME TO loads')
            loads_cols = {row[1] for row in conn.execute('PRAGMA table_info(loads)')}

        # message_id added to the primary key, 2026-10-06 — client-reported
        # real bug: order_id is a BROKER-assigned reference, not globally
        # unique; two different brokers posting under the same order_id (a
        # genuine real-world collision, not a re-poll of the same email)
        # silently overwrote each other via the old (license_key, order_id)
        # upsert, so only the second one ever showed up on the Live Feed.
        # message_id (the Gmail message id — always real for poller-sourced
        # loads; '' for desktop-sourced ones, which have no such concept and
        # keep today's overwrite-on-repost behavior, matching the desktop's
        # own existing dedup) disambiguates distinct postings so every one
        # of them keeps its own row. get_load(license_key, order_id) below
        # still resolves to a single row (newest by created_at) — every
        # "act on this load" call site (bid_actions, driver notify, the
        # Telegram button callbacks) keeps working unchanged against
        # whichever posting of that order_id is most recent.
        if "message_id" not in loads_cols:
            conn.execute('''CREATE TABLE loads_v2 (
                license_key TEXT NOT NULL,
                order_id    TEXT NOT NULL,
                message_id  TEXT NOT NULL DEFAULT '',
                data_json   TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (license_key, order_id, message_id)
            )''')
            conn.execute(
                '''INSERT OR REPLACE INTO loads_v2
                   (license_key, order_id, message_id, data_json, created_at)
                   SELECT license_key, order_id, '', data_json, created_at FROM loads'''
            )
            conn.execute('DROP TABLE loads')
            conn.execute('ALTER TABLE loads_v2 RENAME TO loads')

        conn.execute('''CREATE TABLE IF NOT EXISTS bid_template (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            template   TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        # bid_template goes from one single global row (id=1) to one row
        # per license, keyed by license_key — '' is the reserved key for
        # the true global fallback (never a real license_key). The old
        # id=1 row's content becomes BOTH the new '' fallback row AND
        # the real account's own row, so anyone else's fallback behavior
        # is unchanged while the real account's already-customized
        # template becomes properly its own (future edits from either
        # side no longer bleed into each other).
        bt_cols = {row[1] for row in conn.execute('PRAGMA table_info(bid_template)')}
        if "license_key" not in bt_cols:
            old_row = conn.execute("SELECT template, updated_at FROM bid_template WHERE id=1").fetchone()
            old_template, old_updated_at = old_row if old_row else (_DEFAULT_BID_TEMPLATE, _now())
            conn.execute('''CREATE TABLE bid_template_new (
                license_key TEXT PRIMARY KEY,
                template    TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )''')
            conn.execute(
                "INSERT INTO bid_template_new (license_key, template, updated_at) VALUES ('', ?, ?)",
                (old_template, old_updated_at)
            )
            conn.execute(
                "INSERT INTO bid_template_new (license_key, template, updated_at) VALUES (?, ?, ?)",
                (LEGACY_DATA_LICENSE_KEY, old_template, old_updated_at)
            )
            conn.execute('DROP TABLE bid_template')
            conn.execute('ALTER TABLE bid_template_new RENAME TO bid_template')
        # Seed the '' fallback row if it's somehow still missing (fresh
        # DB with no id=1 row ever existed) — never overwrites an
        # existing row, matching the old id=1 seed's own guarantee.
        if not conn.execute("SELECT 1 FROM bid_template WHERE license_key=''").fetchone():
            conn.execute(
                "INSERT INTO bid_template (license_key, template, updated_at) VALUES ('', ?, ?)",
                (_DEFAULT_BID_TEMPLATE, _now()),
            )

        # poller.py's heartbeat — single row, overwritten every outer-loop
        # tick regardless of per-license outcomes. This is the direct fix
        # for "I can't tell if it's working": the Settings UI polls this
        # to show "poller last ran Xs ago" instead of silence. Deliberately
        # NOT per-license — one poller process, one liveness signal.
        conn.execute('''CREATE TABLE IF NOT EXISTS poller_heartbeat (
            id                 INTEGER PRIMARY KEY CHECK (id = 1),
            last_run_at        TEXT,
            licenses_processed INTEGER DEFAULT 0,
            last_error         TEXT
        )''')
        conn.commit()
    finally:
        conn.close()


def get_load(license_key: str, order_id: str) -> Optional[dict]:
    """The single load to ACT on for this order_id — every bid-action call
    site (Telegram button callbacks, the web dashboard's Bid PC/Phone/
    Draft, driver notify) wants exactly one row, so this resolves to
    whichever posting of that order_id is newest when more than one
    exists (see put_load's message_id note)."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT data_json FROM loads WHERE license_key=? AND order_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (license_key, order_id)
        )
        row = cur.fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def get_recent_loads(license_key: str, limit: int = 30) -> list:
    """Newest first, for this account only — what /api/web/feed wants
    directly (the old dict version had to do items[-limit:][::-1]
    itself; ORDER BY does it here)."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT data_json, created_at FROM loads WHERE license_key=? ORDER BY created_at DESC LIMIT ?",
            (license_key, limit)
        )
        out = []
        for data_json, created_at in cur.fetchall():
            item = json.loads(data_json)
            # received_at (2026-09-29, "how long ago the load came" on the
            # web feed): this row's own created_at, the moment put_load
            # first stored this order — not something data_json carried
            # itself, so it's stitched on here rather than duplicated into
            # every write.
            item["received_at"] = created_at
            out.append(item)
        return out
    finally:
        conn.close()


def put_load(license_key: str, order_id: str, data: dict):
    """Upsert one load, then evict everything past MAX_LOADS oldest-first
    — same 500-entry cap the old in-memory dict enforced, now scoped
    per license so one busy account can't evict another's loads out
    of existence."""
    try:
        payload = json.dumps(data)
    except TypeError:
        # Defensive only — every field in this dict has always had to be
        # JSON-safe already (the same dict is returned verbatim as part
        # of the /api/parse JSON response), but never let a stray
        # non-serializable value crash the whole write.
        payload = json.dumps({k: v for k, v in data.items()
                               if k != "original_msg_full"})
    # message_id disambiguates distinct postings of the same order_id (see
    # the migration note on the loads table) — '' for desktop-sourced
    # loads, which keeps today's overwrite-on-repost behavior for them.
    message_id = (data.get("original_msg_full") or {}).get("id") or ""
    conn = _connect()
    try:
        now = _now()
        conn.execute(
            '''INSERT INTO loads (license_key, order_id, message_id, data_json, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(license_key, order_id, message_id) DO UPDATE SET
                   data_json=excluded.data_json, created_at=excluded.created_at''',
            (license_key, order_id, message_id, payload, now),
        )
        # Row-count cap, not a distinct-order_id cap — a given order_id can
        # now legitimately hold more than one row (one per message_id).
        conn.execute('''
            DELETE FROM loads WHERE license_key=? AND rowid NOT IN (
                SELECT rowid FROM loads WHERE license_key=? ORDER BY created_at DESC, rowid DESC LIMIT ?
            )''', (license_key, license_key, MAX_LOADS))
        conn.commit()
    finally:
        conn.close()


def get_bid_template(license_key: str = "") -> str:
    """Checks this license's own template first, falling back to the
    shared '' default row (matches the old single-global-row behavior
    for any account that hasn't customized its own)."""
    conn = _connect()
    try:
        if license_key:
            row = conn.execute(
                "SELECT template FROM bid_template WHERE license_key=?", (license_key,)
            ).fetchone()
            if row:
                return row[0]
        row = conn.execute(
            "SELECT template FROM bid_template WHERE license_key=''"
        ).fetchone()
        return row[0] if row else _DEFAULT_BID_TEMPLATE
    finally:
        conn.close()


def set_bid_template(license_key: str, template: str):
    conn = _connect()
    try:
        conn.execute(
            '''INSERT INTO bid_template (license_key, template, updated_at) VALUES (?,?,?)
               ON CONFLICT(license_key) DO UPDATE SET template=excluded.template,
                                                        updated_at=excluded.updated_at''',
            (license_key or "", template, _now()),
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
