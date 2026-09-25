"""
desktop_compat_check.py — MUST pass before every server deploy.

Standing rule (user, 2026-09-25): the web dashboard / standalone engine /
any server-side change must NEVER affect or break the shipped desktop app
(client/main copy.py, currently build 2026-09-23b). Real incident that
prompted this: the client's desktop stopped working on the same day a
large batch of server changes shipped; it turned out to be a network
blip, but there was no fast, repeatable way to PROVE the server still
honors the exact contract the desktop exe speaks.

What this does: drives every endpoint the desktop calls (the full list
lives in client/api_client.py + client/activation_screen.py) through
FastAPI's TestClient, using the same request shapes the desktop sends,
and asserts HTTP 200 plus the response keys the desktop actually reads.
Uses a throwaway license and cleans up after itself. Fails loudly (exit
code 1) on ANY deviation.

Old desktop builds are frozen: they never get updated unless the user
rebuilds and hands over a new exe. So desktop-facing endpoints must
keep their existing paths, request fields, and response keys FOREVER —
add new optional things, never rename/remove/require-new-fields.

Run:  python desktop_compat_check.py
Post-deploy live check:  python desktop_compat_check.py --live-logs
      (SSHes to the VPS and confirms zero non-200 responses on desktop
       endpoints over the last 15 minutes)
"""

import os
import sys
import sqlite3
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

# Every path the shipped desktop calls — kept in sync with
# client/api_client.py + client/activation_screen.py by hand; if the
# desktop ever gains a new call, add it here in the same change.
DESKTOP_PATHS = [
    "/api/activate", "/api/heartbeat", "/api/parse", "/api/build_bid",
    "/api/route_map", "/webhook/poll", "/api/record_bid", "/api/classify_reply",
    "/api/update_bid_amount", "/api/thread_learning/enable",
    "/api/thread_learning/disable", "/api/thread_learning/status",
    "/api/telegram/enable", "/api/telegram/disable", "/api/telegram/status",
    "/api/backfill_thread",
]


def live_log_check():
    key = os.path.join(HERE, ".monitor_ssh", "mailbot_monitor")
    pattern = "|".join(p.replace("/", r"\/") for p in DESKTOP_PATHS)
    cmd = (
        "journalctl -u mailbot-api --since '-15min' --no-pager 2>/dev/null "
        f"| grep -E '\"(GET|POST) ({pattern})' "
        "| grep -vE 'HTTP/1.1\" 200' | tail -20"
    )
    out = subprocess.run(
        ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20",
         "mailbot@178.105.208.7", cmd],
        capture_output=True, text=True, timeout=60,
    )
    bad = out.stdout.strip()
    if out.returncode not in (0, 1) and not bad:
        print(f"SSH/log check could not run: {out.stderr.strip()}")
        return 2
    if bad:
        print("NON-200 responses on desktop endpoints in the last 15 min:\n" + bad)
        return 1
    print("LIVE: no non-200 responses on any desktop endpoint in the last 15 min.")
    return 0


