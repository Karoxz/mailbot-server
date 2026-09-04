import os
import json
import time
import base64 as _b64
import threading
import collections
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles

from models import (ParseRequest, ParseResponse, ActivateRequest, HeartbeatRequest,
                     RecordBidRequest, ClassifyReplyRequest, UpdateBidAmountRequest,
                     ThreadLearningToggleRequest, BackfillThreadRequest,
                     WebLoginRequest, WebTruckIn, WebTruckUpdate, WebBlacklistRequest,
                     WebRecordBidRequest, WebBidTemplateRequest, TelegramToggleRequest)
import thread_learner
import license_db
from license_db import init_db, validate_license, activate_license, heartbeat, \
    validate_license_key_only
import parser_core
from parser_core import parse_email_for_api
import bid_history
import reply_classifier
import fleet_store
import load_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mailbot")

_push_queue: collections.deque = collections.deque()
_push_lock = threading.Lock()


# ── Load .env file manually (works without python-dotenv) ─────────────────
def _load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            # Strip surrounding quotes if present
            key, _, val = line.partition("=")
            val = val.strip().strip("'\"")
            os.environ.setdefault(key.strip(), val)

_load_env_file()

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY")
API_SECRET          = os.environ.get("API_SECRET", "dev-secret-local")


@asynccontextmanager
async def lifespan(app):
    init_db()
    bid_history.init_db()
    bid_history.init_processed_threads_table()
    fleet_store.init_db()
    load_store.init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(title="MailBot API", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook/gmail")
async def gmail_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
        message = body.get("message", {})
        if not message:
            return {"status": "ok"}
        data = message.get("data", "")
        if data:
            decoded = _b64.b64decode(data).decode("utf-8")
            notification = json.loads(decoded)
            history_id = str(notification.get("historyId", ""))
            print(f"WEBHOOK_HIT t={time.time():.3f}", flush=True)
            logger.info(f"PUSH_IN historyId={history_id} t={time.time():.3f}")
            with _push_lock:
                _push_queue.append((history_id, time.time()))
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "ok"}


@app.get("/webhook/poll")
async def poll_push(request: Request):
    check = validate_license(
        request.headers.get("X-License-Key", ""),
        request.headers.get("X-Machine-Id", "")
    )
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    with _push_lock:
        items = list(_push_queue)
        _push_queue.clear()
    if items:
        for history_id, pushed_at in items:
            lag = time.time() - pushed_at
            logger.info(f"PUSH_OUT historyId={history_id} lag={lag:.3f}s")
    # Return only the LATEST historyId — client just needs "new mail arrived"
    # and will walk history from its own cursor forward
    if items:
        latest = max(items, key=lambda x: int(x[0]))
        return {"history_ids": [latest[0]]}
    return {"history_ids": []}


@app.post("/api/activate")
async def activate(req: ActivateRequest):
    result = activate_license(req.license_key, req.machine_id, req.machine_name)
    if not result["success"]:
        raise HTTPException(status_code=403, detail=result["reason"])
    return {"success": True, "message": "Activated"}


@app.post("/api/heartbeat")
async def hb(req: HeartbeatRequest):
    ok = heartbeat(req.license_key, req.machine_id)
    if not ok:
        raise HTTPException(status_code=403, detail="License invalid or revoked")
    return {"valid": True}


@app.post("/api/parse", response_model=ParseResponse)
def parse(req: ParseRequest):
    check = validate_license(req.license_key, req.machine_id)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    try:
        result = parse_email_for_api(req.dict())
        return ParseResponse(**result)
    except Exception as e:
        logger.error(f"Parse error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal parsing error")


