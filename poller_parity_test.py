"""
poller_parity_test.py — proves the web/standalone engine (poller.py)
behaves like the desktop app (client/main copy.py), without a real Gmail
account or Telegram bot: a fake Gmail service + captured Telegram calls.

Each test names the desktop behavior it mirrors. Run:  python poller_parity_test.py
Exit code 0 = all pass. Uses throwaway license keys and cleans up.
"""

import os
import sys
import json
import base64
import sqlite3
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
try:                                   # test names contain emoji; Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httplib2
from googleapiclient.errors import HttpError

import license_db
import activity_log
import fleet_store
import poller
import desktop_parity

license_db.init_db()
activity_log.init_db()
fleet_store.init_db()

LK = "PARITY-TEST-LICENSE"
BOT, CHAT = "999:parity-test-token", 424242
PASSED, FAILED = [], []


# ── fakes ────────────────────────────────────────────────────────────
def _b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode()


def make_msg(mid, subject, body, labels=("INBOX", "UNREAD"), thread="t1", date_ms="1790000000000"):
    return {
        "id": mid, "threadId": thread, "labelIds": list(labels), "internalDate": date_ms,
        "payload": {"headers": [{"name": "Subject", "value": subject}],
                    "mimeType": "text/plain", "body": {"data": _b64(body)}},
    }


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeGmail:
    def __init__(self):
        self.msgs = {}            # id -> message
        self.thread_labels = {}   # thread -> [label ids present anywhere in the thread]
        self.thread_subject = {}
        self.label_names = {"Label_1": "bid", "Label_2": "RC"}
        self.modified = []        # ids marked read
        self.list_queries = []
        self.get_failures = {}    # id -> list of exceptions to raise before succeeding
        self.list_ids = []        # ids returned by list()

    # users().messages()/threads()/labels()
    def users(self):
        return self

    def messages(self):
        g = self

        class M:
            def get(self, userId, id, format, metadataHeaders=None):
                def run():
                    fails = g.get_failures.get(id)
                    if fails:
                        raise fails.pop(0)
                    if id not in g.msgs:
                        raise HttpError(httplib2.Response({"status": 404}), b"nf")
                    return g.msgs[id]
                return _Exec(run)

            def list(self, **kw):
                g.list_queries.append(kw.get("q"))
                return _Exec(lambda: {"messages": [{"id": i} for i in g.list_ids]})

            def modify(self, userId, id, body):
                def run():
                    g.modified.append(id)
                    return {}
                return _Exec(run)
        return M()

    def threads(self):
        g = self

        class T:
            def get(self, userId, id, format, metadataHeaders=None):
                def run():
                    labels = g.thread_labels.get(id, [])
                    return {"messages": [{"labelIds": ["INBOX"] + labels,
                                          "payload": {"headers": [{"name": "Subject",
                                                                   "value": g.thread_subject.get(id, "")}]}}]}
                return _Exec(run)
        return T()

    def labels(self):
        g = self

        class L:
            def list(self, userId):
                return _Exec(lambda: {"labels": [{"id": k, "name": v} for k, v in g.label_names.items()]})
        return L()


class Capture:
    """Replaces poller._tg_api; records every Telegram call."""
    def __init__(self):
        self.calls = []

    def __call__(self, bot_token, method, payload, timeout=10):
        self.calls.append((method, payload))

        class R:
            ok = True
            text = "ok"
        return R()

    def sends(self):
        return [p for m, p in self.calls if m == "sendMessage"]


def fresh_ctx(**over):
    ctx = {"license_key": LK, "allowed_vehicles": ["LARGE STRAIGHT"], "radius_miles": 200,
           "chat_ids": [CHAT], "bot_token": BOT, "telegram_enabled": True}
    ctx.update(over)
    return ctx


def reset_state():
    poller._seen.clear()
    poller._last_labeled_notify.clear()
    poller._last_classify.clear()
    poller._yielding.clear()
    poller._svc_cache.clear()
    poller._label_cache.clear()


