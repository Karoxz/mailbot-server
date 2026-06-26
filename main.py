import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from models import ParseRequest, ParseResponse, ActivateRequest, HeartbeatRequest
from license_db import init_db, validate_license, activate_license, heartbeat
from parser_core import parse_email_for_api
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
import threading
import json
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mailbot")

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

API_SECRET = os.environ.get("API_SECRET", "dev-secret-local")

@asynccontextmanager
async def lifespan(app):
    init_db()
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
        import base64 as _b64
        data = message.get("data", "")
        if data:
            decoded = _b64.b64decode(data).decode("utf-8")
            notification = json.loads(decoded)
            history_id = str(notification.get("historyId", ""))
            import time as _time
            logger.info(f"PUSH_IN historyId={history_id} t={_time.time():.3f}")
            with _push_lock:
                _push_queue.append((history_id, _time.time()))
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
    import time as _time
    with _push_lock:
        items = list(_push_queue)
        _push_queue.clear()
    if items:
        for history_id, pushed_at in items:
            lag = _time.time() - pushed_at
            logger.info(f"PUSH_OUT historyId={history_id} lag={lag:.3f}s")
    return {"history_ids": [h for h, t in items]}
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
async def parse(req: ParseRequest):
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
async def build_bid(req: dict):
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
            bid_template     = load_data.get("bid_template"),   # ← ADD THIS
        )
        return {"bid_text": bid_text}
    except Exception as e:
        logger.error(f"build_bid error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to build bid text")
 
# ── Health check ──────────────────────────────────────────────────────────
@app.get('/health')
async def health():
    return {'status': 'ok'}