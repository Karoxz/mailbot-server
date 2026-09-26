# =============================================================
# poller.py — the standalone/web engine (Phase C 2026-09-05; rewritten
# for full desktop parity 2026-09-26)
#
# Runs as its OWN process — a separate systemd unit (mailbot-poller),
# never one of the 4 `mailbot-api` uvicorn workers (that would land in
# one arbitrary worker or run 4x redundantly). Every module below is
# imported directly as a library — no HTTP calls to the API server
# itself; this process IS part of the same server, reading/writing the
# same on-disk SQLite files the API workers share safely.
#
# Default-off. Nothing happens for a license unless it has
# standalone_mode_enabled=1, a working stored Gmail token and at least
# one allowed vehicle — re-checked every cycle, so turning it off in
# Settings takes effect within one poll interval, no restart.
#
# PARITY TARGET: the desktop app (client/main copy.py), which this file
# only ever READS — never modifies. _process_message() is a
# step-for-step port of the desktop's _process_email(): fetch -> custom
# label guard -> reply/freight detection -> thread-label guard (labeled
# thread => "Label / States" ping) -> body -> classify reply in the
# background -> strip quoted reply -> parse -> Telegram (identical
# buttons) -> _safe_mark_read. Pure helpers live in desktop_parity.py.
#
# DESKTOP/WEB MUTUAL EXCLUSION (no desktop change needed): the desktop's
# push poller hits /webhook/poll ~5x/second while it is running;
# main.py records that as licenses.desktop_poll_heartbeat and this
# engine yields a license's Gmail account for as long as it is fresh
# (see run_one_license_cycle). Web and desktop can share one Gmail
# account and "switch" automatically without ever racing on its
# read/unread state.
#
# Still deliberately NOT ported (documented, not needed for parity):
# Gmail Pub/Sub push + history API (3s list polling instead), the
# 20-worker pool (messages processed serially), the desktop's startup
# mass mark-read cleanup, and the "Mark All Read" tool.
# =============================================================

import os
import json
import time
import ssl
import logging
import threading
import traceback
from collections import OrderedDict
from urllib.parse import quote

import requests
from googleapiclient.errors import HttpError

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
import zip_geocode
import desktop_parity
import reply_handler
from gmail_client import GmailAuthError
from parser_core import parse_email_for_api, extract_text_from_full_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [poller] %(message)s")
logger = logging.getLogger("poller")


# ── Load .env manually — kept independent from main.py's own copy of
# this so this process doesn't pull in FastAPI/pydantic just for a few
# env vars. In production both systemd units already have
# EnvironmentFile=.env; this only matters running poller.py directly. ──
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

# Origin for the BID PC price+map link (bid_price.html is served from the
# same web/ StaticFiles mount as the dashboard). No default — if unset,
# BID PC falls back to recording a price-less bid + the bid text.
WEB_BASE_URL = os.environ.get("WEB_BASE_URL", "").rstrip("/")

# Desktop parity (2026-09-26): the desktop's own fast list poller runs
# every 3s over a 1h window, and its START does a one-time 2-day
# catch-up scan (up to 500 messages). Same numbers here.
POLL_INTERVAL_SECONDS = 3
FRESH_WINDOW = "1h"
INITIAL_SCAN_WINDOW = "2d"
INITIAL_SCAN_LIMIT = 500
MAX_RESULTS_PER_CYCLE = 50
DEFAULT_RADIUS_MILES = 300    # used only if a license never set one

_SERVICE_TTL = 1800           # desktop rebuilds its Gmail service every 30 min
_LABEL_TTL = 600
_SEEN_CAP = 2000              # desktop's processed_ids cap
_COOLDOWN_SEC = 300           # desktop's per-thread ping/classify cooldown
_RETRIES = 3                  # desktop: 3 retries, 2/4/8s backoff

_sleep = time.sleep           # indirection so tests can skip real waits


# =============================================================
# TELEGRAM
# =============================================================

def _tg_api(bot_token: str, method: str, payload: dict, timeout: int = 10):
    try:
        r = requests.post(f"https://api.telegram.org/bot{bot_token}/{method}",
                          json=payload, timeout=timeout)
        if not r.ok:
            logger.warning(f"Telegram {method} failed: {r.text[:200]}")
        return r
    except Exception as e:
        logger.warning(f"Telegram {method} exception: {e}")
        return None