def main():
    if "--live-logs" in sys.argv:
        sys.exit(live_log_check())

    from fastapi.testclient import TestClient
    import license_db
    import main as server

    license_db.init_db()
    KEY = "DESKTOP-COMPAT-CHECK"
    MID = "compat-check-machine"
    failures = []

    def check(name, resp, expect_keys=(), status=200):
        ok = resp.status_code == status
        body = None
        try:
            body = resp.json()
        except Exception:
            pass
        missing = [k for k in expect_keys if not isinstance(body, dict) or k not in body]
        if not ok or missing:
            failures.append(f"{name}: HTTP {resp.status_code}, missing keys {missing}, body={str(resp.text)[:200]}")
            print(f"  FAIL  {name}")
        else:
            print(f"  ok    {name}")
        return body

    license_db.add_license(KEY, label="compat-check")
    base = {"license_key": KEY, "machine_id": MID}

    with TestClient(server.app) as c:
        # ── activation_screen.py ─────────────────────────────────
        check("POST /api/activate", c.post("/api/activate", json={**base, "machine_name": "compat"}), ["success"])
        check("POST /api/heartbeat", c.post("/api/heartbeat", json=base), ["valid"])

        # ── api_client.py ────────────────────────────────────────
        parse_payload = {
            **base,
            "email_body": "Bid on Order #999001\nVehicle required: LARGE STRAIGHT\n"
                          "Pickup: Cleveland, OH 44101\nDelivery: Columbus, OH 43215\n",
            "internal_date_ms": 1790000000000,
            "allowed_vehicles": ["LARGE STRAIGHT"],
            "max_radius_miles": 200,
            # Mirrors client/main copy.py's trucks_payload key-for-key.
            "trucks": [{"vehicle": "LARGE STRAIGHT", "driver_name": "T", "dimensions": "",
                        "max_payload_lbs": None, "equipment": "", "allowed_states": None,
                        "zip_location": "44101", "pickup_date": "", "radius_miles": None,
                        "loaded_miles_min": None, "loaded_miles_max": None}],
            "bid_template": "Rate: $\n{vehicle_type}",
        }
        check("POST /api/parse", c.post("/api/parse", json=parse_payload), ["success", "message"])

        check("POST /api/build_bid", c.post("/api/build_bid", json={**base, "load_data": {
            "order": "999001", "vehicle_required": "LARGE STRAIGHT", "pickup_loc": "Cleveland, OH",
            "delivery_loc": "Columbus, OH", "google_deadhead": 10, "driver_name": "T",
            "truck_type": "LARGE STRAIGHT", "truck_dimensions": "", "deadhead_eta_minutes": 30,
            "truck_equipment": "", "bid_template": "Rate: $\n{vehicle_type}", "price": 500, "rate_per_mile": 2.5,
        }}), ["bid_text"])

        check("POST /api/route_map", c.post("/api/route_map", json={
            **base, "pickup_loc": "Cleveland, OH", "delivery_loc": "Columbus, OH"}))

        r = c.get("/webhook/poll", headers={"X-License-Key": KEY, "X-Machine-Id": MID})
        body = check("GET /webhook/poll", r, ["history_ids"])
        if body is not None and not isinstance(body.get("history_ids"), list):
            failures.append("GET /webhook/poll: history_ids is not a list")

        bid = check("POST /api/record_bid", c.post("/api/record_bid", json={
            **base, "order_id": "999001", "thread_id": "t-compat", "bid_method": "pc",
            "vehicle_type": "LARGE STRAIGHT", "driver_name": "T", "pickup_loc": "Cleveland, OH",
            "delivery_loc": "Columbus, OH", "broker_name": "B", "broker_email": "b@x.com",
        }), ["bid_id"])
        if bid and bid.get("bid_id") is not None:
            check("POST /api/update_bid_amount", c.post("/api/update_bid_amount", json={
                **base, "bid_id": bid["bid_id"], "bid_amount": 500.0}))

        check("POST /api/classify_reply", c.post("/api/classify_reply", json={
            **base, "thread_id": "no-such-thread", "subject": "Re: x", "message_body": "hi"}), ["success"])

        # thread learning: status / enable / status / disable, then backfill (skipped when off)
        check("POST /api/thread_learning/status", c.post("/api/thread_learning/status", json=base), ["enabled"])
        check("POST /api/thread_learning/enable", c.post("/api/thread_learning/enable", json=base))
        check("POST /api/thread_learning/disable", c.post("/api/thread_learning/disable", json=base))
        check("POST /api/backfill_thread", c.post("/api/backfill_thread", json={
            **base, "thread_id": "t-compat", "order_id": None,
            "messages": [{"message_id": "m1", "date_ms": 1790000000000, "is_from_me": False,
                          "subject": "s", "body": "b", "label_ids": []}]}))

        # telegram: status / disable / enable
        check("POST /api/telegram/status", c.post("/api/telegram/status", json=base), ["enabled"])
        check("POST /api/telegram/disable", c.post("/api/telegram/disable", json=base))
        check("POST /api/telegram/enable", c.post("/api/telegram/enable", json=base))

    # cleanup
    for db, table, col in (("licenses.db", "licenses", "key"),):
        conn = sqlite3.connect(os.path.join(HERE, db))
        conn.execute(f"DELETE FROM {table} WHERE {col}=?", (KEY,))
        conn.commit()
        conn.close()
    try:
        import bid_history
        conn = sqlite3.connect(bid_history.DB_PATH)
        conn.execute("DELETE FROM bids WHERE license_key=?", (KEY,))
        conn.commit()
        conn.close()
    except Exception:
        pass

    print()
    if failures:
        print("DESKTOP COMPATIBILITY CHECK FAILED — DO NOT DEPLOY:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print(f"DESKTOP COMPATIBILITY CHECK PASSED ({len(DESKTOP_PATHS)} desktop endpoints exercised).")


if __name__ == "__main__":
    main()
