# =============================================================
# parser_core.py  —  server-side module
# Thread-safe: process_bid_email takes trucks/template as
# parameters — zero global mutation during request handling.
# =============================================================

import os
import re
import html as html_lib
import base64
import json
import time
import threading
import socket
from urllib.parse import quote
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parseaddr
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
import queue as _queue

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

import math

def _haversine_miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi  = math.radians(lat2 - lat1)
    dlam  = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
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
_ORS_DISABLED   = False
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

ROUTE_CACHE_TTL_DAYS = 30

FRESH_WINDOW    = "2d"
STOP_EVENT      = threading.Event()

# LOAD_STORE is only written once per request (after processing),
# never read during processing — safe without a request lock.
LOAD_STORE      = {}
LOAD_STORE_LOCK = threading.Lock()

# Default template — read-only after startup, never mutated per-request
BID_TEMPLATE_LOCK = threading.Lock()
BID_TEMPLATE = """Rate: $
{vehicle_type}
Dims: {truck_dimensions}
MC#

Truck is {google_deadhead} miles out
{truck_equipment}

ETA to PU: {deadhead_eta_str}

ALL BIDS ARE VALID 15 MIN"""

session = requests.Session()
_http_retry = Retry(
    total=4,
    backoff_factor=0.6,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
    raise_on_status=False,
)
session.mount("https://", HTTPAdapter(max_retries=_http_retry))
session.mount("http://",  HTTPAdapter(max_retries=_http_retry))
_gh_session = requests.Session()
_gh_session.mount("http://", HTTPAdapter(max_retries=0))

def _cache_flush_worker():
    """Flush caches to disk every 30s only when dirty."""
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
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC",
}

