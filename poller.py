# =============================================================
# poller.py — the standalone autonomous engine (Phase C, 2026-09-05)
#
# Runs as its OWN process — a separate systemd unit (mailbot-poller),
# never one of the 4 `mailbot-api` uvicorn workers. That separation
# matters: if this loop ran inside the API app itself it would either
# land in one arbitrary worker (no control over which) or run 4x
# redundantly (duplicate Telegram sends, duplicate parses, wasted
# Gemini quota). Every module below is imported directly as a library —
# no HTTP calls to the API server itself; this process IS part of the
# same server, just a different entry point, reading/writing the same
# on-disk SQLite files the 4 API workers already share safely.
#
# Default-off. Nothing here does anything for a license unless it has
# standalone_mode_enabled=1 (license_db.list_standalone_enabled_licenses()),
# a working stored Gmail token, and at least one allowed vehicle
# configured — re-checked every cycle, not just at startup, so turning
# it off in Settings takes effect within one poll interval, no restart.
#
# Deliberately NOT ported from the desktop's main_loop() (see
# MAILBOT_ROADMAP.md's "Standalone engine" section for the full
# reasoning, not repeated here):
#   - Gmail push/Pub/Sub watch + history-API polling. This uses the
#     simpler `is:unread` list-based query only — the same fallback
#     path the desktop itself already relies on (_fast_gmail_poller).
#   - The 5-worker ThreadPoolExecutor concurrency pool. Messages are
#     processed one at a time — a 20s poll interval already dominates
#     latency, concurrency buys nothing here.
#   - The Telegram inline-button callback flow (BID PC/PHONE/DRAFT/
#     REPLY). Those only work on the desktop because it ALSO runs a
#     separate getUpdates long-poll loop to handle the clicks
#     (clipboard copy, opening Gmail — Tkinter-specific). Bid actions
#     are already available via the web Loads page, so notifications
#     here are send-only with a single "open route" link.
#   - reply_classifier outcome-detection and delivery_states matching —
#     reasonable fast-follows once this base loop is proven, not part
#     of the first working version.
# =============================================================

import json
import time
import logging
import traceback

import requests

import license_db
import fleet_store
import load_store
import gmail_store
import bid_history
import gmail_client
from gmail_client import GmailAuthError
from parser_core import parse_email_for_api, FREIGHT_MARKERS, extract_text_from_full_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [poller] %(message)s")
logger = logging.getLogger("poller")

# Not the desktop's aggressive 3s — this is an unattended background
# loop with nobody watching a GUI in real time, not a latency-sensitive
# interactive tool.
POLL_INTERVAL_SECONDS = 20
FRESH_WINDOW = "1h"           # same window as the desktop's fallback poller
MAX_RESULTS_PER_CYCLE = 10
DEFAULT_RADIUS_MILES = 300    # used only if a license never set one


def _telegram_send(bot_token: str, chat_ids: list, text: str, route_url: str = None):
    """Deliberately minimal — plain text plus at most one URL button, no
    callback-based buttons (see module docstring)."""
    payload = {"text": text}
    if route_url:
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": [[{"text": "🚩 ROUTE 🚩", "url": route_url}]]
        })
    for chat_id in chat_ids:
        try:
            body = dict(payload)
            body["chat_id"] = chat_id
            r = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                               json=body, timeout=15)
            if not r.ok:
                logger.warning(f"Telegram send failed (chat {chat_id}): {r.text[:200]}")
        except Exception as e:
            logger.warning(f"Telegram send exception (chat {chat_id}): {e}")


def _parse_chat_ids(chat_ids_csv: str) -> list:
    return [int(c.strip()) for c in (chat_ids_csv or "").split(",")
            if c.strip().lstrip("-").isdigit()]


def _build_query(allowed_vehicles: list) -> str:
    veh_terms = " OR ".join(f'"{v}"' for v in allowed_vehicles)
    return f'is:unread newer_than:{FRESH_WINDOW} ({veh_terms})'


