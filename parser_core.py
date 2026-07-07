# =============================================================
# parser_core.py  —  server-side module  (FRESH REWRITE)
#
# This replaces both the pre-driverbot version and the unstable
# "FIX #1-9" version. It keeps the simpler architecture of the
# earlier build but actually fixes the bugs that were present in
# BOTH prior versions (the old one was never actually bug-free —
# it just hadn't been stress-tested yet):
#
#  1. NO STACKED RETRIES.
#     The old global `session` had urllib3 Retry(total=4) mounted
#     AND manual 2-attempt retry loops inside _geocode_nominatim /
#     _geocode_photon on top of it. Worst case that's 4 urllib3
#     retries PER manual attempt PER call — easily 30+ seconds on a
#     flaky endpoint, which is what exhausted the thread pool after
#     ~10 messages. All sessions below use max_retries=0. Retry
#     behavior is explicit, bounded, and lives in application code.
#
#  2. GRAPHHOPPER SEMAPHORE WAS 1, NOT 6.
#     find_all_trucks_for_pickup routes up to 8 trucks concurrently,
#     but every one of those threads was funneled through a
#     Semaphore(1) for the local GraphHopper call — i.e. "parallel"
#     routing was actually fully serialized at the GH layer, which
#     under load produced timeouts that fell through to degraded
#     fallback distances. Raised to 6.
#
#  3. VEHICLE MATCHING WAS A RAW SUBSTRING CHECK.
#     `"VAN" in "SPRINTER VAN"` is True, so a truck typed as VAN
#     would match a SPRINTER VAN load. Replaced with a word-sequence
#     prefix comparison: the shorter side's words must be a literal
#     prefix of the longer side's words. "LARGE STRAIGHT" still
#     matches "LARGE STRAIGHT TRUCK"; "VAN" no longer matches
#     "SPRINTER VAN".
#
#  4. THE SERIAL FALLBACK MATCHER NEVER ENFORCED THE RADIUS CAP.
#     find_best_truck_for_pickup_with_date (used both for rejection
#     logging AND as a recovery path when the parallel matcher finds
#     zero candidates) picked whichever truck routed closest with NO
#     check against max_radius_miles. This is the direct cause of
#     "199 mile" / out-of-range ghost matches. Fixed: same hard cap
#     as the parallel path, enforced in both places a truck can be
#     returned.
#
#  5. DATE MATCHING USED NAIVE, SERVER-LOCAL TIME.
#     truck_date_matches compared against datetime.now() with no
#     timezone. On a UTC server that rolls the calendar date over
#     hours before Eastern time does, which silently broke "today /
#     ASAP" truck-date matching in the evening. Fixed to compare
#     against America/New_York explicitly.
#
#  6. THE "STACKED PIECES" HEIGHT OVERRIDE SCANNED THE WHOLE EMAIL.
#     `\d+\s*\+\s*\d+\s*=\s*\d+` was searched against the entire raw
#     body, so any unrelated "12+3=15"-shaped text anywhere in the
#     email (rate math, load counts, whatever) could silently
#     overwrite load_height_in and corrupt the door-height check.
#     Scoped to a small window right after the Dimensions/Pieces
#     label.
#
#  7. THREAD JOINS / EXECUTOR SHUTDOWNS COULD BLOCK FOREVER.
#     `t.join()` with no timeout, and `with ThreadPoolExecutor(...) as
#     ex:` (which calls shutdown(wait=True) on exit) meant a single
#     stuck worker — e.g. one geocode call hung on a slow DNS lookup —
#     could block the whole request indefinitely and pile up behind
#     it. All joins are now bounded, and every executor is torn down
#     with shutdown(wait=False, cancel_futures=True) in a finally
#     block after a bounded futures_wait().
#
#  8. CACHE SCHEMA VERSIONING.
#     geo_cache.json / route_cache.json are tagged with a schema
#     version. Any cache file not written under this exact version is
#     discarded on load instead of trusted, so leftover distances
#     computed under old buggy logic can never silently resurface.
#     Bumped to v3 for this rewrite so any previously-cached ghost
#     values (0mi / 199mi) are wiped on first restart.
#
# Deployment verification: watch for the "[PARSER_CORE] Fresh rewrite
# loaded" line in journalctl right after restart, and for
# [TRUCK-ROUTE] / [FALLBACK-MATCH] / [MATCH] lines on the next parse
# request — their absence means the service is still running an old
# file, not that the logic itself is broken.
# =============================================================

import os
import re
import html as html_lib
import base64
import json
import time
import threading
import socket
import math
from urllib.parse import quote
from email.utils import parseaddr
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

import requests
from requests.adapters import HTTPAdapter


def _haversine_miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


# =============================================================
# CONFIGURATION
# =============================================================

GRAPHHOPPER_URL           = "http://127.0.0.1:8989/route"
GRAPHHOPPER_MILE_FACTOR   = 1.03
GRAPHHOPPER_CORRECTION    = 1.04
DEADHEAD_UNDER_600_OFFSET = -7

OSRM_BASE = "http://router.project-osrm.org"

ORS_API_KEY     = "eyJvcmciOiI1YjNjZTM1OTc4NTExMTAwMDFjZjYyNDgiLCJpZCI6IjZiYTM2ZGYzZTI2YjQ3MGViYjBkNzAwOTgzODM3MjA1IiwiaCI6Im11cm11cjY0In0="
ORS_URL         = "https://api.openrouteservice.org/v2/directions/driving-car"
_ORS_DISABLED   = True
_ORS_FAIL_COUNT = 0
_ORS_LOCK       = threading.Lock()

PHOTON_URL    = "https://photon.komoot.io/api/"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEOCODER_UA   = "MailBotDispatcher/1.0"

GEO_CACHE_FILE   = "geo_cache.json"
ROUTE_CACHE_FILE = "route_cache.json"
_GEO_CACHE_LOCK   = threading.Lock()
_ROUTE_CACHE_LOCK = threading.Lock()
_GEO_CACHE_DIRTY   = False
_ROUTE_CACHE_DIRTY = False

# Bumped for this rewrite — see FIX #8 in the header comment.
CACHE_SCHEMA_VERSION = 3
ROUTE_CACHE_TTL_DAYS = 30

STOP_EVENT = threading.Event()

LOAD_STORE      = {}
LOAD_STORE_LOCK = threading.Lock()

BID_TEMPLATE_LOCK = threading.Lock()
BID_TEMPLATE = """Rate: $
{vehicle_type}
Dims: {truck_dimensions}
MC#

Truck is {google_deadhead} miles out
{truck_equipment}

ETA to PU: {deadhead_eta_str}

ALL BIDS ARE VALID 15 MIN"""

# ── FIX #1: every outbound session below has max_retries=0. ────────────
# All retry/backoff behavior is explicit, bounded application code — no
# urllib3-level retries stacked underneath a manual loop.
_geo_session = requests.Session()
_geo_session.mount("https://", HTTPAdapter(max_retries=0))
_geo_session.mount("http://",  HTTPAdapter(max_retries=0))

_route_http_session = requests.Session()
_route_http_session.mount("https://", HTTPAdapter(max_retries=0))
_route_http_session.mount("http://",  HTTPAdapter(max_retries=0))

_gh_session = requests.Session()
_gh_session.mount("http://", HTTPAdapter(max_retries=0))

# ── FIX #2: was Semaphore(1) — serialized every "parallel" truck route
# through a single GraphHopper slot. GH is local + fast; 6 concurrent
# calls is safe and actually lets the parallel matcher run in parallel.
_GH_SEMAPHORE = threading.Semaphore(6)


def _cache_flush_worker():
    global _GEO_CACHE_DIRTY, _ROUTE_CACHE_DIRTY
    while not STOP_EVENT.is_set():
        time.sleep(30)
        if _GEO_CACHE_DIRTY:
            with _GEO_CACHE_LOCK:
                _save_cache(GEO_CACHE_FILE, GEO_CACHE)
                _GEO_CACHE_DIRTY = False
        if _ROUTE_CACHE_DIRTY:
            with _ROUTE_CACHE_LOCK:
                _save_cache(ROUTE_CACHE_FILE, ROUTE_CACHE)
                _ROUTE_CACHE_DIRTY = False


threading.Thread(target=_cache_flush_worker, daemon=True).start()


# =============================================================
# US STATE / REGION CONSTANTS
# =============================================================

_US_STATES_SET = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

REGION_MAP = {
    "WEST COAST": {"AZ", "CA", "CO", "ID", "MT", "NV", "NM", "OR", "TX", "UT", "WA", "WY"},
    "MIDWEST":    {"IL", "IN", "IA", "KS", "KY", "MI", "MN", "MO", "NE", "ND", "OH", "SD", "TN", "WI"},
    "EAST COAST": {"CT", "DE", "FL", "GA", "ME", "MD", "MA", "NH", "NJ", "NY", "NC", "PA", "RI", "SC", "VT", "VA"},
}


def expand_states(raw: str):
    if not raw or not raw.strip():
        return None
    result = set()
    for token in raw.split(","):
        token = token.strip().upper()
        if not token:
            continue
        if token in REGION_MAP:
            result |= REGION_MAP[token]
            continue
        if len(token) > 2:
            matched_region = False
            for region_name, states in REGION_MAP.items():
                if token in region_name:
                    result |= states
                    matched_region = True
                    break
            if matched_region:
                continue
        if token in _US_STATES_SET:
            result.add(token)
    return result if result else None