def _tg_send(bot_token: str, chat_id, text: str, keyboard=None) -> bool:
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    r = _tg_api(bot_token, "sendMessage", payload, timeout=15)
    return bool(r is not None and r.ok)


def _tg_broadcast(bot_token: str, chat_ids: list, text: str, keyboard=None) -> int:
    """Send to every chat in parallel with a 6s join — same as the
    desktop's send_to_telegram. Returns how many sends succeeded."""
    results = []

    def _one(cid):
        results.append(_tg_send(bot_token, cid, text, keyboard))

    threads = [threading.Thread(target=_one, args=(cid,), daemon=True) for cid in chat_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=6)
    return sum(1 for ok in results if ok)


def _load_keyboard(order_id, route_url) -> list:
    """Identical to the desktop's load message buttons: row 1 is
    BID PC | BID PHONE | DRAFT (callbacks), row 2 is the ROUTE url."""
    rows = []
    if order_id:
        rows.append([
            {"text": "💵 BID PC",    "callback_data": f"bid:{order_id}"},
            {"text": "💵 BID PHONE", "callback_data": f"phone:{order_id}"},
            {"text": "📋 DRAFT",     "callback_data": f"text:{order_id}"},
        ])
    if route_url:
        rows.append([{"text": "🚩ROUTE🚩", "url": route_url}])
    return rows


def _gmail_url(order_id, broker_email, thread_id=None):
    """Real thread deep link when we have one, else a search link — same
    two-tier scheme as common.js's gmailSearchUrl()."""
    if thread_id:
        return f"https://mail.google.com/mail/u/0/#all/{thread_id}"
    q = str(order_id or "").strip()
    if broker_email:
        q += f" from:{broker_email}"
    return f"https://mail.google.com/mail/u/0/#search/{quote(q)}"


# ── Telegram button callbacks — port of the desktop's handle_bid_callbacks ──
#   bid   = BID PC    -> the web equivalent of the desktop's price dialog:
#                        reply with the price+map page link (plain text, so
#                        no "Open this link?" prompt). That page records the
#                        bid, copies the text and lands on the exact thread.
#   phone = BID PHONE -> send the bid text, create a REAL empty Gmail reply
#                        draft on the thread, record the bid, send an
#                        "Open Draft & Send" button.
#   text  = DRAFT     -> record the bid and send the bid text.
# With several matched trucks the desktop first asks which driver
# ("👤 Select driver for Order #X"); callback_data then carries ":idx".
_TG_METHOD_MAP = {
    "bid":   ("pc",    "💵 BID PC"),
    "phone": ("phone", "💵 BID PHONE"),
    "text":  ("draft", "📋 DRAFT"),
}
_DRIVER_EMOJI = {"bid": "🚛", "phone": "📱", "text": "📋"}
_DRIVER_PROMPT_SUFFIX = {"bid": "", "phone": " (Phone)", "text": " (Draft)"}


def _record_bid_and_build_text(license_key: str, order_id: str, method: str,
                                price: float = None, rate_per_mile: float = None,
                                truck: dict = None):
    """Thin wrapper around the shared bid_actions helper — adds the
    gmail_url this caller needs."""
    result = bid_actions.record_bid_and_build_text(license_key, order_id, method,
                                                    price, rate_per_mile, truck)
    if not result:
        return None
    result["gmail_url"] = _gmail_url(order_id, result["broker_email"], result["thread_id"])
    return result


def _answer_callback_query(bot_token: str, callback_query_id: str, text: str = None):
    body = {"callback_query_id": callback_query_id}
    if text:
        body["text"] = text
    _tg_api(bot_token, "answerCallbackQuery", body, timeout=10)


def _reply_chats(license_key: str, pressed_chat_id) -> list:
    """The desktop's callback replies go to ALL configured chats
    (send_to_telegram); fall back to the chat that pressed the button
    if none are configured."""
    s = license_db.get_standalone_settings(license_key) or {}
    chats = _parse_chat_ids(s.get("chat_ids", ""))
    return chats or ([pressed_chat_id] if pressed_chat_id else [])


