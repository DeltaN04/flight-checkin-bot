"""Offline tests: webhook verify/parse, bot router, owner isolation, owner pings."""
import hashlib
import hmac
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["DRY_RUN"] = "true"

from app import bot, store, whatsapp


def test_verify_token():
    os.environ["META_WA_VERIFY_TOKEN"] = "secret123"
    assert whatsapp.verify_token_ok("secret123")
    assert not whatsapp.verify_token_ok("wrong")


def test_verify_signature():
    os.environ["META_WA_APP_SECRET"] = "appsecret"
    body = b'{"hello":"world"}'
    good = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
    assert whatsapp.verify_signature(body, good)
    assert not whatsapp.verify_signature(body, "sha256=bad")
    del os.environ["META_WA_APP_SECRET"]
    assert whatsapp.verify_signature(body, "")  # skipped when unconfigured


def test_parse_inbound():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919999999999", "type": "text", "text": {"body": "hi"}},
        {"from": "918888888888", "type": "image",
         "image": {"id": "mid1", "mime_type": "image/jpeg"}, "caption": ""},
        {"from": "917777777777", "type": "document",
         "document": {"id": "mid2", "mime_type": "application/pdf", "filename": "ticket.pdf"}},
        {"from": "919999999999", "type": "interactive",
         "interactive": {"button_reply": {"id": "ADD", "title": "Add"}}},
    ]}}]}]}
    msgs = whatsapp.parse_inbound(payload)
    assert [m["kind"] for m in msgs] == ["text", "media", "media", "button"]
    assert msgs[0]["text"] == "hi" and msgs[0]["from"] == "919999999999"
    assert msgs[1]["media_id"] == "mid1" and msgs[2]["filename"] == "ticket.pdf"
    assert msgs[3]["button_id"] == "ADD"


def test_bot_menu_and_help():
    store.init_db()
    assert any("Flight Check-in" in r for r in bot.process_text("919000000001", "hi"))
    assert any("ABC123" in r for r in bot.process_text("919000000001", "help"))
    assert any("one line" in r for r in bot.process_text("919000000001", "add"))
    # session is in "awaiting_ticket" after "add" above, so gibberish gets guidance
    assert any("couldn't read" in r for r in bot.process_text("919000000001", "something random here"))
    assert any("menu" in r for r in bot.process_text("919000000099", "something random here"))


def test_bot_oneliner_and_isolation():
    store.init_db()
    replies = bot.process_text("919000000002", "ABC123 Sharma 2026-12-20 14:35 6E 2345 DEL BOM")
    assert any("Ticket added" in r for r in replies), replies
    mine = bot.process_text("919000000002", "my bookings")[0]
    assert "ABC123" in mine
    other = bot.process_text("919000000003", "my bookings")[0]
    assert "No flights" in other
    assert store.bookings_for_owner("919000000003") == []


def test_bot_oneliner_past_rejected():
    store.init_db()
    replies = bot.process_text("919000000004", "ZZZ999 Sharma 2020-01-01 10:00 6E 100 DEL BOM")
    assert any("departed" in r for r in replies)


def test_bot_prefs_per_owner():
    store.init_db()
    replies = bot.process_text("919000000005", "aisle seat at back, non-veg meal")
    assert any("Taste saved" in r for r in replies), replies
    assert store.get_preferences("919000000005")["seat_type"] == "aisle"
    assert store.get_preferences("919000000006")["seat_type"] != "aisle"  # others unaffected
    assert any("aisle" in r for r in bot.process_text("919000000005", "myprefs"))


def test_bot_paid_flow():
    store.init_db()
    bot.process_text("919000000007", "PAY123 Sharma 2026-12-20 14:35 6E 2345 DEL BOM")
    b = store.bookings_for_owner("919000000007")[0]
    store.update_booking(b["id"], status="awaiting_payment", payment_amount_inr=500)
    replies = bot.process_text("919000000007", "paid PAY123")
    assert any("marked paid" in r for r in replies), replies
    assert store.get_booking(b["id"])["status"] == "done"


