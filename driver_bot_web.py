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
import tg_notify

logger = logging.getLogger("driver_bot_web")

_PENDING = {}                    # (token, chat_id, order_id) -> {"order_id","load_data","driver_name","prompt_msg_id"}
_PENDING_LOCK = threading.Lock()


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
def format_driver_summary(driver_name: str, load_data: dict) -> str:
    base = load_data.get("formatted_message", "")
    exclude = ("⏱️ Email time", "🤝 Broker", "Name:", "Company:", "Phone:", "Email:", "draft :", "Driver:")
    lines = [ln for ln in base.splitlines() if not any(ln.strip().startswith(x) for x in exclude)]
    cleaned = "\n".join(lines).strip()
    return f"👤 {driver_name}\n{'─' * 30}\n{cleaned}"


def format_load_card(order_id: str, load_data: dict) -> str:
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
    if load_data.get("google_deadhead") is not None:
        lines.append(f"📏 Deadhead: {load_data['google_deadhead']} mi")
    if load_data.get("driver_name"):
        lines.append(f"👤 Matched:  {load_data['driver_name']}")
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


def drivers_for_license(license_key: str) -> list:
    """Fleet trucks that have a driver Telegram chat ID — the web
    equivalent of the desktop's driver_config.json drivers list."""
    out = []
    for t in fleet_store.list_trucks(license_key):
        if t.get("telegram_chat_id"):
            out.append({"name": t["driver_name"], "telegram_chat_id": t["telegram_chat_id"],
                        "truck_type": (t.get("vehicle") or "")})
    return out


# ── notify (desktop: notify_drivers) ──────────────────────────────────
def notify_drivers(license_key: str, token: str, order_id: str, load_data: dict, formatted: str = "") -> int:
    """Send the driver card to every eligible driver. Returns how many
    cards were sent."""
    load_data = dict(load_data or {})
    if formatted:
        load_data["formatted_message"] = formatted
    vehicle_required = (load_data.get("vehicle_required") or "").upper().strip()
    sent = 0
    for d in drivers_for_license(license_key):
        chat_id, name = d["telegram_chat_id"], d["name"]
        truck_type = d["truck_type"].upper().strip()
        if truck_type and vehicle_required and truck_type not in vehicle_required \
                and vehicle_required not in truck_type:
            logger.info(f"[{license_key}] skipping {name} ({truck_type} != {vehicle_required})")
            continue
        if load_data.get("formatted_message"):
            card = format_driver_summary(name, load_data)
        else:
            card = f"👤 {name}\n{'─' * 30}\n" + format_load_card(order_id, load_data)
        keyboard = {"inline_keyboard": [[
            {"text": "💰 BID", "callback_data": f"driverbid:{order_id}:{name}"},
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
    chat_id = (cq.get("from") or {}).get("id")
    parts = data.split(":", 2)
    if len(parts) < 3:
        _answer_callback(token, cq_id, "Invalid data")
        return
    order_id, driver_name = parts[1], parts[2]
    _answer_callback(token, cq_id, "💰 Enter your rate below")
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


def forward_bid(license_key: str, driver_name: str, order_id: str, load_data: dict, rate_str: str):
    """Forward the driver's rate to the dispatcher chat(s) with the same
    BID PC / BID PHONE / DRAFT buttons, and record the bid with its amount."""
    order_id = load_data.get("order", order_id)
    route_url = load_data.get("route_url", "")
    full_msg = f"💰 {driver_name} — Rate: ${rate_str}\n{'─' * 30}\n" + load_data.get("formatted_message", "")
    rows = [[
        {"text": "💵 BID PC",    "callback_data": f"bid:{order_id}"},
        {"text": "💵 BID PHONE", "callback_data": f"phone:{order_id}"},
        {"text": "📋 DRAFT",     "callback_data": f"text:{order_id}"},
    ]]
    if route_url:
        rows.append([{"text": "🚩 ROUTE 🚩", "url": route_url}])
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
