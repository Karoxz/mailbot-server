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
import driver_bot_web
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

# A dead/revoked Gmail token (GmailAuthError) isn't something retrying
# 3s later ever fixes — it needs real human action (reconnect via the
# web dashboard's OAuth flow). Found live 2026-10-03: a license's
# token was revoked and the poller hammered Google's token endpoint
# with the same doomed refresh attempt every single cycle for over two
# days straight (tens of thousands of identical log lines), which also
# buried the one signal worth seeing under enough noise that it went
# unnoticed. Back off hard once a license is known broken this way —
# AUTH_FAIL_COOLDOWN_SECONDS between attempts — while every other
# license keeps polling at the normal cadence untouched.
AUTH_FAIL_COOLDOWN_SECONDS = 900  # 15 min

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
    """Send to every chat in parallel. The desktop's send_to_telegram
    joins with timeout=6 too, but its request timeout is 5s, so a join
    there can never fire before the request itself is done. _tg_send's
    request timeout is 15s, so the join here must be >= that — found
    live 2026-10-03: with a 6s join, a Telegram response landing between
    6-15s got misreported as a failed send (and the load was then never
    retried — _process_with_retry marks it seen regardless of outcome),
    even though the message had actually gone through or was about to."""
    results = []

    def _one(cid):
        kb = keyboard(cid) if callable(keyboard) else keyboard   # keyboards may differ per chat
        results.append(_tg_send(bot_token, cid, text, kb))

    threads = [threading.Thread(target=_one, args=(cid,), daemon=True) for cid in chat_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return sum(1 for ok in results if ok)


def _web_app_ok(chat_id) -> bool:
    """Telegram only allows web_app buttons in PRIVATE chats (positive
    ids); groups/channels have negative ids and get the link fallback."""
    return isinstance(chat_id, int) and chat_id > 0


def _bid_pc_url(license_key: str, order_id: str, truck_idx=None, method: str = "pc"):
    """Signed link to the price+map page (None when no public web origin
    is configured). 48h token, same as the price-page links always were.
    method="phone" (client, 2026-10-06: "bid phone should show the map
    with the bid amount, just like the pc version") reuses the exact
    same page — bid_price.html branches its post-submit behavior on it
    (create a Gmail draft instead of just showing the thread link)."""
    if not WEB_BASE_URL:
        return None
    url = f"{WEB_BASE_URL}/app/bid_price.html?t={map_token.make_bid_token(license_key, order_id)}"
    if truck_idx is not None:
        url += f"&truck={truck_idx}"
    if method != "pc":
        url += f"&method={method}"
    return url


def _load_keyboard(order_id, route_url, bid_pc_url=None) -> list:
    """Identical layout to the desktop's load message buttons: row 1 is
    BID PC | BID PHONE | DRAFT, row 2 is the ROUTE url. bid_pc_url (set
    for single-truck loads in private chats) makes BID PC a web_app
    button: one tap opens the price page straight away inside Telegram,
    no "Open this link?" prompt and no separate link message."""
    rows = []
    if order_id:
        bid_pc = ({"text": "💵 BID PC", "web_app": {"url": bid_pc_url}} if bid_pc_url
                  else {"text": "💵 BID PC", "callback_data": f"bid:{order_id}"})
        rows.append([
            bid_pc,
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


def _driver_keyboard(action: str, order_id: str, trucks: list, license_key: str):
    """Per-chat keyboard for the "Select driver" prompt. `trucks` are the
    Maps-VERIFIED entries (bid_actions.verified_trucks) so the miles match
    the notification's "Out Miles". For BID PC in a private chat each
    driver button opens the price page directly (web_app)."""
    def build(chat_id):
        rows = []
        for i, t in enumerate(trucks):
            name = t.get("driver_name", f"Driver {i + 1}")
            text = f"{_DRIVER_EMOJI[action]} {name}  —  {t.get('google_deadhead', '?')} mi out"
            if action in ("bid", "phone") and _web_app_ok(chat_id) and WEB_BASE_URL:
                method = "phone" if action == "phone" else "pc"
                rows.append([{"text": text, "web_app": {"url": _bid_pc_url(license_key, order_id, i, method)}}])
            else:
                rows.append([{"text": text, "callback_data": f"{action}:{order_id}:{i}"}])
        return rows
    return build


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


def _bid_phone(bot_token, chats, license_key, order_id, load, truck, idx=None):
    """Client, 2026-10-06: "bid phone should show the map with the bid
    amount, just like the pc version" — now sends the same price+map
    page link BID PC does (method=phone), instead of immediately
    building price-less bid text. bid_price.html's submit still ends in
    a real Gmail reply draft for phone (see /api/web/bid_price/submit),
    just with a confirmed price in it now, same as PC gets."""
    if WEB_BASE_URL:
        url = _bid_pc_url(license_key, order_id, idx, method="phone")
        who = f" — {truck.get('driver_name')}" if truck else ""
        _tg_broadcast(bot_token, chats,
                      f"📱 BID PHONE — Order #{order_id}{who}\nEnter your price:\n{url}")
        return
    # No public web origin configured: fall back to the old price-less
    # behavior — bid text + a real Gmail reply draft, no price entry.
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


_recent_bid_actions = {}  # (license_key, order_id, action, idx) -> last-handled time.monotonic()
_RECENT_ACTION_COOLDOWN_S = 8


def _is_duplicate_bid_action(license_key: str, order_id: str, action: str, idx) -> bool:
    """True if this exact (license, order, action, driver) was just
    handled — client, 2026-10-01: "on telegram when client pressed bid
    phone once it created draft 3 times". _bid_phone/_bid_pc/_bid_text
    each only ever call their one-shot side effect (create one draft,
    record one bid) once per invocation — there's no retry loop in any
    of them — so 3 drafts from 1 tap means _handle_callback_query itself
    ran 3 times for what was really one action: Telegram inline buttons
    never disable themselves while a slow call (a real Gmail API round
    trip here) is in flight, so an impatient second/third tap — or any
    other source of the same callback being redelivered — reaches this
    function as what looks like 3 separate, individually-legitimate
    events. This collapses repeats within a short window into one real
    action regardless of why the repeat happened, without needing to
    pin down which of those causes actually fired."""
    key = (license_key, order_id, action, idx)
    now = time.monotonic()
    last = _recent_bid_actions.get(key)
    _recent_bid_actions[key] = now
    if len(_recent_bid_actions) > 2000:  # cheap unbounded-growth guard
        cutoff = now - 120
        for k, v in list(_recent_bid_actions.items()):
            if v < cutoff:
                del _recent_bid_actions[k]
    return last is not None and (now - last) < _RECENT_ACTION_COOLDOWN_S


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

    if _is_duplicate_bid_action(license_key, order_id, action, idx):
        _answer_callback_query(bot_token, callback_id, text="Already on it — one moment.")
        return

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
                      _driver_keyboard(action, order_id, bid_actions.verified_trucks(load), license_key))
        return

    if action == "bid":
        _bid_pc(bot_token, chats, license_key, order_id, load, truck, idx)
    elif action == "phone":
        _bid_phone(bot_token, chats, license_key, order_id, load, truck, idx)
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
        dtoken = settings and settings.get("driver_bot_token")
        if dtoken and dtoken not in _callback_threads:
            t = threading.Thread(target=driver_bot_web.run_loop, args=(dtoken, lic),
                                 daemon=True, name=f"tg-driver-{dtoken[-6:]}")
            t.start()
            _callback_threads[dtoken] = t


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
_last_auth_fail = {}        # license -> (ts, gmail_credentials.updated_at at failure time)


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


_STATUS_LABEL = {"won": "✅ Won", "lost": "❌ Lost", "countered": "💰 Countered"}


def _classify_in_background(ctx: dict, thread_id: str, subject: str, body: str):
    """Desktop's _run_classify_and_notify: classify the broker's reply and
    record the outcome server-side, on its own thread so it never delays
    the poll loop; 5-min per-thread cooldown.

    2026-10-01 (client: "bid reply didnt came in, there should also be
    bid replys in the web version") — the desktop's own equivalent is
    deliberately silent on Telegram (2026-09-17 client decision: the
    only reply notification desktop sends is the labeled-thread ping,
    which needs the dispatcher to have manually applied a Gmail label
    to the thread). That's the same reason this was silent here too —
    but a manually-labeled thread turns out to be the uncommon case in
    practice, which left most real broker replies invisible on the web
    side with no ping of any kind. This sends a notification on every
    CONFIDENT classification (won/lost/countered) regardless of any
    Gmail label, independent of (and in addition to) the labeled-thread
    ping — the two can both fire for the same reply if it happens to be
    in a labeled thread, which is fine, not a regression of that path."""
    license_key = ctx["license_key"]
    if not _cooldown_ok(_last_classify, (license_key, thread_id)):
        return None

    def _run():
        try:
            res = reply_handler.classify_and_record(license_key, thread_id, subject, body)
            if res.get("matched") and res.get("updated"):
                order_ctx = res.get("order") or {}
                order = order_ctx.get("order_id", "")
                cls = res.get("classification") or {}
                status = cls.get("status", "")
                activity_log.log_event(license_key, "bid_outcome",
                                       f"Broker reply on order #{order}: {status}")
                if _can_send(ctx):
                    label = _STATUS_LABEL.get(status, status.capitalize() or "Reply")
                    route = f"{order_ctx.get('pickup_loc', '')} → {order_ctx.get('delivery_loc', '')}".strip(" →")
                    lines = [f"💬 Broker reply — Order #{order}" if order else "💬 Broker reply",
                             label]
                    if route and route != "→":
                        lines.append(route)
                    if order_ctx.get("broker_name"):
                        lines.append(order_ctx["broker_name"])
                    _tg_broadcast(ctx["bot_token"], ctx["chat_ids"], "\n".join(lines),
                                 [[{"text": "✉️ Open thread", "url": _gmail_url(order, None, thread_id)}]])
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


def _notify_drivers_async(ctx: dict, result: dict):
    """Desktop's driver-bot notify (after the dispatcher send, independent
    of whether that send succeeded): every eligible driver with a Telegram
    chat ID gets a card. The formatted message is stored on the persisted
    load so a driver's BID tap still works after a poller restart."""
    token = ctx.get("driver_bot_token")
    order_id = result.get("order_id")
    if not token or not order_id:
        return
    lk = ctx["license_key"]
    load_data = result.get("load_data") or {}
    try:
        stored = load_store.get_load(lk, order_id)
        if stored is not None:
            stored["formatted_message"] = result.get("formatted", "")
            load_store.put_load(lk, order_id, stored)
    except Exception as e:
        logger.warning(f"[{lk}] could not persist formatted message for driver bot: {e}")
    threading.Thread(target=driver_bot_web.notify_drivers,
                     args=(lk, token, order_id, load_data, result.get("formatted", "")),
                     daemon=True, name="driver-notify").start()


def _deliver_load(ctx: dict, result: dict, thread_id: str) -> str:
    outcome = _deliver_to_dispatcher(ctx, result, thread_id)
    _notify_drivers_async(ctx, result)
    return outcome


def _deliver_to_dispatcher(ctx: dict, result: dict, thread_id: str) -> str:
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
    route_url = load_data.get("route_url")
    pc_url = (_bid_pc_url(lk, order_id)
              if order_id and len(load_data.get("all_trucks") or []) <= 1 else None)
    sent = _tg_broadcast(ctx["bot_token"], ctx["chat_ids"], result["formatted"],
                         lambda cid: _load_keyboard(order_id, route_url,
                                                    pc_url if _web_app_ok(cid) else None))
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
        _classify_in_background(ctx, thread_id, subject, body)
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

def _build_query(window: str = None) -> str:
    """Used to also OR in a Gmail-side vehicle-keyword clause as a
    pre-filter. Dropped 2026-10-05: Gmail's phrase search doesn't
    substring/stem-match the way allowed_vehicles matching does
    downstream (process_bid_email's `v in vehicle_required.upper()`
    check) — a one-letter typo in a license's allowed-vehicle list
    ("SMALL STRAIGH") silently excluded every real SMALL STRAIGHT load
    from ever being fetched at all, with no log trace of it happening.
    Every unread message is now fetched and goes through the existing,
    already-detailed parse/match/log path instead (non-freight mail is
    still screened out there via is_freight_subject(), independent of
    vehicle keywords), so a load being excluded is always visible in
    the activity log as a real skip reason."""
    return f'is:unread newer_than:{window or FRESH_WINDOW}'


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


# ── Automatic thread learning (desktop parity) ────────────────────────
# The desktop, while running, does a light 3-day thread-learning pass
# every 15 minutes (reads the dispatcher's own quoted rates out of
# "bid"-labeled threads and confirms wins via the "RC" label) whenever
# the server-side thread_learning_enabled toggle is on. Same here, on a
# background thread so it never delays the 3s intake loop, one at a time
# per license. First pass is 15 minutes after the license is first seen
# (like the desktop's first timer tick). Failures are logged to the
# activity feed instead of dying silently in a log nobody reads.
LEARNING_INTERVAL_SEC = 15 * 60
LEARNING_DAYS_BACK = 3
_learning_last = {}      # license -> ts of last pass start (or first sighting)
_learning_thread = {}    # license -> running Thread


def _maybe_run_thread_learning(license_key: str):
    if not license_db.get_thread_learning_enabled(license_key):
        return None
    now = time.time()
    if license_key not in _learning_last:
        _learning_last[license_key] = now
        return None
    if now - _learning_last[license_key] < LEARNING_INTERVAL_SEC:
        return None
    running = _learning_thread.get(license_key)
    if running is not None and running.is_alive():
        return None
    _learning_last[license_key] = now

    def _run():
        try:
            import thread_backfill
            res = thread_backfill.run_backfill(license_key, days_back=LEARNING_DAYS_BACK)
            if res.get("processed") or res.get("errors"):
                activity_log.log_event(
                    license_key, "thread_learning",
                    f"Thread learning pass: {res.get('processed', 0)} processed, "
                    f"{res.get('skipped', 0)} skipped, {res.get('errors', 0)} errors")
        except Exception as e:
            logger.error(f"[{license_key}] thread learning pass failed:\n{traceback.format_exc()}")
            activity_log.log_event(license_key, "poller_error",
                                   f"Thread learning pass failed: {str(e)[:100]}")

    t = threading.Thread(target=_run, daemon=True, name="thread-learning")
    _learning_thread[license_key] = t
    t.start()
    return t


def _initial_scan(ctx: dict, service, label_map: dict):
    """The desktop's START: a 'Watching' Telegram message plus a one-time
    catch-up scan of the last 2 days of unread freight mail. Runs once
    per ENABLE (the flag is reset by /api/web/standalone/enable), never
    on a mere poller restart."""
    lk = ctx["license_key"]
    ids = _list_message_ids(service, _build_query(INITIAL_SCAN_WINDOW),
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

    # Known-dead token backoff — see AUTH_FAIL_COOLDOWN_SECONDS above.
    # Skipped entirely (no Gmail/network call at all) until the cooldown
    # elapses — UNLESS gmail_credentials.updated_at has moved since the
    # failure was recorded, which means an out-of-band reconnect (the
    # web dashboard's OAuth flow, a separate process) already happened;
    # in that case retry immediately instead of waiting out the timer.
    # Found live 2026-10-03: a user reconnected via the web mid-cooldown
    # and the poller sat idle for minutes despite the fix being in place.
    with _cooldown_lock:
        last_fail = _last_auth_fail.get(license_key)
    if last_fail is not None:
        fail_ts, updated_at_at_failure = last_fail
        current_updated_at = gmail_store.get_status(license_key).get("updated_at")
        if current_updated_at == updated_at_at_failure and time.time() - fail_ts < AUTH_FAIL_COOLDOWN_SECONDS:
            return

    try:
        service = _get_service(license_key)
    except GmailAuthError as e:
        logger.warning(f"[{license_key}] skipped: {e}")
        updated_at_now = gmail_store.get_status(license_key).get("updated_at")
        with _cooldown_lock:
            _last_auth_fail[license_key] = (time.time(), updated_at_now)
        return

    # A cycle reached this point without a GmailAuthError — clear any
    # earlier failure record so a stale cooldown can never linger.
    with _cooldown_lock:
        _last_auth_fail.pop(license_key, None)

    _maybe_run_thread_learning(license_key)

    ctx = {
        "license_key":      license_key,
        "allowed_vehicles": allowed_vehicles,
        "radius_miles":     settings["max_radius_miles"] or DEFAULT_RADIUS_MILES,
        "chat_ids":         _parse_chat_ids(settings["chat_ids"]),
        "bot_token":        settings["bot_token"],
        "driver_bot_token": settings.get("driver_bot_token", ""),
        "telegram_enabled": license_db.get_telegram_enabled(license_key),
    }
    try:
        label_map = _get_label_map(license_key, service)
        if not license_db.get_standalone_initial_scan_done(license_key):
            _initial_scan(ctx, service, label_map)
        for mid in _list_message_ids(service, _build_query(), MAX_RESULTS_PER_CYCLE):
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
