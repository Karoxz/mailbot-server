# =============================================================
# thread_backfill.py — server-side module, added 2026-09-24
#
# Server-side port of the desktop's run_thread_learning_backfill()
# (client/main copy.py) — walks the same 5 labels, pulls each thread's
# messages, and hands them to thread_learner.process_thread() directly
# in-process (the same function /api/backfill_thread's handler in
# main.py already calls) instead of a desktop process driving it via
# HTTP. This is what lets the web dashboard's "Run backfill now" button
# work without the desktop app running at all — the gap this exists to
# close is that thread learning could be toggled on/off from the web,
# but the actual backfill RUN always required a desktop process with
# its own Gmail OAuth connection until now.
#
# Uses gmail_client.py's server-side Gmail access (the same module
# poller.py/standalone mode already use), not a new OAuth flow.
# =============================================================

from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

import gmail_client
import thread_learner
from parser_core import extract_text_from_full_message

BACKFILL_LABELS = ["bid", "Finished Loads", "BROKERS", "RC", "in route"]


def run_backfill(license_key: str, days_back: int = 45) -> dict:
    service = gmail_client.build_service(license_key)
    my_email = service.users().getProfile(userId="me").execute().get("emailAddress", "")
    label_map = gmail_client.get_label_map(service)

    after_ts = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp())

    # Query each label separately, same reasoning as the desktop version:
    # a single combined query lets high-volume labels (e.g. "bid") crowd
    # rarer, more valuable ones (e.g. "RC") out of Gmail's own per-query
    # result cap.
    per_label_counts = {}
    thread_ids_seen = set()
    thread_ids = []
    for label in BACKFILL_LABELS:
        label_q = f'label:"{label}"' if " " in label else f"label:{label}"
        results = service.users().threads().list(
            userId="me", q=f"{label_q} after:{after_ts}", maxResults=200
        ).execute()
        found = [t["id"] for t in results.get("threads", [])]
        per_label_counts[label] = len(found)
        for tid in found:
            if tid not in thread_ids_seen:
                thread_ids_seen.add(tid)
                thread_ids.append(tid)

    processed, skipped, errors = 0, 0, 0
    for tid in thread_ids:
        try:
            thread = service.users().threads().get(
                userId="me", id=tid, format="full"
            ).execute()
            messages_out = []
            for msg in thread.get("messages", []):
                headers = {h["name"].lower(): h["value"]
                           for h in msg.get("payload", {}).get("headers", [])}
                from_addr = parseaddr(headers.get("from", ""))[1].lower()
                messages_out.append({
                    "message_id": msg["id"],
                    "date_ms":    int(msg.get("internalDate", "0")),
                    "is_from_me": bool(my_email) and from_addr == my_email.lower(),
                    "subject":    headers.get("subject", ""),
                    "body":       extract_text_from_full_message(msg),
                    "label_ids":  [label_map.get(lid, lid) for lid in msg.get("labelIds", [])],
                })
            result = thread_learner.process_thread(
                thread_id=tid, order_id=None, messages=messages_out
            )
            if result.get("processed"):
                processed += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"[BACKFILL] thread {tid} failed: {e}", flush=True)
            errors += 1

    return {"total": len(thread_ids), "processed": processed,
            "skipped": skipped, "errors": errors,
            "per_label_counts": per_label_counts}