REGION_MAP = {
    "WEST COAST": {"AZ","CA","CO","ID","MT","NV","NM","OR","TX","UT","WA","WY"},
    "MIDWEST":    {"IL","IN","IA","KS","KY","MI","MN","MO","NE","ND","OH","SD","TN","WI"},
    "EAST COAST": {"CT","DE","FL","GA","ME","MD","MA","NH","NJ","NY","NC","PA","RI","SC","VT","VA"},
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
    """
    Parse truck max usable height from dims string.
    Format: LxWxH(H_door x W_door)  e.g. 264x98x97(94x91)
    Returns door opening height if present, else interior height.
    """
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
# GEOCODING
# =============================================================

def _load_cache(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


GEO_CACHE   = _load_cache(GEO_CACHE_FILE)
ROUTE_CACHE = _load_cache(ROUTE_CACHE_FILE)

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
            r = session.get(NOMINATIM_URL, params=params,
                            headers={"User-Agent": GEOCODER_UA}, timeout=5)
            if r.status_code != 200:
                time.sleep(1)
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
            time.sleep(1)
        except Exception as e:
            print(f"Nominatim exception '{place_clean}' attempt {attempt}: {e}")
            time.sleep(1)
    return None


def _geocode_photon(place: str, place_clean: str):
    place_us = place_clean if re.search(r"\bUSA?\b", place_clean, re.I) else place_clean + ", USA"
    for attempt in range(1, 3):
        if STOP_EVENT.is_set():
            return None
        try:
            r = session.get(PHOTON_URL, params={"q": place_us, "limit": 5, "lang": "en"},
                            headers={"User-Agent": GEOCODER_UA}, timeout=5)
            if r.status_code != 200:
                time.sleep(1)
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
            time.sleep(1)
        except Exception as e:
            print(f"Photon exception '{place_us}' attempt {attempt}: {e}")
            time.sleep(1)
    return None


def photon_geocode(place: str):
    key = place.strip().upper()
    with _GEO_CACHE_LOCK:
        cached = GEO_CACHE.get(key)
    if cached:
        return cached

    place_clean = _normalize_address(place.strip())
    result_holder = [None]
    found_event   = threading.Event()

    def _try_nominatim():
        r = _geocode_nominatim(place, place_clean)
        if r and not found_event.is_set():
            result_holder[0] = r
            found_event.set()

    def _try_photon():
        time.sleep(0.25)
        if found_event.is_set():
            return
        r = _geocode_photon(place, place_clean)
        if r and not found_event.is_set():
            result_holder[0] = r
            found_event.set()

    t_nom = threading.Thread(target=_try_nominatim, daemon=True)
    t_pho = threading.Thread(target=_try_photon,    daemon=True)
    t_nom.start()
    t_pho.start()
    found_event.wait(timeout=12)

    out = result_holder[0]
    if out:
        global _GEO_CACHE_DIRTY
        with _GEO_CACHE_LOCK:
            GEO_CACHE[key] = out
            _GEO_CACHE_DIRTY = True
    else:
        print(f"Geocoding failed for '{place_clean}'")
    return out


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
        r = session.post(ORS_URL,
                         json={"coordinates": [[lon1, lat1], [lon2, lat2]], "units": "mi"},
                         headers={"Authorization": ORS_API_KEY,
                                  "Content-Type": "application/json"},
                         timeout=12)
        if r.status_code in (403, 429):
            with _ORS_LOCK:
                _ORS_FAIL_COUNT += 1
                if _ORS_FAIL_COUNT >= 3:
                    _ORS_DISABLED = True
                    print("ORS disabled for this session.")
            return None
        if r.status_code != 200:
            print(f"ORS HTTP {r.status_code}: {r.text[:200]}")
            return None
        with _ORS_LOCK:
            _ORS_FAIL_COUNT = 0
        summary = r.json()["routes"][0]["summary"]
        return {"miles": round(summary["distance"]),
                "minutes": round(summary["duration"] / 60)}
    except requests.exceptions.Timeout:
        print("ORS timeout")
        return None
    except Exception as e:
        print(f"ORS exception: {e}")
        return None


_GH_PORT_CACHE = {"up": None, "checked_at": 0}
_GH_PORT_TTL   = 10


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
    try:
        r = _gh_session.get(GRAPHHOPPER_URL, params={
            "point":        [f"{lat1},{lon1}", f"{lat2},{lon2}"],
            "profile":      "car",
            "locale":       "en",
            "calc_points":  "false",
            "instructions": "false",
        }, timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        if "paths" not in data or not data["paths"]:
            return None
        path = data["paths"][0]
        base_miles = (path["distance"] / 1609.344) * GRAPHHOPPER_MILE_FACTOR
        miles = round(base_miles * GRAPHHOPPER_CORRECTION)
        if miles < 600:
            miles += DEADHEAD_UNDER_600_OFFSET
        return {"miles": max(0, miles), "minutes": round(path["time"] / 60000)}
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return None
    except Exception as e:
        print(f"GraphHopper exception: {e}")
        return None


def _osrm_route_fallback(origin_latlon, dest_latlon):
    PERMANENT_CODES = {"NoRoute", "NoSegment", "InvalidUrl", "InvalidValue", "TooBig"}
    lat1, lon1 = origin_latlon
    lat2, lon2 = dest_latlon
    url = f"{OSRM_BASE}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
    for _ in range(2):
        try:
            r = session.get(url, params={"overview": "false"}, timeout=10)
            try:
                data = r.json()
            except ValueError:
                data = {}
            osrm_code = data.get("code", "")
            if osrm_code in PERMANENT_CODES or r.status_code in (400, 422):
                return None
            if r.status_code != 200 or osrm_code != "Ok" or not data.get("routes"):
                time.sleep(2)
                continue
            route = data["routes"][0]
            return {"miles": round(route["distance"] / 1609.344),
                    "minutes": round(route["duration"] / 60)}
        except Exception:
            time.sleep(2)
    return None


def compute_route(origin_latlon, dest_latlon):
    global _ROUTE_CACHE_DIRTY
    cache_key = (f"{origin_latlon[0]:.5f},{origin_latlon[1]:.5f}"
                 f"|{dest_latlon[0]:.5f},{dest_latlon[1]:.5f}")
    now = time.time()

    with _ROUTE_CACHE_LOCK:
        cached = ROUTE_CACHE.get(cache_key)

    if cached:
        age_secs      = now - cached.get("ts", 0)
        cached_source = cached.get("source", "unknown")
        gh_running    = is_port_open()

        if gh_running and cached_source != "gh" and age_secs > 3600:
            gh_result = _graphhopper_route(origin_latlon, dest_latlon)
            if gh_result:
                gh_result.update({"source": "gh", "ts": now})
                with _ROUTE_CACHE_LOCK:
                    ROUTE_CACHE[cache_key] = gh_result
                    _ROUTE_CACHE_DIRTY = True
                return gh_result

        if age_secs < ROUTE_CACHE_TTL_DAYS * 86400:
            return cached

    result = _graphhopper_route(origin_latlon, dest_latlon)
    source = "gh"
    if not result:
        result = _ors_route(origin_latlon, dest_latlon)
        source = "ors"
    if not result:
        result = _osrm_route_fallback(origin_latlon, dest_latlon)
        source = "osrm"

    if result:
        result["source"] = source
        result["ts"]     = now
        with _ROUTE_CACHE_LOCK:
            ROUTE_CACHE[cache_key] = result
            _ROUTE_CACHE_DIRTY = True
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
# DATE UTILITIES
# =============================================================

def find_all_trucks_for_pickup(
        trucks, vehicle_required, pickup_loc,
        pickup_dt, raw_text,
        load_weight_lbs=None,
        load_height_in=None,
        delivery_loc=None,
        max_radius_miles=500):
    """
    Returns list of all qualifying trucks sorted by deadhead distance.
    Each entry: {"driver_name", "truck_type", "truck_dimensions",
                 "truck_equipment", "google_deadhead", "deadhead_eta_minutes"}
    """
    matches      = []
    delivery_state = extract_state_from_location(delivery_loc) if delivery_loc else None

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

        truck_coords  = photon_geocode(t["zip"])
        pickup_coords = photon_geocode(pickup_loc)
        if truck_coords and pickup_coords:
            sl = _haversine_miles(truck_coords[0], truck_coords[1],
                                   pickup_coords[0], pickup_coords[1])
            if sl > max_radius_miles * 1.4:
                continue

        dist = get_distance_from_zip(t["zip"], pickup_loc)
        if not dist or dist["miles"] > max_radius_miles:
            continue

        matches.append({
            "driver_name":          t.get("driver_name", ""),
            "truck_type":           t.get("vehicle", ""),
            "truck_dimensions":     t.get("dimensions", ""),
            "truck_equipment":      t.get("equipment", ""),
            "google_deadhead":      dist["miles"],
            "deadhead_eta_minutes": dist["minutes"],
        })

    matches.sort(key=lambda x: x["google_deadhead"])
    return matches

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
    today            = datetime.now().strftime("%m/%d/%Y")
    pickup_date_only = extract_pickup_date_only(pickup_dt)
    if pickup_date_only:
        return pickup_date_only == norm
    if has_pickup_asap(raw_text):
        return norm == today
    return False


# =============================================================
# TRUCK DEFINITIONS
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
        dims         = parts[2]
        payload_text = parts[3]
        equipment    = parts[4] if len(parts) > 4 else ""
        states_raw   = parts[5] if len(parts) > 5 else ""
        zip_loc      = parts[6] if len(parts) > 6 else ""
        date         = parts[7].upper() if len(parts) > 7 else ""
        truck_states = expand_states(states_raw) if states_raw.strip() else None
        trucks.append({
            "vehicle":         vehicle.upper(),
            "zip":             zip_loc,
            "driver_name":     driver,
            "dimensions":      dims,
            "max_payload_lbs": parse_weight_lbs(payload_text),
            "max_height_in":   parse_height_from_dims(dims),
            "pickup_date":     date,
            "allowed_states":  truck_states,
            "equipment":       equipment,
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
                f"Line {i}: need VEHICLE:DRIVER:DIMS:PAYLOAD "
                f"(got {len(parts)} field{'s' if len(parts) != 1 else ''})"
            )
            continue
        if not parts[0]:
            errors.append(f"Line {i}: vehicle type is empty")
        if not parts[1]:
            errors.append(f"Line {i}: driver name is empty")
        if parse_weight_lbs(parts[3]) is None:
            errors.append(f"Line {i}: cannot parse payload '{parts[3]}' as a number")
        if len(parts) > 5 and parts[5].strip():
            if not expand_states(parts[5]):
                errors.append(
                    f"Line {i}: cannot expand '{parts[5]}' — "
                    f"use state codes (OH,PA) or region names "
                    f"(East Coast, Midwest, West Coast)"
                )
        if len(parts) > 7 and parts[7].strip():
            if not normalize_mmddyyyy(parts[7]):
                errors.append(
                    f"Line {i}: date '{parts[7]}' must be MM/DD/YYYY or MM/DD/YY")
    return errors


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
    # Use passed template, fall back to global default
    if bid_template is None:
        with BID_TEMPLATE_LOCK:
            bid_template = BID_TEMPLATE
    try:
        return bid_template.format(**data)
    except KeyError as e:
        print(f"BID_TEMPLATE missing key: {e}")
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
# TRUCK MATCHING
# =============================================================

def _vehicle_matches(truck_veh: str, required: str) -> bool:
    t = truck_veh.upper().strip()
    r = (required or "").upper().strip()
    return t == r or t in r or r in t


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

        # AFTER — haversine pre-filter skips routing for obvious misses
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

        per_truck_log.append((name, f"✓ {dist['miles']} mi deadhead"))
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
# Thread-safe: trucks and bid_template are parameters, not globals.
# Multiple requests can run fully in parallel with zero contention.
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
    """
    Fully thread-safe — all state passed as parameters.
    trucks: list of truck dicts (built by caller from request data)
    bid_template: string template for bid body
    """
    # Use passed trucks/template — never touch globals during processing
    local_trucks   = trucks if trucks is not None else []
    local_template = bid_template
    if local_template is None:
        with BID_TEMPLATE_LOCK:
            local_template = BID_TEMPLATE

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

    # Geocode pickup + delivery in parallel (thread-safe — only reads/writes cache)
    _pu_coords = [None]
    _dl_coords = [None]

    def _geo_pu():
        _pu_coords[0] = photon_geocode(pickup_loc) if pickup_loc else None

    def _geo_dl():
        _dl_coords[0] = photon_geocode(delivery_loc) if delivery_loc else None

    _t1 = threading.Thread(target=_geo_pu, daemon=True)
    _t2 = threading.Thread(target=_geo_dl, daemon=True)
    _t1.start(); _t2.start()
    _t1.join();  _t2.join()

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

    # Stackable height adjustment
    stackable_flag    = _find(r"Stackable:\s*(Yes|No)", t)
    pieces_for_height = _find(r"Pieces:\s*([0-9]+)", t)
    if load_height_in is not None:
        stacked_note = re.search(r"\b(\d+)\s*\+\s*(\d+)\s*=\s*(\d+)\b", t)
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

    if local_trucks:
        if not pickup_loc:
            return (None,
                    "PICKUP LOCATION NOT FOUND\n  (cannot compute deadhead)",
                    order, None)

        best_truck, deadhead_miles, reject_reason, per_truck_log = \
            find_best_truck_for_pickup_with_date(
                local_trucks, vehicle_required, pickup_loc, pickup_dt, t,
                load_weight_lbs, load_height_in, delivery_loc=delivery_loc,
                max_radius_miles=max_radius_miles
            )

        if not best_truck:
            return None, reject_reason + _fmt_truck_detail(per_truck_log), order, None

        if deadhead_miles and deadhead_miles > max_radius_miles:
            return (None,
                    f"DEADHEAD TOO FAR {deadhead_miles} mi (max {max_radius_miles})"
                    + _fmt_truck_detail(per_truck_log),
                    order, None)

    if pickup_loc and delivery_loc:
        get_distance(pickup_loc, delivery_loc)

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
            }

    return "\n".join(lines), vehicle_required, order, bid_url


# =============================================================
# EMAIL BODY EXTRACTION
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
# FREIGHT MARKERS / LABEL HELPERS
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
# Thread-safe: builds local_trucks from request, passes directly
# to process_bid_email as a parameter. Zero global mutation.
# Fully concurrent — no locks held during processing.
# =============================================================

def parse_email_for_api(request_data: dict) -> dict:
    """
    Fully thread-safe FastAPI entry point.
    Builds trucks locally from request data and passes them as
    a parameter to process_bid_email — no global state is mutated
    during request processing, so all workers run in parallel.
    """
    # Build local trucks list — never touches global TRUCKS
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

    # Pre-warm geocode cache for truck ZIPs — parallel, no locks held
    def _warm(zip_loc):
        if zip_loc:
            photon_geocode(zip_loc)

    if local_trucks:
        with ThreadPoolExecutor(max_workers=min(8, len(local_trucks))) as ex:
            ex.map(_warm, [t["zip"] for t in local_trucks])

    local_bid_template = request_data.get('bid_template') or BID_TEMPLATE

    dummy_msg = {'payload': {'headers': [], 'parts': []},
                 'threadId': '', 'labelIds': [], 'id': ''}

    # process_bid_email is now fully stateless — runs with no locks
    formatted, info, order, bid_url = process_bid_email(
        raw_text           = request_data['email_body'],
        allowed_vehicles   = request_data['allowed_vehicles'],
        internal_date_ms   = request_data['internal_date_ms'],
        max_radius_miles   = request_data['max_radius_miles'],
        original_msg_full  = dummy_msg,
        trucks             = local_trucks,
        bid_template       = local_bid_template,
    )

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
    # After the existing process_bid_email call and result building:
    if result.get("load_data") and local_trucks:
        raw_text = request_data.get("email_body", "")
        ld = result["load_data"]

        # Parse weight/height from email to pass correct filters
        # (avoids re-routing trucks already rejected by weight/height)
        import re as _re
        _weight_raw = _re.search(
            r"Weight:\s*([0-9,.\s]+(?:lb|lbs|pounds)?)",
            raw_text, _re.IGNORECASE)
        _dims_raw = _re.search(
            r"Dimensions:\s*([^\n]+)", raw_text, _re.IGNORECASE)

        all_trucks = find_all_trucks_for_pickup(
            local_trucks,
            ld.get("vehicle_required", ""),
            ld.get("pickup_loc", ""),
            ld.get("pickup_dt"),
            raw_text,
            load_weight_lbs=parse_weight_lbs(
                _weight_raw.group(1) if _weight_raw else None),
            load_height_in=parse_load_height_from_dims(
                _dims_raw.group(1) if _dims_raw else None),
            max_radius_miles=request_data["max_radius_miles"],
            delivery_loc=ld.get("delivery_loc"),
        )
        result["load_data"]["all_trucks"] = all_trucks

    return result