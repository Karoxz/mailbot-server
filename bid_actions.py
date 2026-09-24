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
# =============================================================

from typing import Optional

import bid_history
import load_store
import parser_core


def record_bid_and_build_text(order_id: str, method: str,
                               price: Optional[float] = None,
                               rate_per_mile: Optional[float] = None) -> Optional[dict]:
    """Returns {"bid_id", "bid_text", "thread_id", "broker_email"}, or
    None if order_id isn't in the current live feed (load_store)."""
    load = load_store.get_load(order_id)
    if not load:
        return None

    maps_v = load.get("maps_verification") or {}
    thread_id = (load.get("original_msg_full") or {}).get("threadId", "")

    bid_id = bid_history.record_bid(
        order_id=order_id,
        thread_id=thread_id,
        bid_method=method,
        vehicle_type=load.get("truck_type") or load.get("vehicle_required", ""),
        driver_name=load.get("driver_name", ""),
        pickup_loc=load.get("pickup_loc", ""),
        delivery_loc=load.get("delivery_loc", ""),
        broker_name=load.get("broker_name", ""),
        broker_email=load.get("broker_email", ""),
        deadhead_miles=load.get("google_deadhead"),
        loaded_miles=load.get("loaded_miles"),
        total_miles=load.get("total_miles"),
        verified_miles=maps_v.get("verified_miles"),
        verified_source=maps_v.get("verified_source"),
        bid_amount=price,
    )

    bid_text = parser_core.build_bid_reply_body(
        order=order_id,
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
        price=price, rate_per_mile=rate_per_mile,
    )

    return {
        "bid_id": bid_id,
        "bid_text": bid_text,
        "thread_id": thread_id,
        "broker_email": load.get("broker_email", ""),
    }
