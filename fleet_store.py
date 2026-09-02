# =============================================================
# fleet_store.py — server-side module
#
# NEW source of truth for truck fleet + broker blacklist, added for
# the web dashboard. Previously trucks only ever existed in the
# desktop client's local config file (plutus_config.json) and were
# passed to /api/parse per-request — never persisted server-side. The
# web app has no desktop process to lean on, so this gives it (and,
# eventually, the desktop app too) a real, shared, persistent store.
#
# Field names mirror models.TruckDef exactly (vehicle, driver_name,
# dimensions, max_payload_lbs, equipment, allowed_states, zip_location,
# pickup_date) so a future desktop migration to this store is a
# straightforward field-for-field mapping, not a redesign.
#
# Pattern mirrors bid_history.py: module-level DB_PATH, _connect() per
# call, WAL mode, try/finally close.
# =============================================================

import sqlite3
import os
import json
from typing import Optional
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "fleet_store.db")


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
        conn.execute('''CREATE TABLE IF NOT EXISTS trucks (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle          TEXT NOT NULL,
            driver_name      TEXT NOT NULL,
            dimensions       TEXT DEFAULT '',
            max_payload_lbs  INTEGER,
            equipment        TEXT DEFAULT '',
            allowed_states   TEXT,
            zip_location     TEXT NOT NULL,
            pickup_date      TEXT DEFAULT '',
            active           INTEGER DEFAULT 1,
            created_at       TEXT,
            updated_at       TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS broker_blacklist (
            broker_email  TEXT PRIMARY KEY,
            broker_name   TEXT DEFAULT '',
            note          TEXT DEFAULT '',
            created_at    TEXT
        )''')
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(cursor, row) -> dict:
    cols = [c[0] for c in cursor.description]
    return dict(zip(cols, row))


def _truck_out(d: dict) -> dict:
    # allowed_states is stored as a JSON string (or NULL); expose it as
    # a real list to match TruckDef's shape.
    d["allowed_states"] = json.loads(d["allowed_states"]) if d.get("allowed_states") else None
    d["active"] = bool(d.get("active", 1))
    return d


# =============================================================
# TRUCKS
# =============================================================

def list_trucks(active_only: bool = True) -> list:
    conn = _connect()
    try:
        where = "WHERE active=1" if active_only else ""
        cur = conn.execute(f"SELECT * FROM trucks {where} ORDER BY driver_name")
        return [_truck_out(_row_to_dict(cur, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def add_truck(vehicle: str, driver_name: str, zip_location: str,
              dimensions: str = "", max_payload_lbs: Optional[int] = None,
              equipment: str = "", allowed_states: Optional[list] = None,
              pickup_date: str = "") -> int:
    now = _now()
    conn = _connect()
    try:
        cur = conn.execute(
            '''INSERT INTO trucks (vehicle, driver_name, dimensions, max_payload_lbs,
                equipment, allowed_states, zip_location, pickup_date, active,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,1,?,?)''',
            (vehicle.upper().strip(), driver_name.strip(), dimensions.strip(),
             max_payload_lbs, equipment.strip(),
             json.dumps(allowed_states) if allowed_states else None,
             zip_location.strip(), pickup_date.strip(), now, now)
        )
        conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid
    finally:
        conn.close()


def update_truck(truck_id: int, **fields) -> bool:
    """Partial update — pass only the fields that changed. allowed_states,
    if given, must be a list or None."""
    if not fields:
        return False
    allowed_cols = {"vehicle", "driver_name", "dimensions", "max_payload_lbs",
                     "equipment", "allowed_states", "zip_location", "pickup_date",
                     "active"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed_cols:
            continue
        if k == "allowed_states":
            v = json.dumps(v) if v else None
        if k == "vehicle" and isinstance(v, str):
            v = v.upper().strip()
        sets.append(f"{k}=?")
        params.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(_now())
    params.append(truck_id)
    conn = _connect()
    try:
        conn.execute(f"UPDATE trucks SET {', '.join(sets)} WHERE id=?", params)
        updated = conn.total_changes > 0
        conn.commit()
        return updated
    finally:
        conn.close()


def delete_truck(truck_id: int) -> bool:
    """Soft delete (active=0) — keeps history/references intact rather
    than hard-deleting a truck that may be referenced elsewhere."""
    return update_truck(truck_id, active=0)


# =============================================================
# BROKER BLACKLIST
# =============================================================

def is_broker_blacklisted(broker_email: str) -> bool:
    if not broker_email:
        return False
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM broker_blacklist WHERE broker_email=?",
            (broker_email.lower().strip(),)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def list_blacklisted_brokers() -> list:
    conn = _connect()
    try:
        cur = conn.execute("SELECT * FROM broker_blacklist ORDER BY created_at DESC")
        return [_row_to_dict(cur, r) for r in cur.fetchall()]
    finally:
        conn.close()


def blacklist_broker(broker_email: str, broker_name: str = "", note: str = "") -> bool:
    conn = _connect()
    try:
        conn.execute(
            '''INSERT INTO broker_blacklist (broker_email, broker_name, note, created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(broker_email) DO UPDATE SET
                 broker_name=excluded.broker_name, note=excluded.note''',
            (broker_email.lower().strip(), broker_name, note, _now())
        )
        conn.commit()
        return True
    finally:
        conn.close()


def unblacklist_broker(broker_email: str) -> bool:
    conn = _connect()
    try:
        conn.execute("DELETE FROM broker_blacklist WHERE broker_email=?",
                      (broker_email.lower().strip(),))
        deleted = conn.total_changes > 0
        conn.commit()
        return deleted
    finally:
        conn.close()