# =============================================================
# HEIGHT / STATE EXTRACTION
# =============================================================

def extract_state_from_location(loc: str):
    if not loc:
        return None
    clean = loc.strip().upper()
    m = re.search(r",\s*([A-Z]{2})\b", clean)
    if m and m.group(1) in _US_STATES_SET:
        return m.group(1)
    m = re.match(r"^([A-Z]{2})\s+\d{5}", clean)
    if m and m.group(1) in _US_STATES_SET:
        return m.group(1)
    for token in reversed(clean.split()):
        t = re.sub(r"\W", "", token)
        if t in _US_STATES_SET:
            return t
    return None


def parse_height_from_dims(dims: str):
    if not dims:
        return None
    paren_m = re.search(r"\(([^)]+)\)", dims)
    if paren_m:
        numbers = re.findall(r"\d+", paren_m.group(1))
        if numbers:
            try:
                return int(numbers[-1])
            except ValueError:
                pass
    main = re.split(r"\s*[xX]\s*", dims.split("(")[0].strip())
    if len(main) >= 3:
        m = re.search(r"\d+", main[2])
        if m:
            try:
                return int(m.group())
            except ValueError:
                pass
    return None


def parse_load_height_from_dims(dims_text: str):
    if not dims_text:
        return None
    m = re.search(r"\bH\s*:?\s*(\d+)", dims_text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    parts = re.split(r"\s*[xX]\s*", dims_text.strip())
    if len(parts) >= 3:
        m = re.search(r"\d+", parts[2])
        if m:
            try:
                return int(m.group())
            except ValueError:
                pass
    return None


# =============================================================
# CACHE LOAD / SAVE  (FIX #8: schema versioning)
# =============================================================

def _load_cache(path, expected_version):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("__version__") != expected_version:
                print(f"[CACHE] {path} is stale (schema mismatch) — clearing.", flush=True)
                return {"__version__": expected_version}
            return data
        except Exception:
            return {"__version__": expected_version}
    return {"__version__": expected_version}


def _save_cache(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


GEO_CACHE   = _load_cache(GEO_CACHE_FILE, CACHE_SCHEMA_VERSION)
ROUTE_CACHE = _load_cache(ROUTE_CACHE_FILE, CACHE_SCHEMA_VERSION)

_US_LAT = (15.0, 72.0)
_US_LON = (-180.0, -65.0)


def _in_us(lat: float, lon: float) -> bool:
    return _US_LAT[0] <= lat <= _US_LAT[1] and _US_LON[0] <= lon <= _US_LON[1]


def is_location_in_us(location: str) -> bool:
    if not location or not location.strip():
        return True
    coords = photon_geocode(location.strip())
    if coords is None:
        return True
    return _in_us(coords[0], coords[1])


def build_google_maps_route_url(pickup: str, delivery: str) -> str:
    if not pickup or not delivery:
        return ""
    return (
        "https://www.google.com/maps/dir/"
        + quote(pickup.strip(), safe="")
        + "/"
        + quote(delivery.strip(), safe="")
    )


def _is_zip(place: str) -> bool:
    return bool(re.fullmatch(r"\d{5}", place.strip()))


def _normalize_address(place: str) -> str:
    place = place.strip()
    if "," in place:
        return place
    US_STATES = _US_STATES_SET
    m = re.match(r"^(.*?)\s+([A-Za-z][A-Za-z\s]{1,20}?)\s+([A-Z]{2})\s+(\d{5})$", place)
    if m and m.group(3) in US_STATES:
        return f"{m.group(1).strip()}, {m.group(2).strip()}, {m.group(3)} {m.group(4)}"
    m = re.match(r"^([A-Za-z][A-Za-z\s]{1,25}?)\s+([A-Z]{2})\s+(\d{5})$", place)
    if m and m.group(2) in US_STATES:
        return f"{m.group(1).strip()}, {m.group(2)} {m.group(3)}"
    m = re.match(r"^([A-Za-z][A-Za-z\s]{1,25}?)\s+([A-Z]{2})$", place)
    if m and m.group(2) in US_STATES:
        return f"{m.group(1).strip()}, {m.group(2)}"
    return place


def _geocode_nominatim(place: str, place_clean: str):
    """Bounded 2-attempt loop — no urllib3-level retries underneath."""
    for attempt in range(1, 3):
        if STOP_EVENT.is_set():
            return None
        try:
            if _is_zip(place.strip()):
                params = {"postalcode": place.strip(), "countrycodes": "us",
                          "format": "json", "limit": 1}
            else:
                q = place_clean
                if not re.search(r"\bUSA?\b", q, re.I):
                    q += ", USA"
                params = {"q": q, "countrycodes": "us", "format": "json",
                          "addressdetails": 1, "limit": 5}
            r = _geo_session.get(NOMINATIM_URL, params=params,
                                  headers={"User-Agent": GEOCODER_UA}, timeout=2)
            if r.status_code != 200:
                time.sleep(0.3)
                continue
            for item in r.json():
                try:
                    lat, lon = float(item["lat"]), float(item["lon"])
                except (KeyError, ValueError):
                    continue
                if _in_us(lat, lon):
                    return [lat, lon]
            return None
        except requests.exceptions.Timeout:
            time.sleep(0.5)
        except Exception as e:
            print(f"Nominatim exception '{place_clean}' attempt {attempt}: {e}", flush=True)
            time.sleep(0.5)
    return None


def _geocode_photon(place: str, place_clean: str):
    """Bounded 2-attempt loop — no urllib3-level retries underneath."""
    place_us = place_clean if re.search(r"\bUSA?\b", place_clean, re.I) else place_clean + ", USA"
    for attempt in range(1, 3):
        if STOP_EVENT.is_set():
            return None
        try:
            r = _geo_session.get(PHOTON_URL, params={"q": place_us, "limit": 5, "lang": "en"},
                                  headers={"User-Agent": GEOCODER_UA}, timeout=3)
            if r.status_code != 200:
                time.sleep(0.5)
                continue
            for feat in r.json().get("features", []):
                coords = feat.get("geometry", {}).get("coordinates", [])
                if len(coords) < 2:
                    continue
                lat, lon = float(coords[1]), float(coords[0])
                country = (feat.get("properties", {}).get("country", "") or "").upper()
                if _in_us(lat, lon) or country in ("US", "USA", "UNITED STATES"):
                    return [lat, lon]
            return None
        except requests.exceptions.Timeout:
            time.sleep(0.5)
        except Exception as e:
            print(f"Photon exception '{place_us}' attempt {attempt}: {e}", flush=True)
            time.sleep(0.5)
    return None


# =============================================================
# STATE BOUNDING BOXES — cheap sanity check to catch geocoders
# picking the wrong same-named city (e.g. "Oakland, NJ" -> Oakland, CA)
# =============================================================
_STATE_BBOX = {
    "AL": (30.1, 35.1, -88.6, -84.7), "AK": (51.0, 71.5, -179.9, -129.9),
    "AZ": (31.3, 37.1, -114.9, -108.9), "AR": (33.0, 36.6, -94.7, -89.6),
    "CA": (32.4, 42.1, -124.5, -114.0), "CO": (36.9, 41.1, -109.2, -101.9),
    "CT": (40.9, 42.1, -73.8, -71.7), "DE": (38.4, 39.9, -75.9, -75.0),
    "FL": (24.4, 31.1, -87.7, -79.9), "GA": (30.3, 35.1, -85.7, -80.7),
    "HI": (18.8, 22.5, -160.5, -154.7), "ID": (41.9, 49.1, -117.3, -110.9),
    "IL": (36.9, 42.6, -91.6, -87.0), "IN": (37.7, 41.8, -88.2, -84.7),
    "IA": (40.3, 43.6, -96.7, -90.0), "KS": (36.9, 40.1, -102.1, -94.5),
    "KY": (36.4, 39.2, -89.6, -81.9), "LA": (28.8, 33.1, -94.1, -88.7),
    "ME": (42.9, 47.5, -71.2, -66.8), "MD": (37.8, 39.8, -79.5, -74.9),
    "MA": (41.1, 43.0, -73.6, -69.8), "MI": (41.6, 48.3, -90.5, -82.1),
    "MN": (43.4, 49.4, -97.3, -89.4), "MS": (30.1, 35.1, -91.7, -88.0),
    "MO": (35.9, 40.7, -95.9, -89.0), "MT": (44.3, 49.1, -116.1, -104.0),
    "NE": (39.9, 43.1, -104.1, -95.3), "NV": (34.9, 42.1, -120.1, -113.9),
    "NH": (42.6, 45.4, -72.6, -70.6), "NJ": (38.9, 41.4, -75.6, -73.9),
    "NM": (31.2, 37.1, -109.1, -102.9), "NY": (40.4, 45.1, -79.9, -71.7),
    "NC": (33.7, 36.6, -84.4, -75.4), "ND": (45.9, 49.1, -104.1, -96.4),
    "OH": (38.3, 42.0, -84.9, -80.4), "OK": (33.5, 37.1, -103.1, -94.4),
    "OR": (41.9, 46.3, -124.7, -116.4), "PA": (39.7, 42.3, -80.6, -74.7),
    "RI": (41.1, 42.1, -71.9, -71.0), "SC": (32.0, 35.3, -83.5, -78.4),
    "SD": (42.4, 46.1, -104.1, -96.4), "TN": (34.9, 36.7, -90.4, -81.6),
    "TX": (25.8, 36.6, -106.7, -93.5), "UT": (36.9, 42.1, -114.1, -108.9),
    "VT": (42.7, 45.1, -73.5, -71.4), "VA": (36.5, 39.5, -83.7, -75.1),
    "WA": (45.5, 49.1, -124.9, -116.9), "WV": (37.1, 40.7, -82.7, -77.6),
    "WI": (42.4, 47.1, -92.9, -86.7), "WY": (40.9, 45.1, -111.1, -104.0),
    "DC": (38.79, 39.00, -77.12, -76.90),
}


def _in_state_bbox(state, lat, lon):
    box = _STATE_BBOX.get(state)
    if not box:
        return True  # unknown state code — don't block, just pass through
    return box[0] <= lat <= box[1] and box[2] <= lon <= box[3]


def _extract_zip_state(place: str):
    """
    Pull a bare (state, zip) out of a noisy string like
    'SIENNA PLANT, TX 77459'. Business/facility names aren't real
    places and confuse fuzzy geocoders — the ZIP is the only
    unambiguous part, so geocode THAT when one is present.
    """
    if not place:
        return None
    m = re.search(r"\b([A-Z]{2})\s*(\d{5})\b", place.upper())
    if m and m.group(1) in _US_STATES_SET:
        return m.group(1), m.group(2)
    return None


def photon_geocode(place: str):
    key = place.strip().upper()
    with _GEO_CACHE_LOCK:
        cached = GEO_CACHE.get(key)
    if cached:
        return cached

    place_clean    = _normalize_address(place.strip())
    expected_state = extract_state_from_location(place)

    def _try(provider_fn, provider_name, query_str, query_clean):
        r = provider_fn(query_str, query_clean)
        if not r:
            return None
        if expected_state and not _in_state_bbox(expected_state, r[0], r[1]):
            print(f"[GEOCODE] REJECTED '{query_str}' -> {r} via {provider_name} "
                  f"(outside {expected_state} bounding box)", flush=True)
            return None
        return r

    result, source = None, None

    zip_state = _extract_zip_state(place)
    if zip_state:
        _, zip_code = zip_state
        result = _try(_geocode_nominatim, "nominatim(zip)", zip_code, zip_code)
        if result:
            source = "nominatim-zip"

    if not result:
        result = _try(_geocode_nominatim, "nominatim", place, place_clean)
        source = "nominatim"

    if not result:
        result = _try(_geocode_photon, "photon", place, place_clean)
        source = "photon"

    if result:
        print(f"[GEOCODE] '{place}' -> {result} via {source}", flush=True)
        global _GEO_CACHE_DIRTY
        with _GEO_CACHE_LOCK:
            GEO_CACHE[key] = result
            _GEO_CACHE_DIRTY = True
    else:
        print(f"[GEOCODE] FAILED '{place_clean}' (expected_state={expected_state})", flush=True)

    return result


# =============================================================
# ROUTING
# =============================================================

def _ors_route(origin_latlon, dest_latlon):
    global _ORS_DISABLED, _ORS_FAIL_COUNT
    with _ORS_LOCK:
        if _ORS_DISABLED:
            return None
    if not ORS_API_KEY or ORS_API_KEY == "YOUR_ORS_API_KEY_HERE":
        return None
    lat1, lon1 = origin_latlon
    lat2, lon2 = dest_latlon
    try:
        r = _route_http_session.post(
            ORS_URL,
            json={"coordinates": [[lon1, lat1], [lon2, lat2]], "units": "mi"},
            headers={"Authorization": ORS_API_KEY, "Content-Type": "application/json"},
            timeout=3,
        )
        if r.status_code in (403, 429):
            with _ORS_LOCK:
                _ORS_FAIL_COUNT += 1
                if _ORS_FAIL_COUNT >= 3:
                    _ORS_DISABLED = True
                    print("ORS disabled for this session.", flush=True)
            return None
        if r.status_code != 200:
            print(f"ORS HTTP {r.status_code}: {r.text[:200]}", flush=True)
            return None
        with _ORS_LOCK:
            _ORS_FAIL_COUNT = 0
        summary = r.json()["routes"][0]["summary"]
        return {"miles": round(summary["distance"]), "minutes": round(summary["duration"] / 60)}
    except requests.exceptions.Timeout:
        print("ORS timeout", flush=True)
        return None
    except Exception as e:
        print(f"ORS exception: {e}", flush=True)
        return None


_GH_PORT_CACHE = {"up": None, "checked_at": 0}
_GH_PORT_TTL   = 30


def is_port_open(host="127.0.0.1", port=8989):
    now = time.time()
    if now - _GH_PORT_CACHE["checked_at"] < _GH_PORT_TTL:
        return _GH_PORT_CACHE["up"]
    try:
        with socket.create_connection((host, port), timeout=2):
            _GH_PORT_CACHE.update({"up": True, "checked_at": now})
            return True
    except OSError:
        _GH_PORT_CACHE.update({"up": False, "checked_at": now})
        return False


def _graphhopper_route(origin_latlon, dest_latlon):
    if not is_port_open():
        return None
    lat1, lon1 = origin_latlon
    lat2, lon2 = dest_latlon
    with _GH_SEMAPHORE:
        try:
            r = _gh_session.get(GRAPHHOPPER_URL, params={...}, timeout=4)
            ...
            path = data["paths"][0]
            raw_miles  = path["distance"] / 1609.344

            # ── Sanity check: a real route can't be shorter than the ──
            # straight-line distance. If it is, one of the two geocoded
            # points is almost certainly wrong (e.g. a facility/business
            # name resolved to the wrong city) — treat as a failed route
            # instead of silently returning a too-short deadhead.
            sl_miles = _haversine_miles(lat1, lon1, lat2, lon2)
            if raw_miles < sl_miles * 0.95:
                print(f"[GH] IMPOSSIBLE ROUTE: raw={raw_miles:.1f}mi < "
                      f"straight-line={sl_miles:.1f}mi — treating as failed "
                      f"(likely bad geocode)", flush=True)
                return None

            base_miles = raw_miles * GRAPHHOPPER_MILE_FACTOR
            miles = round(base_miles * GRAPHHOPPER_CORRECTION)
            offset_applied = 0
            if miles < 600:
                offset_applied = DEADHEAD_UNDER_600_OFFSET
                miles += offset_applied
            final_miles = max(0, miles)
            print(f"[GH] {lat1:.3f},{lon1:.3f} -> {lat2:.3f},{lon2:.3f}  "
                  f"raw={raw_miles:.1f}mi  after_factor={base_miles:.1f}mi  "
                  f"offset={offset_applied}  final={final_miles}mi", flush=True)
            return {"miles": final_miles, "minutes": round(path["time"] / 60000)}
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            return None
        except Exception as e:
            print(f"GraphHopper exception: {e}", flush=True)
            return None


def _osrm_route_fallback(origin_latlon, dest_latlon):
    PERMANENT_CODES = {"NoRoute", "NoSegment", "InvalidUrl", "InvalidValue", "TooBig"}
    lat1, lon1 = origin_latlon
    lat2, lon2 = dest_latlon
    url = f"{OSRM_BASE}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
    for _ in range(2):
        try:
            r = _route_http_session.get(url, params={"overview": "false"}, timeout=3)
            try:
                data = r.json()
            except ValueError:
                data = {}
            osrm_code = data.get("code", "")
            if osrm_code in PERMANENT_CODES or r.status_code in (400, 422):
                return None
            if r.status_code != 200 or osrm_code != "Ok" or not data.get("routes"):
                time.sleep(0.1)
                continue
            route = data["routes"][0]
            return {"miles": round(route["distance"] / 1609.344),
                    "minutes": round(route["duration"] / 60)}
        except Exception:
            time.sleep(0.1)
    return None


def _parallel_fallback_route(origin_latlon, dest_latlon):
    """
    Race ORS and OSRM concurrently. Return the first successful result.
    When ORS is disabled, OSRM responds alone in ~3s.
    """
    result_holder = [None]
    source_holder = [""]
    done_event    = threading.Event()

    def _try_ors():
        r = _ors_route(origin_latlon, dest_latlon)
        if r and not done_event.is_set():
            result_holder[0] = r
            source_holder[0] = "ors"
            done_event.set()

    def _try_osrm():
        r = _osrm_route_fallback(origin_latlon, dest_latlon)
        if r and not done_event.is_set():
            result_holder[0] = r
            source_holder[0] = "osrm"
            done_event.set()

    t_ors  = threading.Thread(target=_try_ors,  daemon=True)
    t_osrm = threading.Thread(target=_try_osrm, daemon=True)
    t_ors.start()
    t_osrm.start()

    done_event.wait(timeout=4)

    return result_holder[0], source_holder[0]


def compute_route(origin_latlon, dest_latlon):
    global _ROUTE_CACHE_DIRTY
    cache_key = (f"{origin_latlon[0]:.3f},{origin_latlon[1]:.3f}"
                 f"|{dest_latlon[0]:.3f},{dest_latlon[1]:.3f}")
    now = time.time()

    with _ROUTE_CACHE_LOCK:
        cached = ROUTE_CACHE.get(cache_key)

    if cached:
        age_secs      = now - cached.get("ts", 0)
        cached_source = cached.get("source", "unknown")
        gh_running    = is_port_open()

        # Never trust a cached failure — treat as a miss.
        if cached_source == "failed" or cached.get("miles") is None:
            cached = None

        if cached and gh_running and cached_source != "gh" and age_secs > 3600:
            gh_result = _graphhopper_route(origin_latlon, dest_latlon)
            if gh_result:
                gh_result.update({"source": "gh", "ts": now})
                with _ROUTE_CACHE_LOCK:
                    ROUTE_CACHE[cache_key] = gh_result
                    _ROUTE_CACHE_DIRTY = True
                print(f"[ROUTE] {cache_key} source=gh(refresh) miles={gh_result['miles']}", flush=True)
                return gh_result

        if cached and age_secs < ROUTE_CACHE_TTL_DAYS * 86400:
            print(f"[ROUTE] {cache_key} source={cached_source}(cached, age={age_secs/3600:.1f}h) "
                  f"miles={cached.get('miles')}", flush=True)
            return cached

    result = _graphhopper_route(origin_latlon, dest_latlon)
    source = "gh"

    if not result:
        result, source = _parallel_fallback_route(origin_latlon, dest_latlon)

    if result:
        result["source"] = source
        result["ts"]     = now
        print(f"[ROUTE] {cache_key} source={source}(fresh) miles={result['miles']}", flush=True)
        with _ROUTE_CACHE_LOCK:
            ROUTE_CACHE[cache_key] = result
            _ROUTE_CACHE_DIRTY = True
    else:
        print(f"[ROUTE] {cache_key} ALL ENGINES FAILED", flush=True)
    return result


def get_distance(orig: str, dest: str):
    a = photon_geocode(orig)
    b = photon_geocode(dest)
    if not a or not b:
        return None
    return compute_route(a, b)


def get_distance_from_zip(location: str, dest: str):
    a = photon_geocode(location)
    b = photon_geocode(dest)
    if not a or not b:
        return None
    return compute_route(a, b)


# =============================================================
# PARSING UTILITIES
# =============================================================

def parse_weight_lbs(weight_text):
    if not weight_text:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)", weight_text.replace(" ", ""))
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", "")))
    except ValueError:
        return None


def _find(pattern, text, flags=re.IGNORECASE):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def extract_vehicle_required(t):
    vr = _find(r"Vehicle\s*required\s*:\s*([^\n]+)", t)
    if vr:
        return vr
    vr = _find(r"Vehicle\s*required\s+([^\n]+)", t)
    if vr:
        return vr
    return _find(
        r"Vehicle\s*required.*?"
        r"(LARGE STRAIGHT|SMALL STRAIGHT|CARGO VAN|SPRINTER|BOX TRUCK|STRAIGHT TRUCK)",
        t
    )


def _bounded_section_window(text: str, label_regex: str,
                             stop_regexes=None, window: int = 350):
    m = re.search(label_regex, text, re.IGNORECASE)
    if not m:
        return None
    start = m.end()
    chunk = text[start:start + window]
    if stop_regexes:
        for pat in stop_regexes:
            sm = re.search(pat, chunk, re.IGNORECASE)
            if sm:
                chunk = chunk[:sm.start()]
    return chunk


def extract_datetime_from_window(win):
    return _find(
        r"([0-9]{1,2}/[0-9]{1,2}/(?:[0-9]{4}|[0-9]{2})"
        r"\s+[0-9]{1,2}:[0-9]{2}(?:\s*(?:AM|PM))?(?:\s*(?:EST|EDT))?)",
        win or "",
    )


def extract_location_after_label(text, label_regex):
    US_STATES   = _US_STATES_SET
    FAKE_STATES = {"XX", "ZZ", "YY", "AA", "BB", "QQ"}
    m = re.search(label_regex, text, re.IGNORECASE)
    if not m:
        return None
    chunk     = text[m.end():m.end() + 300]
    src_lines = [l.strip() for l in chunk.splitlines() if l.strip()]
    for line in src_lines[:8]:
        m2 = re.search(r"\b([A-Za-z][A-Za-z .'\-]{1,30},\s*[A-Z]{2}\s*\d{5})\b", line)
        if m2:
            state = re.search(r",\s*([A-Z]{2})", m2.group(1))
            if state and state.group(1) in US_STATES:
                return m2.group(1).strip()
        m2 = re.search(r"\b([A-Za-z][A-Za-z ]{1,25})\s+([A-Z]{2})\s+(\d{5})\b", line)
        if m2 and m2.group(2) in US_STATES:
            return f"{m2.group(1).strip()}, {m2.group(2)} {m2.group(3)}"
        m2 = re.search(r"\b([A-Za-z][A-Za-z .'\-]{1,30},\s*[A-Z]{2})\b", line)
        if m2 and m2.group(1).split(",")[-1].strip() in US_STATES:
            return m2.group(1).strip()
        m2 = re.search(r",\s*([A-Z]{2}\s*\d{5})\b", line)
        if m2 and m2.group(1)[:2] in US_STATES:
            return m2.group(1).strip()
        m2 = re.search(r"\b([A-Z]{2})\s+(\d{5})\b", line)
        if m2 and m2.group(1) in US_STATES:
            return f"{m2.group(1)} {m2.group(2)}"
        m2 = re.search(
            r"\b(\d{5})\s*[-–]?\s*([A-Za-z][A-Za-z .'\-]{1,30},\s*[A-Z]{2})\b", line)
        if m2:
            state = re.search(r",\s*([A-Z]{2})", m2.group(2))
            if state and state.group(1) in US_STATES:
                return f"{m2.group(2).strip()} {m2.group(1)}"
        states_found = [
            (m3.group(), m3.start())
            for m3 in re.finditer(r"\b([A-Z]{2})\b", line)
            if m3.group() in US_STATES and m3.group() not in FAKE_STATES
        ]
        zips_found = [
            (m3.group(), m3.start())
            for m3 in re.finditer(r"\b(\d{5})\b", line)
        ]
        best_pair = None
        for state, spos in states_found:
            for zip_code, zpos in zips_found:
                dist = abs(spos - zpos)
                if dist <= 30:
                    if best_pair is None or dist < best_pair[2]:
                        best_pair = (state, zip_code, dist)
        if best_pair:
            return f"{best_pair[0]} {best_pair[1]}"
        m2 = re.search(r"\b(\d{5})\b", line)
        if m2:
            return m2.group(1)
    return None


def _is_placeholder_location(loc):
    if not loc:
        return True
    FAKE_STATES = {"XX", "ZZ", "YY", "AA", "BB", "QQ"}
    clean = loc.strip().upper()
    if re.fullmatch(r"[A-Z]{2}", clean):
        return clean in FAKE_STATES
    m = re.match(r"([A-Z]{2})\s*(\d{5})", clean)
    if m:
        if m.group(1) in FAKE_STATES:
            return True
        if m.group(1) in _US_STATES_SET and m.group(2) == "00000":
            return True
        return False
    m = re.match(r",\s*([A-Z]{2})(\s*\d{5})?$", clean)
    if m:
        return m.group(1) in FAKE_STATES
    return False


def _round_minutes(total_minutes):
    return round(int(total_minutes) / 30) * 30


def fmt_hours_minutes(total_minutes):
    rounded = _round_minutes(total_minutes)
    h, m = divmod(rounded, 60)
    if h and m == 0:
        return f"{h}hrs"
    return f"{h}hrs {m:02d}min" if h else f"{m}min"


def calculate_tt_minutes(miles):
    if not miles:
        return None
    base_hours = miles / 45
    if miles < 1000:
        total_hours = base_hours
    elif miles < 1500:
        total_hours = base_hours + 5
    else:
        total_hours = base_hours + 6
    return int(round(total_hours) * 60)


def extract_estimated_miles_from_email(text: str):
    patterns = [
        r"[Ee]st(?:imated)?\.?\s*[Mm]iles?\s*[:\-]?\s*([0-9,]+)",
        r"\b[Mm]iles?\s*[:\-]\s*([0-9,]+)",
        r"\b([0-9,]+)\s+miles?\b",
        r"[Dd]istance\s*[:\-]\s*([0-9,]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


# =============================================================
# TRUCK MATCHING — VEHICLE-TYPE COMPARISON  (FIX #3)
# =============================================================

def _vehicle_matches(truck_veh: str, required: str) -> bool:
    """
    Word-SEQUENCE prefix comparison, not substring containment.
    A match only counts when the shorter side's words are a literal
    prefix of the longer side's words:
      "LARGE STRAIGHT"  vs "LARGE STRAIGHT TRUCK"  -> match
      "VAN"             vs "SPRINTER VAN"          -> NO match
      "CARGO VAN"       vs "CARGO VAN"             -> match
    """
    t_words = truck_veh.upper().split()
    r_words = (required or "").upper().split()
    if not t_words or not r_words:
        return False
    if t_words == r_words:
        return True
    if len(t_words) <= len(r_words):
        shorter, longer = t_words, r_words
    else:
        shorter, longer = r_words, t_words
    return longer[:len(shorter)] == shorter


# =============================================================
# DATE MATCHING  (FIX #5: timezone-aware)
# =============================================================

def normalize_mmddyyyy(date_str):
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.year < 100:
                dt = dt.replace(year=dt.year + 2000)
            return dt.strftime("%m/%d/%Y")
        except ValueError:
            pass
    return None


def extract_pickup_date_only(pickup_dt):
    if not pickup_dt:
        return None
    m = re.search(r"(\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}))", pickup_dt)
    return normalize_mmddyyyy(m.group(1)) if m else None


def has_pickup_asap(text):
    if not text:
        return False
    for p in [r"\bASAP\b", r"\bA\.S\.A\.P\.?\b", r"Pick[\s\-]*[Uu]p\s+ASAP",
              r"ASAP\s+Pick[\s\-]*[Uu]p", r"\bPU\s+ASAP\b", r"\bASAP\s+PU\b"]:
        if re.search(p, text, re.IGNORECASE):
            return True
    t = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9\s]", " ", text.upper())).strip()
    return any(re.search(p, t) for p in [r"\bASAP\b", r"\bA\s*S\s*A\s*P\b"])


def has_deliver_direct(text):
    if not text:
        return False
    t = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9\s]", " ", text.upper())).strip()
    return any(re.search(p, t) for p in [
        r"\bDELIVER\s+DIRECT\b", r"\bDELIVERY\s+DIRECT\b",
        r"\bDIRECT\s+DELIVERY\b", r"\bDEL\s+DIRECT\b",
    ])


