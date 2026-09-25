# =============================================================
# route_cache_store.py — server-side module, added 2026-09-25
#
# Replaces parser_core.py's GEO_CACHE / ROUTE_CACHE / MAPS_CACHE — three
# plain in-process dicts, each loaded once from a JSON file at worker
# startup and flushed back every 30s by a per-worker daemon thread.
# Invisible across uvicorn's 4 worker processes: a geocode/route/Maps
# lookup learned by one worker was never visible to the other 3 until
# THAT worker itself restarted and re-read the JSON file — up to 4x
# redundant network geocoding/routing calls, and up to 4x redundant
# PAID Google Maps Directions calls (MAPS_CACHE exists specifically so
# a repeated origin/dest pair rarely hits the paid API after warmup;
# with 4 independent caches that was closer to a 25% hit rate). Same
# SQLite/WAL pattern as load_store.py/route_calibration.py — proven
# safe across the 4 worker processes.
#
# init_db() also does a ONE-TIME import from the existing
# geo_cache.json/route_cache.json/maps_verify_cache.json files (if
# present and the corresponding table is still empty) so the real
# accumulated production cache data isn't discarded by this migration.
# =============================================================

import sqlite3
import os
import json
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "geo_route_cache.db")

# Bumped whenever the cached VALUE shape changes enough that old entries
# could be wrong/stale under new code — mirrors parser_core.py's old
# CACHE_SCHEMA_VERSION JSON-file convention. Bumping this wipes all three
# tables on next startup, same "stale cache -> clean slate" behavior.
SCHEMA_VERSION = "1"

_LEGACY_FILES = {
    "geo_cache":   "geo_cache.json",
    "route_cache": "route_cache.json",
    "maps_cache":  "maps_verify_cache.json",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _import_legacy_json(conn: sqlite3.Connection, table: str, filename: str) -> None:
    path = os.path.join(BASE_DIR, filename)
    if not os.path.exists(path):
        return
    (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    if count:
        return  # already has data — never overwrite with a stale one-time import
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    now = time.time()
    rows = [
        (key, json.dumps(value), now)
        for key, value in data.items()
        if key != "__version__"
    ]
    if rows:
        conn.executemany(
            f"INSERT OR IGNORE INTO {table} (cache_key, value_json, updated_at) VALUES (?, ?, ?)",
            rows,
        )
        print(f"[CACHE] imported {len(rows)} entries from {filename} into {table}", flush=True)


def init_db():
    conn = _connect()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS _meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )''')
        for table in _LEGACY_FILES:
            conn.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
                cache_key  TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
        conn.commit()

        row = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            print(f"[CACHE] schema version changed ({row[0] if row else None} -> "
                  f"{SCHEMA_VERSION}) — clearing geo/route/maps caches.", flush=True)
            for table in _LEGACY_FILES:
                conn.execute(f"DELETE FROM {table}")
            conn.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.commit()

        for table, filename in _LEGACY_FILES.items():
            _import_legacy_json(conn, table, filename)
        conn.commit()
    finally:
        conn.close()


def _get(table: str, key: str):
    conn = _connect()
    try:
        row = conn.execute(f"SELECT value_json FROM {table} WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def _set(table: str, key: str, value) -> None:
    conn = _connect()
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {table} (cache_key, value_json, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def geo_get(key: str):
    return _get("geo_cache", key)


def geo_set(key: str, value) -> None:
    _set("geo_cache", key, value)


def route_get(key: str):
    return _get("route_cache", key)


def route_set(key: str, value) -> None:
    _set("route_cache", key, value)


def maps_get(key: str):
    return _get("maps_cache", key)


def maps_set(key: str, value) -> None:
    _set("maps_cache", key, value)
