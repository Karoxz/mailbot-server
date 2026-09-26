# =============================================================
# bid_actions.py — server-side module, added 2026-09-24
#
# The one shared "record this bid + build its reply text" path, used
# by every caller that can fire a BID PC/PHONE/DRAFT action: poller.py
# (Telegram), main.py's /api/web/record_bid (dashboard), and the new
# /api/web/bid_price/submit (the BID PC price+map mobile page).
#
# Before this, poller.py and main.py each had their own near-identical
# copy of this logic, and NEITHER ever threaded a confirmed price
# through to build_bid_reply_body — the one caller that could (the
# desktop's Tk dialog) built the draft itself, in-process, so the gap
# never surfaced there. This is what lets a price confirmed anywhere
# (phone, dashboard, Telegram) land in the actual draft text the same
# way the desktop's BID PC dialog already does.
#
# 2026-09-26 (web/desktop parity): optional `truck` — one entry of a
# load's all_trucks list, the driver the dispatcher picked when several
# trucks matched. Mirrors the desktop's _build_bid_body_for_load (bid
# text built from THAT truck's deadhead/ETA/driver/dimensions/equipment)
# and _record_bid (the bid is recorded against THAT truck). truck=None
# keeps the previous single-driver behavior exactly. build_bid_text and
# record_bid are also exposed separately because the desktop's BID PHONE
# builds the text first and only records the bid after the Gmail draft
# was actually created.
# =============================================================

from typing import Optional

import bid_history
import load_store
import parser_core


def get_truck(load: dict, truck_idx) -> Optional[dict]:
    """all_trucks[truck_idx], or None when there's no valid selection
    (no index, out of range, not a number)."""
    try:
        idx = int(truck_idx)
    except (TypeError, ValueError):
        return None
    trucks = (load or {}).get("all_trucks") or []
    return trucks[idx] if 0 <= idx < len(trucks) else None


def build_bid_text(load: dict, order_id: str, license_key: str,
                   truck: Optional[dict] = None,
                   price: Optional[float] = None,
                   rate_per_mile: Optional[float] = None) -> str:
    src = truck if truck else load
    return parser_core.build_bid_reply_body(
        order=order_id,
        vehicle_required=load.get("vehicle_required"),
        pickup_loc=load.get("pickup_loc"),
        pickup_dt=load.get("pickup_dt"),
        delivery_loc=load.get("delivery_loc"),
        delivery_dt=load.get("delivery_dt"),
        google_deadhead=src.get("google_deadhead"),
        driver_name=src.get("driver_name", ""),
        truck_type=src.get("truck_type", ""),
        truck_dimensions=src.get("truck_dimensions", ""),
        deadhead_eta_minutes=src.get("deadhead_eta_minutes"),
        truck_equipment=src.get("truck_equipment", ""),
        bid_template=load.get("bid_template"),
        price=price, rate_per_mile=rate_per_mile,
        license_key=license_key,
    )


def record_bid(load: dict, order_id: str, license_key: str, method: str,
               truck: Optional[dict] = None,
               price: Optional[float] = None) -> int:
    maps_v = load.get("maps_verification") or {}
    thread_id = (load.get("original_msg_full") or {}).get("threadId", "")
    driver = truck if truck else load
    return bid_history.record_bid(
        license_key=license_key,
        order_id=order_id,
        thread_id=thread_id,
        bid_method=method,
        vehicle_type=(driver.get("truck_type") or load.get("truck_type")
                      or load.get("vehicle_required", "")),
        driver_name=driver.get("driver_name", ""),
        pickup_loc=load.get("pickup_loc", ""),
        delivery_loc=load.get("delivery_loc", ""),
        broker_name=load.get("broker_name", ""),
        broker_email=load.get("broker_email", ""),
        deadhead_miles=driver.get("google_deadhead") or load.get("google_deadhead"),
        loaded_miles=load.get("loaded_miles"),
        total_miles=load.get("total_miles"),
        verified_miles=maps_v.get("verified_miles"),
        verified_source=maps_v.get("verified_source"),
        bid_amount=price,
    )


def record_bid_and_build_text(license_key: str, order_id: str, method: str,
                               price: Optional[float] = None,
                               rate_per_mile: Optional[float] = None,
                               truck: Optional[dict] = None) -> Optional[dict]:
    """Returns {"bid_id", "bid_text", "thread_id", "broker_email"}, or
    None if order_id isn't in the current live feed (load_store)."""
    load = load_store.get_load(license_key, order_id)
    if not load:
        return None

    bid_id = record_bid(load, order_id, license_key, method, truck, price)
    bid_text = build_bid_text(load, order_id, license_key, truck, price, rate_per_mile)
    thread_id = (load.get("original_msg_full") or {}).get("threadId", "")

    return {
        "bid_id": bid_id,
        "bid_text": bid_text,
        "thread_id": thread_id,
        "broker_email": load.get("broker_email", ""),
    }
