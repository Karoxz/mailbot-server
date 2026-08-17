# =============================================================
# reply_classifier.py  —  server-side module
#
# Classifies a broker's email reply on a bid thread into an outcome
# (won / lost / countered / no_signal) using the Claude API. Called
# from the /api/classify_reply endpoint in main.py, which ALWAYS
# checks bid_history.get_pending_bids_for_thread() first — this module
# is never invoked unless a pending bid already exists on that thread,
# so ordinary inbox traffic never spends an LLM call.
#
# Fails soft: any error (missing key, package missing, API failure,
# unparseable response, low confidence) returns/leads to 'no_signal',
# so a bad classification can never silently overwrite a bid's real
# outcome with the wrong one.
# =============================================================

import os
import json
import re

_ANTHROPIC_KEY_WARNED = False
_CLASSIFY_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = """You are classifying a single email reply from a freight broker, in the context of a truck bid a dispatcher already sent them.

Read the reply and decide which ONE outcome it represents:
- "won"        — broker is accepting the bid / booking the truck / confirming the load / sending pickup numbers
- "lost"       — broker says the load is covered, booked with someone else, rate too high, or otherwise declining
- "countered"  — broker is proposing a different rate or asking to negotiate further (not yet won or lost)
- "no_signal"  — reply doesn't indicate any outcome (auto-reply, unrelated question, forwarded thread, etc.)

Respond with ONLY a JSON object, no markdown fences, no preamble:
{"status": "won" | "lost" | "countered" | "no_signal", "confidence": 0.0-1.0, "reason": "one short sentence", "counter_rate": number or null}
"""


def _get_anthropic_client():
    global _ANTHROPIC_KEY_WARNED
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        if not _ANTHROPIC_KEY_WARNED:
            print("[CLASSIFY] ANTHROPIC_API_KEY not set — reply classification disabled.", flush=True)
            _ANTHROPIC_KEY_WARNED = True
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    except ImportError:
        if not _ANTHROPIC_KEY_WARNED:
            print("[CLASSIFY] 'anthropic' package not installed — run: pip install anthropic", flush=True)
            _ANTHROPIC_KEY_WARNED = True
        return None


def _no_signal(reason: str) -> dict:
    return {"status": "no_signal", "confidence": 0.0, "reason": reason, "counter_rate": None}


def classify_broker_reply(subject: str, body: str) -> dict:
    """
    Returns {"status", "confidence", "reason", "counter_rate"}.
    Never raises — any failure degrades to a 'no_signal' result so the
    caller can safely leave the bid's status untouched.
    """
    client = _get_anthropic_client()
    if not client:
        return _no_signal("classifier unavailable (no API key / package)")

    # Cap input size — replies are short; this just bounds a
    # pathologically long thread from ballooning the request.
    trimmed_body = (body or "")[:4000]

    try:
        resp = client.messages.create(
            model=_CLASSIFY_MODEL,
            max_tokens=200,
            system=_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Subject: {subject or '(no subject)'}\n\nBody:\n{trimmed_body}",
            }],
            timeout=20.0,  # bounded — a hung network call must never tie up
                           # a server request-thread indefinitely
        )
        # resp.content is a union of many block types (TextBlock,
        # ThinkingBlock, ToolUseBlock, ...) — only TextBlock has .text.
        # getattr(..., "") sidesteps needing Pylance to narrow that union
        # and is exactly as safe at runtime (falls back to "" for any
        # non-text block instead of raising).
        text = "".join(
            getattr(block, "text", "") for block in resp.content
            if getattr(block, "type", "") == "text"
        ).strip()
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        data = json.loads(text)
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