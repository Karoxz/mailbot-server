# =============================================================
# broker_note_extractor.py  —  server-side module  (Gemini backend)
#
# Extracts structured freight details (pallet count/dims, commodity,
# special handling, equipment restrictions, driver requirements,
# appointment flexibility, detention/layover, accessorials, and
# dimensional-freight flags like pipe/tubing/steel/oversized) from
# the raw broker email body using the Google Gemini API's free tier.
#
# Runs on EVERY matched load (unlike reply_classifier, which only
# fires when a broker actually replies) — uses gemini-2.5-flash-lite
# accordingly, the fastest/cheapest current model, since this is the
# highest-frequency call in the whole pipeline. A bounded 12s timeout
# keeps a slow/hung call from meaningfully delaying the bid response
# the dispatcher is waiting on.
#
# Uses a JSON response_schema, so Gemini enforces the output shape
# itself rather than us hoping the model follows a prompt instruction
# and stripping markdown fences afterward.
#
# Fails soft everywhere: missing key, missing package, API error, bad
# JSON, timeout — all return None. Callers must treat None as "no
# extraction available this time," never as an error to surface to
# the dispatcher; a slow/broken call must never block or corrupt a
# bid that would otherwise have gone out fine.
# =============================================================

import os
import json
from typing import Any, Dict, Optional

_GEMINI_KEY_WARNED = False
_EXTRACT_MODEL = "gemini-3.5-flash-lite"

_SYSTEM_PROMPT = """You are extracting structured freight-shipping details from a broker's load-posting email for a truck dispatcher. A truck has ALREADY been matched to this load using separate logic — your job is only to surface details from the email text that a human dispatcher would want to know before confirming the bid.

Read the email and extract:
- pallet_count: number of pallets, if mentioned
- pallet_dimensions: dimensions if given (e.g. "48x40x60")
- commodity: what's being shipped
- dimensional_freight_type: one of "pipe","tubing","steel","oversized","long_freight","irregular","none"
- special_handling: list of special handling instructions (e.g. "team required", "hazmat", "temperature controlled")
- equipment_restrictions: list of equipment needs mentioned (e.g. "liftgate required", "no e-track")
- driver_requirements: list (e.g. "TWIC card", "must call on arrival")
- appointment_flexibility: "strict", "flexible", or "fcfs" if determinable
- detention_terms: text describing detention pay/terms, if mentioned
- layover_terms: text describing layover pay/terms, if mentioned
- accessorials: list of extra charges/services mentioned (e.g. "lumper fee", "inside delivery")
- hidden_constraints: list of any other operational constraints implied but not in an obvious labeled field
- risk_flags: list of SHORT strings — ONLY include items here that a dispatcher should be warned about before bidding: equipment conflicts with what's typically available, oversized/dimensional freight needing special permits or securement, unusually strict appointment windows, driver requirements that could disqualify most drivers, or anything else genuinely risky/unusual. Leave this EMPTY if nothing stands out — most loads should have no risk_flags.

Omit any field entirely if the email gives no information for it, rather than guessing.
"""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "pallet_count":             {"type": "integer"},
        "pallet_dimensions":        {"type": "string"},
        "commodity":                {"type": "string"},
        "dimensional_freight_type": {"type": "string",
                                      "enum": ["pipe", "tubing", "steel", "oversized",
                                                "long_freight", "irregular", "none"]},
        "special_handling":         {"type": "array", "items": {"type": "string"}},
        "equipment_restrictions":   {"type": "array", "items": {"type": "string"}},
        "driver_requirements":      {"type": "array", "items": {"type": "string"}},
        "appointment_flexibility":  {"type": "string", "enum": ["strict", "flexible", "fcfs"]},
        "detention_terms":          {"type": "string"},
        "layover_terms":            {"type": "string"},
        "accessorials":             {"type": "array", "items": {"type": "string"}},
        "hidden_constraints":       {"type": "array", "items": {"type": "string"}},
        "risk_flags":               {"type": "array", "items": {"type": "string"}},
    },
    "required": ["dimensional_freight_type", "special_handling", "equipment_restrictions",
                 "driver_requirements", "accessorials", "hidden_constraints", "risk_flags"],
}

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


def _get_gemini_client():
    global _GEMINI_KEY_WARNED
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        if not _GEMINI_KEY_WARNED:
            print("[EXTRACT] GEMINI_API_KEY not set — broker-note extraction disabled.", flush=True)
            _GEMINI_KEY_WARNED = True
        return None
    try:
        from google import genai
        from google.genai import types
        return genai.Client(api_key=api_key,
                             http_options=types.HttpOptions(timeout=12_000))
    except ImportError:
        if not _GEMINI_KEY_WARNED:
            print("[EXTRACT] 'google-genai' package not installed — run: pip install google-genai", flush=True)
            _GEMINI_KEY_WARNED = True
        return None


def _normalize(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fill in any keys the model omitted and coerce obviously-wrong
    types (e.g. a string where a list was expected) back to a safe
    default, rather than letting a malformed field propagate into
    LOAD_STORE / the Telegram message. Kept as its own function so it
    can be unit-tested without a live API call.
    """
    out = dict(_DEFAULTS)
    for key, default in _DEFAULTS.items():
        val = data.get(key, default)
        if isinstance(default, list) and not isinstance(val, list):
            val = default
        out[key] = val
    return out


def extract_broker_notes(raw_text: str) -> Optional[Dict[str, Any]]:
    """
    Returns the structured extraction dict, or None on any failure
    (missing key/package, API error, bad JSON, timeout). Callers
    should treat None exactly like "nothing to add" — never as a
    reason to alter the bid that's already been computed.
    """
    client = _get_gemini_client()
    if not client:
        return None

    # Bound input size — broker notes are short; this just guards
    # against a pathologically long email ballooning the request.
    trimmed = (raw_text or "")[:6000]
    if not trimmed.strip():
        return None

    try:
        from google.genai import types
        resp = client.models.generate_content(
            model=_EXTRACT_MODEL,
            contents=trimmed,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                max_output_tokens=600,
            ),
        )
        # resp.text is Optional[str] — None if Gemini returned no text
        # part (e.g. safety-blocked). "" fails json.loads cleanly and
        # falls into the except below, same fail-soft path either way.
        data = json.loads(resp.text or "")
    except Exception as e:
        print(f"[EXTRACT] error: {e}", flush=True)
        return None

    return _normalize(data)