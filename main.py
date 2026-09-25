import os
import json
import time
import base64 as _b64
import threading
import collections
import logging
import requests
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from models import (ParseRequest, ParseResponse, ActivateRequest, HeartbeatRequest,
                     RecordBidRequest, ClassifyReplyRequest, UpdateBidAmountRequest,
                     ThreadLearningToggleRequest, BackfillThreadRequest,
                     WebLoginRequest, WebTruckIn, WebTruckUpdate, WebBlacklistRequest,
                     WebRecordBidRequest, WebBidTemplateRequest, TelegramToggleRequest,
                     WebGmailTokenUpload, WebStandaloneSettings)
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
import gmail_store
import gmail_client
from gmail_client import GmailAuthError
import thread_backfill
import bid_actions
import map_token
import route_calibration
import zip_geocode
import activity_log

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mailbot")

import push_queue
import route_cache_store


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
API_SECRET          = os.environ.get("API_SECRET", "dev-secret-local")
# Same constant poller.py defines for its own outbound links (BID PC) —
# needed here too now for the Gmail OAuth redirect_uri and the
# post-consent bounce back to settings.html. No default: an unset
# value fails the OAuth endpoints closed (see web_gmail_oauth_start)
# rather than building a broken redirect_uri.
WEB_BASE_URL = os.environ.get("WEB_BASE_URL", "").rstrip("/")


@asynccontextmanager
async def lifespan(app):
    init_db()
    bid_history.init_db()
    bid_history.init_processed_threads_table()
    fleet_store.init_db()
    load_store.init_db()
    gmail_store.init_db()
    route_calibration.init_db()
    push_queue.init_db()
    route_cache_store.init_db()
    activity_log.init_db()
    zip_geocode.warmup()
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
            push_queue.push(history_id)
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
    items = push_queue.drain_all()
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
            # BID PC price-entry dialog (2026-09-19) — a confirmed
            # price/rate from the client, plain numbers, None when not
            # provided (every other caller of /api/build_bid keeps
            # working exactly as before).
            price            = load_data.get("price"),
            rate_per_mile    = load_data.get("rate_per_mile"),
        )
        return {"bid_text": bid_text}
    except Exception as e:
        logger.error(f"build_bid error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to build bid text")


