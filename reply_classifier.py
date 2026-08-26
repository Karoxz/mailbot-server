# =============================================================
# reply_classifier.py  —  server-side module  (Gemini backend)
#
# Classifies a broker's email reply on a bid thread into an outcome
# (won / lost / countered / no_signal) using the Google Gemini API's
# free tier. Called from the /api/classify_reply endpoint in main.py,
# which ALWAYS checks bid_history.get_pending_bids_for_thread() first
# — this module is never invoked unless a pending bid already exists
# on that thread, so ordinary inbox traffic never spends a call.
#
# Uses gemini-2.5-flash with a JSON response_schema — Gemini enforces
# the schema itself rather than us hoping the model follows a prompt
# instruction and stripping markdown fences afterward, so this is
# actually more robust than the old Claude-based version, not just a
# drop-in swap.
#
# Fails soft: any error (missing key, package missing, API failure,
# unparseable response, low confidence) returns/leads to 'no_signal',
# so a bad classification can never silently overwrite a bid's real
# outcome with the wrong one.
# =============================================================

import os
import json
from typing import Any, Dict, Optional

_GEMINI_KEY_WARNED = False
_CLASSIFY_MODEL = "gemini-2.5-flash"

_SYSTEM_PROMPT = """You are classifying a single email reply from a freight broker, in the context of a truck bid a dispatcher already sent them.

Read the reply and decide which ONE outcome it represents:
- "won"        — broker is accepting the bid / booking the truck / confirming the load / sending pickup numbers
- "lost"       — broker says the load is covered, booked with someone else, rate too high, or otherwise declining
- "countered"  — broker is proposing a different rate or asking to negotiate further (not yet won or lost)
- "no_signal"  — reply doesn't indicate any outcome (auto-reply, unrelated question, forwarded thread, etc.)

Also include a one-sentence reason, a confidence from 0.0-1.0, and — only if the broker mentioned a specific counter rate — that number.
"""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "status":       {"type": "string", "enum": ["won", "lost", "countered", "no_signal"]},
        "confidence":   {"type": "number"},
        "reason":       {"type": "string"},
        "counter_rate": {"type": "number"},
    },
    "required": ["status", "confidence", "reason"],
}


def _get_gemini_client():
    global _GEMINI_KEY_WARNED
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        if not _GEMINI_KEY_WARNED:
            print("[CLASSIFY] GEMINI_API_KEY not set — reply classification disabled.", flush=True)
            _GEMINI_KEY_WARNED = True
        return None
    try:
        from google import genai
        from google.genai import types
        return genai.Client(api_key=api_key,
                             http_options=types.HttpOptions(timeout=20_000))
    except ImportError:
        if not _GEMINI_KEY_WARNED:
            print("[CLASSIFY] 'google-genai' package not installed — run: pip install google-genai", flush=True)
            _GEMINI_KEY_WARNED = True
        return None


def _no_signal(reason: str) -> Dict[str, Any]:
    return {"status": "no_signal", "confidence": 0.0, "reason": reason, "counter_rate": None}


def classify_broker_reply(subject: str, body: str) -> Dict[str, Any]:
    """
    Returns {"status", "confidence", "reason", "counter_rate"}.
    Never raises — any failure degrades to a 'no_signal' result so the
    caller can safely leave the bid's status untouched.
    """
    client = _get_gemini_client()
    if not client:
        return _no_signal("classifier unavailable (no API key / package)")

    # Cap input size — replies are short; this just bounds a
    # pathologically long thread from ballooning the request.
    trimmed_body = (body or "")[:4000]

    try:
        from google.genai import types
        resp = client.models.generate_content(
            model=_CLASSIFY_MODEL,
            contents=f"Subject: {subject or '(no subject)'}\n\nBody:\n{trimmed_body}",
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                max_output_tokens=200,
            ),
        )
        # resp.text is Optional[str] — None if Gemini returned no text
        # part (e.g. safety-blocked). "" fails json.loads cleanly and
        # falls into the except below, same fail-soft path either way.
        data = json.loads(resp.text or "")
    except Exception as e:
        print(f"[CLASSIFY] error: {e}", flush=True)
        return _no_signal(f"classifier error: {e}")

    status = data.get("status")
    if status not in ("won", "lost", "countered", "no_signal"):
        return _no_signal(f"unrecognized status in model output: {status!r}")

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "status":       status,
        "confidence":   max(0.0, min(1.0, confidence)),
        "reason":       str(data.get("reason", ""))[:300],
        "counter_rate": data.get("counter_rate"),
    }