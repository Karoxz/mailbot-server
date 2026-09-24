# =============================================================
# map_token.py — server-side module, added 2026-09-24
#
# Scoped, short-lived, stateless tokens for the BID PC price+map
# mobile page (bid_price.html), reached via a link poller.py sends
# over Telegram to a phone with no prior logged-in dashboard session.
#
# The raw license_key is never put into a Telegram message/link — it's
# the master credential for the whole account, not meaningfully
# revocable per-link, and Telegram's own infrastructure could log or
# preview a URL. This token carries only what bid_price.html actually
# needs (which license, which order) and expires on its own — no new
# DB table, no revocation list, just a signed JWT via python-jose
# (already a pinned dependency, confirmed unused anywhere else in this
# codebase before this).
# =============================================================

import os
import time

from jose import jwt, JWTError

_ALGORITHM = "HS256"
_DEFAULT_TTL_SECONDS = 60 * 60 * 48  # 48h — a load's worth biddable for a while,
                                      # not indefinitely; single-operator tool,
                                      # no revocation needed for that window.


def _secret() -> str:
    # Real bug, caught testing: main.py imports this module BEFORE it
    # calls its own _load_env_file() (imports run top-to-bottom before
    # any of a module's later top-level code) — a module-level
    # `os.environ.get(...)` read here would always see "" regardless of
    # what's actually in .env, since .env isn't loaded into the process
    # environment yet at import time. Reading it fresh on every call
    # (cheap — a dict lookup) sidesteps the ordering entirely: by the
    # time any request actually triggers make/verify_bid_token, .env
    # has definitely already been loaded.
    return os.environ.get("MAP_TOKEN_SECRET", "")


def make_bid_token(license_key: str, order_id: str, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> str:
    payload = {"lk": license_key, "oid": order_id, "exp": int(time.time()) + ttl_seconds}
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def verify_bid_token(token: str):
    """Returns {"license_key":..., "order_id":...} or None (expired,
    tampered, or malformed — jose.jwt.decode checks both the signature
    and "exp" itself). Never raises."""
    secret = _secret()
    if not secret or not token:
        return None
    try:
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM])
        return {"license_key": claims["lk"], "order_id": claims["oid"]}
    except (JWTError, KeyError):
        return None
