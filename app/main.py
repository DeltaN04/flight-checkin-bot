"""FastAPI entrypoint: upload / manual / list / gmail-scan / run-now."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

load_dotenv()

from . import store
from .parser_llm import extract_text_from_pdf, parse_ticket_text
from .scheduler import run_due_now, start_scheduler

store.init_db()

app = FastAPI(title="Flight Auto Check-in Agent")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "web" / "templates"))


@app.on_event("startup")
def _startup():
    try:
        start_scheduler(app)
    except Exception as e:
        print(f"[scheduler] failed to start: {e}", flush=True)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    bookings = store.list_bookings()
    prefs = store.get_preferences()
    return templates.TemplateResponse(request, "index.html", {"bookings": bookings, "prefs": prefs})


@app.get("/api/bookings")
def api_list():
    return store.list_bookings()


@app.get("/api/preferences")
def api_prefs_get():
    return store.get_preferences()


@app.post("/api/preferences")
def api_prefs_set(
    request: Request,
    seat_type: str = Form(""),
    seat_zone: str = Form(""),
    preferred_seat: str = Form(""),
    exit_row_ok: str = Form(""),
    max_seat_price_inr: str = Form(""),
    meal_pref: str = Form(""),
    preferred_meal: str = Form(""),
    max_meal_price_inr: str = Form(""),
    pref_text: str = Form(""),
):
    """Save defaults via form fields and/or natural language (pref_text)."""
    from .preferences import parse_preferences_text, sanitize

    partial: dict = {}
    if pref_text.strip():
        parsed, _method = parse_preferences_text(pref_text)
        partial.update(parsed)
    # explicit fields win over natural language
    if seat_type:
        partial["seat_type"] = seat_type.lower()
    if seat_zone:
        partial["seat_zone"] = seat_zone.lower()
    if preferred_seat:
        partial["preferred_seat"] = preferred_seat.upper().strip()
    if exit_row_ok:
        partial["exit_row_ok"] = exit_row_ok.lower() in ("true", "on", "1", "yes")
    if max_seat_price_inr.strip():
        try:
            partial["max_seat_price_inr"] = int(max_seat_price_inr)
        except ValueError:
            pass
    if meal_pref:
        partial["meal_pref"] = meal_pref.lower()
    if preferred_meal:
        partial["preferred_meal"] = preferred_meal.strip()
    if max_meal_price_inr.strip():
        try:
            partial["max_meal_price_inr"] = int(max_meal_price_inr)
        except ValueError:
            pass
    current = store.get_preferences()
    current.update(partial)
    saved = store.save_preferences(sanitize(current))
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return saved


@app.post("/api/bookings/manual")
def api_manual(
    request: Request,
    pnr: str = Form(...),
    airline_code: str = Form(""),
    flight_no: str = Form(""),
    origin: str = Form(""),
    destination: str = Form(""),
    departure_local: str = Form(...),  # "YYYY-MM-DD HH:MM" IST
    last_name: str = Form(...),
    first_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    seat_type: str = Form(""),
    seat_zone: str = Form(""),
    preferred_seat: str = Form(""),
    meal_pref: str = Form(""),
    preferred_meal: str = Form(""),
):
    from zoneinfo import ZoneInfo

    try:
        dt = datetime.strptime(departure_local.strip(), "%Y-%m-%d %H:%M")
        dep_utc = dt.replace(tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return JSONResponse({"error": "departure_local must be 'YYYY-MM-DD HH:MM' (IST)"}, status_code=400)
    if not pnr or not last_name or not dep_utc:
        return JSONResponse({"error": "pnr, last_name, departure_local required"}, status_code=400)
    from .parser_llm import AIRLINE_MAP

    code = airline_code.upper().strip()
    airline = AIRLINE_MAP.get(code, (code or "Unknown", code))[0]
    seat_pref = {k: v for k, v in {
        "seat_type": seat_type.lower(), "seat_zone": seat_zone.lower(),
        "preferred_seat": preferred_seat.upper().strip(),
        "meal_pref": meal_pref.lower(), "preferred_meal": preferred_meal.strip(),
    }.items() if v}
    dup = store.find_active_booking(pnr)
    if dup:
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        return {"id": dup["id"], "status": dup["status"], "duplicate": True}
    bid = store.insert_booking({
        "pnr": pnr, "airline": airline, "airline_code": code, "flight_no": flight_no.upper(),
        "origin": origin.upper(), "destination": destination.upper(), "departure_utc": dep_utc,
        "passenger_last_name": last_name, "passenger_first_name": first_name,
        "email": email, "phone": phone, "source": "manual", "raw_text": "manual entry",
        "seat_pref": seat_pref,
    })
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"id": bid, "status": "scheduled"}


@app.post("/api/bookings/upload")
async def api_upload(request: Request, file: UploadFile = File(...)):
    data = await file.read()
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    dest = data_dir / "uploads" / file.filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    if file.filename.lower().endswith(".pdf"):
        text = extract_text_from_pdf(data)
    else:
        # image/txt: try decode as text (OCR hook: plug pytesseract here if needed)
        try:
            text = data.decode("utf-8", "ignore")
        except Exception:
            text = ""
    if not text.strip():
        return JSONResponse({"error": "Could not extract text from file. Try a text-based PDF or manual entry."}, status_code=400)
    booking, method = parse_ticket_text(text)
    if not booking.get("pnr"):
        return JSONResponse({"error": "PNR not found. Check file or use manual entry.", "extract": text[:2000]}, status_code=400)
    if not booking.get("departure_utc"):
        return JSONResponse({"error": "Departure date not found. Use manual entry with date.", "extract": text[:2000]}, status_code=400)
    booking["source"] = f"upload:{file.filename}"
    dup = store.find_active_booking(booking["pnr"])
    if dup:
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        return {"id": dup["id"], "status": dup["status"], "duplicate": True}
    bid = store.insert_booking(booking)
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"id": bid, "method": method, "booking": {k: booking.get(k) for k in
            ("pnr", "airline", "airline_code", "flight_no", "origin", "destination",
             "departure_utc", "passenger_last_name", "passenger_first_name")}}


@app.post("/api/run-due-now")
def api_run_due():
    return run_due_now()


@app.post("/api/bookings/{bid}/reset")
def api_reset(bid: int):
    b = store.get_booking(bid)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    store.update_booking(bid, status="scheduled", last_error="")
    return {"id": bid, "status": "scheduled"}


@app.post("/api/bookings/{bid}/paid")
def api_paid(bid: int):
    """Mark an awaiting_payment booking done after you pay on the airline site."""
    b = store.get_booking(bid)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    store.update_booking(bid, status="done", last_error="")
    return {"id": bid, "status": "done"}


@app.get("/api/bookings/{bid}")
def api_get(bid: int):
    b = store.get_booking(bid)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    return b


@app.post("/api/gmail/scan")
def api_gmail_scan():
    if os.getenv("GMAIL_ENABLED", "false").lower() != "true":
        return JSONResponse({"error": "Set GMAIL_ENABLED=true and add client_secret.json first"}, status_code=400)
    from .gmail_watcher import scan_inbox

    found = scan_inbox()
    imported = []
    for item in found:
        booking, _ = parse_ticket_text(f"{item['subject']}\n{item['body_text']}")
        if booking.get("pnr") and booking.get("departure_utc"):
            booking["source"] = f"gmail:{item['subject'][:80]}"
            bid = store.insert_booking(booking)
            imported.append({"id": bid, "pnr": booking["pnr"], "subject": item["subject"]})
    return {"scanned": len(found), "imported": imported}


# ------------------------------------------------------- WhatsApp webhook

@app.get("/webhook/whatsapp")
def wa_verify_get(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
):
    """Meta webhook verification handshake."""
    from fastapi.responses import PlainTextResponse

    from . import whatsapp

    if hub_mode == "subscribe" and whatsapp.verify_token_ok(hub_verify_token):
        return PlainTextResponse(hub_challenge)
    return JSONResponse({"error": "verification failed"}, status_code=403)


@app.post("/webhook/whatsapp")
async def wa_inbound(request: Request):
    """Receive Meta WhatsApp messages. Always 200 (else Meta retries)."""
    from . import bot, whatsapp

    raw = await request.body()
    if not whatsapp.verify_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        return JSONResponse({"error": "bad signature"}, status_code=403)
    try:
        payload = await request.json()
    except Exception:
        return {"status": "ok"}
    for msg in whatsapp.parse_inbound(payload):
        owner = store.normalize_phone(msg.get("from", ""))
        if not owner:
            continue
        try:
            if not whatsapp.is_allowed(owner):
                _wa_send(whatsapp, owner, ["⛔ This bot isn't registered for your number. Ask the admin to add you."])
                continue
            kind = msg.get("kind")
            if kind == "text":
                replies = bot.process_text(owner, msg.get("text", ""))
                _wa_send(whatsapp, owner, replies)
            elif kind == "button":
                _wa_send(whatsapp, owner, _handle_button(owner, msg.get("button_id", "")))
            elif kind == "media":
                try:
                    data, mime = whatsapp.download_media(msg["media_id"])
                except Exception as e:
                    _wa_send(whatsapp, owner, [f"⚠️ Couldn't download that file ({e}). Try again or send the PNR line."])
                    continue
                replies = bot.process_upload(owner, data, msg.get("filename") or "ticket", mime)
                _wa_send(whatsapp, owner, replies)
        except Exception as e:
            print(f"[webhook] handler error for {owner}: {e}", flush=True)
    return {"status": "ok"}


def _handle_button(owner: str, button_id: str) -> list[str]:
    from . import bot

    if button_id == "ADD":
        return bot.process_text(owner, "add")
    if button_id == "BOOKINGS":
        return bot.process_text(owner, "my bookings")
    if button_id == "PREFS":
        return bot.process_text(owner, "myprefs")
    return bot.process_text(owner, "menu")


def _wants_html(request: Request) -> bool:
    """Browser form posts (Accept: text/html) get a redirect back to the
    dashboard; API clients get JSON."""
    return "text/html" in request.headers.get("accept", "")


def _wa_send(whatsapp, owner: str, replies: list[str]) -> None:
    for r in replies or []:
        try:
            whatsapp.send_text(owner, r)
        except Exception as e:
            print(f"[webhook] send failed to {owner}: {e}", flush=True)


# ------------------------------------------------------- QR-bridge API
# Used by wa-bridge/ (unofficial Baileys sidecar). Same bot brain, no Meta needed.

def _bridge_guard(request: Request) -> bool:
    expected = os.getenv("WA_BRIDGE_SECRET", "")
    if not expected:
        return True
    return request.headers.get("X-Bridge-Secret", "") == expected


@app.post("/api/wa/process")
async def wa_process(request: Request):
    """QR bridge: {owner, text} -> {replies}. Localhost + shared secret."""
    from . import bot

    if not _bridge_guard(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    owner = store.normalize_phone((payload or {}).get("owner", ""))
    text = str((payload or {}).get("text", ""))
    if not owner:
        return JSONResponse({"error": "owner required"}, status_code=400)
    if not whatsapp_is_allowed(owner):
        return {"replies": ["⛔ This bot isn't registered for your number. Ask the admin to add you."]}
    return {"replies": bot.process_text(owner, text)}


@app.post("/api/wa/upload")
async def wa_upload(request: Request, owner: str = Form(""), file: UploadFile = File(...)):
    """QR bridge: multipart owner+file -> {replies}."""
    from . import bot

    if not _bridge_guard(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    owner = store.normalize_phone(owner)
    if not owner:
        return JSONResponse({"error": "owner required"}, status_code=400)
    if not whatsapp_is_allowed(owner):
        return {"replies": ["⛔ This bot isn't registered for your number. Ask the admin to add you."]}
    data = await file.read()
    return {"replies": bot.process_upload(owner, data, file.filename or "ticket", file.content_type or "")}


def whatsapp_is_allowed(owner: str) -> bool:
    from . import whatsapp

    try:
        return whatsapp.is_allowed(owner)
    except Exception:
        return True
