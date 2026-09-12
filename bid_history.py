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
import math
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

def init_processed_threads_table():
    """
    Call once at startup (main.py's lifespan already calls bid_history.init_db();
    add this call right next to it). Tracks which Gmail threads thread_learner
    has already walked, and how many messages were in them last time, so a
    recurring sweep only re-processes threads that actually got new replies.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS processed_threads (
        thread_id      TEXT PRIMARY KEY,
        message_count  INTEGER,
        last_checked_at TEXT
    )''')
    conn.commit()
    conn.close()


def get_processed_thread_count(thread_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        'SELECT message_count FROM processed_threads WHERE thread_id=?', (thread_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def mark_thread_processed(thread_id: str, message_count: int):
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'INSERT INTO processed_threads (thread_id, message_count, last_checked_at) '
        'VALUES (?,?,?) '
        'ON CONFLICT(thread_id) DO UPDATE SET message_count=excluded.message_count, '
        'last_checked_at=excluded.last_checked_at',
        (thread_id, message_count, now)
    )
    conn.commit()
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
                bid_amount: Optional[float] = None,
                occurred_at: Optional[str] = None) -> int:
    """
    Insert one row per bid-send action (BID PC / BID PHONE / DRAFT
    click). Returns the new row's id.

    bid_amount is usually unknown at click time (the template ships
    with a blank "Rate: $" the dispatcher fills in by hand before
    sending) — pass it when known, None otherwise. rate_per_mile is
    only computed when both bid_amount and a mileage figure exist.

    occurred_at: when the bid actually happened, as an ISO timestamp —
    for a live PC/PHONE/DRAFT click this is always "now" (the default,
    leave unset). thread_learner's backfill MUST pass the real
    message date here instead: without it, every backfilled row's
    created_at silently becomes "whenever the backfill script ran"
    instead of the real historical date, which is actively misleading
    on any UI that sorts/displays by created_at (confirmed: this is
    exactly what happened — 148 backfilled rows all showed up dated
    the night the backfill ran, not their real Gmail dates).
    """
    pickup_state   = _derive_state(pickup_loc)
    delivery_state = _derive_state(delivery_loc)
    lane = (f"{pickup_state}-{delivery_state}"
            if pickup_state and delivery_state else "")

    # rate_per_mile MUST be price-per-real-trip-mile (loaded, or
    # loaded+deadhead) — never deadhead-alone. Found live 2026-09-09: a
    # client-visible "$9,287 suggested bid" ($128.98/mi) on an 85-mile
    # total trip, traced to exactly this — verified_miles/deadhead_miles
    # (the truck-to-pickup deadhead leg ALONE, typically a small slice
    # of the real trip) was being preferred over total_miles whenever
    # both existed, and used as a last-resort denominator even when it
    # was the ONLY figure on hand. A $3000 bid divided by a 13-mile
    # deadhead produces a meaningless $230/mi that has no relationship
    # to real freight economics, and several of these had already
    # poisoned the vehicle_type-wide average other bids get compared
    # against. total_miles/loaded_miles are the only valid basis now;
    # if neither is known, rate_per_mile stays None (never guessed from
    # deadhead) rather than storing a number that will mislead every
    # future suggestion pulling from this pool.
    miles_for_rate = total_miles or loaded_miles
    rate_per_mile = (round(bid_amount / miles_for_rate, 2)
                      if bid_amount and miles_for_rate else None)

    now = occurred_at or _now()
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
    quoted, or the automatic thread-learning backfill). Recomputes
    rate_per_mile from the real trip mileage — see record_bid()'s
    miles_for_rate docstring for why verified_miles/deadhead_miles are
    deliberately excluded here."""
    conn = _connect()
    try:
        row = conn.execute(
            'SELECT total_miles, loaded_miles FROM bids WHERE id=?',
            (bid_id,)
        ).fetchone()
        if not row:
            return False
        miles_for_rate = row[0] or row[1]
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


def get_recent_bids(limit: int = 50) -> list:
    """Most recent bids across all orders — for the web dashboard's
    bid-history table. Newest first."""
    conn = _connect()
    try:
        cur = conn.execute(
            'SELECT * FROM bids ORDER BY created_at DESC LIMIT ?',
            (limit,)
        )
        return [_row_to_dict(cur, r) for r in cur.fetchall()]
    finally:
        conn.close()


