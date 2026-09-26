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
#
# Per-license isolation, added 2026-09-25 — every function here now
# takes license_key and every query filters by it. Before this fix,
# ALL licenses shared one global fleet and one global blacklist (a
# truck or a blacklisted broker added under any account showed up for
# every other account too) — confirmed via direct inspection, not
# assumed. See migration_defaults.py for the one-time backfill target.
# =============================================================

import sqlite3
import os
import json
from typing import Optional
from datetime import datetime, timezone

from migration_defaults import LEGACY_DATA_LICENSE_KEY
import zip_geocode

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
            radius_miles     INTEGER,
            active           INTEGER DEFAULT 1,
            created_at       TEXT,
            updated_at       TEXT
        )''')
        # Migration for any trucks.db created before this column existed —
        # CREATE TABLE IF NOT EXISTS above is a no-op on an existing table,
        # so a new column needs its own ALTER TABLE, same pattern as
        # license_db.py's thread_learning_enabled/telegram_enabled columns.
        # NULL (not a number) means "use the global max_radius_miles
        # default" — this is an override, not a required value.
        try:
            conn.execute('ALTER TABLE trucks ADD COLUMN radius_miles INTEGER')
        except sqlite3.OperationalError:
            pass  # column already exists
        # Same migration pattern, 2026-09-24 — the desktop app's per-truck
        # config already has loaded_miles_min/max (a filter on the LOAD's
        # own loaded-miles distance, distinct from radius_miles which is
        # deadhead), but this server-side store never gained the columns,
        # so the web dashboard couldn't set them for standalone-mode
        # matching. NULL means "no bound" either way.
        try:
            conn.execute('ALTER TABLE trucks ADD COLUMN loaded_miles_min INTEGER')
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute('ALTER TABLE trucks ADD COLUMN loaded_miles_max INTEGER')
        except sqlite3.OperationalError:
            pass
        # Per-license isolation, 2026-09-25 — plain ADD COLUMN is enough
        # here (unlike broker_blacklist below): `id` is already the
        # primary key, license_key is just a new filter column, no key
        # change needed. Every pre-existing row backfills to the one
        # real account already using this table (see migration_defaults.py).
        try:
            conn.execute('ALTER TABLE trucks ADD COLUMN license_key TEXT')
        except sqlite3.OperationalError:
            pass
        conn.execute(
            'UPDATE trucks SET license_key=? WHERE license_key IS NULL',
            (LEGACY_DATA_LICENSE_KEY,)
        )

        # Map of truck locations, 2026-09-25 — plain ADD COLUMN again, same
        # reasoning as license_key above (no primary-key change needed).
        # Geocoded from zip_location via the already-built offline
        # zip_geocode.lookup() (no network call). NULL means "couldn't be
        # resolved" (bad/foreign ZIP) — the map simply skips that truck,
        # not an error.
        for _col in ("lat", "lon"):
            try:
                conn.execute(f'ALTER TABLE trucks ADD COLUMN {_col} REAL')
            except sqlite3.OperationalError:
                pass
        # Driver's own Telegram chat ID (desktop truck-line field 10,
        # 2026-09-26 web parity) — used by the web driver bot; NULL means
        # "this driver isn't on the driver bot".
        try:
            conn.execute('ALTER TABLE trucks ADD COLUMN telegram_chat_id INTEGER')
        except sqlite3.OperationalError:
            pass
        rows = conn.execute(
            "SELECT id, zip_location FROM trucks WHERE lat IS NULL AND zip_location IS NOT NULL AND zip_location != ''"
        ).fetchall()
        for truck_id, zip_loc in rows:
            coords = zip_geocode.lookup(zip_loc)
            if coords:
                conn.execute("UPDATE trucks SET lat=?, lon=? WHERE id=?",
                             (coords[0], coords[1], truck_id))

        # broker_blacklist's primary key changes from broker_email alone
        # to (license_key, broker_email) — a broker one account
        # blacklists must not silently apply to every other account.
        # SQLite can't ALTER a primary key, so this recreates the table
        # (production has 0 rows in it today, confirmed before writing
        # this — trivial/safe either way).
        conn.execute('''CREATE TABLE IF NOT EXISTS broker_blacklist (
            broker_email  TEXT PRIMARY KEY,
            broker_name   TEXT DEFAULT '',
            note          TEXT DEFAULT '',
            created_at    TEXT
        )''')
        cols = {row[1] for row in conn.execute('PRAGMA table_info(broker_blacklist)')}
        if "license_key" not in cols:
            conn.execute('''CREATE TABLE broker_blacklist_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key   TEXT NOT NULL,
                broker_email  TEXT NOT NULL,
                broker_name   TEXT DEFAULT '',
                note          TEXT DEFAULT '',
                created_at    TEXT,
                UNIQUE(license_key, broker_email)
            )''')
            conn.execute(
                '''INSERT INTO broker_blacklist_new
                       (license_key, broker_email, broker_name, note, created_at)
                   SELECT ?, broker_email, broker_name, note, created_at
                   FROM broker_blacklist''',
                (LEGACY_DATA_LICENSE_KEY,)
            )
            conn.execute('DROP TABLE broker_blacklist')
            conn.execute('ALTER TABLE broker_blacklist_new RENAME TO broker_blacklist')

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

def list_trucks(license_key: str, active_only: bool = True) -> list:
    conn = _connect()
    try:
        where = "WHERE license_key=?" + (" AND active=1" if active_only else "")
        cur = conn.execute(f"SELECT * FROM trucks {where} ORDER BY driver_name", (license_key,))
        return [_truck_out(_row_to_dict(cur, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def add_truck(license_key: str, vehicle: str, driver_name: str, zip_location: str,
              dimensions: str = "", max_payload_lbs: Optional[int] = None,
              equipment: str = "", allowed_states: Optional[list] = None,
              pickup_date: str = "", radius_miles: Optional[int] = None,
              loaded_miles_min: Optional[int] = None,
              loaded_miles_max: Optional[int] = None,
              telegram_chat_id: Optional[int] = None) -> int:
    now = _now()
    coords = zip_geocode.lookup(zip_location.strip()) or [None, None]
    conn = _connect()
    try:
        cur = conn.execute(
            '''INSERT INTO trucks (license_key, vehicle, driver_name, dimensions, max_payload_lbs,
                equipment, allowed_states, zip_location, pickup_date, radius_miles,
                loaded_miles_min, loaded_miles_max, lat, lon, telegram_chat_id,
                active, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)''',
            (license_key, vehicle.upper().strip(), driver_name.strip(), dimensions.strip(),
             max_payload_lbs, equipment.strip(),
             json.dumps(allowed_states) if allowed_states else None,
             zip_location.strip(), pickup_date.strip(), radius_miles,
             loaded_miles_min, loaded_miles_max, coords[0], coords[1], telegram_chat_id, now, now)
        )
        conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid
    finally:
        conn.close()


def update_truck(license_key: str, truck_id: int, **fields) -> bool:
    """Partial update — pass only the fields that changed. allowed_states,
    if given, must be a list or None. Scoped to license_key so a truck
    id belonging to a different account can't be modified even if
    guessed/reused."""
    if not fields:
        return False
    allowed_cols = {"vehicle", "driver_name", "dimensions", "max_payload_lbs",
                     "equipment", "allowed_states", "zip_location", "pickup_date",
                     "radius_miles", "loaded_miles_min", "loaded_miles_max", "telegram_chat_id",
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
        if k == "zip_location" and v:
            coords = zip_geocode.lookup(v) or [None, None]
            sets.append("lat=?")
            params.append(coords[0])
            sets.append("lon=?")
            params.append(coords[1])
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(_now())
    params.append(truck_id)
    params.append(license_key)
    conn = _connect()
    try:
        conn.execute(f"UPDATE trucks SET {', '.join(sets)} WHERE id=? AND license_key=?", params)
        updated = conn.total_changes > 0
        conn.commit()
        return updated
    finally:
        conn.close()


def delete_truck(license_key: str, truck_id: int) -> bool:
    """Soft delete (active=0) — keeps history/references intact rather
    than hard-deleting a truck that may be referenced elsewhere."""
    return update_truck(license_key, truck_id, active=0)


# =============================================================
# BROKER BLACKLIST
# =============================================================

def is_broker_blacklisted(license_key: str, broker_email: str) -> bool:
    if not broker_email:
        return False
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM broker_blacklist WHERE license_key=? AND broker_email=?",
            (license_key, broker_email.lower().strip())
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def list_blacklisted_brokers(license_key: str) -> list:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM broker_blacklist WHERE license_key=? ORDER BY created_at DESC",
            (license_key,)
        )
        return [_row_to_dict(cur, r) for r in cur.fetchall()]
    finally:
        conn.close()


def blacklist_broker(license_key: str, broker_email: str, broker_name: str = "", note: str = "") -> bool:
    conn = _connect()
    try:
        conn.execute(
            '''INSERT INTO broker_blacklist (license_key, broker_email, broker_name, note, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(license_key, broker_email) DO UPDATE SET
                 broker_name=excluded.broker_name, note=excluded.note''',
            (license_key, broker_email.lower().strip(), broker_name, note, _now())
        )
        conn.commit()
        return True
    finally:
        conn.close()


def unblacklist_broker(license_key: str, broker_email: str) -> bool:
    conn = _connect()
    try:
        conn.execute("DELETE FROM broker_blacklist WHERE license_key=? AND broker_email=?",
                      (license_key, broker_email.lower().strip()))
        deleted = conn.total_changes > 0
        conn.commit()
        return deleted
    finally:
        conn.close()
