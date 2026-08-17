# =============================================================
# bid_history.py  —  server-side module
#
# Stores every bid a dispatcher sends (one row per BID PC / BID PHONE /
# DRAFT click) and, later, the outcome inferred from the broker's
# email reply on that thread.
#
# Pattern mirrors license_db.py: module-level DB_PATH next to this
# file, plain sqlite3.connect() per call (no ORM, no pooling — SQLite
# handles that fine at this volume), init_db() called once from the
# FastAPI lifespan.
#
# HARDENING (post-incident): every function that opens a connection
# now does so via _connect(), and every function guarantees the
# connection is closed with try/finally — even on exception. Before
# this fix, an exception mid-function (e.g. a caller passing a bad
# field) could leave a connection open holding SQLite's write lock,
# which then blocked every subsequent write/read until the leaked
# connection was garbage-collected — the exact "works fine, then the
# whole server goes unreachable after N requests" failure mode.
# WAL mode + a real busy_timeout are also enabled so concurrent
# readers/writers from multiple request threads don't contend as
# easily in the first place.
# =============================================================

import sqlite3
import os
from typing import Optional
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bid_history.db")

_VALID_STATUSES = {"pending", "won", "lost", "no_response", "countered", "expired"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    """
    Every DB function gets its connection from here — never call
    sqlite3.connect() directly elsewhere in this module. timeout=10
    means SQLite will wait up to 10s for a lock instead of failing
    immediately under normal contention; WAL mode lets readers proceed
    without waiting on a writer at all.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    conn = _connect()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS bids (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id         TEXT NOT NULL,
            thread_id        TEXT,
            bid_method       TEXT,
            vehicle_type     TEXT,
            driver_name      TEXT,
            pickup_loc       TEXT,
            delivery_loc     TEXT,
            pickup_state     TEXT,
            delivery_state   TEXT,
            lane             TEXT,
            broker_name      TEXT,
            broker_email     TEXT,
            deadhead_miles   REAL,
            loaded_miles     REAL,
            total_miles      REAL,
            verified_miles   REAL,
            verified_source  TEXT,
            bid_amount       REAL,
            rate_per_mile    REAL,
            status           TEXT DEFAULT 'pending',
            outcome_source   TEXT,
            outcome_note     TEXT,
            created_at       TEXT,
            updated_at       TEXT,
            outcome_at       TEXT
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_bids_thread   ON bids(thread_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_bids_order    ON bids(order_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_bids_status   ON bids(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_bids_broker   ON bids(broker_email)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_bids_lane     ON bids(lane)')
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(cursor, row) -> dict:
    cols = [c[0] for c in cursor.description]
    return dict(zip(cols, row))


def _derive_state(loc: str) -> str:
    """Local mini-copy of parser_core's state extraction so this module
    has no hard import dependency on parser_core (keeps it usable
    standalone / from tools/scripts without pulling in geocoding etc.)."""
    if not loc:
        return ""
    import re
    US_STATES = {
        "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
        "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
        "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
        "TX","UT","VT","VA","WA","WV","WI","WY","DC",
    }
    clean = loc.strip().upper()
    m = re.search(r",\s*([A-Z]{2})\b", clean)
    if m and m.group(1) in US_STATES:
        return m.group(1)
    m = re.match(r"^([A-Z]{2})\s+\d{5}", clean)
    if m and m.group(1) in US_STATES:
        return m.group(1)
    for token in reversed(clean.split()):
        t = re.sub(r"\W", "", token)
        if t in US_STATES:
            return t
    return ""


def record_bid(order_id: str,
                thread_id: Optional[str] = None,
                bid_method: str = "",
                vehicle_type: str = "",
                driver_name: str = "",
                pickup_loc: str = "",
                delivery_loc: str = "",
                broker_name: str = "",
                broker_email: str = "",
                deadhead_miles: Optional[float] = None,
                loaded_miles: Optional[float] = None,
                total_miles: Optional[float] = None,
                verified_miles: Optional[float] = None,
                verified_source: Optional[str] = None,
                bid_amount: Optional[float] = None) -> int:
    """
    Insert one row per bid-send action (BID PC / BID PHONE / DRAFT
    click). Returns the new row's id.

    bid_amount is usually unknown at click time (the template ships
    with a blank "Rate: $" the dispatcher fills in by hand before
    sending) — pass it when known, None otherwise. rate_per_mile is
    only computed when both bid_amount and a mileage figure exist.
    """
    pickup_state   = _derive_state(pickup_loc)
    delivery_state = _derive_state(delivery_loc)
    lane = (f"{pickup_state}-{delivery_state}"
            if pickup_state and delivery_state else "")

    miles_for_rate = verified_miles or total_miles or deadhead_miles
    rate_per_mile = (round(bid_amount / miles_for_rate, 2)
                      if bid_amount and miles_for_rate else None)

    now = _now()
    conn = _connect()
    try:
        cur = conn.execute(
            '''INSERT INTO bids (
                order_id, thread_id, bid_method, vehicle_type, driver_name,
                pickup_loc, delivery_loc, pickup_state, delivery_state, lane,
                broker_name, broker_email,
                deadhead_miles, loaded_miles, total_miles, verified_miles, verified_source,
                bid_amount, rate_per_mile,
                status, created_at, updated_at
            ) VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?, ?,?,?,?,?, ?,?, 'pending', ?, ?)''',
            (order_id, thread_id, bid_method, vehicle_type, driver_name,
             pickup_loc, delivery_loc, pickup_state, delivery_state, lane,
             broker_name, broker_email,
             deadhead_miles, loaded_miles, total_miles, verified_miles, verified_source,
             bid_amount, rate_per_mile,
             now, now)
        )
        conn.commit()
        # lastrowid is only None when the last statement wasn't an INSERT —
        # it always was here, so this assert is a real invariant, not a cast.
        assert cur.lastrowid is not None
        return cur.lastrowid
    finally:
        conn.close()


def update_bid_amount(bid_id: int, bid_amount: float) -> bool:
    """Attach a rate to an existing bid row once it's known (e.g. a
    later UI step where the dispatcher confirms what they actually
    quoted). Recomputes rate_per_mile from whatever mileage is stored."""
    conn = _connect()
    try:
        row = conn.execute(
            'SELECT verified_miles, total_miles, deadhead_miles FROM bids WHERE id=?',
            (bid_id,)
        ).fetchone()
        if not row:
            return False
        miles_for_rate = row[0] or row[1] or row[2]
        rate_per_mile = round(bid_amount / miles_for_rate, 2) if miles_for_rate else None
        conn.execute(
            'UPDATE bids SET bid_amount=?, rate_per_mile=?, updated_at=? WHERE id=?',
            (bid_amount, rate_per_mile, _now(), bid_id)
        )
        conn.commit()
        return True
    finally:
        conn.close()


def update_bid_outcome(bid_id: int, status: str,
                        outcome_source: str = "", outcome_note: str = "") -> bool:
    """
    Mark a bid's outcome. status must be one of _VALID_STATUSES.
    outcome_source records HOW we know (e.g. 'broker_reply', 'manual',
    'timeout') — useful later to weight confidence in the learning
    layer (an inferred outcome is less certain than a manual one).
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid status '{status}' — must be one of {_VALID_STATUSES}")
    now = _now()
    conn = _connect()
    try:
        conn.execute(
            '''UPDATE bids SET status=?, outcome_source=?, outcome_note=?,
               updated_at=?, outcome_at=? WHERE id=?''',
            (status, outcome_source, outcome_note, now, now, bid_id)
        )
        updated = conn.total_changes > 0
        conn.commit()
        return updated
    finally:
        conn.close()