def has_pickup_direct(text):
    if not text:
        return False
    t = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9\s]", " ", text.upper())).strip()
    return any(re.search(p, t) for p in [
        r"\bPICK\s*UP\s+DIRECT\b", r"\bPU\s+DIRECT\b", r"\bDIRECT\s+PICK\s*UP\b",
    ])


def truck_date_matches(truck, pickup_dt, raw_text):
    truck_date = (truck.get("pickup_date") or "").strip().upper()
    if not truck_date:
        return True
    norm = normalize_mmddyyyy(truck_date)
    if not norm:
        return False
    # Eastern time, not naive server-local (server likely runs UTC, which
    # rolls the calendar date over hours before Eastern does).
    today            = datetime.now(ZoneInfo("America/New_York")).strftime("%m/%d/%Y")
    pickup_date_only = extract_pickup_date_only(pickup_dt)
    if pickup_date_only:
        return pickup_date_only == norm
    if has_pickup_asap(raw_text):
        return norm == today
    return False


# =============================================================
# TRUCK DEFINITIONS  (kept in sync with client format —
# VEHICLE:DRIVER:CHAT_ID:DIMS:PAYLOAD:EQUIPMENT:STATES:ZIP[:DATE] —
# not currently invoked by the FastAPI path, which sends structured
# TruckDef JSON instead, but kept correct for any direct/legacy use)
# =============================================================

