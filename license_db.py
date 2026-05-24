import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get('LICENSE_DB', 'licenses.db')


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


def add_license(key: str, expires_at: str = None):
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'INSERT OR IGNORE INTO licenses (key, active, created_at, expires_at) VALUES (?,1,?,?)',
        (key, now, expires_at)
    )
    conn.commit()
    conn.close()


def revoke_license(key: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute('UPDATE licenses SET active=0 WHERE key=?', (key,))
    conn.commit()
    conn.close()