# =============================================================
# route_calibration.py — server-side module
#
# Self-learning correction layer on top of GraphHopper's static blanket
# factors (GRAPHHOPPER_MILE_FACTOR / GRAPHHOPPER_CORRECTION in
# parser_core.py). Every time a route gets independently verified
# against Google Maps, we now remember how far off GraphHopper was for
# that STATE-TO-STATE lane, and apply a running-average correction to
# every future GraphHopper-only distance on the same lane — so the
# second (and every later) time a CO<->OK deadhead comes up, the
# GraphHopper number is nudged to match what Google Maps actually said
# last time, instead of repeating the same known error.
#
# Lane key is a SORTED two-state pair ("CO-OK", never "OK-CO") since
# real road distance is directionally symmetric — this doubles the
# sample density per lane for free.
#
# Same SQLite pattern as load_store.py/fleet_store.py/bid_history.py:
# module-level DB_PATH, _connect() per call, WAL mode — proven safe
# across the 4 uvicorn worker processes.
# =============================================================

import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "route_calibration.db")

# Guardrail: never let one bad Google Maps/GraphHopper reading (a wrong
# geocode, a Maps API hiccup) send a lane's correction factor off into
# the weeds. A real GH-vs-Maps miss this large would already be way
# outside anything routing on this project has produced.
_MIN_FACTOR = 0.5
_MAX_FACTOR = 2.0


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
        conn.execute('''CREATE TABLE IF NOT EXISTS lane_calibration (
            lane            TEXT PRIMARY KEY,
            factor          REAL NOT NULL,
            samples         INTEGER NOT NULL DEFAULT 1,
            last_gh_miles   REAL,
            last_maps_miles REAL,
            updated_at      TEXT
        )''')
        conn.commit()
    finally:
        conn.close()


def _lane_key(state_a: Optional[str], state_b: Optional[str]) -> Optional[str]:
    if not state_a or not state_b:
        return None
    a, b = sorted([state_a.upper(), state_b.upper()])
    if a == b:
        # Same-state ("intra-state") pairs deliberately excluded — found
        # live in production 2026-09-09: a "CO-CO" lane's factor, learned
        # from one short ~15mi-vs-25mi observation, got applied to a
        # completely different ~60mi-raw CO-CO route and inflated it to
        # 100mi when the real Maps-verified distance was 69mi (visible in
        # journalctl: "[ROUTE-CAL] lane CO-CO: 60mi -> 100mi", immediately
        # followed by a MAPS-VERIFY flag on the same load) — a state can
        # contain both a 5-mile hop and a 400-mile cross-state-sized trip,
        # so one blended factor for "the whole state" is far noisier than
        # a real inter-state pair, where both endpoints are pinned near
        # the state line and the corridor length is much more consistent.
        # Cross-state lanes are unaffected by this.
        return None
    return f"{a}-{b}"


def get_calibration_factor(state_a: Optional[str], state_b: Optional[str]) -> Optional[float]:
    """Learned correction factor for this lane, or None if never observed."""
    lane = _lane_key(state_a, state_b)
    if not lane:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT factor FROM lane_calibration WHERE lane=?", (lane,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def apply_calibration(state_a: Optional[str], state_b: Optional[str], gh_miles) -> float:
    """Nudge a GraphHopper-computed distance using this lane's learned
    factor. Returns gh_miles unchanged (rounded) if no calibration is
    on file yet for this lane, or if gh_miles is falsy."""
    if not gh_miles:
        return gh_miles
    factor = get_calibration_factor(state_a, state_b)
    if not factor:
        return gh_miles
    return round(gh_miles * factor)


def record_calibration(state_a: Optional[str], state_b: Optional[str],
                        gh_miles, maps_miles) -> None:
    """Record a fresh GH-vs-Maps observation for this lane. Blends into
    a running average of the correction factor rather than overwriting,
    so one noisy reading can't swing a well-established lane."""
    lane = _lane_key(state_a, state_b)
    if not lane or not gh_miles or not maps_miles:
        return
    new_factor = maps_miles / gh_miles
    if not (_MIN_FACTOR <= new_factor <= _MAX_FACTOR):
        print(f"[ROUTE-CAL] {lane}: rejecting outlier factor "
              f"{new_factor:.3f} (gh={gh_miles} maps={maps_miles})", flush=True)
        return

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT factor, samples FROM lane_calibration WHERE lane=?", (lane,)
        ).fetchone()
        if row:
            old_factor, samples = row
            blended = ((old_factor * samples) + new_factor) / (samples + 1)
            conn.execute(
                "UPDATE lane_calibration SET factor=?, samples=?, "
                "last_gh_miles=?, last_maps_miles=?, updated_at=? WHERE lane=?",
                (blended, samples + 1, gh_miles, maps_miles, _now(), lane),
            )
            print(f"[ROUTE-CAL] {lane}: factor {old_factor:.3f} -> {blended:.3f} "
                  f"(n={samples + 1}, this obs gh={gh_miles} maps={maps_miles})", flush=True)
        else:
            conn.execute(
                "INSERT INTO lane_calibration "
                "(lane, factor, samples, last_gh_miles, last_maps_miles, updated_at) "
                "VALUES (?, ?, 1, ?, ?, ?)",
                (lane, new_factor, gh_miles, maps_miles, _now()),
            )
            print(f"[ROUTE-CAL] {lane}: new lane, factor={new_factor:.3f} "
                  f"(gh={gh_miles} maps={maps_miles})", flush=True)
        conn.commit()
    finally:
        conn.close()