def _driver_buttons(action: str, order_id: str, all_trucks: list) -> list:
    rows = []
    for i, t in enumerate(all_trucks):
        name = t.get("driver_name", f"Driver {i + 1}")
        dh = t.get("google_deadhead", "?")
        rows.append([{"text": f"{_DRIVER_EMOJI[action]} {name}  —  {dh} mi out",
                      "callback_data": f"{action}:{order_id}:{i}"}])
    return rows


def _bid_pc(bot_token, chats, license_key, order_id, load, truck, idx):
    if WEB_BASE_URL:
        tok = map_token.make_bid_token(license_key, order_id)
        url = f"{WEB_BASE_URL}/app/bid_price.html?t={tok}"
        if truck is not None and idx is not None:
            url += f"&truck={idx}"
        who = f" — {truck.get('driver_name')}" if truck else ""
        _tg_broadcast(bot_token, chats,
                      f"💵 BID PC — Order #{order_id}{who}\nEnter your price:\n{url}")
        return
    # No public web origin configured: fall back to a price-less bid.
    result = _record_bid_and_build_text(license_key, order_id, "pc", truck=truck)
    if result:
        _tg_broadcast(bot_token, chats, f"💵 BID PC — Order #{order_id}\n\n{result['bid_text']}",
                      [[{"text": "✉️ Find in Gmail", "url": result["gmail_url"]}]])


def _bid_phone(bot_token, chats, license_key, order_id, load, truck):
    body = bid_actions.build_bid_text(load, order_id, license_key, truck)
    try:
        _tg_broadcast(bot_token, chats, body)
        msg_id = (load.get("original_msg_full") or {}).get("id") or ""
        if not msg_id:
            raise RuntimeError("original message unavailable (this load didn't come from the web engine)")
        service = gmail_client.build_service(license_key)
        headers = gmail_client.get_message_headers(service, msg_id)
        draft = gmail_client.create_reply_draft(service, headers)
        draft_id = draft.get("id", "")
        bid_actions.record_bid(load, order_id, license_key, "phone", truck)
        activity_log.log_event(license_key, "bid_recorded", f"Recorded PHONE bid on order #{order_id}")
        if draft_id:
            head = (f"✅ Draft created for {truck['driver_name']} — Order #{order_id}\n"
                    if truck else f"✅ Draft created for Order #{order_id}\n")
            _tg_broadcast(bot_token, chats,
                          head + "Tap below → opens Gmail draft ready to send:",
                          [[{"text": "📨 Open Draft & Send",
                             "url": f"https://mail.google.com/mail/u/0/#drafts/{draft_id}"}]])
        else:
            _tg_broadcast(bot_token, chats,
                          f"✅ Draft created for Order #{order_id} — open your Drafts:",
                          [[{"text": "📂 Open Gmail Drafts",
                             "url": "https://mail.google.com/mail/u/0/#drafts"}]])
    except Exception as e:
        logger.error(f"[{license_key}] BID PHONE failed: {e}")
        _tg_broadcast(bot_token, chats, f"❌ Failed to create draft: {e}")


def _bid_text(bot_token, chats, license_key, order_id, load, truck):
    body = bid_actions.build_bid_text(load, order_id, license_key, truck)
    if not body:
        return
    bid_actions.record_bid(load, order_id, license_key, "draft", truck)
    activity_log.log_event(license_key, "bid_recorded", f"Recorded DRAFT bid on order #{order_id}")
    head = f"📋 ORDER #{order_id} — {truck['driver_name']}:" if truck else f"📋 ORDER #{order_id}:"
    _tg_broadcast(bot_token, chats, f"{head}\n\n{body}")


