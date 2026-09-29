import sqlite3
import os
from typing import Optional
from datetime import datetime, timezone

# Forces the script to always find its files right where the code sits
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "licenses.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS licenses (
        key          TEXT PRIMARY KEY,
        active       INTEGER DEFAULT 1,
        machine_id   TEXT,
        machine_name TEXT,
        created_at   TEXT,
        expires_at   TEXT,
        last_heartbeat TEXT
    )''')
    # Migration: add thread_learning_enabled to any DB created before this
    # feature existed. ALTER TABLE ADD COLUMN errors if the column is
    # already there, so this is wrapped and safe to run on every startup.
    try:
        conn.execute(
            'ALTER TABLE licenses ADD COLUMN thread_learning_enabled INTEGER DEFAULT 0'
        )
    except sqlite3.OperationalError:
        pass  # column already exists
    # Same migration pattern, opposite default: Telegram sending is
    # EXISTING behavior (unlike thread_learning, which was a new opt-in
    # feature) — defaulting to 0 here would silently mute every current
    # user the moment this column exists. Default 1 so nothing changes
    # for anyone until they explicitly turn it off.
    try:
        conn.execute(
            'ALTER TABLE licenses ADD COLUMN telegram_enabled INTEGER DEFAULT 1'
        )
    except sqlite3.OperationalError:
        pass  # column already exists
    # Standalone engine (2026-09-05) — the server acting as the bot itself,
    # with no desktop needing to be open. Every one of these defaults to
    # off/empty: nothing here should ever silently start reading someone's
    # inbox or sending Telegram messages just because the column now
    # exists. standalone_mode_enabled is the actual master switch;
    # standalone_bot_token/chat_ids are separate from the existing
    # telegram_enabled toggle (that one gates whether the DESKTOP is
    # allowed to send — these are the credentials the STANDALONE poller
    # sends with, since it has no desktop config to read from at all).
    for _col, _decl in [
        ('standalone_mode_enabled',     'INTEGER DEFAULT 0'),
        ('standalone_allowed_vehicles', "TEXT DEFAULT ''"),
        ('standalone_max_radius_miles', 'INTEGER'),
        ('standalone_chat_ids',         "TEXT DEFAULT ''"),
        ('standalone_bot_token',        "TEXT DEFAULT ''"),
        # Web driver bot (2026-09-26): this license's OWN driver-bot token,
        # separate from the dispatcher bot token above and never the
        # desktop's hardcoded driver token (see KNOWN_DESKTOP_DRIVER_BOT_TOKENS).
        ('standalone_driver_bot_token',  "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f'ALTER TABLE licenses ADD COLUMN {_col} {_decl}')
        except sqlite3.OperationalError:
            pass  # column already exists
    # A human-assigned label (e.g. client name), distinct from machine_name
    # (which the DESKTOP app sets automatically from the device it's
    # activated on) — this one is set by us, purely so a raw license key
    # isn't the only way to tell whose account is whose.
    try:
        conn.execute("ALTER TABLE licenses ADD COLUMN label TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Desktop/standalone mutual exclusion, 2026-09-25 — a real conflict
    # surfaced live: two licenses sharing one Gmail account raced on
    # Gmail's own read/unread state (whichever side polled first marked
    # everything read, starving the other). User's explicit design for
    # the real case this needs to solve (ONE license switching between
    # desktop and standalone against the SAME Gmail account): automatic
    # mutual exclusion, not just a warning. This column is the desktop's
    # side of that — updated every ~20s by a running desktop's poll loop
    # (see /api/desktop/poll_heartbeat), read by poller.py to skip a
    # license's standalone cycle while the desktop is presumed active.
    try:
        conn.execute("ALTER TABLE licenses ADD COLUMN desktop_poll_heartbeat TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Web version parity with the desktop's START (2026-09-26): the
    # desktop does a one-time catch-up scan of the last 2 days of unread
    # mail (plus a "Watching" Telegram message) every time it starts.
    # This flag is reset to 0 whenever standalone mode is ENABLED and set
    # to 1 by poller.py once that scan + message have been done, so the
    # scan runs exactly once per enable, never on every poller restart.
    try:
        conn.execute("ALTER TABLE licenses ADD COLUMN standalone_initial_scan_done INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()


def _get_row(key: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        'SELECT key, active, machine_id, machine_name, created_at, expires_at, last_heartbeat '
        'FROM licenses WHERE key=?', (key,)
    ).fetchone()
    conn.close()
    return row

def get_thread_learning_enabled(key: str) -> bool:
    row = _get_row(key)
    if not row:
        return False
    conn = sqlite3.connect(DB_PATH)
    val = conn.execute(
        'SELECT thread_learning_enabled FROM licenses WHERE key=?', (key,)
    ).fetchone()
    conn.close()
    return bool(val[0]) if val else False


def set_thread_learning_enabled(key: str, enabled: bool) -> bool:
    row = _get_row(key)
    if not row:
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'UPDATE licenses SET thread_learning_enabled=? WHERE key=?',
        (1 if enabled else 0, key)
    )
    conn.commit()
    conn.close()
    return True


def get_telegram_enabled(key: str) -> bool:
    row = _get_row(key)
    if not row:
        return True  # unknown license: fail open, matches the column's default
    conn = sqlite3.connect(DB_PATH)
    val = conn.execute(
        'SELECT telegram_enabled FROM licenses WHERE key=?', (key,)
    ).fetchone()
    conn.close()
    return bool(val[0]) if val and val[0] is not None else True


def set_telegram_enabled(key: str, enabled: bool) -> bool:
    row = _get_row(key)
    if not row:
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'UPDATE licenses SET telegram_enabled=? WHERE key=?',
        (1 if enabled else 0, key)
    )
    conn.commit()
    conn.close()
    return True


def get_standalone_settings(key: str) -> Optional[dict]:
    """Returns None if the license doesn't exist at all, otherwise a
    dict of every standalone_* field (bot_token included — callers that
    show this to a UI are responsible for masking it, this function
    doesn't decide that)."""
    row = _get_row(key)
    if not row:
        return None
    conn = sqlite3.connect(DB_PATH)
    val = conn.execute(
        '''SELECT standalone_mode_enabled, standalone_allowed_vehicles,
                  standalone_max_radius_miles, standalone_chat_ids, standalone_bot_token,
                  standalone_driver_bot_token
           FROM licenses WHERE key=?''', (key,)
    ).fetchone()
    conn.close()
    if not val:
        return None
    enabled, vehicles, radius, chat_ids, bot_token, driver_bot_token = val
    return {
        'standalone_mode_enabled': bool(enabled),
        'allowed_vehicles':        vehicles or '',
        'max_radius_miles':        radius,
        'chat_ids':                chat_ids or '',
        # Hardcoded defaults (2026-09-29) kick in only when this license
        # never saved its own token — every reader of this dict (the web
        # settings page, poller.py's callback listeners/cycle) sees the
        # same effective value with no per-license setup needed.
        'bot_token':               bot_token or DEFAULT_STANDALONE_BOT_TOKEN,
        'driver_bot_token':        driver_bot_token or DEFAULT_STANDALONE_DRIVER_BOT_TOKEN,
    }


# Known bot tokens hardcoded into the DESKTOP app (client/main copy.py's
# BOT_TOKEN constant) — reused here purely to reject a dispatcher pasting
# that SAME token into standalone mode's config. If both then ran
# concurrently, Telegram getUpdates calls would race and button-press
# replies would land on whichever process happened to poll first
# (documented, unresolved bug, MAILBOT_ROADMAP.md's "Known conflict").
# The desktop and standalone poller are meant to be alternate modes for
# the SAME account/chat — this only blocks the literal token collision,
# it doesn't (and shouldn't) force them onto permanently different bots.
# Update this set whenever the desktop's BOT_TOKEN is next rotated.
KNOWN_DESKTOP_BOT_TOKENS = {
    "8157082619:AAETFqdzP_VOXPEoWmKi3Uq48CQuHNU_Z08",
}


# The desktop's hardcoded DRIVER bot token (client/main copy.py's
# DRIVER_BOT_TOKEN). The web driver bot must use its own token so the two
# never long-poll the same bot; update on the next desktop rotation.
KNOWN_DESKTOP_DRIVER_BOT_TOKENS = {
    "8371628317:AAFa9yNDSfT_aks_OPYn_GQPchuEwOGuxt8",
}


def is_known_desktop_token(token: str) -> bool:
    return bool(token) and token.strip() in KNOWN_DESKTOP_BOT_TOKENS


def is_known_desktop_driver_token(token: str) -> bool:
    return bool(token) and token.strip() in KNOWN_DESKTOP_DRIVER_BOT_TOKENS


# 2026-09-29 (client: "make dispatcher bot token and driver bot token
# hardcoded, the client doesnt need to type it in") — a pre-made bot WE
# (the operator) own and hand out for standalone/web use, distinct from
# both of the desktop's own hardcoded bots above (checked — not in
# either KNOWN_DESKTOP_* set), so there's no token collision with what
# the desktop already uses. Applied in get_standalone_settings() below
# whenever a license's own bot_token column is still blank; a license
# keeps working unmodified if it already saved a different token of its
# own (nothing here overwrites the column).
DEFAULT_STANDALONE_BOT_TOKEN = "8989438062:AAHIR3wz04P76QnBAwAoLHQscB3I1RM0320"  # plutus_web_bot

# Driver bot default — deliberately NOT set yet. The only "driver bot"
# token seen in this project's history is 8371628317:AAFa9y... which
# KNOWN_DESKTOP_DRIVER_BOT_TOKENS above shows is actually the DESKTOP
# app's own driver bot (client/main copy.py's DRIVER_BOT_TOKEN) — reusing
# it here would silently violate the exact one-bot-token-per-license
# assumption poller.py's _telegram_callback_loop relies on and fight the
# desktop's real driver bot for the same long-poll. Needs a genuinely
# separate bot token from the operator before this can default like the
# dispatcher one does.
DEFAULT_STANDALONE_DRIVER_BOT_TOKEN = ""


def set_standalone_settings(key: str, **fields) -> bool:
    """Partial update — only columns present in `fields` are touched.
    Valid keys: allowed_vehicles, max_radius_miles, chat_ids, bot_token.
    Raises ValueError if `bot_token` is the desktop app's own known
    token (see KNOWN_DESKTOP_BOT_TOKENS above)."""
    if 'bot_token' in fields and is_known_desktop_token(fields['bot_token']):
        raise ValueError(
            "This is the desktop app's own bot token — standalone mode "
            "needs its own separate bot. Create a new bot via @BotFather "
            "and use that token here instead."
        )
    if 'driver_bot_token' in fields and fields['driver_bot_token']:
        dt = fields['driver_bot_token'].strip()
        if is_known_desktop_driver_token(dt) or is_known_desktop_token(dt):
            raise ValueError(
                "This is one of the desktop app's own bot tokens — the web driver bot "
                "needs its own separate bot. Create a new bot via @BotFather and use "
                "that token here instead."
            )
        other = (fields.get('bot_token') if 'bot_token' in fields
                 else (get_standalone_settings(key) or {}).get('bot_token'))
        if other and other.strip() == dt:
            raise ValueError(
                "The driver bot token must be a DIFFERENT bot from the dispatcher bot token."
            )
    if 'bot_token' in fields and fields['bot_token'] and is_known_desktop_driver_token(fields['bot_token']):
        raise ValueError(
            "This is the desktop app's driver bot token — standalone mode needs its own "
            "separate bot. Create a new bot via @BotFather and use that token here instead."
        )
    row = _get_row(key)
    if not row:
        return False
    col_map = {
        'allowed_vehicles':  'standalone_allowed_vehicles',
        'max_radius_miles':  'standalone_max_radius_miles',
        'chat_ids':          'standalone_chat_ids',
        'bot_token':         'standalone_bot_token',
        'driver_bot_token':  'standalone_driver_bot_token',
    }
    sets, values = [], []
    for k, v in fields.items():
        if k in col_map:
            sets.append(f'{col_map[k]}=?')
            values.append(v)
    if not sets:
        return True
    conn = sqlite3.connect(DB_PATH)
    conn.execute(f'UPDATE licenses SET {", ".join(sets)} WHERE key=?', (*values, key))
    conn.commit()
    conn.close()
    return True


def get_standalone_mode_enabled(key: str) -> bool:
    row = _get_row(key)
    if not row:
        return False
    conn = sqlite3.connect(DB_PATH)
    val = conn.execute(
        'SELECT standalone_mode_enabled FROM licenses WHERE key=?', (key,)
    ).fetchone()
    conn.close()
    return bool(val[0]) if val and val[0] is not None else False


def set_standalone_mode_enabled(key: str, enabled: bool) -> bool:
    row = _get_row(key)
    if not row:
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'UPDATE licenses SET standalone_mode_enabled=? WHERE key=?',
        (1 if enabled else 0, key)
    )
    conn.commit()
    conn.close()
    return True


def get_standalone_initial_scan_done(key: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    val = conn.execute(
        'SELECT standalone_initial_scan_done FROM licenses WHERE key=?', (key,)
    ).fetchone()
    conn.close()
    return bool(val[0]) if val and val[0] is not None else False


def set_standalone_initial_scan_done(key: str, done: bool) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'UPDATE licenses SET standalone_initial_scan_done=? WHERE key=?',
        (1 if done else 0, key)
    )
    conn.commit()
    conn.close()


def record_desktop_poll_heartbeat(key: str) -> bool:
    """Called every ~20s by a running desktop's Gmail-poll loop (only
    while it's actually polling, not just while the app is open) — see
    /api/desktop/poll_heartbeat. Doesn't require the license to exist
    (mirrors heartbeat()'s own tolerance) since a failed write here
    should never crash the desktop's poll loop over it."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'UPDATE licenses SET desktop_poll_heartbeat=? WHERE key=?',
        (datetime.now(timezone.utc).isoformat(), key)
    )
    updated = conn.total_changes > 0
    conn.commit()
    conn.close()
    return updated


def is_desktop_recently_active(key: str, within_seconds: int = 60) -> bool:
    """True if a desktop's poll loop sent a heartbeat within the last
    `within_seconds` — poller.py checks this before running a license's
    standalone cycle, so the two never process the same Gmail account
    at the same time (see the desktop/standalone mutual-exclusion note
    on desktop_poll_heartbeat's own ALTER TABLE, above)."""
    row = _get_row(key)
    if not row:
        return False
    conn = sqlite3.connect(DB_PATH)
    val = conn.execute(
        'SELECT desktop_poll_heartbeat FROM licenses WHERE key=?', (key,)
    ).fetchone()
    conn.close()
    if not val or not val[0]:
        return False
    try:
        last = datetime.fromisoformat(val[0])
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - last).total_seconds() < within_seconds


def list_standalone_enabled_licenses() -> list:
    """Active licenses with standalone_mode_enabled=1 — what poller.py
    (Phase C) iterates every cycle."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT key FROM licenses WHERE active=1 AND standalone_mode_enabled=1"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def validate_license(key: str, machine_id: str) -> dict:
    row = _get_row(key)
    if not row:
        return {'valid': False, 'reason': 'License not found'}

    _, active, db_machine, _, _, expires_at, _ = row

    if not active:
        return {'valid': False, 'reason': 'License revoked'}

    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
                return {'valid': False, 'reason': 'License expired'}
        except Exception:
            pass

    if db_machine and db_machine != machine_id:
        return {'valid': False, 'reason': 'Machine mismatch — contact support'}

    return {'valid': True}


def validate_license_key_only(key: str) -> dict:
    """
    Same checks as validate_license() (exists, active, not expired) but
    WITHOUT the machine-binding check — for the web dashboard, where
    "the machine" is whatever browser/device the dispatcher happens to
    log in from, not a single bound install the way the desktop app is.
    Deliberate v1 choice: anyone with the license key can view the web
    dashboard from anywhere. Fine for a single-operator tool today;
    real session/device-scoped auth is still an open question for a
    multi-user version (see MAILBOT_ROADMAP.md, Phase W).
    """
    row = _get_row(key)
    if not row:
        return {'valid': False, 'reason': 'License not found'}

    _, active, _, _, _, expires_at, _ = row

    if not active:
        return {'valid': False, 'reason': 'License revoked'}

    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
                return {'valid': False, 'reason': 'License expired'}
        except Exception:
            pass

    return {'valid': True}


def activate_license(key: str, machine_id: str, machine_name: str) -> dict:
    row = _get_row(key)
    if not row:
        return {'success': False, 'reason': 'License key not found'}

    _, active, db_machine, _, _, _, _ = row

    if not active:
        return {'success': False, 'reason': 'License is revoked'}

    if db_machine and db_machine != machine_id:
        return {'success': False, 'reason': 'Already bound to a different machine'}

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'UPDATE licenses SET machine_id=?, machine_name=?, last_heartbeat=? WHERE key=?',
        (machine_id, machine_name, now, key)
    )
    conn.commit()
    conn.close()
    return {'success': True}


def heartbeat(key: str, machine_id: str) -> bool:
    result = validate_license(key, machine_id)
    if not result['valid']:
        return False

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'UPDATE licenses SET last_heartbeat=? WHERE key=?', (now, key)
    )
    conn.commit()
    conn.close()
    return True


def add_license(key: str, expires_at: Optional[str] = None, label: str = ''):
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'INSERT OR IGNORE INTO licenses (key, active, created_at, expires_at, label) VALUES (?,1,?,?,?)',
        (key, now, expires_at, label)
    )
    conn.commit()
    conn.close()


def revoke_license(key: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute('UPDATE licenses SET active=0 WHERE key=?', (key,))
    conn.commit()
    conn.close()


def set_license_label(key: str, label: str) -> bool:
    row = _get_row(key)
    if not row:
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute('UPDATE licenses SET label=? WHERE key=?', (label, key))
    conn.commit()
    conn.close()
    return True


def list_all_licenses() -> list:
    """Every license with its label, for identifying who's who without
    reading raw keys — key, label, active, machine_name, created_at,
    expires_at, last_heartbeat, ordered newest-first."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        '''SELECT key, label, active, machine_name, created_at, expires_at, last_heartbeat
           FROM licenses ORDER BY created_at DESC'''
    ).fetchall()
    conn.close()
    return [
        {
            'key': r[0], 'label': r[1] or '', 'active': bool(r[2]),
            'machine_name': r[3], 'created_at': r[4],
            'expires_at': r[5], 'last_heartbeat': r[6],
        }
        for r in rows
    ]
