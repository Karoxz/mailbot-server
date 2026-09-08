# =============================================================
# reply_classifier.py  —  server-side module  (Groq backend)
#
# Classifies a broker's email reply on a bid thread into an outcome
# (won / lost / countered / no_signal) using Groq's free tier
# (replaced Gemini 2026-09-08 — see llm_client.py). Called from the
# /api/classify_reply endpoint in main.py, which ALWAYS checks
# bid_history.get_pending_bids_for_thread() first — this module is
# never invoked unless a pending bid already exists on that thread,
# so ordinary inbox traffic never spends a call.
#
# Groq's JSON mode guarantees syntactically valid JSON but, unlike
# Gemini's response_schema, does NOT enforce field names/types itself
# — the schema is spelled out in the system prompt instead, and the
# post-processing below stays defensive (unrecognized status ->
# no_signal, bad confidence -> 0.0) exactly as it already was.
#
# Fails soft: any error (missing key, API failure, unparseable
# response, low confidence) returns/leads to 'no_signal', so a bad
# classification can never silently overwrite a bid's real outcome
# with the wrong one.
# =============================================================

import json
from typing import Any, Dict

import llm_client

_SYSTEM_PROMPT = """You are classifying a single email reply from a freight broker, in the context of a truck bid a dispatcher already sent them.

Read the reply and decide which ONE outcome it represents:
- "won"        — broker is accepting the bid / booking the truck / confirming the load / sending pickup numbers
- "lost"       — broker says the load is covered, booked with someone else, rate too high, or otherwise declining
- "countered"  — broker is proposing a different rate or asking to negotiate further (not yet won or lost)
- "no_signal"  — reply doesn't indicate any outcome (auto-reply, unrelated question, forwarded thread, etc.)

Also include a one-sentence reason, a confidence from 0.0-1.0, and — only if the broker mentioned a specific counter rate — that number.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{"status": "won" | "lost" | "countered" | "no_signal", "confidence": <number 0.0-1.0>, "reason": "<one sentence>", "counter_rate": <number or null>}
"""


def _no_signal(reason: str) -> Dict[str, Any]:
    return {"status": "no_signal", "confidence": 0.0, "reason": reason, "counter_rate": None}


def classify_broker_reply(subject: str, body: str) -> Dict[str, Any]:
    """
    Returns {"status", "confidence", "reason", "counter_rate"}.
    Never raises — any failure degrades to a 'no_signal' result so the
    caller can safely leave the bid's status untouched.
    """
    if not llm_client.has_api_key():
        return _no_signal("classifier unavailable (no API key)")

    # Cap input size — replies are short; this just bounds a
    # pathologically long thread from ballooning the request.
    trimmed_body = (body or "")[:4000]

    try:
        text = llm_client.chat(
            _SYSTEM_PROMPT,
            f"Subject: {subject or '(no subject)'}\n\nBody:\n{trimmed_body}",
            json_mode=True,
            max_tokens=200,
        )
        data = json.loads(text or "")
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
