"""Offline tests: seat/meal pickers, pref parsing, prefs store, dry-run with prefs."""
import asyncio
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["DRY_RUN"] = "true"

from app import store
from app.preferences import (
    effective_prefs,
    parse_preferences_text,
    pick_meal,
    pick_seat,
    sanitize,
)
from app.checkin.adapters import _demo_cabin, _demo_meals, get_adapter


def test_pick_seat_window_front():
    prefs = sanitize({"seat_type": "window", "seat_zone": "front",
                      "max_seat_price_inr": 2000})
    s = pick_seat(_demo_cabin(), prefs)
    assert s is not None
    assert s["code"] in ("5A", "5F", "6A", "6F", "7A", "7F"), s


def test_pick_seat_exact_and_budget():
    prefs = sanitize({"preferred_seat": "14A", "max_seat_price_inr": 2000})
    assert pick_seat(_demo_cabin(), prefs)["code"] == "14A"
    # over budget -> falls back to free seat, not the paid exact one
    prefs = sanitize({"preferred_seat": "5A", "max_seat_price_inr": 0})
    s = pick_seat(_demo_cabin(), prefs)
    assert s is not None and s["price_inr"] == 0, s


def test_pick_seat_free_only_back_aisle():
    prefs = sanitize({"seat_type": "aisle", "seat_zone": "back", "max_seat_price_inr": 0})
    s = pick_seat(_demo_cabin(), prefs)
    assert s is not None and s["price_inr"] == 0
    assert s["code"][-1] in ("C", "D"), s


def test_pick_meal_veg_and_none():
    prefs = sanitize({"meal_pref": "veg", "max_meal_price_inr": 1000})
    m = pick_meal(_demo_meals(), prefs)
    assert m is not None and "veg" in m["name"].lower(), m
    prefs = sanitize({"meal_pref": "none"})
    assert pick_meal(_demo_meals(), prefs) is None
    prefs = sanitize({"meal_pref": "vegan"})
    m = pick_meal(_demo_meals(), prefs)
    assert m is not None and "vegan" in m["name"].lower(), m


def test_pref_text_regex():
    parsed, method = parse_preferences_text("window seat in front, veg paneer meal")
    assert method == "regex"
    assert parsed.get("seat_type") == "window"
    assert parsed.get("seat_zone") == "front"
    assert parsed.get("meal_pref") == "veg"
    assert parsed.get("preferred_meal") == "paneer"


def test_prefs_store_roundtrip():
    store.init_db()
    saved = store.save_preferences({"seat_type": "aisle", "meal_pref": "nonveg"})
    assert saved["seat_type"] == "aisle"
    assert store.get_preferences()["meal_pref"] == "nonveg"


def test_effective_prefs_booking_override():
    import json

    store.init_db()
    store.save_preferences({"seat_type": "window", "meal_pref": "veg"})
    booking = {"seat_pref": json.dumps({"seat_type": "aisle"})}
    eff = effective_prefs(booking, store.get_preferences())
    assert eff["seat_type"] == "aisle" and eff["meal_pref"] == "veg"


def test_dry_run_with_prefs_stops_at_payment():
    store.init_db()
    store.save_preferences({"seat_type": "window", "seat_zone": "front",
                            "meal_pref": "veg", "preferred_meal": "paneer"})
    adapter = get_adapter("6E")
    booking = {"pnr": "PAY123", "flight_no": "6E 100",
               "passenger_first_name": "A", "passenger_last_name": "B",
               "airline": "IndiGo", "origin": "DEL", "destination": "BOM"}
    res = asyncio.run(adapter.run(booking))
    assert res.ok and res.needs_payment, res.message
    assert res.seat in ("5A", "5F", "6A", "6F", "7A", "7F"), res.seat
    assert "paneer" in res.meal.lower(), res.meal
    assert res.payment_amount_inr > 0