@app.post("/api/build_bid")
def build_bid(req: dict):
    check = validate_license(req.get("license_key", ""), req.get("machine_id", ""))
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    try:
        load_data = req.get("load_data", {})
        from parser_core import build_bid_reply_body
        bid_text = build_bid_reply_body(
            order            = load_data.get("order"),
            vehicle_required = load_data.get("vehicle_required"),
            pickup_loc       = load_data.get("pickup_loc"),
            pickup_dt        = load_data.get("pickup_dt"),
            delivery_loc     = load_data.get("delivery_loc"),
            delivery_dt      = load_data.get("delivery_dt"),
            google_deadhead  = load_data.get("google_deadhead"),
            driver_name      = load_data.get("driver_name"),
            truck_type       = load_data.get("truck_type"),
            truck_dimensions = load_data.get("truck_dimensions"),
            deadhead_eta_minutes = load_data.get("deadhead_eta_minutes"),
            truck_equipment  = load_data.get("truck_equipment", ""),
            bid_template     = load_data.get("bid_template"),
        )
        return {"bid_text": bid_text}
    except Exception as e:
        logger.error(f"build_bid error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to build bid text")


@app.post("/api/record_bid")
def record_bid(req: RecordBidRequest):
    check = validate_license(req.license_key, req.machine_id)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    try:
        bid_id = bid_history.record_bid(
            order_id=req.order_id, thread_id=req.thread_id, bid_method=req.bid_method,
            vehicle_type=req.vehicle_type, driver_name=req.driver_name,
            pickup_loc=req.pickup_loc, delivery_loc=req.delivery_loc,
            broker_name=req.broker_name, broker_email=req.broker_email,
            deadhead_miles=req.deadhead_miles, loaded_miles=req.loaded_miles,
            total_miles=req.total_miles, verified_miles=req.verified_miles,
            verified_source=req.verified_source, bid_amount=req.bid_amount,
        )
        return {"success": True, "bid_id": bid_id}
    except Exception as e:
        logger.error(f"record_bid error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to record bid")


@app.post("/api/update_bid_amount")
def update_bid_amount(req: UpdateBidAmountRequest):
    check = validate_license(req.license_key, req.machine_id)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    try:
        ok = bid_history.update_bid_amount(req.bid_id, req.bid_amount)
        return {"success": ok}
    except Exception as e:
        logger.error(f"update_bid_amount error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update bid amount")

@app.post("/api/thread_learning/enable")
def enable_thread_learning(req: ThreadLearningToggleRequest):
    check = validate_license(req.license_key, req.machine_id)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    ok = license_db.set_thread_learning_enabled(req.license_key, True)
    if not ok:
        raise HTTPException(status_code=404, detail="License not found")
    logger.info(f"[THREAD-LEARNING] enabled for {req.license_key}")
    return {"success": True, "enabled": True}


@app.post("/api/thread_learning/disable")
def disable_thread_learning(req: ThreadLearningToggleRequest):
    check = validate_license(req.license_key, req.machine_id)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    ok = license_db.set_thread_learning_enabled(req.license_key, False)
    if not ok:
        raise HTTPException(status_code=404, detail="License not found")
    logger.info(f"[THREAD-LEARNING] disabled for {req.license_key}")
    return {"success": True, "enabled": False}


@app.post("/api/thread_learning/status")
def thread_learning_status(req: ThreadLearningToggleRequest):
    check = validate_license(req.license_key, req.machine_id)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return {"enabled": license_db.get_thread_learning_enabled(req.license_key)}


# ── Telegram on/off — same dual pattern as thread_learning above: this
# machine-bound set is for the desktop client (which already has a real
# machine_id and enforces the binding), a license-key-only /api/web/
# set further down is for the browser. Unlike thread_learning,
# telegram_enabled defaults to 1 (see license_db.init_db()'s comment) —
# this gates EXISTING behavior, not a new opt-in feature, so nothing
# should go silent for anyone who hasn't touched this setting.
@app.post("/api/telegram/enable")
def enable_telegram(req: TelegramToggleRequest):
    check = validate_license(req.license_key, req.machine_id)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    ok = license_db.set_telegram_enabled(req.license_key, True)
    if not ok:
        raise HTTPException(status_code=404, detail="License not found")
    logger.info(f"[TELEGRAM] enabled for {req.license_key}")
    return {"success": True, "enabled": True}


