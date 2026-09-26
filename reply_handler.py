# =============================================================
# reply_handler.py — server-side, added 2026-09-26
#
# The web/standalone engine's equivalent of what the desktop gets by
# calling POST /api/classify_reply for every message that has a thread:
# classify a broker's reply (won/lost/countered/no_signal) and record the
# outcome against the pending bid(s) for that thread.
#
# Deliberately a NEW module that mirrors main.py's classify_reply
# endpoint using the same underlying calls (bid_history +
# reply_classifier) — NOT a refactor of that endpoint, which is a
# desktop-facing contract that must never change (see
# desktop_compat_check.py). Like the desktop, this records the outcome
# silently: the only Telegram notification a reply produces is the
# "📌 Label / 📍 States" ping (poller.py), with no AI commentary.
# =============================================================

import logging

import bid_history
import reply_classifier

logger = logging.getLogger("reply_handler")


def classify_and_record(license_key: str, thread_id: str, subject: str, body: str) -> dict:
    # Cheap DB lookup FIRST — the LLM is only ever called when this
    # thread actually has a bid awaiting an outcome.
    pending = bid_history.get_pending_bids_for_thread(license_key, thread_id)
    if not pending:
        return {"success": True, "matched": False}

    order_context = {
        "order_id":     pending[0].get("order_id", ""),
        "pickup_loc":   pending[0].get("pickup_loc", ""),
        "delivery_loc": pending[0].get("delivery_loc", ""),
        "broker_name":  pending[0].get("broker_name", ""),
    }
    result = reply_classifier.classify_broker_reply(subject, body)

    if result["status"] == "no_signal" or result["confidence"] < 0.55:
        logger.info(f"[CLASSIFY] thread={thread_id} no actionable signal "
                    f"(status={result['status']} conf={result['confidence']})")
        return {"success": True, "matched": True, "updated": False,
                "classification": result, "order": order_context}

    updated_ids = []
    for bid in pending:
        if bid_history.update_bid_outcome(
            license_key, bid["id"], result["status"],
            outcome_source="broker_reply", outcome_note=result["reason"],
        ):
            updated_ids.append(bid["id"])

    logger.info(f"[CLASSIFY] thread={thread_id} -> {result['status']} "
                f"(conf={result['confidence']}) bids={updated_ids}")
    return {"success": True, "matched": True, "updated": True,
            "bid_ids": updated_ids, "classification": result, "order": order_context}