def overall_summary() -> dict:
    """Aggregate win-rate + volume across ALL bids — the web
    dashboard's top-line stats cards. Mirrors broker_summary()'s shape
    but with no WHERE clause; pending bids excluded from the win-rate
    denominator since they haven't resolved yet, same as broker_summary."""
    conn = _connect()
    try:
        cur = conn.execute('SELECT status, COUNT(*) FROM bids GROUP BY status')
        counts = {status: n for status, n in cur.fetchall()}
    finally:
        conn.close()

    won      = counts.get("won", 0)
    lost     = counts.get("lost", 0)
    pending  = counts.get("pending", 0)
    resolved = won + lost + counts.get("countered", 0)
    win_rate = round(won / resolved, 3) if resolved else None

    conn = _connect()
    try:
        row = conn.execute(
            'SELECT AVG(rate_per_mile), COUNT(*) FROM bids WHERE rate_per_mile IS NOT NULL'
        ).fetchone()
        avg_rate, rate_n = row
    finally:
        conn.close()

    return {
        "total_bids":       sum(counts.values()),
        "won":              won,
        "lost":             lost,
        "pending":          pending,
        "win_rate":         win_rate,
        "avg_rate_per_mile": round(avg_rate, 3) if avg_rate else None,
        "rate_sample_size": rate_n,
    }


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


def list_all_brokers() -> list:
    """Every distinct broker_email that's ever appeared on a bid, with
    the same summary shape as broker_summary() — for the web
    dashboard's Brokers page. Skips blank broker_email (a lot of
    gmail_backfill rows never captured one)."""
    conn = _connect()
    try:
        emails = [r[0] for r in conn.execute(
            "SELECT DISTINCT broker_email FROM bids "
            "WHERE broker_email IS NOT NULL AND broker_email != ''"
        ).fetchall()]
    finally:
        conn.close()
    return [broker_summary(email) for email in emails]


# =============================================================
# BID RECOMMENDATION  (foundation for the bidding/decision engine)
# =============================================================

# A pool needs at least this many resolved (rate_per_mile IS NOT NULL)
# bids before its average is trusted enough to suggest a rate from.
# Early on there simply won't be 10 bids in most pools yet — that's
# intentional: no recommendation is safer than one built on 2 data
# points. As more bids accumulate this naturally starts firing.
MIN_SAMPLE_SIZE = 10

# Smaller floor for the vehicle_type+distance tier below — it's already
# narrowed by mileage bracket on top of vehicle type, so a pool this
# specific reaching even half of MIN_SAMPLE_SIZE is still a meaningful
# signal, and requiring the full 10 here would mean it essentially never
# fires until there's a LOT more history than exists today.
_BRACKET_MIN_SAMPLE_SIZE = 5

# ── Industry-baseline fallback ───────────────────────────────────────
# Client-reported 2026-09-12: suggested bids were coming back way too
# high on long-haul loads. Root cause: the old "vehicle_type" tier
# averaged EVERY historical bid for that vehicle type together
# regardless of distance — and real dispatcher economics don't work
# that way. Expedited/hot-shot freight is priced per mile at its
# HIGHEST on short/local runs (a truck has real minimum-charge
# economics: driver time, fuel to get moving, dispatch overhead, none
# of which shrink just because the trip is short) and settles to a
# lower, roughly flat per-mile rate on long hauls as those fixed costs
# amortize over more miles. Blending a batch of short local bids (high
# $/mi, inherently) into a suggestion for an 800-mile lane produced
# exactly the client's real "$9,287 for an 85-mile load" bug and this
# same-shaped "too high on long-haul" complaint.
#
# A given STATE's own freight density/backhaul availability shifts
# that long-haul floor too — a geographically peripheral/lower-density
# market (fewer other loads to pick up nearby once you deliver) means
# more empty-mile risk on the truck's NEXT load, so brokers price that
# risk into the rate even at real distance. Client named FL/WA/UT
# specifically as running ~$2.50/mi long-haul vs. ~$1.80/mi elsewhere —
# these three are kept as an easily-extended list below, not a claim
# that only these three states behave this way.
#
# This whole curve is ONLY the fallback used when real historical data
# for a tighter, more specific pool (broker+lane, lane, or vehicle+
# distance-bracket) doesn't meet its sample threshold yet — which is
# nearly always true today (industry-wide, the whole system has under
# 40 resolved bids on file). As real volume accumulates, the tiers
# above this naturally take over on their own; nothing about this
# fallback blocks that.
PREMIUM_RATE_STATES = {"FL", "WA", "UT"}  # extend freely — a flat set, not tuned per-state

_LOCAL_RATE_PER_MILE        = 3.00  # asymptotic rate as distance -> 0
_LONG_HAUL_RATE_STANDARD    = 1.80  # asymptotic rate as distance -> large, typical state
_LONG_HAUL_RATE_PREMIUM     = 2.50  # asymptotic rate as distance -> large, PREMIUM_RATE_STATES
_RATE_DECAY_MILES           = 180   # how fast the "local premium" fades with distance