def test_bot_upload_txt():
    store.init_db()
    data = (b"IndiGo e-Ticket PNR: TXT789 Passenger: Riya Verma Flight 6E 500 "
            b"Delhi (DEL) -> Goa (GOI) Departure: 20 Dec 2026, 10:00")
    replies = bot.process_upload("919000000008", data, "ticket.txt", "text/plain")
    assert any("Ticket added" in r for r in replies), replies
    assert store.bookings_for_owner("919000000008")[0]["pnr"] == "TXT789"


def test_webhook_verify_and_text(monkeypatch):
    from starlette.testclient import TestClient

    from app.main import app

    os.environ["META_WA_VERIFY_TOKEN"] = "tok123"
    monkeypatch.setenv("FAMILY_PHONES", "")
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=tok123&hub.challenge=CHAL")
    assert r.status_code == 200 and r.text == "CHAL"
    r = c.get("/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=CHAL")
    assert r.status_code == 403

    sent = []
    monkeypatch.setattr(whatsapp, "send_text", lambda to, body: sent.append((to, body)))
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919000000009", "type": "text", "text": {"body": "WAX321 Rao 2026-12-21 09:00 AI 202 DEL BLR"}}]}}]}]}
    r = c.post("/webhook/whatsapp", json=payload)
    assert r.status_code == 200
    assert sent and sent[0][0] == "919000000009"
    assert any("Ticket added" in b for _, b in sent), sent
    assert store.bookings_for_owner("919000000009")[0]["pnr"] == "WAX321"


def test_webhook_allowlist_and_media(monkeypatch):
    from starlette.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("FAMILY_PHONES", "911111111111")
    sent = []
    monkeypatch.setattr(whatsapp, "send_text", lambda to, body: sent.append((to, body)))
    c = TestClient(app, raise_server_exceptions=False)
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919000000010", "type": "text", "text": {"body": "hi"}}]}}]}]}
    assert c.post("/webhook/whatsapp", json=payload).status_code == 200
    assert any("register" in b for _, b in sent)

    monkeypatch.setenv("FAMILY_PHONES", "")
    data = (b"Air India Booking Ref MED456 flight AI 202 from DEL to BLR "
            b"on 2026-12-22 08:20 pax Priya Nair")
    monkeypatch.setattr(whatsapp, "download_media", lambda mid: (data, "text/plain"))
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919000000011", "type": "document",
         "document": {"id": "m1", "mime_type": "text/plain", "filename": "e.txt"}}]}}]}]}
    assert c.post("/webhook/whatsapp", json=payload).status_code == 200
    assert store.bookings_for_owner("919000000011")[0]["pnr"] == "MED456"


def test_plain_checkin_dry_run_skips_seat_meal():
    import asyncio

    from app.checkin.adapters import get_adapter
    from app.preferences import sanitize

    prefs = sanitize({"skip_seat": True, "meal_pref": "none"})
    assert prefs["skip_seat"] is True
    res = asyncio.run(get_adapter("QP").run(
        {"pnr": "PLAIN1", "flight_no": "QP 1526",
         "passenger_first_name": "A", "passenger_last_name": "B",
         "seat_pref": '{"skip_seat": true, "meal_pref": "none"}'}))
    assert res.ok and not res.needs_payment, res.message
    assert res.seat == "" and res.meal == ""


def test_bridge_process_and_upload(monkeypatch):
    import io

    from starlette.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("WA_BRIDGE_SECRET", "s3cr3t")
    monkeypatch.setenv("FAMILY_PHONES", "")
    c = TestClient(app, raise_server_exceptions=False)

    r = c.post("/api/wa/process", json={"owner": "919000000020", "text": "hi"},
               headers={"X-Bridge-Secret": "s3cr3t"})
    assert r.status_code == 200 and "Flight Check-in" in r.json()["replies"][0]

    r = c.post("/api/wa/process", json={"owner": "x", "text": "hi"},
               headers={"X-Bridge-Secret": "wrong"})
    assert r.status_code == 403

    data = (b"IndiGo e-Ticket PNR: BRG555 Passenger: Dev Shah Flight 6E 100 "
            b"Delhi (DEL) -> Mumbai (BOM) Departure: 20 Dec 2026, 10:00")
    r = c.post("/api/wa/upload", data={"owner": "919000000021"},
               files={"file": ("t.txt", io.BytesIO(data), "text/plain")},
               headers={"X-Bridge-Secret": "s3cr3t"})
    assert r.status_code == 200
    assert any("Ticket added" in x for x in r.json()["replies"]), r.json()
    assert store.bookings_for_owner("919000000021")[0]["pnr"] == "BRG555"


