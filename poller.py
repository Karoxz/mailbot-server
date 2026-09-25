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

import os
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
import bid_actions
import map_token
import route_cache_store
import activity_log
from gmail_client import GmailAuthError
from parser_core import parse_email_for_api, FREIGHT_MARKERS, extract_text_from_full_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [poller] %(message)s")
logger = logging.getLogger("poller")


# ── Load .env manually — kept independent from main.py's own copy of
# this (not imported from there) so this process doesn't pull in
# FastAPI/pydantic just for a few env vars, matching this file's
# existing no-cross-import-from-main.py design (see this file's top
# docstring). In production both systemd units already have
# EnvironmentFile=.env, so this only actually matters when running
# poller.py directly (local dev) without systemd. ─────────────────────
def _load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip("'\"")
            os.environ.setdefault(key.strip(), val)


_load_env_file()

# Origin for the BID PC price+map link sent over Telegram (bid_price.html
# is served from the same web/ StaticFiles mount as the rest of the
# dashboard). No default — if unset, _process_message falls back to the
# plain gmail_url link instead of a broken URL.
WEB_BASE_URL = os.environ.get("WEB_BASE_URL", "").rstrip("/")

# Not the desktop's aggressive 3s — this is an unattended background
# loop with nobody watching a GUI in real time, not a latency-sensitive
# interactive tool.
POLL_INTERVAL_SECONDS = 20
FRESH_WINDOW = "1h"           # Reverted 2026-09-24 — was widened to "3d" on
                              # 2026-09-05 to exercise the match/notify path
                              # against a backlog of already-unread mail while
                              # testing; the pipeline's been stable in
                              # production for 2+ weeks since, so back to the
                              # desktop's own fallback-poller window.
MAX_RESULTS_PER_CYCLE = 10
DEFAULT_RADIUS_MILES = 300    # used only if a license never set one


def _telegram_send(bot_token: str, chat_ids: list, text: str, order_id: str = None,
                    route_url: str = None, gmail_url: str = None, bid_price_url: str = None):
    """BID PC (2026-09-05) is a plain link in the message TEXT, not an
    inline button — Telegram shows an "Open this link?" interstitial for
    EVERY inline url-type button, on every client, for every bot,
    unconditionally (an anti-phishing measure: a button's label isn't
    the URL, so Telegram confirms the real destination before
    navigating — this can't be suppressed via the Bot API, no matter how
    the button is built). A plain URL sitting in the message body,
    auto-linkified by Telegram, doesn't get that treatment, since the
    destination is already visible with nothing hidden behind a label —
    so putting the link directly in the text is the one lever that
    actually removes the confirmation prompt, at the cost of it not
    looking like a proper button. BID PHONE/DRAFT are unaffected — still
    callback_data buttons, still recording the bid + replying with
    copyable text (long-press to copy — no bot on any platform can
    write to a recipient's device clipboard, that's a hard platform
    limit, not an implementation gap). ROUTE (row 1) is still a real
    button and will show the same interstitial if tapped — not changed,
    since only BID PC's confirmation prompt was raised as an issue.

    bid_price_url (2026-09-24) replaces the plain gmail_url line for
    BID PC — this is the price+map page (route map, price field, live
    rate/mile, same as the desktop's BID PC dialog), which ends its own
    flow with a link to the exact Gmail thread anyway, so nothing is
    lost by not also linking it directly here. Falls back to gmail_url
    if bid_price_url isn't available for any reason (e.g. MAP_TOKEN_SECRET
    isn't configured) — always leaves BID PC actionable somehow."""
    if bid_price_url:
        text = f"{text}\n\n💵 BID PC — enter your price:\n{bid_price_url}"
    elif gmail_url:
        text = f"{text}\n\n💵 BID PC — open thread directly:\n{gmail_url}"
    payload = {"text": text}
    keyboard = []
    if route_url:
        keyboard.append([{"text": "🚩 ROUTE 🚩", "url": route_url}])
    if order_id:
        keyboard.append([
            {"text": "💵 BID PHONE", "callback_data": f"phone:{order_id}"},
            {"text": "📋 DRAFT",     "callback_data": f"text:{order_id}"},
        ])
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
# The actual record+build logic (_record_bid_and_build_text, below) goes
# through the shared bid_actions.record_bid_and_build_text() — the same
# one main.py's web_record_bid() and the BID PC price+map page use — not
# duplicated here anymore (2026-09-24).
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


