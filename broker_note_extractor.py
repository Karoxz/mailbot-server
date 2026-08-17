# =============================================================
# broker_note_extractor.py  —  server-side module
#
# Extracts structured freight details (pallet count/dims, commodity,
# special handling, equipment restrictions, driver requirements,
# appointment flexibility, detention/layover, accessorials, and
# dimensional-freight flags like pipe/tubing/steel/oversized) from
# the raw broker email body using the Claude API.
#
# Runs on EVERY matched load (unlike reply_classifier, which only
# fires when a broker actually replies) — uses a fast/cheap model
# (Haiku) accordingly, since this is the highest-frequency LLM call
# in the whole pipeline. A bounded 12s timeout keeps a slow/hung call
# from meaningfully delaying the bid response the dispatcher is
# waiting on.
#
# Fails soft everywhere: missing key, missing package, API error, bad
# JSON, timeout — all return None. Callers must treat None as "no
# extraction available this time," never as an error to surface to
# the dispatcher; a slow/broken LLM call must never block or corrupt
# a bid that would otherwise have gone out fine.
# =============================================================

import os
import json
import re
from typing import Any, Dict, List, Optional

_ANTHROPIC_KEY_WARNED = False
_EXTRACT_MODEL = "claude-haiku-4-5"

_SYSTEM_PROMPT = """You are extracting structured freight-shipping details from a broker's load-posting email for a truck dispatcher. A truck has ALREADY been matched to this load using separate logic — your job is only to surface details from the email text that a human dispatcher would want to know before confirming the bid.

Read the email and extract:
- pallet_count: number of pallets, or null
- pallet_dimensions: dimensions if given (e.g. "48x40x60"), or null
- commodity: what's being shipped, or null
- dimensional_freight_type: one of "pipe","tubing","steel","oversized","long_freight","irregular","none"
- special_handling: list of special handling instructions (e.g. "team required", "hazmat", "temperature controlled")
- equipment_restrictions: list of equipment needs mentioned (e.g. "liftgate required", "no e-track")
- driver_requirements: list (e.g. "TWIC card", "must call on arrival")
- appointment_flexibility: "strict" | "flexible" | "fcfs" | null
- detention_terms: text describing detention pay/terms, or null
- layover_terms: text describing layover pay/terms, or null
- accessorials: list of extra charges/services mentioned (e.g. "lumper fee", "inside delivery")
- hidden_constraints: list of any other operational constraints implied but not in an obvious labeled field
- risk_flags: list of SHORT strings — ONLY include items here that a dispatcher should be warned about before bidding: equipment conflicts with what's typically available, oversized/dimensional freight needing special permits or securement, unusually strict appointment windows, driver requirements that could disqualify most drivers, or anything else genuinely risky/unusual. Leave this EMPTY if nothing stands out — most loads should have no risk_flags.

Respond with ONLY a JSON object, no markdown fences, no preamble, matching this exact shape:
{"pallet_count": null, "pallet_dimensions": null, "commodity": null, "dimensional_freight_type": "none", "special_handling": [], "equipment_restrictions": [], "driver_requirements": [], "appointment_flexibility": null, "detention_terms": null, "layover_terms": null, "accessorials": [], "hidden_constraints": [], "risk_flags": []}
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


def _get_anthropic_client():
    global _ANTHROPIC_KEY_WARNED
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        if not _ANTHROPIC_KEY_WARNED:
            print("[EXTRACT] ANTHROPIC_API_KEY not set — broker-note extraction disabled.", flush=True)
            _ANTHROPIC_KEY_WARNED = True
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    except ImportError:
        if not _ANTHROPIC_KEY_WARNED:
            print("[EXTRACT] 'anthropic' package not installed — run: pip install anthropic", flush=True)
            _ANTHROPIC_KEY_WARNED = True
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
    client = _get_anthropic_client()
    if not client:
        return None

    # Bound input size — broker notes are short; this just guards
    # against a pathologically long email ballooning the request.
    trimmed = (raw_text or "")[:6000]
    if not trimmed.strip():
        return None

    try:
        resp = client.messages.create(
            model=_EXTRACT_MODEL,
            max_tokens=600,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": trimmed}],
            timeout=12.0,  # bounded — this runs on every matched load, so a
                           # hung call must never meaningfully delay a bid
        )
        text = "".join(
            getattr(block, "text", "") for block in resp.content
            if getattr(block, "type", "") == "text"
        ).strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
    except Exception as e:
        print(f"[EXTRACT] error: {e}", flush=True)
        return None

    return _normalize(data)
