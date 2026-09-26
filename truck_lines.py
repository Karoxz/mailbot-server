# =============================================================
# truck_lines.py — server-side, added 2026-09-26
#
# Server-side port of the desktop's truck-definition text format
# (client/main copy.py: parse_truck_definitions / validate_truck_
# definitions / parse_loaded_miles_range / expand_states, read-only
# reference — the desktop is untouched) so a dispatcher can paste their
# existing desktop truck lines straight into the web dashboard:
#
#   VEHICLE:DRIVER:LxWxH:MAXLBS:EQUIPMENT:STATES:ZIP:DATE:RADIUS:CHAT_ID:LOADED_MILES
#
# Fields 1-4 are required; everything after is optional and trailing
# fields may simply be omitted. STATES accepts state codes or the regions
# "West Coast" / "Midwest" / "East Coast". Same validation messages as
# the desktop's, so a line that's valid there is valid here and vice versa.
# =============================================================

import re
from datetime import datetime

from parser_core import _US_STATES_SET

_REGION_MAP = {
    "WEST COAST": {"AZ", "CA", "CO", "ID", "MT", "NV", "NM", "OR", "TX", "UT", "WA", "WY"},
    "MIDWEST":    {"IL", "IN", "IA", "KS", "KY", "MI", "MN", "MO", "NE", "ND", "OH", "SD", "TN", "WI"},
    "EAST COAST": {"CT", "DE", "FL", "GA", "ME", "MD", "MA", "NH", "NJ", "NY", "NC", "PA", "RI", "SC", "VT", "VA"},
}


def parse_weight_lbs(text):
    if not text:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)", text.replace(" ", ""))
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", "")))
    except ValueError:
        return None


def parse_loaded_miles_range(raw: str):
    """"1000" = 1000 and up; "1000-2000" = inclusive range (swapped if
    reversed); blank/unparseable = (None, None)."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    if "-" in raw:
        lo_s, _, hi_s = raw.partition("-")
        lo, hi = parse_weight_lbs(lo_s), parse_weight_lbs(hi_s)
        if lo is None or hi is None:
            return None, None
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi
    lo = parse_weight_lbs(raw)
    return (lo, None) if lo is not None else (None, None)


def expand_states(raw: str):
    if not raw or not raw.strip():
        return None
    result = set()
    for token in raw.split(","):
        token = token.strip().upper()
        if not token:
            continue
        if token in _REGION_MAP:
            result |= _REGION_MAP[token]
            continue
        if len(token) > 2:
            matched = False
            for rname, states in _REGION_MAP.items():
                if token in rname:
                    result |= states
                    matched = True
                    break
            if matched:
                continue
        if token in _US_STATES_SET:
            result.add(token)
    return result if result else None


def validate(text: str) -> list:
    """Same messages as the desktop's validate_truck_definitions."""
    errors = []
    for i, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(":")]
        if len(parts) < 4:
            errors.append(f"Line {i}: need VEHICLE:DRIVER:DIMS:PAYLOAD "
                          f"(got {len(parts)} field{'s' if len(parts) != 1 else ''})")
            continue
        if not parts[0]:
            errors.append(f"Line {i}: vehicle type is empty")
        if not parts[1]:
            errors.append(f"Line {i}: driver name is empty")
        if parse_weight_lbs(parts[3]) is None:
            errors.append(f"Line {i}: cannot parse payload '{parts[3]}' as a number")
        if len(parts) > 5 and parts[5].strip() and not expand_states(parts[5]):
            errors.append(f"Line {i}: cannot expand '{parts[5]}' — use state codes (OH,PA) "
                          f"or region names (East Coast, Midwest, West Coast)")
        if len(parts) > 7 and parts[7].strip():
            ok = False
            for fmt in ("%m/%d/%Y", "%m/%d/%y"):
                try:
                    datetime.strptime(parts[7].strip(), fmt)
                    ok = True
                    break
                except ValueError:
                    pass
            if not ok:
                errors.append(f"Line {i}: date '{parts[7]}' must be MM/DD/YYYY or MM/DD/YY")
        if len(parts) > 8 and parts[8].strip() and parse_weight_lbs(parts[8]) is None:
            errors.append(f"Line {i}: cannot parse radius '{parts[8]}' as a number")
        if len(parts) > 9 and parts[9].strip():
            try:
                int(parts[9].strip())
            except ValueError:
                errors.append(f"Line {i}: chat ID '{parts[9]}' must be a whole number "
                              f"(get it from @userinfobot on Telegram)")
        if len(parts) > 10 and parts[10].strip():
            lo, _ = parse_loaded_miles_range(parts[10])
            if lo is None:
                errors.append(f"Line {i}: cannot parse loaded miles range '{parts[10]}' — "
                              f"use a single number (1000) or a range (1000-2000)")
    return errors


def parse(text: str) -> list:
    """Lines -> dicts ready for fleet_store.add_truck (lines with fewer
    than 4 fields are skipped, like the desktop; call validate() first)."""
    trucks = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(":")]
        if len(parts) < 4:
            continue
        states_raw = parts[5] if len(parts) > 5 else ""
        radius_raw = parts[8] if len(parts) > 8 else ""
        chat_raw = parts[9] if len(parts) > 9 else ""
        chat_id = None
        if chat_raw.strip():
            try:
                chat_id = int(chat_raw.strip())
            except ValueError:
                chat_id = None
        lm_min, lm_max = parse_loaded_miles_range(parts[10] if len(parts) > 10 else "")
        states = expand_states(states_raw) if states_raw.strip() else None
        trucks.append({
            "vehicle":          parts[0].upper(),
            "driver_name":      parts[1],
            "dimensions":       parts[2],
            "max_payload_lbs":  parse_weight_lbs(parts[3]),
            "equipment":        parts[4] if len(parts) > 4 else "",
            "allowed_states":   sorted(states) if states else None,
            "zip_location":     parts[6] if len(parts) > 6 else "",
            "pickup_date":      parts[7].upper() if len(parts) > 7 else "",
            "radius_miles":     parse_weight_lbs(radius_raw) if radius_raw.strip() else None,
            "telegram_chat_id": chat_id,
            "loaded_miles_min": lm_min,
            "loaded_miles_max": lm_max,
        })
    return trucks


def to_line(t: dict) -> str:
    """Inverse of parse() — a truck row as a desktop-format line, so the
    web dashboard can EXPORT its fleet back to the desktop's format.
    Trailing empty optional fields are trimmed, like the desktop's Add
    Truck dialog does."""
    states = ",".join(t.get("allowed_states") or [])
    lm_min, lm_max = t.get("loaded_miles_min"), t.get("loaded_miles_max")
    if lm_min is not None and lm_max is not None:
        lm = f"{lm_min}-{lm_max}"
    elif lm_min is not None:
        lm = str(lm_min)
    else:
        lm = ""
    fields = [
        t.get("vehicle", ""), t.get("driver_name", ""), t.get("dimensions", ""),
        "" if t.get("max_payload_lbs") is None else str(t["max_payload_lbs"]),
        t.get("equipment", "") or "", states, t.get("zip_location", "") or "",
        t.get("pickup_date", "") or "",
        "" if t.get("radius_miles") is None else str(t["radius_miles"]),
        "" if t.get("telegram_chat_id") is None else str(t["telegram_chat_id"]),
        lm,
    ]
    while len(fields) > 4 and fields[-1] == "":
        fields.pop()
    return ":".join(fields)