def _fetch_route_map(pickup_loc: str, delivery_loc: str, frame_w=None, frame_h=None) -> dict:
    """
    Shared by /api/route_map (desktop, machine-bound auth) and
    /api/web/bid_price/route_map (the BID PC price+map mobile page,
    license/token auth — added 2026-09-24) — one Static-Maps-fetch
    implementation, not two forks that drift. Proxies Google's Static
    Maps API server-side so GOOGLE_MAPS_API_KEY (used elsewhere for
    mileage verification) never reaches the client — same reasoning as
    every other server-held secret in this project. Fails soft: no key
    configured, or the fetch itself fails, returns
    {"success": False, ...} rather than raising — every caller is
    expected to fall back to text-only route info, not break its flow.
    """
    pickup_loc   = (pickup_loc or "").strip()
    delivery_loc = (delivery_loc or "").strip()
    if not pickup_loc or not delivery_loc:
        return {"success": False, "reason": "missing pickup/delivery location"}
    if not GOOGLE_MAPS_API_KEY:
        return {"success": False, "reason": "map not configured"}

    # 2026-09-19, second same-day fix — real client feedback in this
    # order: "map needs to be bigger" (x2) -> "make it responsive" ->
    # "still covers everything the grey box" -> "i cant see the full
    # map". Requesting one fixed-aspect (square) image and then either
    # letterboxing (fit) or cropping (cover) it client-side to match
    # the actual display area was always going to lose on one side of
    # that tradeoff. The real fix: the client now tells us its actual
    # display area (frame_w/frame_h), so we request an image with that
    # SAME aspect ratio directly from Google — a plain fit then has
    # zero letterboxing AND zero cropping, because the source already
    # matches. Falls back to the previous fixed 640x640 when the
    # client doesn't send dimensions (e.g. an older build).
    if isinstance(frame_w, (int, float)) and isinstance(frame_h, (int, float)) \
            and frame_w > 0 and frame_h > 0:
        ratio = frame_w / frame_h
        if ratio >= 1:
            size_w, size_h = 640, max(1, round(640 / ratio))
        else:
            size_h, size_w = 640, max(1, round(640 * ratio))
    else:
        size_w, size_h = 640, 640
    size_param = f"{size_w}x{size_h}"

    # Continuous zoom-in on top of the (integer-only) zoom level below
    # — client, 2026-09-23: "zoom in on the map about 20%". Google
    # Static Maps' zoom parameter is integer-only (confirmed directly:
    # a fractional value like zoom=6.3 is silently ignored, falling
    # back to a near-whole-world view), so a smooth adjustment can't
    # come from zoom itself, and each integer level is a full 2x jump
    # — far coarser than "20%". Instead, request FEWER pixels than the
    # frame actually needs, at the SAME zoom/center — fewer pixels
    # covers proportionally less ground at a fixed zoom, which is a
    # true continuous zoom-in. The client already scales the received
    # image to fill its frame, so the ~17% fewer pixels requested here
    # just get upscaled back to display size on arrival — an
    # imperceptible quality cost for a deliberate 20% adjustment.
    _MAP_ZOOM_BOOST = 1.2
    fetch_size_param = f"{max(1, round(size_w / _MAP_ZOOM_BOOST))}x{max(1, round(size_h / _MAP_ZOOM_BOOST))}"

    # Real driving route, not a straight line — client feedback,
    # 2026-09-19: "it should be an actual route line that the truck
    # will take". A bare two-point `path` in Static Maps draws a
    # straight line between the points regardless of roads; passing
    # an ENCODED POLYLINE from the Routes API (the same API already
    # used for deadhead mileage verification, see parser_core.py)
    # draws the real road-following path instead. Fails soft to the
    # old straight line if the polyline fetch fails for any reason —
    # a straight line is still a real, usable map, just not as
    # accurate.
    polyline = parser_core.get_route_polyline(pickup_loc, delivery_loc)
    if polyline:
        path_param = f"color:0x1a7f4bff|weight:4|enc:{polyline}"
    else:
        path_param = f"color:0x1a7f4bff|weight:4|{pickup_loc}|{delivery_loc}"

    # Real client feedback, 2026-09-21: "the map should be a little
    # more zoomed out so he can see a bigger picture of whats
    # surrounding around delivery route" — Static Maps' own auto-fit
    # (letting path+markers imply the viewport) crops as tight as
    # possible around the route with zero margin. When we have real
    # route geometry, compute an explicit center/zoom padded outward
    # so the rendered map shows genuine surrounding context instead.
    # Falls back to plain auto-fit (no center/zoom) when there's no
    # polyline to pad, same as before.
    extra_params = {}
    if polyline:
        size_w_px, size_h_px = (int(x) * 2 for x in size_param.split("x"))  # scale=2
        view = parser_core.compute_route_view(polyline, size_w_px, size_h_px)
        if view:
            center_lat, center_lon, zoom = view
            extra_params["center"] = f"{center_lat},{center_lon}"
            extra_params["zoom"] = str(zoom)

    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/staticmap",
            params={
                # scale=2 keeps it sharp — real pixel dimensions are
                # double this, up to Google's free/standard-tier
                # ceiling either way. fetch_size_param (not size_param)
                # is what's actually requested — see the zoom-boost
                # note above.
                "size":    fetch_size_param,
                "scale":   "2",
                "path":    path_param,
                "markers": [f"color:green|label:P|{pickup_loc}",
                            f"color:red|label:D|{delivery_loc}"],
                "key":     GOOGLE_MAPS_API_KEY,
                **extra_params,
            },
            timeout=10,
        )
        r.raise_for_status()
        if r.headers.get("Content-Type", "").startswith("image/"):
            return {"success": True, "image_b64": _b64.b64encode(r.content).decode("ascii")}
        # Google returns 200 with an error image / no Content-Type for
        # some bad-request cases (e.g. an ungeocodable address) instead
        # of a real HTTP error — treat that as a soft failure too.
        logger.warning(f"route_map: unexpected content-type from Static Maps API "
                        f"(pickup={pickup_loc!r} delivery={delivery_loc!r})")
        return {"success": False, "reason": "map fetch failed"}
    except Exception as e:
        logger.error(f"route_map error: {e}")
        return {"success": False, "reason": "map fetch failed"}