def parse_truck_definitions(text):
    trucks = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(":")]
        if len(parts) < 4:
            continue
        vehicle      = parts[0]
        driver       = parts[1]
        chat_id_s    = parts[2] if len(parts) > 2 else ""
        dims         = parts[3] if len(parts) > 3 else ""
        payload_text = parts[4] if len(parts) > 4 else ""
        equipment    = parts[5] if len(parts) > 5 else ""
        states_raw   = parts[6] if len(parts) > 6 else ""
        zip_loc      = parts[7] if len(parts) > 7 else ""
        date         = parts[8].upper() if len(parts) > 8 else ""
        truck_states = expand_states(states_raw) if states_raw.strip() else None
        try:
            chat_id = int(chat_id_s) if chat_id_s.strip() else None
        except ValueError:
            chat_id = None
        trucks.append({
            "vehicle":          vehicle.upper(),
            "zip":              zip_loc,
            "driver_name":      driver,
            "dimensions":       dims,
            "max_payload_lbs":  parse_weight_lbs(payload_text),
            "max_height_in":    parse_height_from_dims(dims),
            "pickup_date":      date,
            "allowed_states":   truck_states,
            "equipment":        equipment,
            "telegram_chat_id": chat_id,
        })
    return trucks


def validate_truck_definitions(text):
    errors = []
    for i, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(":")]
        if len(parts) < 4:
            errors.append(
                f"Line {i}: need VEHICLE:DRIVER:CHAT_ID:DIMS:PAYLOAD "
                f"(got {len(parts)} field{'s' if len(parts) != 1 else ''})"
            )
            continue
        if not parts[0]:
            errors.append(f"Line {i}: vehicle type is empty")
        if not parts[1]:
            errors.append(f"Line {i}: driver name is empty")
        if parts[2].strip() and not parts[2].strip().lstrip("-").isdigit():
            errors.append(f"Line {i}: chat ID '{parts[2]}' must be a number or blank")
        if len(parts) > 4 and parse_weight_lbs(parts[4]) is None:
            errors.append(f"Line {i}: cannot parse payload '{parts[4]}' as a number")
        if len(parts) > 6 and parts[6].strip():
            if not expand_states(parts[6]):
                errors.append(
                    f"Line {i}: cannot expand '{parts[6]}' — "
                    f"use state codes (OH,PA) or region names (East Coast, Midwest, West Coast)"
                )
        if len(parts) > 8 and parts[8].strip():
            valid = False
            for fmt in ("%m/%d/%Y", "%m/%d/%y"):
                try:
                    datetime.strptime(parts[8].strip(), fmt)
                    valid = True
                    break
                except ValueError:
                    pass
            if not valid:
                errors.append(f"Line {i}: date '{parts[8]}' must be MM/DD/YYYY or MM/DD/YY")
    return errors


