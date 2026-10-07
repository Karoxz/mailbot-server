# =============================================================
# driver_bot_web.py — server-side, added 2026-09-26
#
# The web version of the desktop's driver bot (client/driver_bot.py,
# read-only reference — the desktop is untouched). Same behavior:
#   * when a load matches, every driver whose truck has a Telegram chat ID
#     (and whose vehicle type fits the load) gets a card with "💰 BID" and
#     "🚩 ROUTE" buttons;
#   * tapping BID sends a ForceReply "Type your rate" prompt;
#   * the driver's numeric reply is forwarded to the dispatcher chat(s)
#     with the SAME BID PC / BID PHONE / DRAFT buttons, the bid is recorded
#     (bid_method="driver_bot", with the amount), and the driver gets a
#     confirmation.
#
# Differences forced by running server-side, per license:
#   * Each license has its own driver bot token (Settings) — deliberately
#     NOT the desktop's hardcoded driver token, so getUpdates on the two
#     can never collide (license_db rejects the desktop's known token).
#   * Drivers come from the fleet (trucks with a Telegram chat ID) instead
#     of the desktop's truck text box.
#   * Load data is read back from load_store (persistent), so a driver can
#     tap BID even after the poller restarted; only the in-flight "waiting
#     for this driver's rate" prompt state is in memory (same as desktop).
# =============================================================

import json
import logging
import os
import re
import threading
import time
import traceback
from typing import Optional

import requests

import activity_log
import bid_history
import fleet_store
import license_db
import load_store
import map_token
import tg_notify
from parser_core import fmt_hours_minutes

logger = logging.getLogger("driver_bot_web")

_PENDING = {}                    # (token, chat_id, order_id) -> {"order_id","load_data","driver_name","prompt_msg_id"}
_PENDING_LOCK = threading.Lock()

# Driver's own BID popup (2026-10-07, client: "when driver presses bid
# it should open map and bid amount just like in the bid phone in
# dispatcher version, without the draft aspects") — same page
# (bid_price.html), same web_app-in-a-private-chat mechanism the
# dispatcher's BID PC/PHONE shortcuts already use (poller.py), just
# with method=driver and a token tied to exactly one driver.
#
# Real bug, found live 2026-10-07: this used to be a plain module-level
# constant, same pattern poller.py's own WEB_BASE_URL uses — but
# poller.py imports driver_bot_web (line ~65) BEFORE it calls its own
# _load_env_file() (line ~90), so driver_bot_web's module body ran
# against an empty os.environ and permanently cached "". Exactly the
# ordering trap map_token.py's _secret() already has a comment about
# ("main.py imports this module BEFORE it calls its own
# _load_env_file()") — missed applying the same fix here the first
# time. Reading fresh on every call (cheap — a dict lookup) sidesteps
# the ordering entirely, same as _secret() does.
def _web_base_url() -> str:
    return os.environ.get("WEB_BASE_URL", "").rstrip("/")


def _web_app_ok(chat_id) -> bool:
    """Telegram only allows web_app buttons in PRIVATE chats (positive
    ids); groups/channels get a plain callback instead — same rule
    poller.py's own _web_app_ok enforces for the dispatcher's buttons."""
    return isinstance(chat_id, int) and chat_id > 0


def _driver_bid_url(license_key: str, order_id: str, driver_name: str) -> Optional[str]:
    base = _web_base_url()
    if not base:
        return None
    tok = map_token.make_bid_token(license_key, order_id, driver_name=driver_name)
    return f"{base}/app/bid_price.html?t={tok}&method=driver"