def _handle_callback_query(bot_token: str, cq: dict, license_key: str):
    callback_id = cq.get("id")
    pressed_chat = (cq.get("message") or {}).get("chat", {}).get("id")
    parts = (cq.get("data") or "").split(":")
    action = parts[0]
    if action not in _TG_METHOD_MAP or len(parts) < 2 or not parts[1]:
        _answer_callback_query(bot_token, callback_id)
        return
    order_id = parts[1]
    idx = parts[2] if len(parts) > 2 else None

    load = load_store.get_load(license_key, order_id)
    if not load:
        _answer_callback_query(bot_token, callback_id,
                               text="Order not found in the current live feed.")
        return
    _answer_callback_query(bot_token, callback_id)      # desktop answers with an empty toast

    chats = _reply_chats(license_key, pressed_chat)
    all_trucks = load.get("all_trucks") or []
    truck = None
    if idx is not None:
        truck = bid_actions.get_truck(load, idx)
        if truck is None:
            return
    elif len(all_trucks) > 1:
        # Several matched trucks: ask which driver first (desktop behavior).
        _tg_broadcast(bot_token, chats,
                      f"👤 Select driver for Order #{order_id}{_DRIVER_PROMPT_SUFFIX[action]}:",
                      _driver_buttons(action, order_id, all_trucks))
        return

    if action == "bid":
        _bid_pc(bot_token, chats, license_key, order_id, load, truck, idx)
    elif action == "phone":
        _bid_phone(bot_token, chats, license_key, order_id, load, truck)
    else:
        _bid_text(bot_token, chats, license_key, order_id, load, truck)


def _telegram_callback_loop(bot_token: str, license_key: str):
    """One thread per distinct bot token, long-polling getUpdates — same
    idea as the desktop's get_telegram_updates(). A bot_token is assumed
    one-per-license in practice (each account's own private bot)."""
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


# =============================================================
# PER-LICENSE CACHES / DEDUP (all in-memory, like the desktop's)
# =============================================================

_svc_cache = {}          # license -> (service, built_at)
_label_cache = {}        # license -> (label_map, built_at)
_seen = {}               # license -> OrderedDict of processed message ids
_last_labeled_notify = {}   # (license, thread) -> ts   (5-min cooldown)
_last_classify = {}         # (license, thread) -> ts   (5-min cooldown)
_cooldown_lock = threading.Lock()
_yielding = {}           # license -> bool (currently paused for the desktop?)


def _get_service(license_key: str):
    ent = _svc_cache.get(license_key)
    if ent and time.time() - ent[1] < _SERVICE_TTL:
        return ent[0]
    service = gmail_client.build_service(license_key)
    _svc_cache[license_key] = (service, time.time())
    _label_cache.pop(license_key, None)
    return service


def _get_label_map(license_key: str, service) -> dict:
    ent = _label_cache.get(license_key)
    if ent and time.time() - ent[1] < _LABEL_TTL:
        return ent[0]
    m = gmail_client.get_label_map(service)
    _label_cache[license_key] = (m, time.time())
    return m


def _drop_service(license_key: str):
    _svc_cache.pop(license_key, None)
    _label_cache.pop(license_key, None)


def _is_seen(license_key: str, msg_id: str) -> bool:
    return msg_id in _seen.get(license_key, ())


def _mark_seen(license_key: str, msg_id: str):
    d = _seen.setdefault(license_key, OrderedDict())
    d[msg_id] = 1
    while len(d) > _SEEN_CAP:
        d.popitem(last=False)


def _cooldown_ok(store: dict, key) -> bool:
    now = time.time()
    with _cooldown_lock:
        if now - store.get(key, 0) < _COOLDOWN_SEC:
            return False
        store[key] = now
        return True


# =============================================================
# RETRY HELPERS — desktop: _is_conn_reset / _is_rate_limited
# =============================================================

def _is_rate_limited(e: Exception) -> bool:
    if isinstance(e, HttpError):
        status = getattr(e.resp, "status", None)
        text = str(e).lower()
        return status == 429 or (status == 403 and "ratelimit" in text.replace(" ", ""))
    return False


def _is_conn_reset(e: Exception) -> bool:
    if isinstance(e, (ConnectionResetError, ConnectionAbortedError, ssl.SSLError, TimeoutError)):
        return True
    s = str(e)
    return "10054" in s or "Connection reset" in s or "10053" in s


# =============================================================
# PER-MESSAGE PIPELINE — port of the desktop's _process_email()
# =============================================================

def _header(full: dict, name: str) -> str:
    for h in full.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _can_send(ctx: dict) -> bool:
    return bool(ctx["telegram_enabled"] and ctx["bot_token"] and ctx["chat_ids"])


