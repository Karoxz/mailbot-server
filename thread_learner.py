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

import re
from typing import Optional
from datetime import datetime, timedelta, timezone


import requests
from requests.adapters import HTTPAdapter

import bid_history
import reply_classifier
import llm_client

TIMEOUT_DAYS = 3  # no broker reply after this many days -> inferred loss

_session = requests.Session()
_session.mount("https://", HTTPAdapter(max_retries=0))

_RATE_SYSTEM_PROMPT = (
    "You are reading one message from a freight rate negotiation "
    "between a dispatcher and a broker. If this message states or "
    "confirms a specific dollar rate for the load (an offer, a "
    "counter-offer, or an acceptance of a rate), return ONLY that "
    "number with no formatting, e.g. 1400 or 1400.50. "
    "If no specific dollar rate is stated, return exactly: null"
)


def _extract_rate_from_text(body: str):
    """
    One bounded LLM call per message (Groq, since 2026-09-08 — see
    llm_client.py) — fails soft to None on any error, same fail-soft
    contract as broker_note_extractor and _google_maps_route. A
    missing/unparseable rate just means that message doesn't
    contribute a data point; it never breaks the walk.
    """
    if not llm_client.has_api_key() or not body or not body.strip():
        return None
    try:
        text = llm_client.chat(_RATE_SYSTEM_PROMPT, f"MESSAGE:\n{body[:3000]}", max_tokens=60)
        text = text.strip()
        if text.lower() == "null" or not text:
            return None
        m = re.search(r"(\d[\d,]*(?:\.\d{1,2})?)", text)
        if not m:
            return None
        return float(m.group(1).replace(",", ""))
    except Exception as e:
        print(f"[THREAD-LEARNER] rate extraction failed (non-fatal): {e}", flush=True)
        return None


def _order_id_from_text(subject: str, body: str):
    m = re.search(r"Order\s*#\s*([0-9]+)", f"{subject}\n{body}", re.IGNORECASE)
    return m.group(1) if m else None


def _extract_order_candidates(text: str) -> set:
    """
    Every digit run of length >=4 in the text. Used ONLY to correlate a
    Rate-Confirmation thread against an already-recorded bid by order
    number (see the RC block in process_thread) — a Rate Confirmation
    often arrives as its own single-message Gmail thread with a
    completely different subject line format per broker ("Our Order
    Number 1257371", "LDi Load #2111928", ...), so there's no single
    reliable regex for "the order number" the way _order_id_from_text
    assumes. Casting a wide net here is safe specifically because the
    caller only acts on a candidate that matches a REAL existing
    order_id already in bid_history — a stray 4+ digit number (a
    weight, a year, part of a phone number) only causes a problem if it
    happens to collide with an actual order_id, which is checked
    directly against the DB, not guessed at.
    """
    return set(re.findall(r"\b\d{4,}\b", text or ""))


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

    # ── RC-label cross-thread correlation ───────────────────────────────
    # A Rate Confirmation is definitive proof a load was won, but it
    # commonly arrives as its OWN single-message Gmail thread — a
    # system-generated form, not a reply within the negotiation thread —
    # and rarely contains a dispatcher-quoted rate itself. Handling it
    # only via this thread's own rate/outcome logic misses it entirely
    # (an RC thread with no rate never even reaches outcome
    # determination below). So: correlate by order number against an
    # ALREADY-recorded bid instead, and upgrade that bid directly.
    if any("RC" in m.get("label_ids", []) for m in messages):
        candidates = []
        if order_id and not order_id.startswith("THR-"):
            candidates.append(order_id)
        found_elsewhere = set()
        for m in messages:
            found_elsewhere |= _extract_order_candidates(
                f'{m.get("subject", "")}\n{m.get("body", "")}')
        candidates.extend(sorted(found_elsewhere - set(candidates)))

        matched_bids = []
        matched_candidate = None
        for cand in candidates:
            # Same order number can carry multiple bid rows across
            # separate Gmail threads (a load re-quoted, or the
            # conversation splitting across threads) — upgrade every
            # unresolved one for the first candidate that matches
            # anything, not just the single most-recent row, so a win
            # doesn't leave sibling rows for the same order stuck as
            # stale "lost" data that would otherwise poison win-rate
            # analytics for that broker/lane.
            unresolved = [b for b in bid_history.get_bids_for_order(cand)
                          if b["status"] != "won"]
            if unresolved:
                matched_bids, matched_candidate = unresolved, cand
                break

        if matched_bids:
            upgraded_ids = []
            for b in matched_bids:
                bid_history.update_bid_outcome(
                    b["id"], "won", outcome_source="rc_label",
                    outcome_note=f"Correlated via RC-labeled thread {thread_id} "
                                 f"(order match: {matched_candidate})",
                )
                upgraded_ids.append(b["id"])
            bid_history.mark_thread_processed(thread_id, len(messages))
            print(f"[THREAD-LEARNER] RC thread={thread_id} matched order="
                  f"{matched_candidate} -> upgraded bid ids={upgraded_ids} to won",
                  flush=True)
            return {"processed": True, "wrote_bid": False,
                    "upgraded_bid_ids": upgraded_ids,
                    "order_id": matched_candidate, "outcome": "won",
                    "outcome_source": "rc_label"}

        print(f"[THREAD-LEARNER] RC thread={thread_id} has RC label but no "
              f"matching existing bid found (candidates checked: {candidates})",
              flush=True)
        # Fall through — this RC thread might still carry its own rate
        # data (rarer, but handled below same as any other thread).

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

    # ── Attach to an EXISTING bid row when one exists for this thread ──
    # (2026-09-09 — this is what makes automatic periodic learning
    # actually replace the removed Telegram "what rate did you quote?"
    # ForceReply prompt, rather than just duplicate it). A live BID PC/
    # PHONE/DRAFT click already called record_bid() with the real load
    # details (vehicle, driver, pickup/delivery, mileage) and thread_id
    # at click time, with bid_amount left null — that's the row this
    # rate belongs on. Only fall back to inserting a fresh row (the
    # original behavior) when nothing pending is on file for this
    # thread — a genuinely historical thread the live app never saw.
    pending = bid_history.get_pending_bids_for_thread(thread_id)
    if pending:
        bid_id = pending[0]["id"]
        bid_history.update_bid_amount(bid_id, final_rate)
    else:
        # occurred_at = the real date of the message the final rate was
        # quoted in, NOT "now" — without this every backfilled row's
        # created_at silently becomes "whenever the backfill ran" instead
        # of its real historical date (confirmed: this actively happened —
        # 148 rows all showed up dated the backfill night, not their real
        # Gmail dates, and was reported back as "the bid history is false").
        final_rate_occurred_at = datetime.fromtimestamp(
            my_rates[-1]["date_ms"] / 1000, tz=timezone.utc
        ).isoformat()
        bid_id = bid_history.record_bid(
            order_id=order_id, thread_id=thread_id, bid_method="gmail_backfill",
            vehicle_type="", driver_name="", pickup_loc="", delivery_loc="",
            broker_name="", broker_email="",
            deadhead_miles=None, loaded_miles=None, total_miles=None,
            verified_miles=None, verified_source=None,
            bid_amount=final_rate,
            occurred_at=final_rate_occurred_at,
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
        "attached_to_existing": bool(pending),
    }