def _record_bid_and_build_text(license_key: str, order_id: str, method: str,
                                price: float = None, rate_per_mile: float = None):
    """Thin wrapper around the shared bid_actions helper (also used by
    main.py's /api/web/record_bid and the BID PC price+map page) — adds
    the gmail_url this caller specifically needs, built from poller.py's
    own _gmail_url (uses the real thread_id poller.py's Gmail access
    provides, unlike desktop-sourced loads)."""
    result = bid_actions.record_bid_and_build_text(license_key, order_id, method, price, rate_per_mile)
    if not result:
        return None
    result["gmail_url"] = _gmail_url(order_id, result["broker_email"], result["thread_id"])
    return result


def _answer_callback_query(bot_token: str, callback_query_id: str, text: str = None):
    try:
        body = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text
        requests.post(f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                     json=body, timeout=10)
    except Exception:
        pass  # non-fatal — worst case the button spinner times out client-side


def _handle_callback_query(bot_token: str, cq: dict, license_key: str):
    callback_id = cq.get("id")
    chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
    data = cq.get("data", "")
    action, _, order_id = data.partition(":")

    if action not in _TG_METHOD_MAP or not order_id:
        _answer_callback_query(bot_token, callback_id)
        return

    method, label = _TG_METHOD_MAP[action]
    result = _record_bid_and_build_text(license_key, order_id, method)
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


def _telegram_callback_loop(bot_token: str, license_key: str):
    """One thread per distinct bot token, long-polling getUpdates —
    same idea as the desktop's get_telegram_updates(), just handling
    button presses instead of also handling REPLY/clipboard actions.
    Runs independently of the main 20s poll loop since a button press
    should feel close to instant, not wait for the next cycle.

    license_key (2026-09-25): a bot_token is assumed one-per-license in
    practice (each account's own private bot) — captured once here at
    thread-start time, from the same lookup _ensure_callback_listeners
    already does, rather than trying to reverse-lookup "which license
    owns this token" later from inside a getUpdates response that has
    no account context of its own."""
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
                        _handle_callback_query(bot_token, cq, license_key)
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
            t = threading.Thread(target=_telegram_callback_loop, args=(token, lic),
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

    # Real bug, reported 2026-09-23 (desktop side) and confirmed here
    # 2026-09-24: a reply keeps the original subject verbatim (most mail
    # clients do this), so a broker's reply in an already-labeled thread
    # ("Re: LARGE STRAIGHT from...") matched a freight marker and got
    # routed to the fresh-posting parse below instead of the thread-label
    # guard, producing no notification at all. Ported from the desktop's
    # same-day fix (client/main copy.py, _process_email).
    subject_upper = subject.upper()
    is_reply_subject = subject_upper.strip().startswith(("RE:", "FW:", "FWD:"))
    is_freight = (not is_reply_subject) and any(m in subject_upper for m in FREIGHT_MARKERS)
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
        "license_key":      license_key,
        "email_body":       body,
        "internal_date_ms": internal_date,
        "allowed_vehicles":  allowed_vehicles,
        "max_radius_miles":  radius_miles,
        "trucks":            fleet_store.list_trucks(license_key),
        "bid_template":      None,  # falls back to load_store's per-license default
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
            order_id = result.get("order_id")
            gmail_url = _gmail_url(order_id, load_data.get("broker_email"), thread_id)
            bid_price_url = None
            if order_id and WEB_BASE_URL:
                tok = map_token.make_bid_token(license_key, order_id)
                bid_price_url = f"{WEB_BASE_URL}/app/bid_price.html?t={tok}"
            _telegram_send(bot_token, chat_ids, result["formatted"],
                           order_id=order_id, route_url=route_url,
                           gmail_url=gmail_url, bid_price_url=bid_price_url)
        logger.info(f"[{license_key}] matched load #{result.get('order_id')}")
        activity_log.log_event(license_key, "load_matched",
                                f"Standalone poller matched load #{result.get('order_id')}")
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
    # or already created it (don't assume startup ordering). Same reason
    # for route_cache_store below — parse_email_for_api's geocode/route
    # calls now hit those tables directly (2026-09-25 multi-worker fix).
    bid_history.init_db()
    bid_history.init_processed_threads_table()
    route_cache_store.init_db()
    activity_log.init_db()

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