OK_RESULT = {"success": True, "message": "ok", "formatted": "LOAD TEXT", "order_id": "555",
             "load_data": {"route_url": "https://maps.example/route"}}


def stub_parse(result=None, record=None):
    def _p(payload):
        if record is not None:
            record.append(payload)
        return dict(result or OK_RESULT)
    poller.parse_email_for_api = _p


def test(name):
    def deco(fn):
        def run():
            reset_state()
            try:
                fn()
                PASSED.append(name)
                print(f"  ok    {name}")
            except Exception as e:
                import traceback
                FAILED.append((name, traceback.format_exc()))
                print(f"  FAIL  {name}: {e}")
        run.__name__ = fn.__name__
        run()          # execute immediately, in file order
        return run
    return deco


tg = Capture()
poller._tg_api = tg
sleeps = []
poller._sleep = lambda s: sleeps.append(s)


def setup(msgs=(), **kw):
    tg.calls.clear()
    sleeps.clear()
    g = FakeGmail()
    for m in msgs:
        g.msgs[m["id"]] = m
    return g


# ── 1. pure helpers (desktop: _strip_quoted_reply / _extract_state_codes / freight) ──
@test("strip_quoted_reply: cuts at 'On ... wrote:', '>' and Outlook markers; fresh posting unchanged")
def _():
    assert desktop_parity.strip_quoted_reply("hello\nOn Mon, X <a@b.c> wrote:\n> old") == "hello"
    assert desktop_parity.strip_quoted_reply("new\n> quoted") == "new"
    assert desktop_parity.strip_quoted_reply("x\n-----Original Message-----\ny") == "x"
    assert desktop_parity.strip_quoted_reply("Bid on Order #1\nVehicle: LARGE STRAIGHT") == \
        "Bid on Order #1\nVehicle: LARGE STRAIGHT"


@test("reply/freight subject detection matches the desktop (Re:/FW:/FWD: are never fresh freight)")
def _():
    assert desktop_parity.is_freight_subject("Bid on Order #5 LARGE STRAIGHT")
    assert not desktop_parity.is_freight_subject("Re: Bid on Order #5 LARGE STRAIGHT")
    assert not desktop_parity.is_freight_subject("FWD: LARGE STRAIGHT")
    assert not desktop_parity.is_freight_subject("Lunch tomorrow?")


@test("labeled-thread ping text is byte-identical to the desktop's")
def _():
    assert desktop_parity.labeled_thread_message(["bid"], "Re: load from OH to GA") == \
        "\n📌 Label:  bid\n📍 States: OH · GA"
    assert desktop_parity.labeled_thread_message(["bid", "RC"], "no states here") == \
        "\n📌 Label:  bid, RC\n📍 States: —"


@test("load message buttons match the desktop: row1 BID PC|PHONE|DRAFT, row2 ROUTE")
def _():
    kb = poller._load_keyboard("555", "https://r")
    assert [b["text"] for b in kb[0]] == ["💵 BID PC", "💵 BID PHONE", "📋 DRAFT"]
    assert [b["callback_data"] for b in kb[0]] == ["bid:555", "phone:555", "text:555"]
    assert kb[1] == [{"text": "🚩ROUTE🚩", "url": "https://r"}]
    assert poller._load_keyboard("555", None) == [kb[0]]


# ── 2. per-message pipeline ────────────────────────────────────────────
@test("fresh freight email: parsed (quoted part stripped), sent with buttons, marked read")
def _():
    rec = []
    stub_parse(record=rec)
    body = "Bid on Order #555\nPickup: A\nOn Mon, Broker wrote:\n> old stuff"
    g = setup([make_msg("m1", "Bid on Order #555 LARGE STRAIGHT", body)])
    out = poller._process_message(fresh_ctx(), g, {}, "m1")
    assert out == "sent"
    assert rec[0]["email_body"] == "Bid on Order #555\nPickup: A"       # quoted history stripped
    assert rec[0]["thread_id"] == "t1" and rec[0]["message_id"] == "m1"
    sent = tg.sends()
    assert len(sent) == 1 and sent[0]["text"] == "LOAD TEXT" and sent[0]["chat_id"] == CHAT
    assert "reply_markup" in sent[0] and "bid:555" in sent[0]["reply_markup"]
    assert g.modified == ["m1"]