def _process_message(service, label_map, msg_id, license_key, allowed_vehicles,
                      radius_miles, chat_ids, bot_token):
    """Mirrors the desktop's _process_email() guard sequence: full fetch
    -> custom-label guard -> freight-marker check -> thread-label guard
    -> extract body -> parse -> mark read. Returns True if a load was
    matched (for logging only)."""
    full = service.users().messages().get(userId="me", id=msg_id, format="full").execute()

    if gmail_client.has_custom_labels(full.get("labelIds", [])):
        # Already has a real label (Bid, RC, Finished Loads, ...) — a
        # dispatcher or the desktop already handled this one.
        return False

    subject = ""
    for h in full.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == "subject":
            subject = h.get("value", "")
            break

    is_freight = any(m in subject.upper() for m in FREIGHT_MARKERS)
    thread_id = full.get("threadId", "")
    if not is_freight and thread_id:
        thread_labels = gmail_client.get_thread_label_names(service, thread_id, label_map)
        if thread_labels:
            # Whole thread already carries a real label somewhere even
            # though this specific message doesn't yet — same guard the
            # desktop applies before treating a message as "new."
            gmail_client.mark_as_read(service, msg_id)
            return False

    body = extract_text_from_full_message(full)
    internal_date = int(full.get("internalDate", "0"))

    result = parse_email_for_api({
        "email_body":       body,
        "internal_date_ms": internal_date,
        "allowed_vehicles":  allowed_vehicles,
        "max_radius_miles":  radius_miles,
        "trucks":            fleet_store.list_trucks(),
        "bid_template":      None,  # falls back to load_store's server default
    })

    # "Already read" is the dedup mechanism (same as the desktop) — no
    # separate processed-ids table. Mark read regardless of match/no-match
    # so a non-matching freight email isn't re-checked every cycle.
    gmail_client.mark_as_read(service, msg_id)

    if result.get("success") and result.get("formatted"):
        if bot_token and chat_ids:
            route_url = (result.get("load_data") or {}).get("route_url")
            _telegram_send(bot_token, chat_ids, result["formatted"], route_url)
        logger.info(f"[{license_key}] matched load #{result.get('order_id')}")
        return True
    return False


def run_one_license_cycle(license_key: str):
    settings = license_db.get_standalone_settings(license_key)
    if not settings or not settings["standalone_mode_enabled"]:
        return
    allowed_vehicles = [v.strip().upper() for v in settings["allowed_vehicles"].split(",") if v.strip()]
    if not allowed_vehicles:
        logger.info(f"[{license_key}] skipped: no allowed vehicles configured")
        return

    try:
        service = gmail_client.build_service(license_key)
    except GmailAuthError as e:
        logger.warning(f"[{license_key}] skipped: {e}")
        return

    label_map = gmail_client.get_label_map(service)
    query = _build_query(allowed_vehicles)
    resp = service.users().messages().list(
        userId="me", q=query, maxResults=MAX_RESULTS_PER_CYCLE
    ).execute()

    radius_miles = settings["max_radius_miles"] or DEFAULT_RADIUS_MILES
    chat_ids = _parse_chat_ids(settings["chat_ids"])
    bot_token = settings["bot_token"]

    for msg in resp.get("messages", []):
        try:
            _process_message(service, label_map, msg["id"], license_key,
                              allowed_vehicles, radius_miles, chat_ids, bot_token)
        except Exception:
            logger.error(f"[{license_key}] error processing message {msg['id']}:\n"
                         f"{traceback.format_exc()}")


def main():
    logger.info(f"poller starting — poll interval {POLL_INTERVAL_SECONDS}s")
    license_db.init_db()
    fleet_store.init_db()
    load_store.init_db()
    gmail_store.init_db()
    # process_bid_email() (called via parse_email_for_api) reads from
    # bid_history for rate recommendations — this process needs that
    # table to exist regardless of whether mailbot-api has started yet
    # or already created it (don't assume startup ordering).
    bid_history.init_db()
    bid_history.init_processed_threads_table()

    while True:
        processed = 0
        last_error = None
        try:
            licenses = license_db.list_standalone_enabled_licenses()
            for lic in licenses:
                try:
                    run_one_license_cycle(lic)
                    processed += 1
                except Exception:
                    last_error = traceback.format_exc()
                    logger.error(f"[{lic}] cycle failed:\n{last_error}")
        except Exception:
            last_error = traceback.format_exc()
            logger.error(f"outer loop error:\n{last_error}")

        load_store.write_poller_heartbeat(processed, last_error)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