# ── Telegram plumbing (single choke point so tests can capture it) ────
def _api(token: str, method: str, payload: dict, timeout: int = 10) -> Optional[dict]:
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=timeout)
        if not r.ok:
            logger.warning(f"driver bot {method} failed: {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        logger.warning(f"driver bot {method} exception: {e}")
        return None


def _send(token: str, chat_id, text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    resp = _api(token, "sendMessage", payload, timeout=10)
    return ((resp or {}).get("result") or {}).get("message_id")


def _answer_callback(token: str, callback_query_id: str, text: str = ""):
    _api(token, "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text}, timeout=8)


# ── Card formatting (ported verbatim) ─────────────────────────────────
def format_driver_summary(driver_name: str, load_data: dict, truck: dict = None) -> str:
    """truck (added 2026-10-07 — real client-reported bug, caught live
    testing 20 drivers at once: every driver's card showed the SAME
    Out/Total Miles and ETA) — load_data["formatted_message"] is built
    ONCE in parser_core.py against a single "best" truck for the
    dispatcher's own message; reusing it verbatim for every driver
    meant every card showed THAT one truck's deadhead regardless of
    which driver it was actually sent to, even though load_data["all_
    trucks"] already carries each matched truck's own figures. Only
    Out Miles / Total Miles / Truck Dims / ETA actually depend on the
    truck (confirmed against parser_core.py's own message-building
    code: TT depends only on loaded miles, Loaded Miles is a load
    property — neither varies by truck, left alone). truck=None (no
    matching all_trucks entry for this driver) falls back to the
    shared text unchanged, same as before this fix."""
    base = load_data.get("formatted_message", "")
    exclude = ("⏱️ Email time", "🤝 Broker", "Name:", "Company:", "Phone:", "Email:", "draft :", "Driver:")
    lines = [ln for ln in base.splitlines() if not any(ln.strip().startswith(x) for x in exclude)]
    if truck:
        loaded_miles = load_data.get("loaded_miles")
        deadhead = truck.get("google_deadhead")
        total_miles = (loaded_miles + deadhead) if (loaded_miles is not None and deadhead is not None) else None
        eta_minutes = truck.get("deadhead_eta_minutes")
        dims = truck.get("truck_dimensions", "")
        patched = []
        for ln in lines:
            s = ln.strip()
            if s.startswith("Out Miles:") and deadhead is not None:
                patched.append(f"Out Miles: {deadhead}")
            elif s.startswith("Total Miles:") and total_miles is not None:
                patched.append(f"Total Miles: {total_miles}")
            elif s.startswith("Truck Dims:"):
                patched.append(f"Truck Dims: {dims}")
            elif s.startswith("🕒 ETA:") and eta_minutes is not None:
                patched.append(f"🕒 ETA: {fmt_hours_minutes(eta_minutes)}")
            else:
                patched.append(ln)
        lines = patched
    cleaned = "\n".join(lines).strip()
    return f"👤 {driver_name}\n{'─' * 30}\n{cleaned}"


def format_load_card(order_id: str, load_data: dict, truck: dict = None) -> str:
    deadhead = (truck or {}).get("google_deadhead", load_data.get("google_deadhead"))
    driver_name = (truck or {}).get("driver_name") or load_data.get("driver_name")
    lines = [
        f"🚛  LOAD #{order_id}",
        f"Vehicle:    {load_data.get('vehicle_required', '')}",
        f"📍 Pickup:   {load_data.get('pickup_loc', 'UNKNOWN')}",
        f"📍 Delivery: {load_data.get('delivery_loc', 'UNKNOWN')}",
    ]
    if load_data.get("pickup_dt"):
        lines.append(f"📅 PU Date:  {load_data['pickup_dt']}")
    if load_data.get("delivery_dt"):
        lines.append(f"📅 DEL Date: {load_data['delivery_dt']}")
    if deadhead is not None:
        lines.append(f"📏 Deadhead: {deadhead} mi")
    if driver_name:
        lines.append(f"👤 Matched:  {driver_name}")
    return "\n".join(lines)


def parse_rate(text: str) -> Optional[str]:
    """1400 | $1400 | 1,400 | $1,400.00 | 1400.00  ->  "1400" / "1,400"."""
    text = (text or "").strip().replace("$", "").strip()
    m = re.search(r"(\d[\d,]*(?:\.\d{1,2})?)", text)
    if not m:
        return None
    raw = m.group(1)
    if raw.endswith(".00"):
        raw = raw[:-3]
    return raw


# ── notify (desktop: notify_drivers) ──────────────────────────────────
def notify_drivers(license_key: str, token: str, order_id: str, load_data: dict, formatted: str = "") -> int:
    """Send the driver card to every eligible driver. Returns how many
    cards were sent.

    Real bug, caught live 2026-10-07 testing 20 drivers at once: this
    used to loop over EVERY fleet truck with a chat ID and re-check
    vehicle type with a crude substring match ("VAN" in "CARGO VAN" is
    True) — looser than the REAL matching find_all_trucks_for_pickup()
    already did in parser_core.py (radius, date, payload, dimensions,
    exact vehicle match), so a driver 800mi outside their own radius
    cap could still get a card. Worse: since that driver was never a
    real match, they had no entry in load_data["all_trucks"] either, so
    the per-driver personalization fix above silently fell back to
    showing them the WINNING truck's deadhead/ETA — reported as "still
    the same for all drivers", which is exactly how it surfaced.
    Iterating all_trucks directly instead (the authoritative, already-
    fully-filtered match list) fixes both at once: only genuinely
    matched trucks are considered at all, and every one of them always
    has its own real entry to personalize from — no fallback case left."""
    load_data = dict(load_data or {})
    if formatted:
        load_data["formatted_message"] = formatted
    all_trucks = load_data.get("all_trucks") or []
    if not all_trucks:
        return 0
    chat_id_by_name = {t.get("driver_name"): t.get("telegram_chat_id")
                       for t in fleet_store.list_trucks(license_key) if t.get("telegram_chat_id")}
    sent = 0
    for truck_entry in all_trucks:
        name = truck_entry.get("driver_name")
        chat_id = chat_id_by_name.get(name)
        if not chat_id:
            continue  # this matched truck's driver isn't on the driver bot
        if load_data.get("formatted_message"):
            card = format_driver_summary(name, load_data, truck_entry)
        else:
            card = f"👤 {name}\n{'─' * 30}\n" + format_load_card(order_id, load_data, truck_entry)
        # One-tap map+price popup in private chats (2026-10-07) — same
        # web_app shortcut the dispatcher's BID PC/PHONE already use;
        # groups keep the callback (Telegram platform limit, not
        # something client-side code can route around).
        bid_url = _driver_bid_url(license_key, order_id, name) if _web_app_ok(chat_id) else None
        bid_button = ({"text": "💰 BID", "web_app": {"url": bid_url}} if bid_url
                      else {"text": "💰 BID", "callback_data": f"driverbid:{order_id}:{name}"})
        keyboard = {"inline_keyboard": [[
            bid_button,
            *([{"text": "🚩 ROUTE", "url": load_data["route_url"]}] if load_data.get("route_url") else []),
        ]]}
        msg_id = _send(token, chat_id, card, keyboard)
        if msg_id:
            sent += 1
            with _PENDING_LOCK:
                _PENDING.setdefault((token, chat_id, order_id), {
                    "order_id": order_id, "load_data": load_data,
                    "driver_name": name, "prompt_msg_id": None})
            activity_log.log_event(license_key, "driver_card", f"Sent load #{order_id} card to driver {name}")
        else:
            logger.warning(f"[{license_key}] FAILED card to {name} order={order_id}")
    return sent


# ── driver actions (desktop: _handle_callback_query / _handle_message) ──
def handle_callback_query(license_key: str, token: str, cq: dict):
    data = cq.get("data", "")
    if not data.startswith("driverbid:"):
        return
    cq_id = cq.get("id", "")
    origin_chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
    presser_id = (cq.get("from") or {}).get("id")
    parts = data.split(":", 2)
    if len(parts) < 3:
        _answer_callback(token, cq_id, "Invalid data")
        return
    order_id, driver_name = parts[1], parts[2]
    # This card's own BID was a group callback (no web_app on the card
    # itself), but cq["from"]["id"] — whoever actually tapped it — is
    # always an individual user id, always eligible for a web_app
    # button in principle (2026-10-07). BUT Telegram still refuses to
    # let a bot DM a user who has never messaged that bot PRIVATELY
    # first ("403: bot can't initiate conversation with a user") — real
    # bug, found live 2026-10-07 testing drivers-as-groups: every DM
    # attempt failed silently (logged, never surfaced), so the driver
    # saw the "Enter your rate" toast and then nothing. If the DM
    # fails, fall back to the ForceReply prompt in the SAME chat the
    # card lives in (origin_chat_id) — that always works regardless of
    # whether this user has ever privately started the bot.
    bid_url = _driver_bid_url(license_key, order_id, driver_name)
    if bid_url:
        prompt_id = _send(token, presser_id, f"💰 Order #{order_id} — tap below to enter your rate:",
                          {"inline_keyboard": [[{"text": "💵 Enter price", "web_app": {"url": bid_url}}]]})
        if prompt_id:
            _answer_callback(token, cq_id, "💰 Check your DM with the bot")
            return
        logger.warning(f"[{license_key}] rate popup DM failed for {driver_name} (hasn't started "
                       f"a private chat with the bot?) — falling back to a reply prompt in this chat")
    _answer_callback(token, cq_id, "💰 Enter your rate below")
    chat_id = origin_chat_id
    prompt_id = _send(token, chat_id,
                      f"💰 Order #{order_id}\nType your rate (numbers only):\nExample:  1400",
                      {"force_reply": True, "selective": True})
    if not prompt_id:
        logger.warning(f"[{license_key}] failed to send the rate prompt to {driver_name}")
        return
    with _PENDING_LOCK:
        existing = _PENDING.get((token, chat_id, order_id))
        load_data = existing.get("load_data") if existing else None
        if load_data is None:
            load_data = load_store.get_load(license_key, order_id)   # survives poller restarts
        _PENDING[(token, chat_id, order_id)] = {
            "order_id": order_id, "load_data": load_data,
            "driver_name": driver_name, "prompt_msg_id": prompt_id}


def _find_pending(token: str, chat_id, reply_to_id):
    with _PENDING_LOCK:
        items = [(k, v) for k, v in _PENDING.items() if k[0] == token and k[1] == chat_id]
        if reply_to_id:
            for k, v in items:
                if v.get("prompt_msg_id") == reply_to_id:
                    return k, v
        for k, v in items:
            if v.get("prompt_msg_id") is not None:
                return k, v
        if items:
            return items[0]
    return None, None


def handle_message(license_key: str, token: str, msg: dict):
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    reply_to = msg.get("reply_to_message") or {}
    if not chat_id or not text:
        return
    key, pending = _find_pending(token, chat_id, reply_to.get("message_id"))
    if not pending:
        return
    order_id, driver_name = pending["order_id"], pending["driver_name"]
    load_data = pending.get("load_data")

    rate_str = parse_rate(text)
    if not rate_str:
        _send(token, chat_id, f'⚠️ Could not read your rate from "{text}".\n'
                              f"Please reply with a number only, e.g.:  1400")
        return
    with _PENDING_LOCK:
        _PENDING.pop(key, None)

    if not load_data:
        load_data = load_store.get_load(license_key, order_id)
    if load_data:
        forward_bid(license_key, driver_name, order_id, load_data, rate_str)
        _send(token, chat_id, f"✅ Bid of ${rate_str} sent to dispatcher!\nOrder #{order_id}")
    else:
        _send(token, chat_id, f"⚠️ Load #{order_id} data not found — bid could not be sent.\n"
                              f"Please contact dispatcher directly.")


def forward_bid(license_key: str, driver_name: str, order_id: str, load_data: dict, rate_str: str,
                desktop_relay: Optional[dict] = None):
    """Forward the driver's rate to the dispatcher chat(s) with the same
    BID PC / BID PHONE / DRAFT buttons, and record the bid with its amount.

    desktop_relay (2026-10-07, client: "driver bot (@plutus_driver_bot)
    to be the same as the web version") — {"dispatcher_bot_token",
    "dispatcher_chat_ids", ...}, present only for a bid that came from
    the DESKTOP's own driver bot's popup (see main.py's
    /api/driver_bid_popup_url and /api/web/bid_price/submit). The
    desktop's dispatcher bot/chat config lives only in
    driver_config.json on the dispatcher's own PC, invisible to
    license_db, so the signed token carries it instead of this
    forwarding through the license's web/standalone settings, which a
    desktop-only license never sets."""
    order_id = load_data.get("order", order_id)
    route_url = load_data.get("route_url", "")
    # Client, 2026-10-07: the Live Feed should hold this load back (set
    # "awaiting" by poller.py's _deliver_to_dispatcher when the driver
    # bot is active for its matched truck) until THIS moment — the
    # driver actually typing a rate — then show it WITH that rate.
    try:
        stored = load_store.get_load(license_key, order_id)
        if stored is not None:
            stored["driver_bid_status"] = "bid"
            stored["driver_bid_amount"] = rate_str
            stored["driver_bid_driver"] = driver_name
            load_store.put_load(license_key, order_id, stored)
    except Exception as e:
        logger.warning(f"[{license_key}] could not update load #{order_id} with the driver's bid for the live feed: {e}")
    full_msg = f"💰 {driver_name} — Rate: ${rate_str}\n{'─' * 30}\n" + load_data.get("formatted_message", "")
    rows = [[
        {"text": "💵 BID PC",    "callback_data": f"bid:{order_id}"},
        {"text": "💵 BID PHONE", "callback_data": f"phone:{order_id}"},
        {"text": "📋 DRAFT",     "callback_data": f"text:{order_id}"},
    ]]
    if route_url:
        rows.append([{"text": "🚩 ROUTE 🚩", "url": route_url}])
    if desktop_relay and desktop_relay.get("dispatcher_bot_token") and desktop_relay.get("dispatcher_chat_ids"):
        keyboard = {"inline_keyboard": rows}
        for cid in desktop_relay["dispatcher_chat_ids"]:
            _send(desktop_relay["dispatcher_bot_token"], cid, full_msg, keyboard)
    else:
        # The desktop's driver forwards bypass its Telegram on/off flag.
        tg_notify.send_to_license(license_key, full_msg, rows, respect_enabled=False)
    try:
        amount = float(rate_str.replace(",", ""))
    except ValueError:
        amount = None
    try:
        maps_v = load_data.get("maps_verification") or {}
        bid_history.record_bid(
            license_key=license_key, order_id=load_data.get("order", order_id),
            thread_id=(load_data.get("original_msg_full") or {}).get("threadId", ""),
            bid_method="driver_bot",
            vehicle_type=load_data.get("truck_type") or load_data.get("vehicle_required", ""),
            driver_name=driver_name,
            pickup_loc=load_data.get("pickup_loc", ""), delivery_loc=load_data.get("delivery_loc", ""),
            broker_name=load_data.get("broker_name", ""), broker_email=load_data.get("broker_email", ""),
            deadhead_miles=load_data.get("google_deadhead"),
            bid_amount=amount,
            verified_miles=maps_v.get("verified_miles"), verified_source=maps_v.get("verified_source"),
        )
    except Exception as e:
        logger.warning(f"[{license_key}] recording the driver bid failed (non-fatal): {e}")
    activity_log.log_event(license_key, "driver_bid", f"{driver_name} bid ${rate_str} on order #{order_id}")


# ── long-poll loop (one thread per distinct driver bot token) ─────────
def run_loop(token: str, license_key: str):
    offset = None
    logger.info(f"driver bot listener starting (bot ...{token[-6:]})")
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=35)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                try:
                    if upd.get("callback_query"):
                        handle_callback_query(license_key, token, upd["callback_query"])
                    elif upd.get("message"):
                        handle_message(license_key, token, upd["message"])
                except Exception:
                    logger.error(f"driver update handling error:\n{traceback.format_exc()}")
        except Exception as e:
            logger.warning(f"driver bot loop error (bot ...{token[-6:]}): {e}")
            time.sleep(5)