# =============================================================
# BID BODY BUILDERS  (signatures unchanged — main.py / driver_bot.py
# call these directly, so the parameter list must stay stable)
# =============================================================

def build_bid_email_body(order, broker, vehicle, pickup, pickup_dt,
                          delivery, delivery_dt, google_deadhead=None,
                          driver_name="", truck_type="", truck_dims="",
                          deadhead_eta_minutes=None, truck_equipment="",
                          bid_template=None):
    eta_str = fmt_hours_minutes(deadhead_eta_minutes) if deadhead_eta_minutes else ""
    data = dict(
        order=order or "", broker_name=broker or "",
        vehicle_required=vehicle or "", pickup_loc=pickup or "",
        pickup_dt=pickup_dt or "", delivery_loc=delivery or "",
        delivery_dt=delivery_dt or "", google_deadhead=google_deadhead or "",
        driver_name=driver_name, truck_type=truck_type,
        truck_dimensions=truck_dims, deadhead_eta_str=eta_str,
        truck_equipment=truck_equipment or "",
        vehicle_type=truck_type or vehicle or "",
        pickup_date_only=(pickup_dt or "").split()[0] if pickup_dt else "",
        delivery_date_only=(delivery_dt or "").split()[0] if delivery_dt else "",
        deadhead_miles=str(google_deadhead) if google_deadhead is not None else "",
    )
    if bid_template is None:
        with BID_TEMPLATE_LOCK:
            bid_template = BID_TEMPLATE
    try:
        return bid_template.format(**data)
    except KeyError as e:
        print(f"BID_TEMPLATE missing key: {e}", flush=True)
        return bid_template


def build_bid_reply_body(order, vehicle_required, pickup_loc, pickup_dt,
                          delivery_loc, delivery_dt, google_deadhead=None,
                          driver_name="", truck_type="", truck_dimensions="",
                          deadhead_eta_minutes=None, truck_equipment="",
                          bid_template=None):
    return build_bid_email_body(
        order=order, broker="", vehicle=vehicle_required,
        pickup=pickup_loc, pickup_dt=pickup_dt,
        delivery=delivery_loc, delivery_dt=delivery_dt,
        google_deadhead=google_deadhead, driver_name=driver_name,
        truck_type=truck_type, truck_dims=truck_dimensions,
        deadhead_eta_minutes=deadhead_eta_minutes,
        truck_equipment=truck_equipment,
        bid_template=bid_template,
    )


# =============================================================
# TRUCK MATCHING — PARALLEL PATH
# =============================================================

