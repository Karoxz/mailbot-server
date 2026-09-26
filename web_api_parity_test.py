"""
web_api_parity_test.py — web dashboard API behaviors added for desktop
parity (2026-09-26): desktop-format truck import/export, per-truck driver
chat ID, null-clearing PATCH, desktop-active status on the settings API,
enable == the desktop's START side effects. Run: python web_api_parity_test.py
"""
import os
import sys
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from fastapi.testclient import TestClient
import license_db
import fleet_store
import truck_lines
import main

LK = "WEB-API-PARITY"
PASSED, FAILED = [], []


def test(name):
    def deco(fn):
        try:
            fn()
            PASSED.append(name)
            print(f"  ok    {name}")
        except Exception:
            import traceback
            FAILED.append((name, traceback.format_exc()))
            print(f"  FAIL  {name}")
        return fn
    return deco


license_db.init_db()
license_db.add_license(LK, label="web-api-parity")


def cleanup():
    for path, sql in ((license_db.DB_PATH, "DELETE FROM licenses WHERE key=?"),
                      (fleet_store.DB_PATH, "DELETE FROM trucks WHERE license_key=?")):
        c = sqlite3.connect(path)
        c.execute(sql, (LK,))
        c.commit()
        c.close()


DESKTOP_LINES = "\n".join([
    "large straight:GRISHA:312x102x96:20,000:E-Track:OH,PA:44101:::-1001234567890:1000-2000",
    "SPRINTER:STAS:144x70x75:3500",
    "CARGO VAN:MIKE:120x60x60:2500::Midwest:60601:12/31/2026:150:555000111:500",
])

# ── pure parser: same results as the desktop's parse_truck_definitions ──
@test("desktop truck lines parse exactly like the desktop (fields, regions, chat id sign, loaded-miles range)")
def _():
    t = truck_lines.parse(DESKTOP_LINES)
    assert len(t) == 3
    g = t[0]
    assert g["vehicle"] == "LARGE STRAIGHT" and g["max_payload_lbs"] == 20000
    assert g["allowed_states"] == ["OH", "PA"] and g["zip_location"] == "44101"
    assert g["telegram_chat_id"] == -1001234567890            # negative group chat ID keeps its sign
    assert (g["loaded_miles_min"], g["loaded_miles_max"]) == (1000, 2000)
    m = t[2]
    assert set(m["allowed_states"]) >= {"IL", "OH", "MI", "WI"} and m["pickup_date"] == "12/31/2026"
    assert m["radius_miles"] == 150 and m["telegram_chat_id"] == 555000111
    assert (m["loaded_miles_min"], m["loaded_miles_max"]) == (500, None)     # "500" = 500 and up
    assert t[1]["zip_location"] == "" and t[1]["radius_miles"] is None


@test("validation gives the desktop's exact messages")
def _():
    errs = truck_lines.validate("A:B:C\nX::d:abc\nV:D:1x1x1:100::ZZZ:44101:99/99/99:abc:xyz:1-x")
    joined = "\n".join(errs)
    assert "Line 1: need VEHICLE:DRIVER:DIMS:PAYLOAD (got 3 fields)" in joined
    assert "Line 2: driver name is empty" in joined and "cannot parse payload 'abc'" in joined
    assert "Line 3: cannot expand 'ZZZ'" in joined and "date '99/99/99' must be MM/DD/YYYY or MM/DD/YY" in joined
    assert "cannot parse radius 'abc'" in joined and "chat ID 'xyz' must be a whole number" in joined
    assert "cannot parse loaded miles range '1-x'" in joined
    assert truck_lines.validate(DESKTOP_LINES) == []


@test("to_line is the inverse of parse (round trip)")
def _():
    for t in truck_lines.parse(DESKTOP_LINES):
        again = truck_lines.parse(truck_lines.to_line(t))[0]
        assert again == t, (again, t)