@test("message with a custom label is skipped silently (no parse, no send, left unread)")
def _():
    stub_parse()
    g = setup([make_msg("m1", "Bid on Order #1 LARGE STRAIGHT", "x", labels=("INBOX", "UNREAD", "Label_1"))])
    assert poller._process_message(fresh_ctx(), g, {}, "m1") == "labeled"
    assert not tg.sends() and not g.modified


@test("broker reply in a labeled thread: label ping w/ REPLY BID button, never parsed, left unread, 5-min cooldown")
def _():
    rec = []
    stub_parse(record=rec)
    g = setup([make_msg("m1", "Re: LARGE STRAIGHT OH to GA", "sure", thread="tt"),
               make_msg("m2", "Re: LARGE STRAIGHT OH to GA", "again", thread="tt")])
    g.thread_labels["tt"] = ["Label_1"]
    g.thread_subject["tt"] = "LARGE STRAIGHT OH to GA"
    lm = {"Label_1": "bid"}
    assert poller._process_message(fresh_ctx(), g, lm, "m1") == "labeled_thread"
    assert not rec and not g.modified
    sent = tg.sends()
    assert len(sent) == 1 and sent[0]["text"] == "\n📌 Label:  bid\n📍 States: OH · GA"
    assert "REPLY BID" in sent[0]["reply_markup"] and "#all/tt" in sent[0]["reply_markup"]
    assert poller._process_message(fresh_ctx(), g, lm, "m2") == "labeled_thread"
    assert len(tg.sends()) == 1, "cooldown should suppress the second ping"


@test("freight email inside a labeled thread: parsed+sent, then left as-is (unread) with the ping (desktop _safe_mark_read)")
def _():
    stub_parse()
    g = setup([make_msg("m1", "Bid on Order #555 LARGE STRAIGHT", "x", thread="tt")])
    g.thread_labels["tt"] = ["Label_2"]
    out = poller._process_message(fresh_ctx(), g, {"Label_2": "RC"}, "m1")
    assert out == "sent" and not g.modified
    texts = [s["text"] for s in tg.sends()]
    assert "LOAD TEXT" in texts and any("📌 Label:  RC" in t for t in texts)


@test("classify runs in the background once per thread (5-min cooldown), on the FULL unstripped body")
def _():
    stub_parse()
    calls = []
    import reply_handler
    reply_handler.classify_and_record = lambda lk, th, subj, body: calls.append((lk, th, subj, body)) or {"matched": False}
    g = setup([make_msg("m1", "Bid on Order #1 LARGE STRAIGHT", "new\nOn Mon, X wrote:\n> old", thread="tc"),
               make_msg("m2", "Bid on Order #2 LARGE STRAIGHT", "again", thread="tc")])
    poller._process_message(fresh_ctx(), g, {}, "m1")
    for t in __import__("threading").enumerate():
        if t.name == "classify":
            t.join(2)
    poller._process_message(fresh_ctx(), g, {}, "m2")
    for t in __import__("threading").enumerate():
        if t.name == "classify":
            t.join(2)
    assert len(calls) == 1, calls
    assert calls[0][3] == "new\nOn Mon, X wrote:\n> old"   # classify sees the full body, parse sees it stripped


@test("Telegram OFF: load still parsed/stored and marked read, nothing sent")
def _():
    stub_parse()
    g = setup([make_msg("m1", "Bid on Order #555 LARGE STRAIGHT", "x")])
    out = poller._process_message(fresh_ctx(telegram_enabled=False), g, {}, "m1")
    assert out == "telegram_off" and not tg.sends() and g.modified == ["m1"]


