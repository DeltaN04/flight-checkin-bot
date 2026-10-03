"""WhatsApp bot conversation logic — transport-free, fully testable offline.

Owns: command routing, one-liner PNR entry, ticket-file ingestion, per-member
preferences, booking status/paid flows. WhatsApp transport (Meta Cloud API)
lives in app/whatsapp.py; FastAPI wiring in app/main.py.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import store

IST = ZoneInfo("Asia/Kolkata")

WELCOME = (
    "✈️ *Flight Check-in Bot*\n"
    "I auto check-in your flights 48 hrs before departure — seat + meal per your taste, "
    "then send you the payment link.\n\n"
    "• Send your *e-ticket PDF/photo* — or tap ➕ Add ticket\n"
    "• Type *my bookings* for status\n"
    "• Type your taste e.g. *window seat in front, veg meal*\n"
    "• Type *help* for everything I understand"
)

HELP = (
    "🤖 *What I understand*\n"
    "• *Send ticket* — e-ticket PDF or screenshot, I schedule check-in\n"
    "• *Add* — `ABC123 Sharma 2026-12-20 14:35 6E 2345 DEL BOM`\n"
    "  (PNR, last name, date, time IST, airline, flight, from, to — last 4 optional)\n"
    "• *my bookings* — your flights + status + pay links\n"
    "• *paid ABC123* — mark payment done after you pay\n"
    "• *myprefs* — show your seat/meal taste\n"
    "• *window seat in front, veg meal* — set taste in plain words\n"
    "• *cancel* — stop what I asked you for"
)

ADD_INSTRUCTIONS = (
    "➕ *Add a ticket* — pick one:\n"
    "1. *Send the e-ticket PDF* (or a clear screenshot) right here, or\n"
    "2. Type one line:\n"
    "`PNR LASTNAME YYYY-MM-DD HH:MM [CODE] [FLIGHT] [FROM TO]`\n"
    "Example: `ABC123 Sharma 2026-12-20 14:35 6E 2345 DEL BOM`\n"
    "Time is IST. Type *cancel* to stop."
)

STATUS_EMOJI = {
    "scheduled": "🕐", "retry": "🔁", "in_progress": "⏳",
    "awaiting_payment": "💳", "done": "✅",
}


def fmt_booking(b: dict) -> str:
    dep = b.get("departure_utc", "")
    try:
        dt = datetime.fromisoformat(dep).astimezone(IST)
        dep_s = dt.strftime("%d %b %H:%M IST")
    except Exception:
        dep_s = dep
    route = f"{b.get('origin','')}→{b.get('destination','')}" if b.get("origin") else ""
    line = (f"{STATUS_EMOJI.get(b.get('status',''), '•')} *{b.get('pnr','')}* · "
            f"{b.get('flight_no') or b.get('airline_code','')} {route} · {dep_s} · "
            f"{b.get('status','')}")
    if b.get("status") == "awaiting_payment" and b.get("payment_amount_inr"):
        line += f" · Pay ₹{b['payment_amount_inr']}"
    extras = []
    if b.get("seat"):
        extras.append(f"seat {b['seat']}")
    if b.get("meal"):
        extras.append(b['meal'])
    if extras:
        line += f" ({', '.join(extras)})"
    try:
        import json as _json

        comp = _json.loads(b.get("companions_json", "") or "[]")
        if comp:
            line += f" · {len(comp) + 1} pax"
    except Exception:
        pass
    return line


def fmt_prefs(p: dict) -> str:
    seat = f"{p.get('seat_type','any')} seat, {p.get('seat_zone','any')}"
    if p.get("preferred_seat"):
        seat = f"seat {p['preferred_seat']}"
    seat += f" (max ₹{p.get('max_seat_price_inr',0)})"
    meal = p.get("meal_pref", "any")
    if p.get("preferred_meal"):
        meal += f" ({p['preferred_meal']})"
    meal += f" (max ₹{p.get('max_meal_price_inr',0)})"
    return f"🪑 {seat}\n🍱 {meal}"


def parse_oneliner(text: str) -> dict | None:
    """`ABC123 Sharma [Rahul] 2026-12-20 14:35 [6E] [6E 2345] [DEL BOM]` → booking dict."""
    m = re.match(
        r"^\s*([A-Z0-9]{6})\s+([A-Za-z]+)(?:\s+([A-Za-z]+))?\s+"
        r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s*(.*)$",
        text.strip(), re.I,
    )
    if not m:
        return None
    pnr, name1, name2, date_s, time_s, tail = m.groups()
    if name2 and re.match(r"^\d", date_s):
        last_name, first_name = name1, name2
    else:
        last_name, first_name = name1, (name2 or "")
    try:
        dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")
        dep_utc = dt.replace(tzinfo=IST).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None
    code, flight_no, origin, dest = "", "", "", ""
    toks = tail.strip().split()
    airports = [t.upper() for t in toks if re.fullmatch(r"[A-Z]{3}", t, re.I)]
    if len(airports) >= 2:
        origin, dest = airports[0], airports[1]
    rest = [t for t in toks if not re.fullmatch(r"[A-Z]{3}", t, re.I)]
    joined = " ".join(rest)
    mf = re.search(r"\b([A-Z0-9]{2})\s?(\d{3,4})\b", joined, re.I)
    if mf:
        code, flight_no = mf.group(1).upper(), f"{mf.group(1).upper()} {mf.group(2)}"
    else:
        mc = re.search(r"\b([A-Z]{2}|6E)\b", joined, re.I)
        if mc:
            code = mc.group(1).upper()
    from .parser_llm import AIRLINE_MAP

    airline = AIRLINE_MAP.get(code, (code or "Unknown", code))[0]
    return {
        "pnr": pnr.upper(), "airline": airline, "airline_code": code,
        "flight_no": flight_no, "origin": origin, "destination": dest,
        "departure_utc": dep_utc, "passenger_last_name": last_name,
        "passenger_first_name": first_name, "email": "", "phone": "",
        "source": "whatsapp:oneline", "raw_text": text.strip()[:2000],
    }


def _departure_in_past(dep_utc: str) -> bool:
    try:
        return datetime.fromisoformat(dep_utc) < datetime.now(timezone.utc)
    except Exception:
        return False


# ------------------------------------------------------------ text router

def process_text(owner: str, text: str) -> list[str]:
    owner = store.normalize_phone(owner)
    t = (text or "").strip()
    low = t.lower()
    session = store.get_session(owner)

    if low in ("hi", "hello", "hey", "hii+", "hii", "namaste", "start", "menu", "/start"):
        store.clear_session(owner)
        return [WELCOME]
    if low in ("help", "/help", "commands"):
        return [HELP]
    if low in ("cancel", "stop", "quit"):
        store.clear_session(owner)
        return ["Cancelled. Type *menu* anytime."]
    if low in ("add", "new", "add ticket", "new ticket", "checkin", "check in", "check-in", "book", "ticket"):
        store.set_session(owner, "awaiting_ticket")
        return [ADD_INSTRUCTIONS]
    if low in ("status", "my bookings", "my booking", "bookings", "my flights", "flights", "list"):
        return [_my_bookings(owner)]
    if low in ("myprefs", "my prefs", "preferences", "prefs", "taste"):
        return ["⭐ *Your taste*\n" + fmt_prefs(store.get_preferences(owner)) +
                "\n\nChange it in plain words, e.g. *aisle seat at back, non-veg meal*."]
    if low.startswith("paid"):
        return _mark_paid(owner, t)

    one = parse_oneliner(t)
    if one:
        if _departure_in_past(one["departure_utc"]):
            return ["⚠️ That flight looks like it's already departed. Check the date and resend."]
        dup = store.find_active_booking(one["pnr"], owner)
        if dup:
            store.clear_session(owner)
            return ["ℹ️ I'm already tracking this one:\n" + fmt_booking(dup)]
        one["owner_phone"] = owner
        bid = store.insert_booking(one)
        store.clear_session(owner)
        b = store.get_booking(bid)
        return ["✅ *Ticket added!* I'll check you in 48 hrs before departure.\n" + fmt_booking(b)]

    from .preferences import parse_preferences_text

    parsed, _method = parse_preferences_text(t)
    if parsed:
        current = store.get_preferences(owner)
        current.update(parsed)
        from .preferences import sanitize

        saved = store.save_preferences(sanitize(current), owner)
        return ["✅ *Taste saved!*\n" + fmt_prefs(saved)]

    if session.get("state") == "awaiting_ticket":
        return ["I couldn't read that. " + ADD_INSTRUCTIONS]
    return ["Hmm, I didn't get that. Type *menu* to see what I can do — or just send your e-ticket PDF."]


def _my_bookings(owner: str) -> str:
    books = store.bookings_for_owner(owner)
    if not books:
        return ("📋 No flights yet. Send your *e-ticket PDF* or type *add* "
                "to add one (`ABC123 Sharma 2026-12-20 14:35 6E 2345 DEL BOM`).")
    lines = ["📋 *Your flights*"]
    for b in books:
        lines.append(fmt_booking(b))
        if b.get("status") == "awaiting_payment" and b.get("payment_url"):
            lines.append(f"   👉 Pay: {b['payment_url']}")
    return "\n".join(lines)


def _mark_paid(owner: str, text: str) -> list[str]:
    m = re.search(r"paid\s*([A-Z0-9]{6})?", text, re.I)
    pnr = (m.group(1) or "").upper() if m else ""
    books = store.bookings_for_owner(owner)
    if pnr:
        for b in books:
            if b["pnr"] == pnr:
                if b["status"] == "awaiting_payment":
                    store.update_booking(b["id"], status="done", last_error="")
                    return [f"✅ *{pnr}* marked paid. Have a great flight! ✈️"]
                return [f"{pnr} is currently *{b['status']}* — nothing to mark."]
        return [f"I don't have a booking *{pnr}* under your number. Type *my bookings*."]
    awaiting = [b for b in books if b["status"] == "awaiting_payment"]
    if len(awaiting) == 1:
        store.update_booking(awaiting[0]["id"], status="done", last_error="")
        return [f"✅ *{awaiting[0]['pnr']}* marked paid. Have a great flight! ✈️"]
    if not awaiting:
        return ["Nothing awaiting payment. Type *my bookings* to see your flights."]
    return ["Multiple awaiting payment — reply *paid PNR*, e.g. *paid " + awaiting[0]["pnr"] + "*.\n" +
            "\n".join(fmt_booking(b) for b in awaiting)]


# ------------------------------------------------------------ uploads

def _ocr_image(data: bytes) -> str:
    try:
        import io as _io

        from PIL import Image
        import pytesseract

        img = Image.open(_io.BytesIO(data))
        return pytesseract.image_to_string(img)
    except Exception:
        return ""


def process_upload(owner: str, data: bytes, filename: str, mime: str = "") -> list[str]:
    """Handle an uploaded ticket file. Returns reply strings."""
    owner = store.normalize_phone(owner)
    data_dir = Path(os.getenv("DATA_DIR", "./data")) / "uploads"
    data_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "ticket")[:80] or "ticket"
    dest = data_dir / f"wa_{owner}_{safe}"
    dest.write_bytes(data)

    lower = safe.lower()
    if lower.endswith(".pdf") or (mime or "").lower() == "application/pdf":
        from .parser_llm import extract_text_from_pdf, parse_ticket_text

        text = extract_text_from_pdf(data)
    elif lower.endswith((".txt", ".eml")) or (mime or "").startswith("text/"):
        text = data.decode("utf-8", "ignore")
    else:  # photo / screenshot
        text = _ocr_image(data)
        if not text.strip():
            return [_photo_no_ocr()]
        from .parser_llm import parse_ticket_text
    from .parser_llm import parse_ticket_text as _parse

    booking, _method = _parse(text)
    problems = []
    if not booking.get("pnr"):
        problems.append("PNR")
    if not booking.get("departure_utc"):
        problems.append("departure date/time")
    if problems:
        return [f"⚠️ I got your file but couldn't find the *{' + '.join(problems)}*.\n"
                "Reply with one line instead:\n"
                "`ABC123 Sharma 2026-12-20 14:35 6E 2345 DEL BOM`"]
    if _departure_in_past(booking["departure_utc"]):
        return ["⚠️ That flight looks like it's already departed. Check the ticket and resend."]
    dup = store.find_active_booking(booking["pnr"], owner)
    if dup:
        store.clear_session(owner)
        return ["ℹ️ I'm already tracking this one:\n" + fmt_booking(dup)]
    booking["owner_phone"] = owner
    booking["source"] = f"whatsapp:{safe}"
    bid = store.insert_booking(booking)
    store.clear_session(owner)
    b = store.get_booking(bid)
    return ["✅ *Ticket added!* I'll check you in 48 hrs before departure.\n" + fmt_booking(b)]


def _photo_no_ocr() -> str:
    try:
        import PIL  # noqa
        import pytesseract  # noqa
        ocr_ready = True
    except Exception:
        ocr_ready = False
    if ocr_ready:
        return ("⚠️ I couldn't read that photo. Send a clearer screenshot, the *PDF*, "
                "or one line: `ABC123 Sharma 2026-12-20 14:35 6E 2345 DEL BOM`")
    return ("📷 I got the photo, but photo-reading (OCR) isn't installed on the server.\n"
            "Please send the *e-ticket PDF* instead, or one line:\n"
            "`ABC123 Sharma 2026-12-20 14:35 6E 2345 DEL BOM`")