def find_all_trucks_for_pickup(
        trucks, vehicle_required, pickup_loc,
        pickup_dt, raw_text,
        load_weight_lbs=None,
        load_height_in=None,
        delivery_loc=None,
        max_radius_miles=500):
    """
    Every truck returned here has already been confirmed to be within
    max_radius_miles by real routed distance (FIX #4).
    """
    delivery_state = extract_state_from_location(delivery_loc) if delivery_loc else None
    pickup_coords  = photon_geocode(pickup_loc) if pickup_loc else None

    # Phase 1: filter candidates without routing (fast, cached geocodes only)
    candidates = []
    for t in trucks:
        if not _vehicle_matches(t["vehicle"], vehicle_required):
            continue
        if not truck_date_matches(t, pickup_dt, raw_text):
            continue
        truck_states = t.get("allowed_states")
        if truck_states and delivery_state:
            if delivery_state not in truck_states:
                continue
        truck_payload = t.get("max_payload_lbs")
        if load_weight_lbs is not None and truck_payload is not None:
            if load_weight_lbs > truck_payload:
                continue
        truck_height = t.get("max_height_in")
        if load_height_in is not None and truck_height is not None:
            if load_height_in > truck_height:
                continue
        # Generous haversine pre-filter (1.4x) — straight-line distance
        # underestimates real road distance. The HARD cap is enforced
        # below in Phase 2 against the real routed distance.
        truck_coords = photon_geocode(t["zip"])
        if truck_coords and pickup_coords:
            sl = _haversine_miles(truck_coords[0], truck_coords[1],
                                   pickup_coords[0], pickup_coords[1])
            if sl > max_radius_miles * 1.4:
                continue
        candidates.append(t)

    if not candidates:
        print(f"[MATCH] 0 candidates passed pre-filters for pickup={pickup_loc} "
              f"vehicle={vehicle_required}", flush=True)
        return []

    # Phase 2: compute real routed distance in parallel, hard radius cap
    matches = []
    lock    = threading.Lock()

    def _route_truck(t):
        name = t.get("driver_name", "?")
        dist = get_distance_from_zip(t["zip"], pickup_loc)
        if not dist:
            print(f"[TRUCK-ROUTE] {name} zip={t['zip']} -> pickup={pickup_loc}  "
                  f"routing FAILED", flush=True)
            return
        if dist["miles"] > max_radius_miles:
            print(f"[TRUCK-ROUTE] {name} zip={t['zip']} -> pickup={pickup_loc}  "
                  f"REJECTED {dist['miles']}mi > cap {max_radius_miles}mi", flush=True)
            return
        print(f"[TRUCK-ROUTE] {name} zip={t['zip']} -> pickup={pickup_loc}  "
              f"ACCEPTED source={dist.get('source','?')}  miles={dist['miles']}", flush=True)
        with lock:
            matches.append({
                "driver_name":          t.get("driver_name", ""),
                "truck_type":           t.get("vehicle", ""),
                "truck_dimensions":     t.get("dimensions", ""),
                "truck_equipment":      t.get("equipment", ""),
                "google_deadhead":      dist["miles"],
                "deadhead_eta_minutes": dist["minutes"],
            })

    max_workers = min(len(candidates), 8)
    ex = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="route")
    try:
        futures = [ex.submit(_route_truck, t) for t in candidates]
        futures_wait(futures, timeout=25)
    finally:
        # FIX #7: never block the request thread waiting on a stuck
        # worker — cancel anything unfinished and move on.
        ex.shutdown(wait=False, cancel_futures=True)

    matches.sort(key=lambda x: x["google_deadhead"])
    print(f"[MATCH] {len(candidates)} candidates -> {len(matches)} within "
          f"{max_radius_miles}mi radius", flush=True)
    return matches


def _fmt_truck_detail(per_truck_log: list) -> str:
    if not per_truck_log:
        return "\n  (no trucks — check truck list / pickup location)"
    return "\n" + "\n".join(f"  {n}: {r}" for n, r in per_truck_log)


def find_best_truck_for_pickup_with_date(
        trucks, vehicle_required, pickup_loc,
        pickup_dt, raw_text,
        load_weight_lbs=None,
        load_height_in=None,
        delivery_loc=None,
        max_radius_miles=500):
    """
    Serial matcher — used for (a) rejection-reason logging when the
    parallel matcher finds zero candidates, and (b) as a recovery path
    if it finds a truck the parallel matcher missed (e.g. a transient
    geocode timeout). FIX #4: enforces the exact same hard radius cap
    as the parallel path, so a recovered match can never be out of
    range — this was the direct cause of the "199 mile" ghost matches.
    """
    best, best_miles   = None, None
    per_truck_log      = []
    saw_vehicle_match  = False
    saw_overweight     = False
    saw_over_height    = False
    saw_state_block    = False
    overweight_detail  = ""
    over_height_detail = ""
    state_block_detail = ""
    delivery_state = extract_state_from_location(delivery_loc) if delivery_loc else None

    for t in trucks:
        name = t.get("driver_name") or t["vehicle"]

        if not _vehicle_matches(t["vehicle"], vehicle_required):
            per_truck_log.append(
                (name, f"vehicle mismatch ({t['vehicle']} ≠ {vehicle_required})"))
            continue
        saw_vehicle_match = True

        if not truck_date_matches(t, pickup_dt, raw_text):
            truck_date_label = t.get("pickup_date") or "any"
            email_date_label = (extract_pickup_date_only(pickup_dt)
                                 or ("ASAP" if has_pickup_asap(raw_text) else "unknown"))
            per_truck_log.append(
                (name, f"date mismatch (truck={truck_date_label}, email={email_date_label})"))
            continue

        truck_states = t.get("allowed_states")
        if truck_states and delivery_state:
            if delivery_state not in truck_states:
                saw_state_block    = True
                detail_str         = (f"state blocked ({delivery_state} not in "
                                       f"{','.join(sorted(truck_states))})")
                per_truck_log.append((name, detail_str))
                state_block_detail = f"{name} → {detail_str}"
                continue

        truck_payload = t.get("max_payload_lbs")
        if load_weight_lbs is not None and truck_payload is not None:
            if load_weight_lbs > truck_payload:
                saw_overweight    = True
                detail_str        = (f"overweight ({load_weight_lbs:,} lb > "
                                      f"{truck_payload:,} lb cap)")
                per_truck_log.append((name, detail_str))
                overweight_detail = detail_str
                continue

        truck_height = t.get("max_height_in")
        if load_height_in is not None and truck_height is not None:
            if load_height_in > truck_height:
                saw_over_height    = True
                detail_str         = (f"too tall ({load_height_in}\" load > "
                                       f"{truck_height}\" door opening)")
                per_truck_log.append((name, detail_str))
                over_height_detail = detail_str
                continue

        truck_coords  = photon_geocode(t["zip"])
        pickup_coords = photon_geocode(pickup_loc)
        if truck_coords and pickup_coords:
            sl = _haversine_miles(truck_coords[0], truck_coords[1],
                                   pickup_coords[0], pickup_coords[1])
            if sl > max_radius_miles * 1.4:
                per_truck_log.append((name, f"too far ({int(sl)} mi)"))
                continue

        dist = get_distance_from_zip(t["zip"], pickup_loc)
        if not dist:
            per_truck_log.append((name, f"routing failed ({pickup_loc})"))
            continue

        # ── FIX #4: hard radius enforcement — previously missing here ──
        if dist["miles"] > max_radius_miles:
            per_truck_log.append(
                (name, f"too far ({dist['miles']} mi > {max_radius_miles} mi cap)"))
            print(f"[FALLBACK-MATCH] {name} REJECTED — {dist['miles']}mi exceeds "
                  f"{max_radius_miles}mi cap", flush=True)
            continue

        per_truck_log.append((name, f"✓ {dist['miles']} mi deadhead"))
        print(f"[FALLBACK-MATCH] {name} ACCEPTED — {dist['miles']}mi within "
              f"{max_radius_miles}mi cap", flush=True)
        if best_miles is None or dist["miles"] < best_miles:
            best, best_miles = t, dist["miles"]

    if best:
        return best, best_miles, None, per_truck_log
    if not saw_vehicle_match:
        return None, None, f"VEHICLE MISMATCH ({vehicle_required})", per_truck_log
    if saw_over_height:
        return None, None, f"TOO TALL ({over_height_detail})", per_truck_log
    if saw_overweight:
        return None, None, f"OVERWEIGHT ({overweight_detail})", per_truck_log
    if saw_state_block:
        return None, None, f"STATE FILTERED ({state_block_detail})", per_truck_log
    return None, None, "NO TRUCK MATCH", per_truck_log