def test_bridge_fallback_ping(monkeypatch):
    from app import scheduler

    calls = []
    monkeypatch.setattr("app.whatsapp.notify_owner_whatsapp", lambda o, t, b: "skipped (x)")

    class FakeResp:
        status_code = 200
        text = "sent"

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, **kw: calls.append((url, kw)) or FakeResp())
    monkeypatch.setenv("WA_BRIDGE_URL", "http://127.0.0.1:8002")
    monkeypatch.setenv("WA_BRIDGE_SECRET", "s3cr3t")
    scheduler._ping_owner({"owner_phone": "919000000030", "pnr": "X"}, "T", "B")
    assert calls and calls[0][1]["json"]["to"] == "919000000030", calls


def test_upload_duplicate_returns_existing():
    from starlette.testclient import TestClient

    from app.main import app

    c = TestClient(app, raise_server_exceptions=False)
    form = {"pnr": "DUP001", "last_name": "Sharma",
            "departure_local": "2026-12-20 14:35", "airline_code": "6E"}
    r1 = c.post("/api/bookings/manual", data=form)
    r2 = c.post("/api/bookings/manual", data=form)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json().get("duplicate") is True


def test_browser_upload_redirects_to_dashboard():
    from starlette.testclient import TestClient

    from app.main import app

    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/bookings/manual",
               data={"pnr": "BRW001", "last_name": "Sharma",
                     "departure_local": "2026-12-20 14:35", "airline_code": "6E"},
               headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "BRW001" in r.text  # landed back on the dashboard


def test_bot_duplicate_oneliner():
    store.init_db()
    first = bot.process_text("919000000031", "WADUP1 Rao 2026-12-21 09:00 AI 202 DEL BLR")
    assert any("Ticket added" in r for r in first), first
    second = bot.process_text("919000000031", "WADUP1 Rao 2026-12-21 09:00 AI 202 DEL BLR")
    assert any("already tracking" in r for r in second), second
    assert len([b for b in store.bookings_for_owner("919000000031") if b["pnr"] == "WADUP1"]) == 1


def test_companions_stored_and_listed():
    import json

    from app import scheduler

    store.init_db()
    bid = store.insert_booking({
        "pnr": "FAM222", "airline": "Akasa Air", "airline_code": "QP",
        "flight_no": "QP 1526", "origin": "DXN", "destination": "BLR",
        "departure_utc": "2026-12-20T04:30:00+00:00",
        "passenger_last_name": "MEHTA", "passenger_first_name": "ARJUN",
        "email": "", "phone": "", "source": "test", "raw_text": "x",
        "passengers": [{"first": "ARJUN", "last": "MEHTA"},
                       {"first": "PRIYA", "last": "MEHTA"}],
    })
    b = store.get_booking(bid)
    comp = json.loads(b["companions_json"])
    assert [c["first"] for c in comp] == ["PRIYA"], comp
    assert scheduler._pax_str(b) == "ARJUN MEHTA + PRIYA MEHTA"
    assert "2 pax" in bot.fmt_booking(b)


def test_scheduler_pings_owner(monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone

    from app import scheduler

    store.init_db()
    dep = (datetime.now(timezone.utc) + timedelta(hours=40)).isoformat()
    bid = store.insert_booking({
        "pnr": "OWN777", "airline": "IndiGo", "airline_code": "6E",
        "flight_no": "6E 100", "origin": "DEL", "destination": "BOM",
        "departure_utc": dep, "passenger_last_name": "Test",
        "passenger_first_name": "A", "email": "", "phone": "",
        "source": "test", "raw_text": "x", "owner_phone": "+91 90000 00012",
    })
    pings = []
    monkeypatch.setattr(whatsapp, "notify_owner_whatsapp",
                        lambda owner, title, body: pings.append((owner, title)))
    out = scheduler.run_due_now()
    assert any(r["pnr"] == "OWN777" for r in out["results"]), out
    assert pings and pings[0][0] == "919000000012", pings
    b = store.get_booking(bid)
    assert b["status"] == "awaiting_payment" and b["owner_phone"] == "919000000012"
