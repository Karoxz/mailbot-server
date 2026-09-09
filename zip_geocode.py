# =============================================================
# zip_geocode.py — server-side module
#
# Offline US ZIP-code -> lat/lon lookup, backed by pgeocode (GeoNames
# postal-code data, downloaded once and cached to disk — no per-request
# network call after that).
#
# Why this exists: confirmed live via production journalctl timing
# (2026-09-09) that "geo pickup+delivery" was regularly taking 7-8
# SECONDS per message during a burst of new load postings, and
# process_bid_email sometimes hit 35-40s total — both photon_geocode's
# existing geocoders (nominatim.openstreetmap.org and photon.komoot.io)
# are PUBLIC, rate-limited third-party services, and every one of the
# up-to-4 uvicorn workers hits them independently. During a burst of
# many new (never-before-geocoded) locations arriving within the same
# minute, that's exactly the client-reported "big delay between Gmail
# receiving the bid and the processed bid reaching Telegram."
#
# The overwhelming majority of broker emails include a 5-digit ZIP
# (photon_geocode's existing _extract_zip_state() already special-cases
# this), so resolving that ZIP against a LOCAL dataset instead of a
# network call removes the bottleneck entirely for that common case —
# same lat/lon granularity Nominatim's own zip-only lookup already gave
# (a ZIP centroid, not a street address), so this is not an accuracy
# downgrade, purely a latency fix. Falls back to the existing Nominatim/
# Photon chain untouched for anything this can't resolve (bad/foreign
# ZIP, or a location with no ZIP in the text at all).
# =============================================================

import math
from typing import Optional, List

_nomi = None
_load_failed = False


def _get_nomi():
    """Lazy singleton — constructing pgeocode.Nominatim() is what
    triggers the one-time GeoNames data load (from its on-disk cache
    after the very first run, from network the very first time ever).
    Constructed once per worker process, reused for every call after."""
    global _nomi, _load_failed
    if _nomi is not None or _load_failed:
        return _nomi
    try:
        import pgeocode
        _nomi = pgeocode.Nominatim("us")
    except Exception as e:
        print(f"[ZIP-GEOCODE] pgeocode unavailable, offline lookup disabled: {e}", flush=True)
        _load_failed = True
    return _nomi


def warmup():
    """Call once at server startup so the (up to several-second,
    one-time-ever) GeoNames data load happens before any real email is
    waiting on it, not on some unlucky first live request."""
    nomi = _get_nomi()
    if nomi is not None:
        try:
            nomi.query_postal_code("10001")  # any valid US zip
            print("[ZIP-GEOCODE] offline ZIP lookup ready", flush=True)
        except Exception as e:
            print(f"[ZIP-GEOCODE] warmup query failed: {e}", flush=True)


def lookup(zip_code: str) -> Optional[List[float]]:
    """[lat, lon] for a 5-digit US ZIP from the local offline dataset,
    or None if unavailable/not found — callers fall back to the
    existing network geocoders exactly as if this module didn't exist."""
    nomi = _get_nomi()
    if nomi is None:
        return None
    try:
        row = nomi.query_postal_code(zip_code.strip())
        lat, lon = float(row.latitude), float(row.longitude)
        if math.isnan(lat) or math.isnan(lon):
            return None
        return [lat, lon]
    except Exception:
        return None