def _notify_labeled_thread(ctx: dict, label_names: list, subject: str, thread_id: str) -> bool:
    """Desktop's _notify_labeled_thread: a Label / States ping with a
    "REPLY BID" button that opens the thread — the ONLY notification a
    broker reply produces. 5-minute per-thread cooldown."""
    lk = ctx["license_key"]
    if not _cooldown_ok(_last_labeled_notify, (lk, thread_id)):
        return False
    text = desktop_parity.labeled_thread_message(label_names, subject)
    logger.info(f"[{lk}] labeled-thread ping labels={label_names} subject={subject[:60]!r}")
    if not _can_send(ctx):
        return False
    sent = _tg_broadcast(ctx["bot_token"], ctx["chat_ids"], text,
                         [[{"text": "💵 REPLY BID", "url": _gmail_url(None, None, thread_id)}]])
    if sent:
        activity_log.log_event(lk, "labeled_ping",
                               f"Reply ping: {', '.join(label_names)} — {subject[:60]}")
    return bool(sent)


def _classify_in_background(license_key: str, thread_id: str, subject: str, body: str):
    """Desktop's _run_classify_and_notify: classify the broker's reply and
    record the outcome server-side, on its own thread so it never delays
    the poll loop; 5-min per-thread cooldown. Silent on Telegram."""
    if not _cooldown_ok(_last_classify, (license_key, thread_id)):
        return None

    def _run():
        try:
            res = reply_handler.classify_and_record(license_key, thread_id, subject, body)
            if res.get("matched") and res.get("updated"):
                order = (res.get("order") or {}).get("order_id", "")
                status = (res.get("classification") or {}).get("status", "")
                activity_log.log_event(license_key, "bid_outcome",
                                       f"Broker reply on order #{order}: {status}")
        except Exception:
            logger.error(f"[{license_key}] classify failed:\n{traceback.format_exc()}")

    t = threading.Thread(target=_run, daemon=True, name="classify")
    t.start()
    return t


def _safe_mark_read(ctx: dict, service, msg_id: str, thread_id: str, label_map: dict, subject: str):
    """Desktop's _safe_mark_read: a message in a labeled thread is left
    exactly as-is (with the ping as the visibility guarantee); anything
    else is marked read — read state is the dedup mechanism."""
    if thread_id:
        names, tsubject = gmail_client.get_thread_info(service, thread_id, label_map)
        if names:
            _notify_labeled_thread(ctx, names, tsubject or subject, thread_id)
            return
    gmail_client.mark_as_read(service, msg_id)


def _deliver_load(ctx: dict, result: dict, thread_id: str) -> str:
    lk = ctx["license_key"]
    load_data = result.get("load_data") or {}
    order_id = result.get("order_id")
    if not ctx["telegram_enabled"]:
        logger.info(f"[{lk}] #{order_id} matched — Telegram is OFF, not sent")
        activity_log.log_event(lk, "load_matched", f"Load #{order_id} matched (Telegram is OFF — not sent)")
        return "telegram_off"
    if not (ctx["bot_token"] and ctx["chat_ids"]):
        logger.info(f"[{lk}] #{order_id} matched — no bot token / chat IDs configured")
        activity_log.log_event(lk, "load_matched", f"Load #{order_id} matched (no bot token/chat ID set — not sent)")
        return "no_recipient"
    sent = _tg_broadcast(ctx["bot_token"], ctx["chat_ids"], result["formatted"],
                         _load_keyboard(order_id, load_data.get("route_url")))
    if not sent:
        logger.warning(f"[{lk}] #{order_id} Telegram send FAILED")
        activity_log.log_event(lk, "send_failed", f"Load #{order_id} matched but the Telegram send failed")
        return "send_failed"
    logger.info(f"[{lk}] matched load #{order_id}")
    activity_log.log_event(lk, "load_matched", f"Standalone poller matched load #{order_id}")
    return "sent"