# ── API ──
with TestClient(main.app) as c:
    @test("import: adds trucks (with driver chat ID) and they show up in the fleet; replace=true swaps the fleet")
    def _():
        bad = c.post("/api/web/trucks/import", json={"license_key": LK, "text": DESKTOP_LINES})
        assert bad.status_code == 400 and "ZIP location is required" in bad.json()["detail"]   # STAS has no zip
        good_lines = DESKTOP_LINES.replace("SPRINTER:STAS:144x70x75:3500", "SPRINTER:STAS:144x70x75:3500::TX:75001")
        r = c.post("/api/web/trucks/import", json={"license_key": LK, "text": good_lines})
        assert r.status_code == 200 and r.json()["added"] == 3, r.text
        items = c.get(f"/api/web/trucks?license_key={LK}").json()["items"]
        assert {t["driver_name"] for t in items} == {"GRISHA", "STAS", "MIKE"}
        assert next(t for t in items if t["driver_name"] == "GRISHA")["telegram_chat_id"] == -1001234567890
        r2 = c.post("/api/web/trucks/import", json={"license_key": LK, "replace": True,
                                                    "text": "BOX TRUCK:ONLY:1x1x1:900::OH:44101"})
        assert r2.json() == {"success": True, "added": 1, "removed": 3}
        assert [t["driver_name"] for t in c.get(f"/api/web/trucks?license_key={LK}").json()["items"]] == ["ONLY"]

    @test("export gives desktop-format lines that re-import identically")
    def _():
        c.post("/api/web/trucks/import", json={"license_key": LK, "replace": True,
                                               "text": DESKTOP_LINES.replace(":3500", ":3500::TX:75001")})
        text = c.get(f"/api/web/trucks/export?license_key={LK}").json()["text"]
        assert len(text.splitlines()) == 3
        assert truck_lines.validate(text) == []
        assert {t["driver_name"] for t in truck_lines.parse(text)} == {"GRISHA", "STAS", "MIKE"}

    @test("PATCH with an explicit null CLEARS the field (was silently ignored); unsent fields untouched; required fields can't be blanked")
    def _():
        items = c.get(f"/api/web/trucks?license_key={LK}").json()["items"]
        mike = next(t for t in items if t["driver_name"] == "MIKE")
        assert mike["radius_miles"] == 150 and mike["telegram_chat_id"] == 555000111
        r = c.patch(f"/api/web/trucks/{mike['id']}", json={"license_key": LK, "radius_miles": None,
                                                           "telegram_chat_id": None, "driver_name": ""})
        assert r.status_code == 200
        mike2 = next(t for t in c.get(f"/api/web/trucks?license_key={LK}").json()["items"] if t["id"] == mike["id"])
        assert mike2["radius_miles"] is None and mike2["telegram_chat_id"] is None
        assert mike2["driver_name"] == "MIKE" and mike2["zip_location"] == "60601"

    @test("enable mirrors the desktop's START: Telegram + thread learning ON, catch-up scan re-armed, reports desktop_active")
    def _():
        import gmail_store
        gmail_store.init_db()
        license_db.set_telegram_enabled(LK, False)
        license_db.set_thread_learning_enabled(LK, False)
        license_db.set_standalone_settings(LK, allowed_vehicles="LARGE STRAIGHT", bot_token="1:not-desktop", chat_ids="1")
        gmail_store.save_token(LK, '{"token":"x"}', "t@example.com")
        license_db.set_standalone_initial_scan_done(LK, True)
        r = c.post("/api/web/standalone/enable", json={"license_key": LK})
        assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["desktop_active"] is False
        assert license_db.get_telegram_enabled(LK) and license_db.get_thread_learning_enabled(LK)
        assert license_db.get_standalone_initial_scan_done(LK) is False
        conn = sqlite3.connect(gmail_store.DB_PATH)
        conn.execute("DELETE FROM gmail_credentials WHERE license_key=?", (LK,))
        conn.commit()
        conn.close()

cleanup()
print()
if FAILED:
    print(f"{len(FAILED)} FAILED / {len(PASSED)} passed")
    for n, tb in FAILED:
        print("\n---", n, "\n", tb)
    sys.exit(1)
print(f"ALL {len(PASSED)} WEB API PARITY TESTS PASSED")