@app.post("/api/telegram/disable")
def disable_telegram(req: TelegramToggleRequest):
    check = validate_license(req.license_key, req.machine_id)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    ok = license_db.set_telegram_enabled(req.license_key, False)
    if not ok:
        raise HTTPException(status_code=404, detail="License not found")
    logger.info(f"[TELEGRAM] disabled for {req.license_key}")
    return {"success": True, "enabled": False}


@app.post("/api/telegram/status")
def telegram_status(req: TelegramToggleRequest):
    check = validate_license(req.license_key, req.machine_id)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return {"enabled": license_db.get_telegram_enabled(req.license_key)}

@app.post("/api/backfill_thread")
def backfill_thread(req: BackfillThreadRequest):
    check = validate_license(req.license_key, req.machine_id)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])

    # Hard gate: even if a client somehow calls this while the toggle is
    # off, the server refuses to run any LLM extraction against the
    # thread — the on/off switch is enforced here, not just in the GUI.
    if not license_db.get_thread_learning_enabled(req.license_key):
        return {"success": False, "skipped": True, "reason": "thread_learning disabled"}

    try:
        result = thread_learner.process_thread(
            thread_id=req.thread_id,
            order_id=req.order_id,
            messages=[m.dict() for m in req.messages],
        )
        return {"success": True, **result}
    except Exception as e:
        logger.error(f"backfill_thread error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to process thread")
@app.post("/api/classify_reply")
def classify_reply(req: ClassifyReplyRequest):
    check = validate_license(req.license_key, req.machine_id)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])

    # Cheap DB lookup FIRST — the LLM is only ever called when this
    # thread actually has a bid awaiting an outcome.
    pending = bid_history.get_pending_bids_for_thread(req.thread_id)
    if not pending:
        return {"success": True, "matched": False}

    result = reply_classifier.classify_broker_reply(req.subject, req.message_body)

    if result["status"] == "no_signal" or result["confidence"] < 0.55:
        logger.info(f"[CLASSIFY] thread={req.thread_id} no actionable signal "
                    f"(status={result['status']} conf={result['confidence']})")
        return {"success": True, "matched": True, "updated": False, "classification": result}

    updated_ids = []
    for bid in pending:
        if bid_history.update_bid_outcome(
            bid["id"], result["status"],
            outcome_source="broker_reply", outcome_note=result["reason"],
        ):
            updated_ids.append(bid["id"])

    logger.info(f"[CLASSIFY] thread={req.thread_id} -> {result['status']} "
                f"(conf={result['confidence']}) bids={updated_ids}")
    return {"success": True, "matched": True, "updated": True,
            "bid_ids": updated_ids, "classification": result}


# =============================================================
# WEB DASHBOARD API  — read-only for now (Phase W, MVP slice 1)
#
# License-key-only auth (validate_license_key_only — no machine
# binding, see that function's docstring for why). Every endpoint
# re-validates on every call, same pattern as the rest of this file —
# no session state kept server-side yet; the frontend just re-sends
# the license key it already has (mirrors how the desktop client
# works today, not a new auth model).
# =============================================================

@app.post("/api/web/login")
def web_login(req: WebLoginRequest):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return {"success": True}


@app.get("/api/web/feed")
def web_feed(license_key: str, limit: int = 50):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])

    # load_store only ever holds loads that got far enough to match a
    # truck (see process_bid_email) — it's not a full "every email
    # seen" log. SQLite-backed as of 2026-09-05 (was an in-process
    # dict, invisible across uvicorn's 4 worker processes — not
    # scoped per-license still, a known limitation, fine while
    # there's a single active dispatcher).
    items = load_store.get_recent_loads(limit=limit)
    # original_msg_full carries raw Gmail payload data — strip it,
    # the frontend never needs it and it's not JSON-clean.
    cleaned = [{k: v for k, v in item.items() if k != "original_msg_full"}
               for item in items]
    return {"success": True, "count": len(cleaned), "items": cleaned}


@app.get("/api/web/bid_history")
def web_bid_history(license_key: str, limit: int = 50):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return {"success": True, "items": bid_history.get_recent_bids(limit=limit)}