@test("non-matching freight email: skipped with a reason, still marked read (no re-check every cycle)")
def _():
    stub_parse({"success": False, "message": "NO TRUCK IN RANGE", "formatted": None, "order_id": "9"})
    g = setup([make_msg("m1", "Bid on Order #9 LARGE STRAIGHT", "x")])
    assert poller._process_message(fresh_ctx(), g, {}, "m1") == "skipped"
    assert not tg.sends() and g.modified == ["m1"]


@test("message deleted between list and fetch (404) is ignored")
def _():
    g = setup()
    assert poller._process_message(fresh_ctx(), g, {}, "nope") == "gone"


# ── 3. reliability ─────────────────────────────────────────────────────
@test("Gmail 429 is retried with 2s/4s backoff then succeeds (desktop retry policy)")
def _():
    stub_parse()
    g = setup([make_msg("m1", "Bid on Order #555 LARGE STRAIGHT", "x")])
    e429 = HttpError(httplib2.Response({"status": 429}), b"rate")
    g.get_failures["m1"] = [e429, e429]
    assert poller._process_with_retry(fresh_ctx(), g, {}, "m1") == "sent"
    assert sleeps == [2, 4]
    assert poller._is_seen(LK, "m1")


@test("persistent failure gives up after 3 retries (2/4/8s) and is not re-fetched forever")
def _():
    stub_parse()
    g = setup([make_msg("m1", "Bid on Order #555 LARGE STRAIGHT", "x")])
    e429 = HttpError(httplib2.Response({"status": 429}), b"rate")
    g.get_failures["m1"] = [e429, e429, e429, e429, e429]
    assert poller._process_with_retry(fresh_ctx(), g, {}, "m1") == "error"
    assert sleeps == [2, 4, 8]
    assert poller._is_seen(LK, "m1")


@test("non-transient error is not retried and the message is marked handled")
def _():
    stub_parse()
    g = setup([make_msg("m1", "Bid on Order #555 LARGE STRAIGHT", "x")])
    g.get_failures["m1"] = [ValueError("boom")]
    assert poller._process_with_retry(fresh_ctx(), g, {}, "m1") == "error"
    assert sleeps == [] and poller._is_seen(LK, "m1")


# ── 4. cycle-level: yield to desktop, one-time catch-up scan, dedup ─────
def enable_license(vehicles="LARGE STRAIGHT"):
    license_db.add_license(LK, label="parity-test")
    license_db.set_standalone_settings(LK, allowed_vehicles=vehicles, chat_ids=str(CHAT), bot_token=BOT,
                                        max_radius_miles=200)
    license_db.set_standalone_mode_enabled(LK, True)
    license_db.set_standalone_initial_scan_done(LK, False)


def cleanup_license():
    conn = sqlite3.connect(license_db.DB_PATH)
    conn.execute("DELETE FROM licenses WHERE key=?", (LK,))
    conn.commit()
    conn.close()
    conn = sqlite3.connect(activity_log.DB_PATH)
    conn.execute("DELETE FROM events WHERE license_key=?", (LK,))
    conn.commit()
    conn.close()


def events(kind=None):
    evs = activity_log.get_recent_events(LK, limit=200)
    return [e for e in evs if kind is None or e["event_type"] == kind]


@test("web bot yields the whole cycle (zero Gmail calls) while the desktop is polling, resumes when it stops")
def _():
    enable_license()
    try:
        built = []
        poller.gmail_client.build_service = lambda lk: built.append(lk) or setup()
        license_db.record_desktop_poll_heartbeat(LK)
        poller.run_one_license_cycle(LK)
        poller.run_one_license_cycle(LK)
        assert built == [], "must not touch Gmail while the desktop is active"
        assert len(events("web_paused")) == 1, "pause should be logged once, not every cycle"
        stale = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)).isoformat()
        conn = sqlite3.connect(license_db.DB_PATH)
        conn.execute("UPDATE licenses SET desktop_poll_heartbeat=? WHERE key=?", (stale, LK))
        conn.commit()
        conn.close()
        poller.run_one_license_cycle(LK)
        assert built == [LK] and len(events("web_resumed")) == 1
    finally:
        cleanup_license()


