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
#   - The REPLY button specifically (desktop-only: replies to a
#     broker's message from the Telegram chat via clipboard + opening
#     Gmail — no server-side equivalent of "the user's own clipboard").
#     BID PC/BID PHONE/DRAFT **are** ported (2026-09-05, requested —
#     "I want telegram messages to work just like they did before") via
#     a getUpdates long-poll thread, same idea as the desktop's
#     get_telegram_updates(), just replying with the bid text in chat
#     (long-press-to-copy in Telegram) instead of a local clipboard,
#     since there's no clipboard to copy to on a headless server.
#   - reply_classifier outcome-detection and delivery_states matching —
#     reasonable fast-follows once this base loop is proven, not part
#     of the first working version.
# =============================================================

import json
import time
import logging
import threading
import traceback
from urllib.parse import quote

import requests

import license_db
import fleet_store
import load_store
import gmail_store
import bid_history
import gmail_client
import parser_core
from gmail_client import GmailAuthError
from parser_core import parse_email_for_api, FREIGHT_MARKERS, extract_text_from_full_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [poller] %(message)s")
logger = logging.getLogger("poller")

# Not the desktop's aggressive 3s — this is an unattended background
# loop with nobody watching a GUI in real time, not a latency-sensitive
# interactive tool.
POLL_INTERVAL_SECONDS = 20
FRESH_WINDOW = "3d"           # TEMP for testing 2026-09-05, was "1h" (the desktop's
                              # own fallback-poller window) — widened on request to
                              # exercise the real match/notify path against the
                              # backlog of already-unread mail. Revert to "1h" once
                              # testing confirms the pipeline works end to end.
MAX_RESULTS_PER_CYCLE = 10
DEFAULT_RADIUS_MILES = 300    # used only if a license never set one


def _telegram_send(bot_token: str, chat_ids: list, text: str, order_id: str = None,
                    route_url: str = None, gmail_url: str = None):
    """BID PC is deliberately a plain `url` button, NOT callback_data
    (2026-09-05, explicit decision) — a genuine one-tap jump straight to
    the Gmail thread, no bot round-trip, no reply message to wait for.
    The tradeoff, accepted: a Telegram button is url XOR callback_data,
    never both, so this specific button no longer records a bid the way
    it used to — BID PHONE/DRAFT still do (they stay callback_data,
    recording the bid and replying with copyable text — long-press to
    copy, since no bot on any platform can write to a recipient's
    device clipboard; that's a hard platform limit, not an
    implementation gap). If BID PC fires before a real thread_id is
    known yet, gmail_url falls back to a search link instead — still a
    genuine one-tap url button either way, just not a guaranteed exact
    thread in that edge case. Row 2 is the ROUTE url button. The REPLY
    button isn't included (desktop-only — see module docstring)."""
    payload = {"text": text}
    keyboard = []
    if order_id:
        row1 = [{"text": "💵 BID PC", "url": gmail_url}] if gmail_url else []
        row1 += [
            {"text": "💵 BID PHONE", "callback_data": f"phone:{order_id}"},
            {"text": "📋 DRAFT",     "callback_data": f"text:{order_id}"},
        ]
        keyboard.append(row1)
    if route_url:
        keyboard.append([{"text": "🚩 ROUTE 🚩", "url": route_url}])
    if keyboard:
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
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


# ── Telegram button callbacks (BID PHONE / DRAFT) ────────────────────
# BID PC (2026-09-05) is a plain url button now, not callback_data — see
# _telegram_send's docstring — so "bid" below is effectively unreachable
# from any newly-sent message. Left in place only so an already-sent
# message from before this change (if one's still sitting in a chat)
# doesn't hit an unknown-action error if tapped.
#
# Mirrors main.py's web_record_bid() exactly (same bid_history.record_bid()
# + parser_core.build_bid_reply_body() call, same fields) — duplicated
# here rather than imported from main.py, since main.py is the FastAPI
# app's entry point and this process isn't that app (same reasoning as
# gmail_client.py keeping its own has_custom_labels() instead of
# importing parser_core's).
_TG_METHOD_MAP = {
    "bid":   ("pc",    "💵 BID PC"),
    "phone": ("phone", "💵 BID PHONE"),
    "text":  ("draft", "📋 DRAFT"),
}


def _gmail_url(order_id, broker_email, thread_id=None):
    """Real thread deep link when we have one (poller-sourced loads now
    carry a real threadId — see parser_core.parse_email_for_api's
    thread_id passthrough), falling back to a search link otherwise —
    same two-tier scheme as common.js's gmailSearchUrl() on the web
    side, kept in sync deliberately."""
    if thread_id:
        return f"https://mail.google.com/mail/u/0/#all/{thread_id}"
    q = str(order_id or "").strip()
    if broker_email:
        q += f" from:{broker_email}"
    return f"https://mail.google.com/mail/u/0/#search/{quote(q)}"