@app.get("/api/web/stats")
def web_stats(license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return {"success": True, **bid_history.overall_summary()}


# =============================================================
# WEB DASHBOARD API — Phase W, slice 2: fleet management, broker
# blacklist, bid actions, bid template. All still license-key-only
# auth (validate_license_key_only), same reasoning as slice 1 above.
# =============================================================

@app.get("/api/web/trucks")
def web_list_trucks(license_key: str, include_inactive: bool = False):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return {"success": True, "items": fleet_store.list_trucks(active_only=not include_inactive)}


@app.post("/api/web/trucks")
def web_add_truck(req: WebTruckIn):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    truck_id = fleet_store.add_truck(
        vehicle=req.vehicle, driver_name=req.driver_name, zip_location=req.zip_location,
        dimensions=req.dimensions, max_payload_lbs=req.max_payload_lbs,
        equipment=req.equipment, allowed_states=req.allowed_states,
        pickup_date=req.pickup_date, radius_miles=req.radius_miles,
    )
    return {"success": True, "truck_id": truck_id}


@app.patch("/api/web/trucks/{truck_id}")
def web_update_truck(truck_id: int, req: WebTruckUpdate):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    fields = req.dict(exclude={"license_key"}, exclude_none=True)
    updated = fleet_store.update_truck(truck_id, **fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Truck not found or nothing to update")
    return {"success": True}


@app.delete("/api/web/trucks/{truck_id}")
def web_delete_truck(truck_id: int, license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    deleted = fleet_store.delete_truck(truck_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Truck not found")
    return {"success": True}


@app.get("/api/web/brokers")
def web_list_brokers(license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    blacklisted = {b["broker_email"] for b in fleet_store.list_blacklisted_brokers()}
    brokers = bid_history.list_all_brokers()
    for b in brokers:
        b["blacklisted"] = b["broker_email"] in blacklisted
    brokers.sort(key=lambda b: b["total_bids"], reverse=True)
    return {"success": True, "items": brokers}


@app.post("/api/web/brokers/blacklist")
def web_blacklist_broker(req: WebBlacklistRequest):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    fleet_store.blacklist_broker(req.broker_email, req.broker_name, req.note)
    logger.info(f"[WEB] blacklisted broker: {req.broker_email}")
    return {"success": True}


@app.delete("/api/web/brokers/blacklist/{broker_email}")
def web_unblacklist_broker(broker_email: str, license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    fleet_store.unblacklist_broker(broker_email)
    logger.info(f"[WEB] un-blacklisted broker: {broker_email}")
    return {"success": True}


@app.post("/api/web/record_bid")
def web_record_bid(req: WebRecordBidRequest):
    """
    The web equivalent of the desktop's BID PC / BID PHONE / DRAFT
    buttons. The desktop's version doesn't just log the action — it
    copies the actual bid reply text to the clipboard and opens the
    Gmail thread, so the dispatcher has something to paste and can
    send it themselves (nothing is ever auto-sent, by design, same
    principle as the rest of this project). The first web version of
    this endpoint skipped that part and only recorded silently, which
    left the dispatcher with nothing to actually act on — fixed here:
    build and return the same bid text build_bid_reply_body() produces
    for /api/build_bid, so the frontend can show/copy it.

    NOTE: unlike the desktop, this can't reliably open the exact Gmail
    thread — LOAD_STORE entries populated via /api/parse always carry
    a placeholder original_msg_full (no real headers/threadId), so
    thread_id and a real broker reply-to address aren't available yet
    server-side. Documented as an open gap, not silently glossed over.
    """
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    if req.method not in ("pc", "phone", "draft"):
        raise HTTPException(status_code=400, detail="method must be pc, phone, or draft")

    load = load_store.get_load(req.order_id)
    if not load:
        raise HTTPException(status_code=404, detail="Order not found in the current live feed")

    thread_id = (load.get("original_msg_full") or {}).get("threadId", "")
    maps_v = load.get("maps_verification") or {}
    bid_id = bid_history.record_bid(
        order_id=req.order_id,
        thread_id=thread_id,
        bid_method=req.method,
        vehicle_type=load.get("truck_type") or load.get("vehicle_required", ""),
        driver_name=load.get("driver_name", ""),
        pickup_loc=load.get("pickup_loc", ""),
        delivery_loc=load.get("delivery_loc", ""),
        broker_name=load.get("broker_name", ""),
        broker_email=load.get("broker_email", ""),
        deadhead_miles=load.get("google_deadhead"),
        verified_miles=maps_v.get("verified_miles"),
        verified_source=maps_v.get("verified_source"),
    )

    bid_text = parser_core.build_bid_reply_body(
        order=req.order_id,
        vehicle_required=load.get("vehicle_required"),
        pickup_loc=load.get("pickup_loc"),
        pickup_dt=load.get("pickup_dt"),
        delivery_loc=load.get("delivery_loc"),
        delivery_dt=load.get("delivery_dt"),
        google_deadhead=load.get("google_deadhead"),
        driver_name=load.get("driver_name", ""),
        truck_type=load.get("truck_type", ""),
        truck_dimensions=load.get("truck_dimensions", ""),
        deadhead_eta_minutes=load.get("deadhead_eta_minutes"),
        truck_equipment=load.get("truck_equipment", ""),
        bid_template=load.get("bid_template"),
    )

    logger.info(f"[WEB] recorded bid: order={req.order_id} method={req.method} bid_id={bid_id}")
    return {"success": True, "bid_id": bid_id, "bid_text": bid_text, "thread_id": thread_id}


@app.get("/api/web/thread_learning/status")
def web_thread_learning_status(license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return {"success": True, "enabled": license_db.get_thread_learning_enabled(license_key)}


@app.post("/api/web/thread_learning/enable")
def web_thread_learning_enable(req: WebLoginRequest):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    license_db.set_thread_learning_enabled(req.license_key, True)
    logger.info(f"[WEB] thread learning enabled for {req.license_key}")
    return {"success": True, "enabled": True}


@app.post("/api/web/thread_learning/disable")
def web_thread_learning_disable(req: WebLoginRequest):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    license_db.set_thread_learning_enabled(req.license_key, False)
    logger.info(f"[WEB] thread learning disabled for {req.license_key}")
    return {"success": True, "enabled": False}


@app.get("/api/web/telegram/status")
def web_telegram_status(license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return {"success": True, "enabled": license_db.get_telegram_enabled(license_key)}


@app.post("/api/web/telegram/enable")
def web_telegram_enable(req: WebLoginRequest):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    license_db.set_telegram_enabled(req.license_key, True)
    logger.info(f"[WEB] telegram enabled for {req.license_key}")
    return {"success": True, "enabled": True}


@app.post("/api/web/telegram/disable")
def web_telegram_disable(req: WebLoginRequest):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    license_db.set_telegram_enabled(req.license_key, False)
    logger.info(f"[WEB] telegram disabled for {req.license_key}")
    return {"success": True, "enabled": False}


@app.get("/api/web/bid_template")
def web_get_bid_template(license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return {"success": True, "template": load_store.get_bid_template()}


@app.post("/api/web/bid_template")
def web_set_bid_template(req: WebBidTemplateRequest):
    """
    NOTE: this sets the SERVER's fallback default template (in
    load_store.db as of 2026-09-05, was parser_core.BID_TEMPLATE — an
    in-process global, invisible across uvicorn's 4 workers, the same
    bug LOAD_STORE had), used only when a client's /api/parse call
    doesn't include its own bid_template. The desktop app always sends
    its own locally-configured template on every parse call, so
    editing this here does NOT change what the desktop actually uses
    day to day — the UI should say so, not imply otherwise.
    """
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    load_store.set_bid_template(req.template)
    logger.info("[WEB] bid template updated (server-side default)")
    return {"success": True}


# Static frontend — mounted LAST and at a sub-path so it can never
# shadow an API route above. Reachable at https://<domain>/app/
# (Caddy already reverse-proxies everything to this server, so no
# Caddy config change is needed for this to work.)
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
if os.path.isdir(_WEB_DIR):
    app.mount("/app", StaticFiles(directory=_WEB_DIR, html=True), name="web")