def get_pending_bids_for_thread(thread_id: str) -> list:
    """
    Used by the broker-reply watcher: given a Gmail thread_id that
    just got a new message, find any bids on that thread still
    awaiting an outcome so the reply can be classified against them.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            '''SELECT * FROM bids WHERE thread_id=? AND status='pending'
               ORDER BY created_at DESC''',
            (thread_id,)
        )
        return [_row_to_dict(cur, r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_bids_for_order(order_id: str) -> list:
    conn = _connect()
    try:
        cur = conn.execute(
            'SELECT * FROM bids WHERE order_id=? ORDER BY created_at DESC',
            (order_id,)
        )
        return [_row_to_dict(cur, r) for r in cur.fetchall()]
    finally:
        conn.close()


def expire_stale_pending(older_than_days: int = 3) -> int:
    """
    Housekeeping: a bid sitting 'pending' for days almost certainly
    means the broker never replied (or the reply-watcher missed it) —
    not that it's still live. Call this periodically so 'pending'
    stays a meaningful signal rather than an ever-growing junk drawer.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
    conn = _connect()
    try:
        cur = conn.execute("SELECT id, created_at FROM bids WHERE status='pending'")
        stale_ids = []
        for bid_id, created_at in cur.fetchall():
            try:
                ts = datetime.fromisoformat(created_at).timestamp()
            except Exception:
                continue
            if ts < cutoff:
                stale_ids.append(bid_id)
        now = _now()
        for bid_id in stale_ids:
            conn.execute(
                '''UPDATE bids SET status='no_response', outcome_source='timeout',
                   updated_at=?, outcome_at=? WHERE id=?''',
                (now, now, bid_id)
            )
        conn.commit()
        return len(stale_ids)
    finally:
        conn.close()


