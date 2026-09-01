# =============================================================
# thread_learner.py — server-side
#
# Walks a full Gmail thread (your messages + broker replies, already
# extracted client-side and handed over as plain text) and:
#   1. Extracts the rate YOU quoted in each of your own messages
#      (free text — not the structured BID_TEMPLATE output, so this
#      needs an LLM pass, same pattern as broker_note_extractor).
#   2. Classifies each broker reply via the existing reply_classifier
#      (won / lost / no_signal) and also extracts any counter-rate.
#   3. Determines a final outcome: explicit signal from the broker,
#      or — if nothing came back after TIMEOUT_DAYS — an inferred
#      loss, tagged distinctly so decision_engine can weight it
#      differently from a confirmed outcome later if desired.
#   4. Writes the result into bid_history via the SAME functions the
#      rest of the app already uses (record_bid / update_bid_amount /
#      update_bid_outcome) — nothing downstream needs to change to
#      consume this data.
#
# Never called directly by a schedule inside this file — main.py's
# /api/backfill_thread is the only entrypoint, and it re-checks the
# thread_learning_enabled flag before calling process_thread() at all.
# =============================================================

import os
import re
import json
import time
from typing import Optional
from datetime import datetime, timedelta, timezone


import requests
from requests.adapters import HTTPAdapter

import bid_history
import reply_classifier

TIMEOUT_DAYS = 3  # no broker reply after this many days -> inferred loss

_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_session = requests.Session()
_session.mount("https://", HTTPAdapter(max_retries=0))


def _extract_rate_from_text(body: str):
    """
    One bounded Gemini call per message — fails soft to None on any
    error, same fail-soft contract as broker_note_extractor and
    _google_maps_route. A missing/unparseable rate just means that
    message doesn't contribute a data point; it never breaks the walk.

    Free-tier Gemini quota is 15 requests/minute for this model — a
    backfill sends many calls back-to-back, so this sleeps briefly
    before every call and retries once on a 429 rather than burning
    the whole backfill run on quota errors.
    """
    if not _GEMINI_API_KEY or not body or not body.strip():
        return None
    time.sleep(4.5)  # ~13/min, safely under the 15/min free-tier cap
    for attempt in range(2):
        try:
            from google import genai
            client = genai.Client(api_key=_GEMINI_API_KEY)
            prompt = (
                "You are reading one message from a freight rate negotiation "
                "between a dispatcher and a broker. If this message states or "
                "confirms a specific dollar rate for the load (an offer, a "
                "counter-offer, or an acceptance of a rate), return ONLY that "
                "number with no formatting, e.g. 1400 or 1400.50. "
                "If no specific dollar rate is stated, return exactly: null\n\n"
                f"MESSAGE:\n{body[:3000]}"
            )
            resp = client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=prompt,
            )
            text = (resp.text or "").strip()
            if text.lower() == "null" or not text:
                return None
            m = re.search(r"(\d[\d,]*(?:\.\d{1,2})?)", text)
            if not m:
                return None
            return float(m.group(1).replace(",", ""))
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                if attempt == 0:
                    print(f"[THREAD-LEARNER] rate limited, waiting 10s and retrying once...", flush=True)
                    time.sleep(10)
                    continue
            print(f"[THREAD-LEARNER] rate extraction failed (non-fatal): {e}", flush=True)
            return None
    return None


def _order_id_from_text(subject: str, body: str):
    m = re.search(r"Order\s*#\s*([0-9]+)", f"{subject}\n{body}", re.IGNORECASE)
    return m.group(1) if m else None


