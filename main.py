import os
import json
import time
import base64 as _b64
import threading
import collections
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks

from models import (ParseRequest, ParseResponse, ActivateRequest, HeartbeatRequest,
                     RecordBidRequest, ClassifyReplyRequest, UpdateBidAmountRequest,
                     ThreadLearningToggleRequest, BackfillThreadRequest)
import thread_learner
import license_db
from license_db import init_db, validate_license, activate_license, heartbeat
from parser_core import parse_email_for_api
import bid_history
import reply_classifier

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