def _industry_baseline_rate(miles: float, pickup_state: Optional[str],
                             delivery_state: Optional[str]) -> float:
    """
    Smoothly blends from the local rate down to the appropriate
    long-haul asymptote (exponential decay, not a hard cutoff at some
    arbitrary "local vs long-haul" mile mark — real rates don't cliff-
    edge like that). At miles=0 this returns _LOCAL_RATE_PER_MILE; by
    a few hundred miles it's converged to within a few cents of the
    long-haul rate for that lane's states.
    """
    states = {s for s in (pickup_state, delivery_state) if s}
    long_haul_rate = (_LONG_HAUL_RATE_PREMIUM if states & PREMIUM_RATE_STATES
                       else _LONG_HAUL_RATE_STANDARD)
    # `miles or 0` then clamped to >= 0 rather than an early return on
    # "falsy" — 0 is a legitimate (if unlikely) distance and the decay
    # formula already handles it correctly on its own (decay=1.0 at
    # miles=0, giving exactly _LOCAL_RATE_PER_MILE); a bare `if not
    # miles` would wrongly treat a real 0 the same as a missing value
    # and skip straight to the long-haul rate instead. get_bid_
    # recommendation() itself already rejects a falsy `miles` before
    # ever reaching here, so this clamp is only a defensive floor
    # against a stray negative value overshooting the local rate.
    miles = max(miles or 0, 0)
    decay = math.exp(-miles / _RATE_DECAY_MILES)
    return round(long_haul_rate + (_LOCAL_RATE_PER_MILE - long_haul_rate) * decay, 2)


def _mileage_bracket_clause(miles: float):
    """
    SQL fragment restricting a rate query to bids of a similar distance
    profile, so the vehicle_type fallback tier can't blend a short
    local trip's naturally-higher $/mi into a long-haul suggestion (or
    vice versa) — see the module-level comment above for why that
    matters. Brackets: local (<150mi), medium (150-500mi), long (500mi+).
    """
    if miles < 150:
        return "AND total_miles IS NOT NULL AND total_miles < 150", ()
    elif miles < 500:
        return ("AND total_miles IS NOT NULL AND total_miles >= 150 "
                 "AND total_miles < 500", ())
    else:
        return "AND total_miles IS NOT NULL AND total_miles >= 500", ()


def get_bid_recommendation(broker_email: str = "", lane: str = "",
                            vehicle_type: str = "",
                            miles: Optional[float] = None) -> Optional[dict]:
    """
    Returns a suggested bid amount using the most specific historical
    pool that has enough data, falling back to progressively broader
    pools, and finally to a calibrated industry-baseline curve
    (_industry_baseline_rate) rather than ever guessing from a
    distance-blind blended average. Returns None only if `miles` is
    unknown/zero — every other case now returns SOME grounded estimate.

    Fallback order (most to least specific):
      1. this broker + this lane
      2. this lane (any broker)                      — a lane is
         already a state pair, so this stays naturally
         distance-consistent without extra bucketing
      3. this vehicle type, bracketed by distance (any lane/broker)
      4. this broker (any lane)
      5. the industry-baseline curve (no real data reaches any tier
         above with enough samples yet)
    Each level is strictly less specific than the last, so this always
    prefers the tightest match that actually has enough volume rather
    than always falling all the way to the broadest pool.
    """
    if not miles:
        return None

    candidates = []
    if broker_email and lane:
        candidates.append(("broker+lane", "AND broker_email=? AND lane=?",
                            (broker_email, lane), MIN_SAMPLE_SIZE))
    if lane:
        candidates.append(("lane", "AND lane=?", (lane,), MIN_SAMPLE_SIZE))
    if vehicle_type:
        bracket_where, bracket_params = _mileage_bracket_clause(miles)
        candidates.append(("vehicle_type+distance",
                            f"AND vehicle_type=? {bracket_where}",
                            (vehicle_type,) + bracket_params,
                            _BRACKET_MIN_SAMPLE_SIZE))
    if broker_email:
        candidates.append(("broker", "AND broker_email=?", (broker_email,), MIN_SAMPLE_SIZE))

    for basis, where, params, min_samples in candidates:
        result = _avg_rate_query(where, params)
        if result["avg_rate_per_mile"] and result["sample_size"] >= min_samples:
            rate = result["avg_rate_per_mile"]
            return {
                "basis":            basis,
                "sample_size":      result["sample_size"],
                "rate_per_mile":    rate,
                "suggested_amount": round(rate * miles, 2),
            }

    pickup_state, delivery_state = (lane.split("-", 1) if lane and "-" in lane
                                     else (None, None))
    rate = _industry_baseline_rate(miles, pickup_state, delivery_state)
    return {
        "basis":            "industry_baseline",
        "sample_size":      0,
        "rate_per_mile":    rate,
        "suggested_amount": round(rate * miles, 2),
    }