@test("enable => 'Watching' message + ONE 2-day catch-up scan; later cycles only use the 1h window; nothing reprocessed")
def _():
    enable_license()
    try:
        stub_parse()
        g = setup([make_msg("m1", "Bid on Order #555 LARGE STRAIGHT", "x")])
        g.list_ids = ["m1"]
        poller.gmail_client.build_service = lambda lk: g
        poller.run_one_license_cycle(LK)
        texts = [s["text"] for s in tg.sends()]
        assert texts[0].startswith("✅ Watching: LARGE STRAIGHT\nWindow: 2d"), texts
        assert "LOAD TEXT" in texts
        assert "newer_than:2d" in g.list_queries[0]
        assert license_db.get_standalone_initial_scan_done(LK)
        n_sends = len(tg.sends())
        poller.run_one_license_cycle(LK)              # m1 already handled
        assert "newer_than:1h" in g.list_queries[-1] and "newer_than:2d" not in g.list_queries[-1]
        assert len(tg.sends()) == n_sends, "an already-handled message must not be re-sent"
        assert g.modified.count("m1") == 1
    finally:
        cleanup_license()


@test("Gmail auth failure skips the license quietly instead of crashing the loop")
def _():
    enable_license()
    try:
        def boom(lk):
            raise poller.GmailAuthError("token revoked")
        poller.gmail_client.build_service = boom
        poller.run_one_license_cycle(LK)   # must not raise
    finally:
        cleanup_license()


# ── 5. Telegram callbacks ──────────────────────────────────────────────
import load_store
import bid_history
import bid_actions
import gmail_client
load_store.init_db()
bid_history.init_db()
poller.WEB_BASE_URL = "https://plutus.example"
os.environ["MAP_TOKEN_SECRET"] = "parity-test-secret"


def seed_load(order_id, all_trucks=None, mid="mid9", **extra):
    data = {"order": order_id, "vehicle_required": "LARGE STRAIGHT", "pickup_loc": "Cleveland, OH",
            "delivery_loc": "Columbus, OH", "pickup_dt": "", "delivery_dt": "", "google_deadhead": 10,
            "driver_name": "GRISHA", "truck_type": "LARGE STRAIGHT", "truck_dimensions": "312x102x96",
            "deadhead_eta_minutes": 30, "truck_equipment": "", "broker_name": "B", "broker_email": "b@x.com",
            "loaded_miles": 140, "total_miles": 150,
            "bid_template": "Truck {driver_name} is {google_deadhead} miles out",
            "original_msg_full": {"threadId": "th9", "id": mid, "payload": {"headers": []}, "labelIds": []},
            "all_trucks": all_trucks or []}
    data.update(extra)
    load_store.put_load(LK, order_id, data)


TRUCKS2 = [{"driver_name": "T1", "google_deadhead": 12, "truck_type": "LARGE STRAIGHT",
            "truck_dimensions": "d1", "truck_equipment": "", "deadhead_eta_minutes": 20},
           {"driver_name": "T2", "google_deadhead": 33, "truck_type": "SMALL STRAIGHT",
            "truck_dimensions": "d2", "truck_equipment": "", "deadhead_eta_minutes": 50}]


def cleanup_loads():
    for path, sql in ((load_store.DB_PATH, "DELETE FROM loads WHERE license_key=?"),
                      (bid_history.DB_PATH, "DELETE FROM bids WHERE license_key=?")):
        c = sqlite3.connect(path)
        c.execute(sql, (LK,))
        c.commit()
        c.close()


def press(data, chat=CHAT):
    tg.calls.clear()
    poller._handle_callback_query(BOT, {"id": "cb", "data": data, "message": {"chat": {"id": chat}}}, LK)
    return tg.sends()