# =============================================================
# ANALYTICS  (foundation for the future bidding/decision engine —
# kept intentionally simple: plain aggregate queries, no ML yet)
# =============================================================

def _avg_rate_query(where_clause: str, params: tuple):
    conn = _connect()
    try:
        row = conn.execute(
            f'''SELECT AVG(rate_per_mile), COUNT(*)
                FROM bids WHERE rate_per_mile IS NOT NULL {where_clause}''',
            params
        ).fetchone()
        avg, n = row
        return {"avg_rate_per_mile": round(avg, 3) if avg else None, "sample_size": n}
    finally:
        conn.close()


def avg_rate_per_mile_by_broker(broker_email: str) -> dict:
    return _avg_rate_query("AND broker_email=?", (broker_email,))


def avg_rate_per_mile_by_lane(lane: str) -> dict:
    return _avg_rate_query("AND lane=?", (lane,))


def avg_rate_per_mile_by_vehicle(vehicle_type: str) -> dict:
    return _avg_rate_query("AND vehicle_type=?", (vehicle_type,))


def broker_summary(broker_email: str) -> dict:
    """Win rate + volume for one broker — pending bids excluded from
    the win-rate denominator since they haven't resolved yet."""
    conn = _connect()
    try:
        cur = conn.execute(
            '''SELECT status, COUNT(*) FROM bids WHERE broker_email=?
               GROUP BY status''',
            (broker_email,)
        )
        counts = {status: n for status, n in cur.fetchall()}
    finally:
        conn.close()

    won      = counts.get("won", 0)
    lost     = counts.get("lost", 0)
    pending  = counts.get("pending", 0)
    resolved = won + lost + counts.get("countered", 0)
    win_rate = round(won / resolved, 3) if resolved else None

    rate_info = avg_rate_per_mile_by_broker(broker_email)
    return {
        "broker_email":  broker_email,
        "total_bids":    sum(counts.values()),
        "won":           won,
        "lost":          lost,
        "pending":       pending,
        "win_rate":      win_rate,
        **rate_info,
    }


# =============================================================
# BID RECOMMENDATION  (foundation for the bidding/decision engine)
# =============================================================

# A pool needs at least this many resolved (rate_per_mile IS NOT NULL)
# bids before its average is trusted enough to suggest a rate from.
# Early on there simply won't be 10 bids in most pools yet — that's
# intentional: no recommendation is safer than one built on 2 data
# points. As more bids accumulate this naturally starts firing.
MIN_SAMPLE_SIZE = 10


def get_bid_recommendation(broker_email: str = "", lane: str = "",
                            vehicle_type: str = "",
                            miles: Optional[float] = None) -> Optional[dict]:
    """
    Returns a suggested bid amount using the most specific historical
    pool that has enough data, falling back to broader pools when it
    doesn't. Returns None if `miles` is unknown/zero or no pool below
    reaches MIN_SAMPLE_SIZE.

    Fallback order (most to least specific — broad mode, per config):
      1. this broker + this lane
      2. this lane (any broker)
      3. this vehicle type (any lane/broker)
      4. this broker (any lane)
    Each level is strictly less specific than the last, so this always
    prefers the tightest match that actually has enough volume rather
    than always falling all the way to the broadest pool.
    """
    if not miles:
        return None

    candidates = []
    if broker_email and lane:
        candidates.append(("broker+lane", "AND broker_email=? AND lane=?",
                            (broker_email, lane)))
    if lane:
        candidates.append(("lane", "AND lane=?", (lane,)))
    if vehicle_type:
        candidates.append(("vehicle_type", "AND vehicle_type=?", (vehicle_type,)))
    if broker_email:
        candidates.append(("broker", "AND broker_email=?", (broker_email,)))

    for basis, where, params in candidates:
        result = _avg_rate_query(where, params)
        if result["avg_rate_per_mile"] and result["sample_size"] >= MIN_SAMPLE_SIZE:
            rate = result["avg_rate_per_mile"]
            return {
                "basis":            basis,
                "sample_size":      result["sample_size"],
                "rate_per_mile":    rate,
                "suggested_amount": round(rate * miles, 2),
            }
    return None