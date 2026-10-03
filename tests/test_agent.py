"""Offline tests: regex parser + store + dry-run check-in. No network/browser needed."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["DRY_RUN"] = "true"

from app import store
from app.parser_llm import regex_parse
from app.checkin.adapters import get_adapter
import asyncio


def test_regex_indigo():
    text = """IndiGo e-Ticket
    PNR: ABC123
    Passenger: Rahul Sharma
    Flight 6E 2345
    Delhi (DEL) → Mumbai (BOM)
    Departure: 05 Oct 2026, 14:35
    Email: rahul@example.com"""
    b = regex_parse(text)
    assert b["pnr"] == "ABC123", b
    assert b["airline_code"] == "6E", b
    assert b["flight_no"] == "6E 2345", b
    assert b["origin"] == "DEL" and b["destination"] == "BOM", b
    assert "2026-10-05" in b["departure_utc"], b
    assert b["passenger_last_name"] == "Sharma", b


def test_regex_akasa_split_datetime():
    """Akasa table layout: date and time on separate rows, decoy booking date,
    (DXN-BLR) route, PASSENGER DETAILS section with bare caps names."""
    text = """Booking Confirmation Noida-Bengaluru One ARJUN MEHTA
    Mobile: +99******44 08 Oct, 2026 Passenger(s)
    BOOKING DETAILS
    Booking Reference/PNR Booking Status Date of Booking Payment Status Promo Code
    QW7X2K Confirmed 28 Jul 2026 Paid
    FLIGHT DETAILS Baggage
    Date Flight From To Stops Departs Arrives Allowance (Per
    Noida 0 16:30 19:20 1 pc - 15 kgs Terminal Bengaluru
    08 Oct 2026 QP 1526 Noida 0 16:30 19:20 1 pc - 15 kgs Terminal
    Check-in counters close 60 minutes prior to departure
    PASSENGER DETAILS
    QP 1526 (DXN-BLR), 08 Oct Corporate
    ARJUN MEHTA PRIYA MEHTA
    Seat 2E, Hyderabadi Veg Biryani Seat 2F, MOM Poha
    Payment date 28 Jul 2026
    Visit : akasaair.com"""
    from app.parser_llm import regex_parse

    b = regex_parse(text)
    assert b["pnr"] == "QW7X2K", b
    assert b["airline_code"] == "QP", b
    assert b["flight_no"] == "QP 1526", b
    assert (b["origin"], b["destination"]) == ("DXN", "BLR"), b
    assert b["departure_utc"].startswith("2026-10-08T11:00"), b  # 16:30 IST
    assert b["passenger_last_name"] == "MEHTA", b
    assert [p["first"] for p in b["passengers"]] == ["ARJUN", "PRIYA"], b


def test_regex_airindia():
    text = "Air India Booking Ref X7Y9Z2 flight AI 202 from DEL to BLR on 2026-10-06 08:20 pax Priya Nair"
    b = regex_parse(text)
    assert b["pnr"] == "X7Y9Z2", b


def test_store_roundtrip():
    store.init_db()
    bid = store.insert_booking({
        "pnr": "TST123", "airline": "IndiGo", "airline_code": "6E",
        "flight_no": "6E 100", "origin": "DEL", "destination": "BOM",
        "departure_utc": "2026-10-05T09:05:00+00:00",
        "passenger_last_name": "Test", "passenger_first_name": "A",
        "email": "", "phone": "", "source": "test", "raw_text": "x",
    })
    assert store.get_booking(bid)["pnr"] == "TST123"


def test_dry_run_adapter():
    store.init_db()
    adapter = get_adapter("6E")
    booking = {"pnr": "DRY123", "flight_no": "6E 100",
               "passenger_first_name": "A", "passenger_last_name": "B"}
    res = asyncio.run(adapter.run(booking))
    assert res.ok, res.message