def process_thread(thread_id: str, order_id: Optional[str], messages: list) -> dict:
    """
    messages: list of dicts, oldest first, each:
        {message_id, date_ms, is_from_me, subject, body}
    (This shape matches ThreadMessageIn in models.py — the client is
    responsible for pulling these out of Gmail and sending them here
    already split into "yours" vs "theirs".)
    """
    if not messages:
        return {"processed": False, "reason": "no messages"}

    prev_count = bid_history.get_processed_thread_count(thread_id)
    if prev_count is not None and prev_count == len(messages):
        return {"processed": False, "reason": "no new messages since last check"}

    messages = sorted(messages, key=lambda m: m["date_ms"])

    if not order_id:
        for m in messages:
            found = _order_id_from_text(m.get("subject", ""), m.get("body", ""))
            if found:
                order_id = found
                break
    order_id = order_id or f"THR-{thread_id[:10]}"

    # ── Walk the thread, extracting a rate from every message ──────────
    turns = []
    for m in messages:
        rate = _extract_rate_from_text(m.get("body", ""))
        turns.append({
            "is_from_me": m["is_from_me"],
            "date_ms":    m["date_ms"],
            "subject":    m.get("subject", ""),
            "body":       m.get("body", ""),
            "rate":       rate,
        })

    my_rates    = [t for t in turns if t["is_from_me"] and t["rate"] is not None]
    their_turns = [t for t in turns if not t["is_from_me"]]

    _from_me_count = sum(1 for t in turns if t["is_from_me"])
    print(f"[THREAD-LEARNER] thread={thread_id} messages={len(turns)} "
          f"from_me={_from_me_count} rates_found={len(my_rates)}", flush=True)

    if not my_rates:
        # Nothing to learn from — you never quoted a number in this thread.
        bid_history.mark_thread_processed(thread_id, len(messages))
        return {"processed": True, "wrote_bid": False, "reason": "no rate found in your messages"}

    final_rate = my_rates[-1]["rate"]   # last number YOU quoted — the
                                         # accepted/most-recent position,
                                         # not necessarily the opening ask

    # ── Determine outcome ────────────────────────────────────────────
    outcome, outcome_source, outcome_note = None, None, None

    # A Rate Confirmation (Gmail label "RC") is definitive proof the load
    # was won, at whatever rate is in this thread — short-circuit past
    # the reply_classifier LLM call entirely rather than inferring from
    # reply tone, which is both cheaper and more trustworthy.
    if any("RC" in m.get("label_ids", []) for m in messages):
        outcome = "won"
        outcome_source = "rc_label"
        outcome_note = "Thread carries the RC (Rate Confirmation) Gmail label"

    if not outcome:
        for t in reversed(their_turns):
            result = reply_classifier.classify_broker_reply(t["subject"], t["body"])
            if result["status"] != "no_signal" and result["confidence"] >= 0.55:
                outcome = result["status"]
                outcome_source = "broker_reply"
                outcome_note = result["reason"]
                break

    if outcome is None:
        last_msg = turns[-1]
        if last_msg["is_from_me"]:
            last_dt = datetime.fromtimestamp(last_msg["date_ms"] / 1000, tz=timezone.utc)
            if datetime.now(timezone.utc) - last_dt > timedelta(days=TIMEOUT_DAYS):
                outcome = "lost"
                outcome_source = "timeout_inferred"
                outcome_note = f"No broker reply after {TIMEOUT_DAYS} days"

    # ── Write to bid_history via the SAME functions the rest of the ────
    # app already uses — record_bid signature matches RecordBidRequest.
    bid_id = bid_history.record_bid(
        order_id=order_id, thread_id=thread_id, bid_method="gmail_backfill",
        vehicle_type="", driver_name="", pickup_loc="", delivery_loc="",
        broker_name="", broker_email="",
        deadhead_miles=None, loaded_miles=None, total_miles=None,
        verified_miles=None, verified_source=None,
        bid_amount=final_rate,
    )

    if outcome:
        bid_history.update_bid_outcome(
            bid_id, outcome,
            outcome_source=outcome_source or "",
            outcome_note=outcome_note or "",
        )

    bid_history.mark_thread_processed(thread_id, len(messages))

    return {
        "processed": True, "wrote_bid": True, "bid_id": bid_id,
        "order_id": order_id, "final_rate": final_rate,
        "outcome": outcome, "outcome_source": outcome_source,
    }