def gmail_with_original():
    g = FakeGmail()
    g.msgs["mid9"] = make_msg("mid9", "Bid on Order #777 LARGE STRAIGHT", "x", thread="th9")
    g.msgs["mid9"]["payload"]["headers"] += [
        {"name": "From", "value": "Broker Bob <bob@broker.com>"},
        {"name": "Message-ID", "value": "<orig123@broker.com>"},
        {"name": "References", "value": "<older@broker.com>"}]
    poller.gmail_client.build_service = lambda lk: g
    return g


@test("BID PC: single truck -> price-page link (plain text, no button); unknown order -> toast")
def _():
    seed_load("555")
    try:
        sent = press("bid:555")
        assert len(sent) == 1 and "bid_price.html?t=" in sent[0]["text"] and "reply_markup" not in sent[0]
        assert "&truck=" not in sent[0]["text"]
        tg.calls.clear()
        poller._handle_callback_query(BOT, {"id": "c2", "data": "bid:404", "message": {"chat": {"id": CHAT}}}, LK)
        ans = [p for m, p in tg.calls if m == "answerCallbackQuery"]
        assert ans and "not found" in ans[0].get("text", "").lower()
    finally:
        cleanup_loads()


@test("several matched trucks: 'Select driver' prompt with the desktop's exact button text per action")
def _():
    seed_load("777", TRUCKS2)
    try:
        s = press("phone:777")[0]
        assert s["text"] == "👤 Select driver for Order #777 (Phone):"
        kb = json.loads(s["reply_markup"])["inline_keyboard"]
        assert kb == [[{"text": "📱 T1  —  12 mi out", "callback_data": "phone:777:0"}],
                      [{"text": "📱 T2  —  33 mi out", "callback_data": "phone:777:1"}]]
        assert press("bid:777")[0]["text"] == "👤 Select driver for Order #777:"
        assert press("text:777")[0]["text"] == "👤 Select driver for Order #777 (Draft):"
    finally:
        cleanup_loads()


@test("BID PC after choosing a driver: price link carries the truck index and driver name")
def _():
    seed_load("777", TRUCKS2)
    try:
        t = press("bid:777:1")[0]["text"]
        assert "— T2" in t and "&truck=1" in t and "bid_price.html?t=" in t
    finally:
        cleanup_loads()


@test("BID PHONE: sends bid text, creates a REAL threaded Gmail draft, records the phone bid for THAT truck, Open Draft button")
def _():
    seed_load("777", TRUCKS2)
    g = gmail_with_original()
    drafts = []
    g.drafts = lambda: type("D", (), {"create": lambda self, userId, body: _Exec(
        lambda: drafts.append(body) or {"id": "draft123"})})()
    try:
        sent = press("phone:777:1")
        assert sent[0]["text"] == "Truck T2 is 33 miles out"           # body built from the chosen truck
        raw = base64.urlsafe_b64decode(drafts[0]["message"]["raw"]).decode()
        assert "To: bob@broker.com" in raw and "Subject: Re: Bid on Order #777 LARGE STRAIGHT" in raw
        assert "In-Reply-To: <orig123@broker.com>" in raw and "<older@broker.com> <orig123@broker.com>" in raw
        assert drafts[0]["message"]["threadId"] == "th9"
        assert sent[1]["text"] == "✅ Draft created for T2 — Order #777\nTap below → opens Gmail draft ready to send:"
        assert "#drafts/draft123" in sent[1]["reply_markup"] and "Open Draft & Send" in sent[1]["reply_markup"]
        bids = bid_history.get_bids_for_order(LK, "777")
        assert len(bids) == 1 and bids[0]["bid_method"] == "phone" and bids[0]["driver_name"] == "T2"
        assert bids[0]["deadhead_miles"] == 33 and bids[0]["vehicle_type"] == "SMALL STRAIGHT"
    finally:
        cleanup_loads()