def _record_bid_and_build_text(order_id: str, method: str):
    load = load_store.get_load(order_id)
    if not load:
        return None
    maps_v = load.get("maps_verification") or {}
    thread_id = (load.get("original_msg_full") or {}).get("threadId", "")
    bid_id = bid_history.record_bid(
        order_id=order_id, thread_id=thread_id, bid_method=method,
        vehicle_type=load.get("truck_type") or load.get("vehicle_required", ""),
        driver_name=load.get("driver_name", ""),
        pickup_loc=load.get("pickup_loc", ""), delivery_loc=load.get("delivery_loc", ""),
        broker_name=load.get("broker_name", ""), broker_email=load.get("broker_email", ""),
        deadhead_miles=load.get("google_deadhead"),
        verified_miles=maps_v.get("verified_miles"), verified_source=maps_v.get("verified_source"),
    )
    bid_text = parser_core.build_bid_reply_body(
        order=order_id, vehicle_required=load.get("vehicle_required"),
        pickup_loc=load.get("pickup_loc"), pickup_dt=load.get("pickup_dt"),
        delivery_loc=load.get("delivery_loc"), delivery_dt=load.get("delivery_dt"),
        google_deadhead=load.get("google_deadhead"), driver_name=load.get("driver_name", ""),
        truck_type=load.get("truck_type", ""), truck_dimensions=load.get("truck_dimensions", ""),
        deadhead_eta_minutes=load.get("deadhead_eta_minutes"),
        truck_equipment=load.get("truck_equipment", ""), bid_template=load.get("bid_template"),
    )
    return {
        "bid_id": bid_id, "bid_text": bid_text,
        "gmail_url": _gmail_url(order_id, load.get("broker_email"), thread_id),
    }


def _answer_callback_query(bot_token: str, callback_query_id: str, text: str = None):
    try:
        body = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text
        requests.post(f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                     json=body, timeout=10)
    except Exception:
        pass  # non-fatal — worst case the button spinner times out client-side


def _handle_callback_query(bot_token: str, cq: dict):
    callback_id = cq.get("id")
    chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
    data = cq.get("data", "")
    action, _, order_id = data.partition(":")

    if action not in _TG_METHOD_MAP or not order_id:
        _answer_callback_query(bot_token, callback_id)
        return

    method, label = _TG_METHOD_MAP[action]
    result = _record_bid_and_build_text(order_id, method)
    if not result:
        _answer_callback_query(bot_token, callback_id,
                               text="Order not found in the current live feed.")
        return

    _answer_callback_query(bot_token, callback_id, text=f"Recorded {label}")
    if not chat_id:
        return
    # Nothing is ever auto-sent — same principle as the web dashboard's
    # bid modal. There's no clipboard to copy into on a headless server,
    # so the bid text comes back as a chat message (long-press to copy
    # in Telegram) instead, plus a Gmail search link to find the thread.
    text = f"{label} — Order #{order_id}\n\n{result['bid_text']}"
    payload = {
        "chat_id": chat_id, "text": text,
        "reply_markup": json.dumps({"inline_keyboard": [[
            {"text": "✉️ Find in Gmail", "url": result["gmail_url"]}
        ]]}),
    }
    try:
        requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                     json=payload, timeout=15)
    except Exception as e:
        logger.warning(f"Telegram bid-text reply failed: {e}")


def _telegram_callback_loop(bot_token: str):
    """One thread per distinct bot token, long-polling getUpdates —
    same idea as the desktop's get_telegram_updates(), just handling
    button presses instead of also handling REPLY/clipboard actions.
    Runs independently of the main 20s poll loop since a button press
    should feel close to instant, not wait for the next cycle."""
    offset = None
    logger.info(f"Telegram callback listener starting (bot ...{bot_token[-6:]})")
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"https://api.telegram.org/bot{bot_token}/getUpdates",
                             params=params, timeout=35)
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                cq = update.get("callback_query")
                if cq:
                    try:
                        _handle_callback_query(bot_token, cq)
                    except Exception:
                        logger.error(f"callback handling error:\n{traceback.format_exc()}")
        except Exception as e:
            logger.warning(f"Telegram callback loop error (bot ...{bot_token[-6:]}): {e}")
            time.sleep(5)


_callback_threads = {}  # bot_token -> Thread, so each unique token gets exactly one listener


def _ensure_callback_listeners():
    for lic in license_db.list_standalone_enabled_licenses():
        settings = license_db.get_standalone_settings(lic)
        token = settings and settings.get("bot_token")
        if token and token not in _callback_threads:
            t = threading.Thread(target=_telegram_callback_loop, args=(token,),
                                 daemon=True, name=f"tg-callback-{token[-6:]}")
            t.start()
            _callback_threads[token] = t


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
        "thread_id":         thread_id,   # real Gmail thread — this is what
        "message_id":        msg_id,      # lets "Find in Gmail"/BID PC land
                                           # on the exact thread, not a search
    })

    # "Already read" is the dedup mechanism (same as the desktop) — no
    # separate processed-ids table. Mark read regardless of match/no-match
    # so a non-matching freight email isn't re-checked every cycle.
    gmail_client.mark_as_read(service, msg_id)

    if result.get("success") and result.get("formatted"):
        if bot_token and chat_ids:
            load_data = result.get("load_data") or {}
            route_url = load_data.get("route_url")
            gmail_url = _gmail_url(result.get("order_id"), load_data.get("broker_email"), thread_id)
            _telegram_send(bot_token, chat_ids, result["formatted"],
                           order_id=result.get("order_id"), route_url=route_url,
                           gmail_url=gmail_url)
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
            _ensure_callback_listeners()
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
