# =============================================================
# broker_note_extractor.py  —  server-side module  (Groq backend)
#
# Extracts structured freight details (pallet count/dims, commodity,
# special handling, equipment restrictions, driver requirements,
# appointment flexibility, detention/layover, accessorials, and
# dimensional-freight flags like pipe/tubing/steel/oversized) from
# the raw broker email body using Groq's free tier (replaced Gemini
# 2026-09-08 — see llm_client.py).
#
# Runs on EVERY matched load (unlike reply_classifier, which only
# fires when a broker actually replies) — this is the highest-
# frequency call in the whole pipeline, which is why llama-3.1-8b-instant
# (llm_client.py's model) was picked specifically for its speed and
# generous free-tier request allowance.
#
# Groq's JSON mode guarantees syntactically valid JSON but, unlike
# Gemini's response_schema, does NOT enforce field names/types itself
# — the schema is spelled out in the system prompt instead, and
# _normalize() below stays the real safety net (fills in any field
# the model omitted, coerces an obviously-wrong type back to a safe
# default) exactly as it already was.
#
# Fails soft everywhere: missing key, API error, bad JSON, timeout —
# all return None. Callers must treat None as "no extraction
# available this time," never as an error to surface to the
# dispatcher; a slow/broken call must never block or corrupt a bid
# that would otherwise have gone out fine.
# =============================================================

import json
from typing import Any, Dict, Optional

import llm_client

_SYSTEM_PROMPT = """You are extracting structured freight-shipping details from a broker's load-posting email for a truck dispatcher. A truck has ALREADY been matched to this load using separate logic — your job is only to surface details from the email text that a human dispatcher would want to know before confirming the bid.

Read the email and extract:
- pallet_count: number of pallets, if mentioned (integer or null)
- pallet_dimensions: dimensions if given (e.g. "48x40x60"), or null
- commodity: what's being shipped, or null
- dimensional_freight_type: one of "pipe","tubing","steel","oversized","long_freight","irregular","none"
- special_handling: list of special handling instructions (e.g. "team required", "hazmat", "temperature controlled")
- equipment_restrictions: list of equipment needs mentioned (e.g. "liftgate required", "no e-track")
- driver_requirements: list (e.g. "TWIC card", "must call on arrival")
- appointment_flexibility: "strict", "flexible", or "fcfs" if determinable, else null
- detention_terms: text describing detention pay/terms, if mentioned, else null
- layover_terms: text describing layover pay/terms, if mentioned, else null
- accessorials: list of extra charges/services mentioned (e.g. "lumper fee", "inside delivery")
- hidden_constraints: list of any other operational constraints implied but not in an obvious labeled field
- risk_flags: list of SHORT strings — ONLY include items here that a dispatcher should be warned about before bidding: equipment conflicts with what's typically available, oversized/dimensional freight needing special permits or securement, unusually strict appointment windows, driver requirements that could disqualify most drivers, or anything else genuinely risky/unusual. Leave this EMPTY if nothing stands out — most loads should have no risk_flags.

Omit any field's real value if the email gives no information for it — use null (or an empty list for list fields) rather than guessing.

Respond with ONLY a JSON object, no other text, with exactly these keys: pallet_count, pallet_dimensions, commodity, dimensional_freight_type, special_handling, equipment_restrictions, driver_requirements, appointment_flexibility, detention_terms, layover_terms, accessorials, hidden_constraints, risk_flags.
"""

_DEFAULTS: Dict[str, Any] = {
    "pallet_count":             None,
    "pallet_dimensions":        None,
    "commodity":                None,
    "dimensional_freight_type": "none",
    "special_handling":         [],
    "equipment_restrictions":   [],
    "driver_requirements":      [],
    "appointment_flexibility":  None,
    "detention_terms":          None,
    "layover_terms":            None,
    "accessorials":             [],
    "hidden_constraints":       [],
    "risk_flags":               [],
}

_VALID_DIMENSIONAL_TYPES = {"pipe", "tubing", "steel", "oversized", "long_freight", "irregular", "none"}
_VALID_APPOINTMENT = {"strict", "flexible", "fcfs"}


def _normalize(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fill in any keys the model omitted and coerce obviously-wrong
    types (e.g. a string where a list was expected) back to a safe
    default, rather than letting a malformed field propagate into
    LOAD_STORE / the Telegram message. Kept as its own function so it
    can be unit-tested without a live API call. This is the real
    safety net now that Groq's JSON mode doesn't structurally enforce
    a schema the way Gemini's response_schema did.
    """
    out = dict(_DEFAULTS)
    for key, default in _DEFAULTS.items():
        val = data.get(key, default)
        if isinstance(default, list) and not isinstance(val, list):
            val = default
        out[key] = val
    if out["dimensional_freight_type"] not in _VALID_DIMENSIONAL_TYPES:
        out["dimensional_freight_type"] = "none"
    if out["appointment_flexibility"] not in _VALID_APPOINTMENT:
        out["appointment_flexibility"] = None
    return out


def extract_broker_notes(raw_text: str) -> Optional[Dict[str, Any]]:
    """
    Returns the structured extraction dict, or None on any failure
    (missing key, API error, bad JSON, timeout). Callers should treat
    None exactly like "nothing to add" — never as a reason to alter
    the bid that's already been computed.
    """
    if not llm_client.has_api_key():
        return None

    # Bound input size — broker notes are short; this just guards
    # against a pathologically long email ballooning the request.
    trimmed = (raw_text or "")[:6000]
    if not trimmed.strip():
        return None

    try:
        text = llm_client.chat(_SYSTEM_PROMPT, trimmed, json_mode=True, max_tokens=600, timeout=12)
        data = json.loads(text or "")
    except Exception as e:
        print(f"[EXTRACT] error: {e}", flush=True)
        return None

    return _normalize(data)