@test("BID PHONE single truck uses the single-driver wording; failure sends '❌ Failed to create draft'")
def _():
    seed_load("778")
    g = gmail_with_original()
    g.drafts = lambda: type("D", (), {"create": lambda self, userId, body: _Exec(lambda: {"id": "d1"})})()
    try:
        sent = press("phone:778")
        assert sent[1]["text"] == "✅ Draft created for Order #778\nTap below → opens Gmail draft ready to send:"
        seed_load("779", mid="")            # no original message -> cannot draft
        sent = press("phone:779")
        assert sent[-1]["text"].startswith("❌ Failed to create draft:"), sent[-1]["text"]
        assert not bid_history.get_bids_for_order(LK, "779"), "no bid is recorded when the draft fails"
    finally:
        cleanup_loads()


@test("DRAFT: '📋 ORDER #X:' (single) / '📋 ORDER #X — driver:' (chosen truck) + bid recorded as 'draft'")
def _():
    seed_load("778")
    seed_load("777", TRUCKS2)
    try:
        assert press("text:778")[0]["text"] == "📋 ORDER #778:\n\nTruck GRISHA is 10 miles out"
        assert press("text:777:1")[0]["text"] == "📋 ORDER #777 — T2:\n\nTruck T2 is 33 miles out"
        assert bid_history.get_bids_for_order(LK, "777")[0]["bid_method"] == "draft"
    finally:
        cleanup_loads()


@test("bid_actions without a truck is unchanged (records the load's own driver/deadhead)")
def _():
    seed_load("780")
    try:
        r = bid_actions.record_bid_and_build_text(LK, "780", "pc", 500.0, 3.33)
        assert r["bid_text"] == "Truck GRISHA is 10 miles out"
        b = bid_history.get_bids_for_order(LK, "780")[0]
        assert b["driver_name"] == "GRISHA" and b["deadhead_miles"] == 10 and b["bid_amount"] == 500.0
    finally:
        cleanup_loads()


@test("price page: ?truck= overrides driver/deadhead/total miles, records THAT truck, sends the desktop's confirmation note")
def _():
    from fastapi.testclient import TestClient
    import main
    enable_license()
    seed_load("777", TRUCKS2)
    notes = []
    main.tg_notify.send_to_license = lambda lk, text, keyboard=None: notes.append((lk, text)) or 1
    try:
        with TestClient(main.app) as c:
            ctx = c.get(f"/api/web/bid_price/context?license_key={LK}&order_id=777&truck=1").json()["order"]
            assert ctx["driver_name"] == "T2" and ctx["google_deadhead"] == 33 and ctx["total_miles"] == 173
            base = c.get(f"/api/web/bid_price/context?license_key={LK}&order_id=777").json()["order"]
            assert base["driver_name"] == "GRISHA" and base["total_miles"] == 150      # no truck: unchanged
            r = c.post("/api/web/bid_price/submit", json={"license_key": LK, "order_id": "777",
                                                          "truck": "1", "price": 692, "rate_per_mile": 4.0})
            assert r.status_code == 200 and r.json()["bid_text"] == "Truck T2 is 33 miles out"
            b = bid_history.get_bids_for_order(LK, "777")[0]
            assert b["driver_name"] == "T2" and b["bid_amount"] == 692 and b["bid_method"] == "pc"
            assert notes[-1] == (LK, "📋 Bid for T2 copied — $692 ($4.00/mi). Press Reply and paste (Ctrl+V).")
            c.post("/api/web/bid_price/submit", json={"license_key": LK, "order_id": "777", "price": 500})
            assert notes[-1][1] == "📋 Bid text copied — $500. Press Reply and paste (Ctrl+V)."
    finally:
        cleanup_loads()
        cleanup_license()


# ── run ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED / {len(PASSED)} passed")
        for name, tb in FAILED:
            print("\n---", name, "\n", tb)
        sys.exit(1)
    print(f"ALL {len(PASSED)} PARITY TESTS PASSED")