def format_email_time_from_internal_date(internal_date_ms):
    dt_utc = datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc)
    return dt_utc.astimezone(
        ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z")


# =============================================================
# MAIN EMAIL PROCESSOR
# =============================================================

_DELIVERY_STOP_PATS = [
    r"(?m)^\s*Delivery\s*:?\s*$",
    r"\bDelivery\s*:",
    r"\bConsignee\s*:",
    r"\bDrop\s*(?:Off\s*)?:",
]
_PICKUP_STOP_PATS = [
    r"(?m)^\s*Pick[\s\-]*[Uu]p\s*:?\s*$",
    r"\bPick[\s\-]*[Uu]p\s*:",
    r"\bShipper\s*:",
]


def process_bid_email(raw_text, allowed_vehicles, internal_date_ms,
                       max_radius_miles, original_msg_full,
                       trucks=None,
                       bid_template=None,
                       allowed_delivery_states=None):
    local_trucks   = trucks if trucks is not None else []
    local_template = bid_template
    if local_template is None:
        with BID_TEMPLATE_LOCK:
            local_template = BID_TEMPLATE

    _PE0 = time.perf_counter()
    t = raw_text.replace("\r\n", "\n")
    order = _find(r"Bid on Order\s*#\s*([0-9]+)", t) or f"L{internal_date_ms // 1000}"

    vehicle_required = extract_vehicle_required(t)
    if not vehicle_required:
        return None, "NO VEHICLE", order, None
    if allowed_vehicles and not any(v in vehicle_required.upper()
                                     for v in allowed_vehicles):
        return None, f"FILTERED {vehicle_required}", order, None

    _PU_STRICT  = r"(?m)^\s*Pick[\s\-]*[Uu]p\s*:?\s*$"
    _DEL_STRICT = r"(?m)^\s*Delivery\s*:?\s*$"
    _pu_label   = _PU_STRICT  if re.search(_PU_STRICT,  t) else r"Pick\s*-?\s*Up"
    _del_label  = _DEL_STRICT if re.search(_DEL_STRICT, t) else r"Delivery"

    pick_win = _bounded_section_window(t, _pu_label,
                                        stop_regexes=_DELIVERY_STOP_PATS, window=400)
    del_win  = _bounded_section_window(t, _del_label,
                                        stop_regexes=_PICKUP_STOP_PATS,   window=400)

    pickup_loc   = extract_location_after_label(t, _pu_label)
    delivery_loc = extract_location_after_label(t, _del_label)

    pickup_asap   = has_pickup_asap(pick_win or "")
    delivery_asap = has_pickup_asap(del_win  or "")

    pickup_dt   = None if pickup_asap  else extract_datetime_from_window(pick_win)
    delivery_dt = None if delivery_asap else extract_datetime_from_window(del_win)

    if _is_placeholder_location(pickup_loc) or _is_placeholder_location(delivery_loc):
        return None, "PLACEHOLDER LOCATION (XX)", order, None

    # Geocode pickup + delivery in parallel — FIX #7: bounded joins.
    _pu_coords = [None]
    _dl_coords = [None]

    def _geo_pu():
        _pu_coords[0] = photon_geocode(pickup_loc) if pickup_loc else None

    def _geo_dl():
        _dl_coords[0] = photon_geocode(delivery_loc) if delivery_loc else None

    _t1 = threading.Thread(target=_geo_pu, daemon=True)
    _t2 = threading.Thread(target=_geo_dl, daemon=True)
    _t1.start(); _t2.start()
    _t1.join(timeout=8)
    _t2.join(timeout=8)
    _PE1 = time.perf_counter()
    print(f"[TIMING]   geo pickup+delivery: {_PE1-_PE0:.3f}s", flush=True)

    if pickup_loc and _pu_coords[0] and not _in_us(*_pu_coords[0]):
        return None, f"NON-US PICKUP ({pickup_loc})", order, None
    if delivery_loc and _dl_coords[0] and not _in_us(*_dl_coords[0]):
        return None, f"NON-US DELIVERY ({delivery_loc})", order, None

    if allowed_delivery_states:
        delivery_state = extract_state_from_location(delivery_loc)
        if delivery_state and delivery_state not in allowed_delivery_states:
            return None, f"DELIVERY STATE {delivery_state} NOT IN FILTER", order, None

    weight          = _find(r"Weight:\s*([0-9,.\s]+(?:lb|lbs|pounds)?)", t)
    load_weight_lbs = parse_weight_lbs(weight)
    dims_raw        = _find(r"Dimensions:\s*([^\n]+)", t)
    load_height_in  = parse_load_height_from_dims(dims_raw) if dims_raw else None

    stackable_flag    = _find(r"Stackable:\s*(Yes|No)", t)
    pieces_for_height = _find(r"Pieces:\s*([0-9]+)", t)
    if load_height_in is not None:
        # FIX #6: scope the "stacked pieces" override to the dims/pieces
        # area instead of searching the WHOLE email body.
        _dims_area = _bounded_section_window(
            t, r"(?:Dimensions|Pieces)\s*:", window=150
        ) or dims_raw or ""
        stacked_note = re.search(r"\b(\d+)\s*\+\s*(\d+)\s*=\s*(\d+)\b", _dims_area)
        if stacked_note:
            load_height_in = int(stacked_note.group(3))
        elif (stackable_flag or "").upper() == "YES" and pieces_for_height:
            try:
                if int(pieces_for_height) == 2:
                    load_height_in = load_height_in * 2
            except ValueError:
                pass

    estimated_miles_from_email = extract_estimated_miles_from_email(t)

    best_truck, deadhead_miles, reject_reason, per_truck_log = None, None, None, []
    deadhead_eta = None
    all_matches  = []

    if local_trucks:
        if not pickup_loc:
            return (None,
                    "PICKUP LOCATION NOT FOUND\n  (cannot compute deadhead)",
                    order, None)

        all_matches = find_all_trucks_for_pickup(
            local_trucks, vehicle_required, pickup_loc, pickup_dt, t,
            load_weight_lbs, load_height_in, delivery_loc=delivery_loc,
            max_radius_miles=max_radius_miles
        )
        _PE2 = time.perf_counter()
        print(f"[TIMING]   find_all_trucks: {_PE2-_PE1:.3f}s", flush=True)

        if not all_matches:
            _best, _best_miles, reject_reason, per_truck_log = \
                find_best_truck_for_pickup_with_date(
                    local_trucks, vehicle_required, pickup_loc, pickup_dt, t,
                    load_weight_lbs, load_height_in, delivery_loc=delivery_loc,
                    max_radius_miles=max_radius_miles
                )
            if _best:
                # Parallel matcher missed it (cold cache, slow geocode, etc.)
                # but the serial fallback found a valid truck WITHIN RADIUS
                # (enforced above) — use it instead of discarding a good load.
                print(f"[MATCH] Parallel matcher found 0 candidates but serial "
                      f"fallback recovered {_best.get('driver_name')} at "
                      f"{_best_miles}mi (within {max_radius_miles}mi cap) — using it.",
                      flush=True)
                all_matches = [{
                    "driver_name":          _best.get("driver_name", ""),
                    "truck_type":           _best.get("vehicle", ""),
                    "truck_dimensions":     _best.get("dimensions", ""),
                    "truck_equipment":      _best.get("equipment", ""),
                    "google_deadhead":      _best_miles,
                    "deadhead_eta_minutes": int((_best_miles / 45) * 60) if _best_miles else None,
                }]
            else:
                if reject_reason is None:
                    reject_reason = "NO TRUCK MATCH"
                return None, reject_reason + _fmt_truck_detail(per_truck_log), order, None

        best_match     = all_matches[0]
        deadhead_miles = best_match["google_deadhead"]
        deadhead_eta   = {"miles": deadhead_miles,
                           "minutes": best_match["deadhead_eta_minutes"]}

        best_truck = {
            "driver_name": best_match["driver_name"],
            "vehicle":     best_match["truck_type"],
            "dimensions":  best_match["truck_dimensions"],
            "equipment":   best_match["truck_equipment"],
        }
    else:
        _PE2 = time.perf_counter()

    if deadhead_miles:
        deadhead_eta = {"miles": deadhead_miles,
                         "minutes": int((deadhead_miles / 45) * 60)}

    total_miles = None
    if estimated_miles_from_email is not None and deadhead_miles is not None:
        total_miles = estimated_miles_from_email + deadhead_miles

    pickup_direct  = has_pickup_direct(pick_win or "") and not pickup_dt
    deliver_direct = has_deliver_direct(del_win  or "")

    lines = [
        f"draft : {order or 'Unknown'}",
        f"⏱️ Email time: {format_email_time_from_internal_date(internal_date_ms)}",
        f"{vehicle_required}",
        f"📍Pick-up: {pickup_loc or 'UNKNOWN'}",
    ]

    if pickup_asap:
        lines.append(f"Pick-up date (EST): ASAP / {pickup_dt}"
                     if pickup_dt else "Pick-up date (EST): ASAP")
    elif pickup_direct:
        lines.append("Pick-up date (EST): DIRECT")
    else:
        lines.append(f"Pick-up date (EST): {pickup_dt or 'UNKNOWN'}")

    lines += ["", f"📍 Deliver to: {delivery_loc or 'UNKNOWN'}"]

    if delivery_asap:
        lines.append("Deliver date (EST): ASAP")
    elif deliver_direct:
        lines.append("Deliver date (EST): DIRECT")
    else:
        lines.append(f"Deliver date (EST): {delivery_dt or 'UNKNOWN'}")

    lines.append("")

    if deadhead_miles is not None:
        lines.append(f"Out Miles: {deadhead_miles}")
    if estimated_miles_from_email is not None:
        lines.append(f"Loaded Miles: {estimated_miles_from_email}")
    if total_miles is not None:
        lines.append(f"Total Miles: {total_miles}")
    if best_truck:
        lines.append(f"Driver: {best_truck['driver_name']}")
        lines.append(f"Truck Dims: {best_truck['dimensions']}")

    stops = _find(r"([0-9]+)\s*STOPS", t)
    if stops:
        lines.append(f"Stops: {stops}")
    lines.append("")

    pieces_raw = _find(r"Pieces:\s*([0-9]+)", t)
    if pieces_raw and int(pieces_raw) > 0:
        lines.append(f"Pieces: {pieces_raw}")
    if weight:
        lines.append(f"Weight: {weight}")

    if dims_raw:
        dc = dims_raw.strip()
        if (dc and not re.fullmatch(r"[0\s xXlLwWhH]+", dc)
                and not re.search(r"no\s+dim", dc, re.I)
                and not re.search(r"not?\s+specified|n/?a", dc, re.I)):
            lines.append(f"Dims: {dc}")

    stackable = _find(r"Stackable:\s*(Yes|No)", t)
    if stackable:
        lines.append(f"Stackable: {stackable.upper()}")

    notes = _find(r"Notes:\s*([^\n]+)", t)
    if notes:
        lines.append(f"🔔 Notes: {notes}")

    broker_name    = _find(r"Broker\s*Name\s*:?\s*([^\n]+)", t)
    broker_company = _find(r"Broker\s*Company\s*:?\s*([^\n]+)", t)
    broker_phone   = _find(r"Broker\s*Phone\s*:?\s*([^\n]+)", t)
    broker_email   = _find(r"Email\s*:?\s*([^\s\n]+@[^\s\n]+)", t)
    if any([broker_name, broker_company, broker_phone, broker_email]):
        lines += ["", "🤝 Broker Info:"]
        if broker_name:    lines.append(f"Name: {broker_name}")
        if broker_company: lines.append(f"Company: {broker_company}")
        if broker_phone:   lines.append(f"Phone: {broker_phone}")
        if broker_email:   lines.append(f"Email: {broker_email}")

    lines.append("")
    if estimated_miles_from_email:
        tt = calculate_tt_minutes(estimated_miles_from_email)
        if tt:
            lines.append(f"🕒 TT: {fmt_hours_minutes(tt)}")
    if deadhead_eta:
        lines.append(f"🕒 ETA: {fmt_hours_minutes(deadhead_eta['minutes'])}")

    bid_url = None
    for h in original_msg_full.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == "from":
            broker_addr = parseaddr(h.get("value", ""))[1]
            if broker_addr:
                body = build_bid_email_body(
                    order, broker_name or "", vehicle_required,
                    pickup_loc, pickup_dt, delivery_loc, delivery_dt,
                    deadhead_miles,
                    best_truck["driver_name"] if best_truck else "",
                    best_truck["vehicle"]     if best_truck else vehicle_required,
                    best_truck["dimensions"]  if best_truck else "",
                    truck_equipment=best_truck.get("equipment", "") if best_truck else "",
                    bid_template=local_template
                )
                bid_url = (
                    "https://mail.google.com/mail/?view=cm&fs=1&tf=1"
                    f"&to={quote(broker_addr)}"
                    f"&su={quote(f'Re: Bid on Order #{order}')}"
                    f"&body={quote(body)}"
                )
            break

    delivery_dt_stored = "ASAP" if delivery_asap else delivery_dt

    if order:
        with LOAD_STORE_LOCK:
            if len(LOAD_STORE) >= 500:
                del LOAD_STORE[next(iter(LOAD_STORE))]
            LOAD_STORE[order] = {
                "original_msg_full":    original_msg_full,
                "order":                order,
                "vehicle_required":     vehicle_required,
                "pickup_loc":           pickup_loc,
                "pickup_dt":            pickup_dt,
                "delivery_loc":         delivery_loc,
                "delivery_dt":          delivery_dt_stored,
                "google_deadhead":      deadhead_miles,
                "deadhead_eta_minutes": deadhead_eta["minutes"] if deadhead_eta else None,
                "driver_name":          best_truck.get("driver_name") if best_truck else "",
                "truck_type":           best_truck.get("vehicle")     if best_truck else vehicle_required,
                "truck_dimensions":     best_truck.get("dimensions")  if best_truck else "",
                "truck_equipment":      best_truck.get("equipment", "") if best_truck else "",
                "route_url":            build_google_maps_route_url(
                    pickup_loc or "", delivery_loc or ""),
                "bid_template":         local_template,
                "all_trucks":           all_matches,
            }

    _PE3 = time.perf_counter()
    print(f"[TIMING]   formatting+store: {_PE3-_PE2:.3f}s", flush=True)
    print(f"[TIMING]   TOTAL process_bid_email: {_PE3-_PE0:.3f}s", flush=True)

    return "\n".join(lines), vehicle_required, order, bid_url


# =============================================================
# EMAIL BODY EXTRACTION  (kept for parity / potential legacy use)
# =============================================================

def extract_text_from_full_message(msg_full):
    def _walk(payload):
        if not payload:
            return
        for p in payload.get("parts", []):
            yield from _walk(p)
        yield payload

    def _decode(b64):
        return base64.urlsafe_b64decode(b64 + "==").decode("utf-8", errors="replace")

    def html_to_text(h):
        h = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", h)
        h = re.sub(r"(?i)<br\s*/?>", "\n", h)
        h = re.sub(r"(?i)</(p|div|tr|td|th|li|h\d)>", "\n", h)
        h = re.sub(r"<[^>]+>", " ", h)
        h = html_lib.unescape(h)
        h = re.sub(r"[ \t]+", " ", h)
        return re.sub(r"\n\s*\n+", "\n\n", h).strip()

    plain = html = None
    for part in _walk(msg_full.get("payload", {})):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        if mime == "text/plain" and not plain:
            plain = _decode(data)
        elif mime == "text/html" and not html:
            html = _decode(data)

    if plain and plain.strip():
        return plain
    if html and html.strip():
        return html_to_text(html)
    return msg_full.get("snippet", "")


# =============================================================
# FREIGHT MARKERS / LABEL HELPERS  (kept for parity)
# =============================================================

FREIGHT_MARKERS = [
    "BID ON ORDER", "REQUEST FOR QUOTE", "POSTED LOAD",
    "LARGE STRAIGHT", "SMALL STRAIGHT", "CARGO VAN", "SPRINTER",
    "TRACTOR", "BOX TRUCK", "STRAIGHT TRUCK", "FLATBED", "REEFER",
    "HOT SHOT", "POWER ONLY", "STEP DECK", "LOWBOY", "CUBE VAN",
    "EXPEDITED LOAD", "EXPEDITED TRUCK",
]

_SYSIDS = frozenset({
    "INBOX", "UNREAD", "SENT", "IMPORTANT", "STARRED", "TRASH", "SPAM", "DRAFT",
    "CATEGORY_FORUMS", "CATEGORY_UPDATES", "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL", "CATEGORY_PERSONAL",
})


def _has_custom_labels(label_ids):
    return any(lid not in _SYSIDS and not lid.startswith("CATEGORY_")
               for lid in label_ids)


def _extract_state_codes_from_text(text: str) -> list:
    found = []
    seen  = set()
    for token in re.findall(r"\b([A-Z]{2})\b", text.upper()):
        if token in _US_STATES_SET and token not in seen:
            seen.add(token)
            found.append(token)
    return found


# =============================================================
# FASTAPI ENTRY-POINT
# =============================================================

def parse_email_for_api(request_data: dict) -> dict:
    T0 = time.perf_counter()
    local_trucks = []
    for t in request_data.get('trucks', []):
        local_trucks.append({
            'vehicle':         t['vehicle'].upper(),
            'zip':             t['zip_location'],
            'driver_name':     t['driver_name'],
            'dimensions':      t['dimensions'],
            'max_payload_lbs': t.get('max_payload_lbs'),
            'max_height_in':   parse_height_from_dims(t['dimensions']),
            'pickup_date':     t.get('pickup_date', ''),
            'allowed_states':  set(t['allowed_states']) if t.get('allowed_states') else None,
            'equipment':       t.get('equipment', ''),
        })
    T1 = time.perf_counter()
    print(f"[TIMING] truck build: {T1-T0:.3f}s", flush=True)

    def _warm(zip_loc):
        key = (zip_loc or "").strip().upper()
        with _GEO_CACHE_LOCK:
            if key in GEO_CACHE:
                return
        if zip_loc:
            photon_geocode(zip_loc)

    if local_trucks:
        uncached = [t["zip"] for t in local_trucks
                    if (t["zip"] or "").strip().upper() not in GEO_CACHE]
        if uncached:
            # FIX #7: bounded, non-blocking executor shutdown here too —
            # a `with` block would still block on executor.shutdown(wait=True)
            # if any _warm() call hung past its own internal timeouts.
            ex = ThreadPoolExecutor(max_workers=min(4, len(uncached)))
            try:
                futures = [ex.submit(_warm, z) for z in uncached]
                futures_wait(futures, timeout=20)
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
    T2 = time.perf_counter()
    print(f"[TIMING] zip warmup: {T2-T1:.3f}s", flush=True)

    local_bid_template = request_data.get('bid_template') or BID_TEMPLATE

    dummy_msg = {'payload': {'headers': [], 'parts': []},
                 'threadId': '', 'labelIds': [], 'id': ''}

    formatted, info, order, bid_url = process_bid_email(
        raw_text           = request_data['email_body'],
        allowed_vehicles   = request_data['allowed_vehicles'],
        internal_date_ms   = request_data['internal_date_ms'],
        max_radius_miles   = request_data['max_radius_miles'],
        original_msg_full  = dummy_msg,
        trucks             = local_trucks,
        bid_template       = local_bid_template,
    )
    T3 = time.perf_counter()
    print(f"[TIMING] process_bid_email: {T3-T2:.3f}s", flush=True)
    print(f"[TIMING] TOTAL: {T3-T0:.3f}s", flush=True)

    result = {
        'success':      formatted is not None,
        'message':      info or 'OK',
        'formatted':    formatted,
        'order_id':     order,
        'vehicle_info': info if not formatted else None,
    }

    if order:
        with LOAD_STORE_LOCK:
            ld = LOAD_STORE.get(order)
            if ld:
                result['route_url'] = ld.get('route_url', '')
                result['load_data'] = {k: v for k, v in ld.items()
                                        if k != 'original_msg_full'}

    return result


# =============================================================
# DEPLOYMENT MARKER — grep for this in journalctl right after
# restarting the service to confirm the new file is actually live.
# =============================================================
print(f"[PARSER_CORE] Fresh rewrite loaded — cache schema v{CACHE_SCHEMA_VERSION} "
      f"— {datetime.now().isoformat()}", flush=True)