@app.post("/api/route_map")
def route_map(req: dict):
    """New endpoint, 2026-09-19: the BID PC price-entry dialog shows a
    route map (pickup -> delivery) alongside the price field. Desktop-
    only (machine-bound auth) — see /api/web/bid_price/route_map for
    the phone/dashboard equivalent, which shares _fetch_route_map above."""
    check = validate_license(req.get("license_key", ""), req.get("machine_id", ""))
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return _fetch_route_map(req.get("pickup_loc", ""), req.get("delivery_loc", ""),
                             req.get("frame_w"), req.get("frame_h"))


@app.post("/api/record_bid")
def record_bid(req: RecordBidRequest):
    check = validate_license(req.license_key, req.machine_id)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    try:
        bid_id = bid_history.record_bid(
            license_key=req.license_key,
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
        ok = bid_history.update_bid_amount(req.license_key, req.bid_id, req.bid_amount)
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
            license_key=req.license_key,
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
    pending = bid_history.get_pending_bids_for_thread(req.license_key, req.thread_id)
    if not pending:
        return {"success": True, "matched": False}

    # Order/lane context for whichever bid this reply is about — added
    # 2026-09-12 so the client can build a real "a broker replied"
    # notification without a second round trip. Previously `matched`
    # was returned with nothing else useful attached, and the client had
    # no way to tell the dispatcher a reply came in at all.
    order_context = {
        "order_id":     pending[0].get("order_id", ""),
        "pickup_loc":   pending[0].get("pickup_loc", ""),
        "delivery_loc": pending[0].get("delivery_loc", ""),
        "broker_name":  pending[0].get("broker_name", ""),
    }

    result = reply_classifier.classify_broker_reply(req.subject, req.message_body)

    if result["status"] == "no_signal" or result["confidence"] < 0.55:
        logger.info(f"[CLASSIFY] thread={req.thread_id} no actionable signal "
                    f"(status={result['status']} conf={result['confidence']})")
        return {"success": True, "matched": True, "updated": False,
                "classification": result, "order": order_context}

    updated_ids = []
    for bid in pending:
        if bid_history.update_bid_outcome(
            req.license_key, bid["id"], result["status"],
            outcome_source="broker_reply", outcome_note=result["reason"],
        ):
            updated_ids.append(bid["id"])

    logger.info(f"[CLASSIFY] thread={req.thread_id} -> {result['status']} "
                f"(conf={result['confidence']}) bids={updated_ids}")
    return {"success": True, "matched": True, "updated": True,
            "bid_ids": updated_ids, "classification": result,
            "order": order_context}


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
    # dict, invisible across uvicorn's 4 worker processes). Per-license
    # isolation added 2026-09-25 — this used to return the SAME global
    # load pool regardless of which license authenticated ("a known
    # limitation, fine while there's a single active dispatcher" — it
    # stopped being fine once a second real account needed to act as
    # an independent one).
    items = load_store.get_recent_loads(license_key, limit=limit)
    # original_msg_full carries raw Gmail payload data the frontend never
    # needs in full — strip it, but pull the one field out of it first
    # that the frontend DOES need: the real threadId (present for
    # poller-sourced loads as of 2026-09-05, still '' for anything
    # sourced from the desktop's /api/parse, which never sends one) —
    # this is what lets "Find in Gmail" land on the exact thread instead
    # of only ever falling back to a search.
    cleaned = []
    for item in items:
        thread_id = (item.get("original_msg_full") or {}).get("threadId", "")
        entry = {k: v for k, v in item.items() if k != "original_msg_full"}
        entry["thread_id"] = thread_id
        cleaned.append(entry)
    return {"success": True, "count": len(cleaned), "items": cleaned}


@app.get("/api/web/bid_history")
def web_bid_history(license_key: str, limit: int = 50):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return {"success": True, "items": bid_history.get_recent_bids(license_key, limit=limit)}


@app.get("/api/web/activity")
def web_activity(license_key: str, limit: int = 100):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return {"success": True, "items": activity_log.get_recent_events(license_key, limit=limit)}


@app.get("/api/web/stats")
def web_stats(license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    return {"success": True, **bid_history.overall_summary(license_key)}


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
    return {"success": True, "items": fleet_store.list_trucks(license_key, active_only=not include_inactive)}


@app.post("/api/web/trucks")
def web_add_truck(req: WebTruckIn):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    truck_id = fleet_store.add_truck(
        license_key=req.license_key,
        vehicle=req.vehicle, driver_name=req.driver_name, zip_location=req.zip_location,
        dimensions=req.dimensions, max_payload_lbs=req.max_payload_lbs,
        equipment=req.equipment, allowed_states=req.allowed_states,
        pickup_date=req.pickup_date, radius_miles=req.radius_miles,
        loaded_miles_min=req.loaded_miles_min, loaded_miles_max=req.loaded_miles_max,
    )
    return {"success": True, "truck_id": truck_id}


@app.patch("/api/web/trucks/{truck_id}")
def web_update_truck(truck_id: int, req: WebTruckUpdate):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    fields = req.dict(exclude={"license_key"}, exclude_none=True)
    updated = fleet_store.update_truck(req.license_key, truck_id, **fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Truck not found or nothing to update")
    return {"success": True}


@app.delete("/api/web/trucks/{truck_id}")
def web_delete_truck(truck_id: int, license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    deleted = fleet_store.delete_truck(license_key, truck_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Truck not found")
    return {"success": True}


@app.get("/api/web/brokers")
def web_list_brokers(license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    blacklisted = {b["broker_email"] for b in fleet_store.list_blacklisted_brokers(license_key)}
    brokers = bid_history.list_all_brokers(license_key)
    for b in brokers:
        b["blacklisted"] = b["broker_email"] in blacklisted
    brokers.sort(key=lambda b: b["total_bids"], reverse=True)
    return {"success": True, "items": brokers}


@app.post("/api/web/brokers/blacklist")
def web_blacklist_broker(req: WebBlacklistRequest):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    fleet_store.blacklist_broker(req.license_key, req.broker_email, req.broker_name, req.note)
    logger.info(f"[WEB] blacklisted broker: {req.broker_email}")
    activity_log.log_event(req.license_key, "broker_blacklisted", f"Blacklisted broker {req.broker_email}")
    return {"success": True}


@app.delete("/api/web/brokers/blacklist/{broker_email}")
def web_unblacklist_broker(broker_email: str, license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    fleet_store.unblacklist_broker(license_key, broker_email)
    logger.info(f"[WEB] un-blacklisted broker: {broker_email}")
    activity_log.log_event(license_key, "broker_unblacklisted", f"Un-blacklisted broker {broker_email}")
    return {"success": True}


@app.post("/api/web/record_bid")
def web_record_bid(req: WebRecordBidRequest):
    """
    The web equivalent of the desktop's BID PC / BID PHONE / DRAFT
    buttons. The desktop's version doesn't just log the action — it
    copies the actual bid reply text to the clipboard and opens the
    Gmail thread, so the dispatcher has something to paste and can
    send it themselves (nothing is ever auto-sent, by design, same
    principle as the rest of this project). Build/return that same
    text (via the shared bid_actions helper, also used by poller.py
    and the BID PC price+map page) so the frontend can show/copy it.

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

    result = bid_actions.record_bid_and_build_text(req.license_key, req.order_id, req.method)
    if not result:
        raise HTTPException(status_code=404, detail="Order not found in the current live feed")

    logger.info(f"[WEB] recorded bid: order={req.order_id} method={req.method} bid_id={result['bid_id']}")
    activity_log.log_event(req.license_key, "bid_recorded",
                            f"Recorded {req.method.upper()} bid on order #{req.order_id}")
    return {"success": True, "bid_id": result["bid_id"], "bid_text": result["bid_text"],
            "thread_id": result["thread_id"]}


# ── BID PC price+map mobile page (bid_price.html), added 2026-09-24 ────
# The desktop's BID PC dialog (map + price + live rate/mile) never had
# a web/Telegram equivalent — poller.py's BID PC was a plain Gmail
# link, and loads.html's Bid PC button built a draft with no price.
# These three endpoints serve bid_price.html, reachable either from a
# Telegram-sent link (a short-lived signed token, ?t=, since a phone
# has no prior dashboard session and the raw license_key should never
# go into a Telegram message) or from the dashboard's own Bid PC button
# (already-authenticated via common.js's normal license_key flow).
def _resolve_bid_price_auth(t: str, license_key: str, order_id: str = None):
    """Returns (license_key, order_id), preferring the token when
    present. Raises HTTPException on any auth/validation failure —
    every /api/web/bid_price/* endpoint calls this first."""
    if t:
        claims = map_token.verify_bid_token(t)
        if not claims:
            raise HTTPException(status_code=403, detail="This link has expired or is invalid — ask for a new one.")
        license_key = claims["license_key"]
        order_id = claims["order_id"]
    if not license_key:
        raise HTTPException(status_code=403, detail="Not authenticated")
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id is required")
    return license_key, order_id


@app.get("/api/web/bid_price/context")
def web_bid_price_context(t: str = None, license_key: str = None, order_id: str = None):
    """Everything bid_price.html needs to render: route/order info for
    the header, plus the fields /api/web/bid_price/route_map needs."""
    license_key, order_id = _resolve_bid_price_auth(t, license_key, order_id)
    load = load_store.get_load(license_key, order_id)
    if not load:
        raise HTTPException(status_code=404, detail="Order not found in the current live feed")
    order = {k: v for k, v in load.items() if k != "original_msg_full"}
    order["order_id"] = order_id
    return {"success": True, "order": order}


@app.post("/api/web/bid_price/route_map")
def web_bid_price_route_map(req: dict):
    """Same map fetch /api/route_map does (shares _fetch_route_map),
    minus the desktop's machine-bound auth — a phone has no machine_id."""
    _resolve_bid_price_auth(req.get("t"), req.get("license_key"), req.get("order_id"))
    return _fetch_route_map(req.get("pickup_loc", ""), req.get("delivery_loc", ""),
                             req.get("frame_w"), req.get("frame_h"))


@app.post("/api/web/bid_price/submit")
def web_bid_price_submit(req: dict):
    """Builds the bid draft WITH the confirmed price via the shared
    bid_actions helper (the same one poller.py and /api/web/record_bid
    use) — this is the one place a price actually reaches the draft
    text from a web/Telegram-triggered BID PC. No Gmail draft is
    created here (same as poller.py's/loads.html's existing BID PC —
    neither has ever done that, even on the desktop's own Telegram-
    triggered path — see build_bid_reply_body's docstring); the
    frontend shows the built text to copy plus a link to the thread,
    same pattern loads.html's own bid modal already uses."""
    license_key, order_id = _resolve_bid_price_auth(
        req.get("t"), req.get("license_key"), req.get("order_id"))

    try:
        price = float(req.get("price"))
        if price <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="A valid price is required")

    rate_per_mile = req.get("rate_per_mile")
    try:
        rate_per_mile = float(rate_per_mile) if rate_per_mile is not None else None
    except (TypeError, ValueError):
        rate_per_mile = None

    result = bid_actions.record_bid_and_build_text(license_key, order_id, "pc", price, rate_per_mile)
    if not result:
        raise HTTPException(status_code=404, detail="Order not found in the current live feed")

    logger.info(f"[WEB] bid_price submit: order={order_id} price={price} bid_id={result['bid_id']}")
    activity_log.log_event(license_key, "bid_recorded",
                            f"Recorded PC bid on order #{order_id} at ${price:g}")
    return {"success": True, "bid_text": result["bid_text"],
            "thread_id": result["thread_id"], "broker_email": result["broker_email"]}


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


@app.post("/api/web/thread_learning/run_backfill")
def web_thread_learning_run_backfill(req: WebLoginRequest):
    """New, 2026-09-24 — the desktop app has had a manual "run backfill
    now" button since thread learning existed, but it required the
    desktop's own Gmail OAuth connection to actually walk threads, so
    the web dashboard could only ever toggle the feature on/off, never
    run it. Gated the same way /api/web/standalone/enable is (real
    prerequisites, not just a UI nicety a raw API call could bypass):
    thread learning must be on, and a Gmail token must be connected.
    Runs synchronously — a full 45-day backfill is a one-off manual
    action, not something needing a background job for a first version.
    """
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])

    if not license_db.get_thread_learning_enabled(req.license_key):
        raise HTTPException(status_code=400,
                             detail="Thread learning is OFF — enable it first.")

    gmail_status = gmail_store.get_status(req.license_key)
    if not gmail_status["connected"]:
        raise HTTPException(status_code=400,
                             detail="No Gmail token connected — connect Gmail first.")

    try:
        result = thread_backfill.run_backfill(req.license_key)
        logger.info(f"[WEB] backfill run for {req.license_key}: {result}")
        activity_log.log_event(req.license_key, "backfill_run",
                                f"Thread-learning backfill: {result.get('processed', 0)} processed, "
                                f"{result.get('skipped', 0)} skipped, {result.get('errors', 0)} errors")
        return {"success": True, **result}
    except GmailAuthError as e:
        raise HTTPException(status_code=400, detail=f"Gmail auth error: {e}")
    except Exception as e:
        logger.error(f"backfill run failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Backfill run failed")


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
    return {"success": True, "template": load_store.get_bid_template(license_key)}


@app.post("/api/web/bid_template")
def web_set_bid_template(req: WebBidTemplateRequest):
    """
    NOTE: this sets THIS license's own fallback template (per-license
    as of 2026-09-25 — before that it was one single global row shared
    by every account, `load_store.db`'s original design from
    2026-09-05), used only when a client's /api/parse call doesn't
    include its own bid_template. The desktop app always sends its own
    locally-configured template on every parse call, so editing this
    here does NOT change what the desktop actually uses day to day —
    the UI should say so, not imply otherwise. Standalone mode and the
    BID PC price+map page, which have no desktop process to supply
    their own, are what this actually drives.
    """
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    load_store.set_bid_template(req.license_key, req.template)
    logger.info(f"[WEB] bid template updated for {req.license_key}")
    return {"success": True}


# ── Standalone engine (Phase B, 2026-09-05) ─────────────────────────────
# See MAILBOT_ROADMAP.md — the server acting as the bot itself, no
# desktop needing to be open. This phase is credential/settings storage
# only: nothing here ever polls Gmail in a loop or sends a Telegram
# message. That's poller.py (Phase C), a separate process, shipped
# default-off.

@app.post("/api/web/gmail/token")
def web_gmail_token_upload(req: WebGmailTokenUpload):
    """Validates the pasted token.json content actually works against
    Gmail (a real getProfile() call) BEFORE saving anything — an
    upload endpoint that trusted arbitrary pasted JSON without checking
    it first would let a dispatcher "connect" a dead/garbage token and
    not find out until the poller silently skips them."""
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])

    probe = gmail_client.validate_and_probe(req.token_json)
    if not probe["ok"]:
        raise HTTPException(status_code=400, detail=probe["error"])

    gmail_store.save_token(req.license_key, probe["token_json"], probe["email"])
    logger.info(f"[WEB] Gmail token connected for {req.license_key} ({probe['email']})")
    activity_log.log_event(req.license_key, "gmail_connected", f"Gmail connected ({probe['email']})")
    return {"success": True, "connected_email": probe["email"]}


@app.get("/api/web/gmail/status")
def web_gmail_status(license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    # oauth_available tells the frontend whether to offer the real
    # "Sign in with Google" button at all — false until GOOGLE_OAUTH_
    # CLIENT_ID/SECRET are configured (see gmail_client.oauth_configured).
    return {"success": True, **gmail_store.get_status(license_key),
            "oauth_available": gmail_client.oauth_configured()}


@app.delete("/api/web/gmail/token")
def web_gmail_token_delete(license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    gmail_store.delete_token(license_key)
    logger.info(f"[WEB] Gmail token disconnected for {license_key}")
    activity_log.log_event(license_key, "gmail_disconnected", "Gmail disconnected")
    return {"success": True}


@app.get("/api/web/gmail/oauth/start")
def web_gmail_oauth_start(license_key: str):
    """The actual "Sign in with Google" entry point — a plain browser
    navigation (not a fetch/XHR call), since completing it means
    physically redirecting the dispatcher's browser to Google's own
    consent screen and back. license_key is carried through via the
    signed `state` param (map_token.make_oauth_state), not a server
    session, matching this project's stateless-where-possible pattern.
    """
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    if not WEB_BASE_URL:
        raise HTTPException(status_code=500, detail="Server misconfigured: WEB_BASE_URL not set.")

    redirect_uri = f"{WEB_BASE_URL}/api/web/gmail/oauth/callback"
    flow = gmail_client.build_oauth_flow(redirect_uri)
    if not flow:
        raise HTTPException(status_code=500,
                             detail="Google sign-in isn't configured on the server yet — "
                                    "use the paste-token option below instead.")

    state = map_token.make_oauth_state(license_key)
    # access_type=offline + prompt=consent: without both, Google won't
    # reliably hand back a refresh_token on a repeat consent (e.g. a
    # dispatcher reconnecting after a revoke) — the whole point of this
    # flow is a token that keeps working unattended, so a refresh_token
    # is not optional here.
    auth_url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true", state=state)
    return RedirectResponse(auth_url)


@app.get("/api/web/gmail/oauth/callback")
def web_gmail_oauth_callback(code: str = None, state: str = None, error: str = None):
    """Google redirects here after the dispatcher approves (or denies)
    consent. Always ends by bouncing back to settings.html — this is a
    page the dispatcher is looking at in their browser, not a JSON API
    caller, so errors are reported via a query param + toast there,
    not an HTTP error response."""
    settings_url = f"{WEB_BASE_URL}/app/settings.html"

    def _fail(reason: str):
        return RedirectResponse(f"{settings_url}?gmail_oauth=error&reason={quote(reason)}")

    if error:
        return _fail(f"Google sign-in was cancelled or denied ({error}).")
    if not code or not state:
        return _fail("Incomplete response from Google.")

    license_key = map_token.verify_oauth_state(state)
    if not license_key:
        return _fail("This sign-in link expired — try connecting Gmail again.")

    redirect_uri = f"{WEB_BASE_URL}/api/web/gmail/oauth/callback"
    flow = gmail_client.build_oauth_flow(redirect_uri)
    if not flow:
        return _fail("Google sign-in isn't configured on the server.")

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        email = gmail_client.get_profile_email(creds)
    except Exception as e:
        logger.error(f"gmail oauth callback failed for {license_key}: {e}", exc_info=True)
        return _fail("Couldn't complete sign-in with Google — try again.")

    gmail_store.save_token(license_key, creds.to_json(), email)
    logger.info(f"[WEB] Gmail connected via OAuth for {license_key} ({email})")
    return RedirectResponse(f"{settings_url}?gmail_oauth=success&email={quote(email)}")


@app.get("/api/web/standalone/settings")
def web_standalone_settings_get(license_key: str):
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    settings = license_db.get_standalone_settings(license_key)
    gmail_status = gmail_store.get_status(license_key)
    return {"success": True, **settings, "gmail_connected": gmail_status["connected"],
            "gmail_connected_email": gmail_status["connected_email"]}


@app.post("/api/web/standalone/settings")
def web_standalone_settings_set(req: WebStandaloneSettings):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    fields = req.dict(exclude={"license_key"}, exclude_none=True)
    try:
        license_db.set_standalone_settings(req.license_key, **fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(f"[WEB] standalone settings updated for {req.license_key}: {list(fields.keys())}")
    return {"success": True}


@app.post("/api/web/standalone/enable")
def web_standalone_enable(req: WebLoginRequest):
    """Unlike the Telegram/thread-learning toggles, this one re-checks
    real prerequisites server-side before flipping on — a raw API call
    must not be able to bypass the safety gate even if a UI's own
    client-side gating is bypassed or stale. Turning it OFF is always
    unconditional (see disable, below)."""
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])

    gmail_status = gmail_store.get_status(req.license_key)
    if not gmail_status["connected"]:
        raise HTTPException(status_code=400,
                             detail="No Gmail token connected — connect Gmail first.")

    settings = license_db.get_standalone_settings(req.license_key)
    if not (settings and settings["allowed_vehicles"].strip()):
        raise HTTPException(status_code=400,
                             detail="No allowed vehicles configured — set at least one first.")
    if license_db.is_known_desktop_token(settings.get("bot_token", "")):
        raise HTTPException(
            status_code=400,
            detail="Standalone mode's bot token is the desktop app's own token — "
                   "set a separate bot token first (Settings), or replies will be "
                   "unpredictably split between the desktop and standalone mode.",
        )

    license_db.set_standalone_mode_enabled(req.license_key, True)
    logger.info(f"[WEB] standalone mode ENABLED for {req.license_key}")
    activity_log.log_event(req.license_key, "standalone_enabled", "Standalone mode enabled")
    return {"success": True, "enabled": True}


@app.post("/api/web/standalone/disable")
def web_standalone_disable(req: WebLoginRequest):
    check = validate_license_key_only(req.license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    license_db.set_standalone_mode_enabled(req.license_key, False)
    logger.info(f"[WEB] standalone mode disabled for {req.license_key}")
    activity_log.log_event(req.license_key, "standalone_disabled", "Standalone mode disabled")
    return {"success": True, "enabled": False}


@app.get("/api/web/standalone/poller_status")
def web_standalone_poller_status(license_key: str):
    """poller.py (a separate process, not one of these 4 workers) writes
    one heartbeat row every outer-loop tick regardless of per-license
    outcomes — this just reads it back. Direct fix for "I can't tell if
    it's working": the Settings UI polls this to show a real liveness
    signal instead of silence."""
    check = validate_license_key_only(license_key)
    if not check["valid"]:
        raise HTTPException(status_code=403, detail=check["reason"])
    hb = load_store.get_poller_heartbeat()
    return {"success": True, "heartbeat": hb}


# Static frontend — mounted LAST and at a sub-path so it can never
# shadow an API route above. Reachable at https://<domain>/app/
# (Caddy already reverse-proxies everything to this server, so no
# Caddy config change is needed for this to work.)
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
if os.path.isdir(_WEB_DIR):
    app.mount("/app", StaticFiles(directory=_WEB_DIR, html=True), name="web")