def _process_message(ctx: dict, service, label_map: dict, msg_id: str) -> str:
    """Step-for-step port of the desktop's _process_email(). Returns a
    short outcome string (used by tests and logging)."""
    lk = ctx["license_key"]
    try:
        full = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    except HttpError as e:
        if getattr(e.resp, "status", None) == 404:
            return "gone"
        raise

    # 3. custom-label guard — silent skip
    if gmail_client.has_custom_labels(full.get("labelIds", [])):
        return "labeled"

    # 4. reply / freight detection
    subject = _header(full, "subject")
    thread_id = full.get("threadId", "")
    is_freight = desktop_parity.is_freight_subject(subject)

    # 5. thread-label guard — a non-freight message in a labeled thread
    # (typically a broker reply) never reaches the parser; it gets the
    # label ping instead and is left as-is.
    if not is_freight and thread_id:
        names, tsubject = gmail_client.get_thread_info(service, thread_id, label_map)
        if names:
            _notify_labeled_thread(ctx, names, tsubject or subject, thread_id)
            return "labeled_thread"

    # 6. body, 7. classify (background), 8. strip quoted reply
    body = extract_text_from_full_message(full)
    if thread_id:
        _classify_in_background(lk, thread_id, subject, body)
    parse_body = desktop_parity.strip_quoted_reply(body)

    # 9. parse
    result = parse_email_for_api({
        "license_key":      lk,
        "email_body":       parse_body,
        "internal_date_ms": int(full.get("internalDate", "0")),
        "allowed_vehicles": ctx["allowed_vehicles"],
        "max_radius_miles": ctx["radius_miles"],
        "trucks":           fleet_store.list_trucks(lk),
        "bid_template":     None,      # falls back to load_store's per-license default
        "thread_id":        thread_id,  # real Gmail thread: lets "Find in Gmail"/BID PC
        "message_id":       msg_id,     # land on the exact thread, not a search
    })

    # 10-11. send (identical buttons) or log the skip reason
    if result.get("success") and result.get("formatted"):
        outcome = _deliver_load(ctx, result, thread_id)
    else:
        reason = (result.get("message") or "").strip()
        logger.info(f"[{lk}] SKIPPED #{result.get('order_id') or '?'} — {reason[:120]}")
        activity_log.log_event(lk, "load_skipped",
                               f"Skipped #{result.get('order_id') or '?'} — {reason[:100]}")
        outcome = "skipped"

    # 13. mark read (unless it lives in a labeled thread)
    _safe_mark_read(ctx, service, msg_id, thread_id, label_map, subject)
    return outcome


def _process_with_retry(ctx: dict, service, label_map: dict, msg_id: str) -> str:
    """Desktop retry policy: a connection reset or Gmail 429/rate-limit
    is retried up to 3 times with 2/4/8s backoff; anything else (or the
    last failure) is logged and the message is considered handled so a
    bad message can't be re-fetched every 3s forever."""
    lk = ctx["license_key"]
    outcome = "error"
    for attempt in range(_RETRIES + 1):
        try:
            outcome = _process_message(ctx, service, label_map, msg_id)
            break
        except Exception as e:
            if (_is_rate_limited(e) or _is_conn_reset(e)) and attempt < _RETRIES:
                wait = 2 ** (attempt + 1)
                logger.warning(f"[{lk}] transient Gmail error on {msg_id[-8:]} "
                               f"(attempt {attempt + 1}); retrying in {wait}s: {e}")
                _sleep(wait)
                continue
            logger.error(f"[{lk}] error processing message {msg_id}:\n{traceback.format_exc()}")
            activity_log.log_event(lk, "poller_error", f"Error processing a message: {str(e)[:100]}")
            outcome = "error"
            break
    _mark_seen(lk, msg_id)
    return outcome


# =============================================================
# CYCLE
# =============================================================

def _build_query(allowed_vehicles: list, window: str = None) -> str:
    veh_terms = " OR ".join(f'"{v}"' for v in allowed_vehicles)
    return f'is:unread newer_than:{window or FRESH_WINDOW} ({veh_terms})'


def _list_message_ids(service, query: str, limit: int) -> list:
    ids, token = [], None
    while len(ids) < limit:
        kw = {"userId": "me", "q": query, "maxResults": min(500, limit - len(ids))}
        if token:
            kw["pageToken"] = token
        resp = service.users().messages().list(**kw).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids


def _note_yield(license_key: str, yielding: bool):
    was = _yielding.get(license_key, False)
    if yielding == was:
        return
    _yielding[license_key] = yielding
    if yielding:
        logger.info(f"[{license_key}] paused: desktop app is actively polling this license")
        activity_log.log_event(license_key, "web_paused", "Web bot paused — the desktop app is running")
    else:
        logger.info(f"[{license_key}] resumed: desktop app is no longer polling")
        activity_log.log_event(license_key, "web_resumed", "Web bot resumed — the desktop app stopped")


def _initial_scan(ctx: dict, service, label_map: dict):
    """The desktop's START: a 'Watching' Telegram message plus a one-time
    catch-up scan of the last 2 days of unread freight mail. Runs once
    per ENABLE (the flag is reset by /api/web/standalone/enable), never
    on a mere poller restart."""
    lk = ctx["license_key"]
    ids = _list_message_ids(service, _build_query(ctx["allowed_vehicles"], INITIAL_SCAN_WINDOW),
                            INITIAL_SCAN_LIMIT)
    license_db.set_standalone_initial_scan_done(lk, True)
    if _can_send(ctx):
        _tg_broadcast(ctx["bot_token"], ctx["chat_ids"],
                      f"✅ Watching: {', '.join(ctx['allowed_vehicles'])}\n"
                      f"Window: {INITIAL_SCAN_WINDOW}\n Delivery states: ALL")
    activity_log.log_event(lk, "standalone_scan",
                           f"Catch-up scan started: {len(ids)} unread message(s) in the last {INITIAL_SCAN_WINDOW}")
    for mid in ids:
        if not _is_seen(lk, mid):
            _process_with_retry(ctx, service, label_map, mid)


def run_one_license_cycle(license_key: str):
    settings = license_db.get_standalone_settings(license_key)
    if not settings or not settings["standalone_mode_enabled"]:
        return
    allowed_vehicles = [v.strip().upper() for v in settings["allowed_vehicles"].split(",") if v.strip()]
    if not allowed_vehicles:
        logger.info(f"[{license_key}] skipped: no allowed vehicles configured")
        return

    # Desktop/web mutual exclusion — skip the ENTIRE cycle (not even a
    # Gmail call) while a desktop is polling this license; see header.
    if license_db.is_desktop_recently_active(license_key):
        _note_yield(license_key, True)
        return
    _note_yield(license_key, False)

    try:
        service = _get_service(license_key)
    except GmailAuthError as e:
        logger.warning(f"[{license_key}] skipped: {e}")
        return

    ctx = {
        "license_key":      license_key,
        "allowed_vehicles": allowed_vehicles,
        "radius_miles":     settings["max_radius_miles"] or DEFAULT_RADIUS_MILES,
        "chat_ids":         _parse_chat_ids(settings["chat_ids"]),
        "bot_token":        settings["bot_token"],
        "telegram_enabled": license_db.get_telegram_enabled(license_key),
    }
    try:
        label_map = _get_label_map(license_key, service)
        if not license_db.get_standalone_initial_scan_done(license_key):
            _initial_scan(ctx, service, label_map)
        for mid in _list_message_ids(service, _build_query(allowed_vehicles), MAX_RESULTS_PER_CYCLE):
            if not _is_seen(license_key, mid):
                _process_with_retry(ctx, service, label_map, mid)
    except Exception:
        _drop_service(license_key)   # rebuild credentials/service next cycle
        raise


def main():
    logger.info(f"poller starting — poll interval {POLL_INTERVAL_SECONDS}s")
    license_db.init_db()
    # zip_geocode warmed up before fleet_store — its truck-location
    # backfill needs the offline geocoder already loaded.
    zip_geocode.warmup()
    fleet_store.init_db()
    load_store.init_db()
    gmail_store.init_db()
    # process_bid_email() (via parse_email_for_api) reads bid_history and
    # the geo/route cache tables — this process must create them itself
    # rather than assume mailbot-api started first.
    bid_history.init_db()
    bid_history.init_processed_threads_table()
    route_cache_store.init_db()
    activity_log.init_db()

    while True:
        processed = 0
        last_error = None
        try:
            _ensure_callback_listeners()
            for lic in license_db.list_standalone_enabled_licenses():
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
