"""Ticket text extraction + LLM parsing with offline regex fallback.

Pipeline: PDF bytes -> text (pdfplumber -> PyPDF2) -> LLM (OpenAI-compat -> Ollama) -> Booking dict.
If no LLM key is configured, regex fallback still extracts PNR/flight/date reliably
for standard Indian e-tickets.
"""
from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

AIRLINE_MAP = {
    "6E": ("IndiGo", "6E"),
    "AI": ("Air India", "AI"),
    "SG": ("SpiceJet", "SG"),
    "QP": ("Akasa Air", "QP"),
    "IX": ("Air India Express", "IX"),
    "I5": ("Air India Express", "IX"),  # AIX Connect old code
    "UK": ("Vistara", "UK"),
}


def extract_text_from_pdf(data: bytes) -> str:
    """Try pdfplumber first, fall back to PyPDF2. Returns '' on failure."""
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            parts = [(p.extract_text() or "") for p in pdf.pages]
        text = "\n".join(parts).strip()
        if text:
            return text
    except Exception:
        pass
    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception:
        return ""


def _guess_airline(text: str) -> tuple[str, str]:
    t = text.lower()
    if "indigo" in t:
        return ("IndiGo", "6E")
    if "air india express" in t or re.search(r"\bI5\s?\d", text) or re.search(r"\bIX\s?\d", text):
        return ("Air India Express", "IX")
    if "air india" in t:
        return ("Air India", "AI")
    if "spicejet" in t or re.search(r"\bSG\s?\d", text):
        return ("SpiceJet", "SG")
    if "akasa" in t or re.search(r"\bQP\s?\d", text):
        return ("Akasa Air", "QP")
    if "vistara" in t or re.search(r"\bUK\s?\d", text):
        return ("Vistara", "UK")
    m = re.search(r"\b(6E|AI|SG|QP|IX|I5|UK)\s?(\d{3,4})", text)
    if m:
        code = m.group(1).upper()
        name, norm = AIRLINE_MAP.get(code, (code, code))
        return (name, norm)
    return ("Unknown", "")


_NAME_BLOCKLIST = {
    "DETAILS", "DETAIL", "LIST", "NAME", "NAMES", "INFORMATION", "INFO",
    "PASSENGER", "GUEST", "CORPORATE", "SEAT", "MEAL", "FLIGHT", "TICKET",
}

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]

# Dates preceded by these words are booking/payment dates, not the flight.
_DATE_EXCLUDE_CTX = ("booking", "payment", "promo", "transaction")


def _find_departure_split(text: str, flight_no: str) -> str:
    """Combine a standalone flight date with a separately-printed time.

    Returns departure_utc ISO or "".
    """
    fm = re.search(r"\b(6E|AI|SG|QP|IX|I5|UK)\s?-?\s?(\d{3,4})\b", text)
    fpos = fm.start() if fm else 0

    # --- date: "08 Oct 2026" / "08 Oct, 2026", nearest the flight mention,
    # skipping booking/payment contexts ("Date of Booking ... 28 Jul 2026").
    best = None  # (distance, day, mon_idx, year)
    for m in re.finditer(
            r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*,?\s*(\d{4})",
            text, re.I):
        ctx = text[max(0, m.start() - 60):m.start()].lower()
        if any(w in ctx for w in _DATE_EXCLUDE_CTX):
            continue
        mon = _MONTHS.index(m.group(2).lower()[:3])
        best_dist = abs(m.start() - fpos)
        if best is None or best_dist < best[0]:
            best = (best_dist, int(m.group(1)), mon, int(m.group(3)))
    if best is None:
        # bare "08 Oct" (no year): assume the upcoming occurrence (IST).
        today = datetime.now(IST).date()
        for m in re.finditer(
                r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b(?!\s*,?\s*\d{4})",
                text, re.I):
            from datetime import date as _date

            try:
                cand = _date(today.year, _MONTHS.index(m.group(2).lower()[:3]) + 1, int(m.group(1)))
            except ValueError:
                continue
            year = cand.year if cand >= today else cand.year + 1
            best_dist = abs(m.start() - fpos)
            if best is None or best_dist < best[0]:
                best = (best_dist, int(m.group(1)), _MONTHS.index(m.group(2).lower()[:3]), year)
    if best is None:
        return ""
    _, day, mon0, year = best

    # --- time: dep/arr pair after the flight no ("16:30 19:20") wins,
    # then an explicit Departs/Departure label.
    hh_mm = ""
    if fm:
        pair = re.search(r"(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})", text[fm.start():fm.start() + 400])
        if pair:
            hh_mm = pair.group(1)
    if not hh_mm:
        m = re.search(r"Departs?\b.{0,120}?(\d{1,2}:\d{2})", text, re.I | re.S)
        if m:
            hh_mm = m.group(1)
    if not hh_mm:
        m = re.search(r"Departure[^\d:]{0,40}(\d{1,2}:\d{2})", text, re.I)
        if m:
            hh_mm = m.group(1)
    if not hh_mm and fm:
        m = re.search(r"(\d{1,2}:\d{2})", text[fm.start():fm.start() + 400])
        if m:
            hh_mm = m.group(1)
    if not hh_mm:
        return ""
    try:
        hh, mm = int(hh_mm.split(":")[0]), int(hh_mm.split(":")[1])
        dt = datetime(year, mon0 + 1, day, hh, mm, tzinfo=IST)
    except ValueError:
        return ""
    return dt.astimezone(timezone.utc).isoformat()


def regex_parse(text: str) -> dict:
    """Deterministic offline parser. Returns Booking dict (departure_utc ISO)."""
    airline, code = _guess_airline(text)

    pnr = ""
    # Common labels: PNR, Booking Ref(erence), Reservation Code, Confirmation.
    # Word-boundary + explicit Reference alternative: avoids matching the "Ref"
    # prefix inside "Reference" (which once yielded PNR="ERENCE").
    m = re.search(
        r"(?:\bPNR\b|\bBooking\s*Reference\b|\bBooking\s*Ref\b(?!erence)"
        r"|\bBooking\s*(?:No|ID)\b|\bReservation\s*Code\b|\bConfirmation\s*(?:No|Code)\b)"
        r"\s*[:#/-]?\s*([A-Z0-9]{6})\b", text, re.I)
    if m:
        pnr = m.group(1).upper()
    else:
        # Table layout: header row holds "PNR", value sits on a following line
        # ("Booking Reference/PNR ... Promo Code\nQW7X2K Confirmed 28 Jul 2026").
        m = re.search(r"(\bPNR\b|Booking\s*Ref[^\n]{0,80})\n([^\n]{0,150})", text, re.I)
        if m:
            for tok in re.findall(r"\b([A-Z0-9]{6})\b", m.group(2)):
                # skip glued flight nos like QP1526, not real PNRs like QW7X2K
                if not re.match(r"^(6E|AI|SG|QP|IX|I5|UK)\d+$", tok) and not tok.isdigit():
                    pnr = tok.upper()
                    break
    if not pnr:
        # fallback: first isolated 6-char alnum token (avoid flight nos like 6E123)
        cands = re.findall(r"\b([A-Z0-9]{6})\b", text)
        for c in cands:
            # skip glued flight nos like QP1526, not real PNRs like QW7X2K
            if not re.match(r"^(6E|AI|SG|QP|IX|I5|UK)\d+$", c) and not c.isdigit():
                pnr = c
                break

    flight_no = ""
    m = re.search(r"\b(6E|AI|SG|QP|IX|I5|UK)\s?-?\s?(\d{3,4})\b", text)
    if m:
        flight_no = f"{m.group(1).upper()} {m.group(2)}"
        if not code:
            airline, code = AIRLINE_MAP.get(m.group(1).upper(), (airline, code))

    # Origin/destination: "(DEL) → (BOM)", "DEL→BOM", "(DXN-BLR)", "from DEL to BLR"
    origin, destination = "", ""
    m = re.search(r"\(([A-Z]{3})\)\s*(?:→|->|to|-)\s*[A-Za-z ]*\(?([A-Z]{3})\)?", text)
    if m:
        origin, destination = m.group(1), m.group(2)
    else:
        m = re.search(r"\b([A-Z]{3})\s*(?:→|->)\s*([A-Z]{3})\b", text)
        if m:
            origin, destination = m.group(1), m.group(2)
        else:
            m = re.search(r"\(?([A-Z]{3})\s*[-–—]\s*([A-Z]{3})\)?", text)
            if m:
                origin, destination = m.group(1), m.group(2)
            else:
                m = re.search(r"\bfrom\s+([A-Z]{3})\s+to\s+([A-Z]{3})\b", text, re.I)
                if m:
                    origin, destination = m.group(1).upper(), m.group(2).upper()

    # Departure: combined "05 Oct 2026, 14:35" first; else date + separately-found
    # time (many tickets print "08 Oct 2026" in the flight row and "16:30"
    # under a "Departs" header while a decoy booking date sits elsewhere).
    departure_utc = ""
    date_patterns = [
        r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}[,\s]+\d{1,2}:\d{2})",
        r"(\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2})",
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4}\s+\d{1,2}:\d{2})",
    ]
    for pat in date_patterns:
        m = re.search(pat, text, re.I)
        if m:
            raw = m.group(1)
            for fmt in ("%d %b %Y %H:%M", "%d %B %Y %H:%M", "%Y-%m-%d %H:%M",
                        "%Y-%m-%dT%H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M",
                        "%m/%d/%Y %H:%M"):
                try:
                    # strip comma
                    dt = datetime.strptime(raw.replace(",", ""), fmt)
                    dt = dt.replace(tzinfo=IST)
                    departure_utc = dt.astimezone(timezone.utc).isoformat()
                    break
                except ValueError:
                    continue
            if departure_utc:
                break
    if not departure_utc:
        departure_utc = _find_departure_split(text, flight_no)

    # Passenger: "Passenger: FIRST LAST", "pax First Last", or a
    # "PASSENGER DETAILS" section holding bare ALL-CAPS names.
    last_name, first_name = "", ""
    passengers: list[dict] = []
    m = re.search(
        r"(?:\bPassenger(?:\(s\))?|\bGuest|\bTravellers?|\bTraveler|\bName|\bPax\b)"
        r"\s*(?:details?|\(s\))?\s*[:#]?\s*([A-Za-z]{2,})\s+([A-Za-z]{2,})", text, re.I)
    if m and m.group(1).upper() not in _NAME_BLOCKLIST and m.group(2).upper() not in _NAME_BLOCKLIST:
        first_name, last_name = m.group(1), m.group(2)
    else:
        sec = re.search(r"PASSENGER DETAILS(.{0,800})", text, re.I | re.S)
        if sec:
            # names come before seat/meal lines — cut there to avoid
            # "Hyderabadi Veg" style false positives
            chunk = re.split(r"\b(?:Seat|Meal|FARE|SSR)\b", sec.group(1), 1,
                             flags=re.I)[0]
            for pm in re.finditer(r"\b([A-Z]{2,})\s+([A-Z]{2,})\b", chunk):
                if pm.group(1) in _NAME_BLOCKLIST or pm.group(2) in _NAME_BLOCKLIST:
                    continue
                passengers.append({"first": pm.group(1), "last": pm.group(2)})
        if passengers:
            first_name, last_name = passengers[0]["first"], passengers[0]["last"]

    email = ""
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    if m:
        email = m.group(0)

    return {
        "pnr": pnr.upper(),
        "airline": airline,
        "airline_code": code,
        "flight_no": flight_no,
        "origin": origin,
        "destination": destination,
        "departure_utc": departure_utc,
        "passenger_last_name": last_name,
        "passenger_first_name": first_name,
        "passengers": passengers,  # all travelers on the PNR (primary first)
        "email": email,
        "phone": "",
        "raw_text": text,
    }


LLM_SYSTEM = """You extract flight booking details from airline e-ticket text.
Return ONLY valid JSON with keys:
pnr, airline, airline_code (6E/AI/SG/QP/IX/UK), flight_no (e.g. "6E 2345"),
origin (3-letter IATA), destination (3-letter IATA),
departure_local (e.g. "2026-10-05 14:35", Asia/Kolkata time as printed),
passenger_first_name, passenger_last_name, email, phone.
Use "" for unknown. departure_local must be YYYY-MM-DD HH:MM."""


def _to_utc_iso(departure_local: str) -> str:
    try:
        dt = datetime.strptime(departure_local.strip(), "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=IST).astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def llm_parse(text: str) -> dict | None:
    """Try OpenAI-compat API, then Ollama. Return Booking dict or None."""
    if not text.strip():
        return None
    # 1) OpenAI-compatible
    if os.getenv("OPENAI_API_KEY"):
        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=os.getenv("OPENAI_API_KEY"),
                base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            )
            resp = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": LLM_SYSTEM},
                    {"role": "user", "content": text[:12000]},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            return _normalize_llm(data, text)
        except Exception:
            pass
    # 2) Ollama
    try:
        import httpx

        host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
        r = httpx.post(
            f"{host}/api/chat",
            json={
                "model": model,
                "format": "json",
                "stream": False,
                "messages": [
                    {"role": "system", "content": LLM_SYSTEM},
                    {"role": "user", "content": text[:12000]},
                ],
            },
            timeout=60,
        )
        if r.status_code == 200:
            data = json.loads(r.json()["message"]["content"])
            return _normalize_llm(data, text)
    except Exception:
        pass
    return None


def _normalize_llm(data: dict, text: str) -> dict:
    code = str(data.get("airline_code", "")).upper()
    airline = str(data.get("airline", ""))
    if code in AIRLINE_MAP and not airline:
        airline = AIRLINE_MAP[code][0]
    return {
        "pnr": str(data.get("pnr", "")).upper().strip(),
        "airline": airline,
        "airline_code": code,
        "flight_no": str(data.get("flight_no", "")).upper().strip(),
        "origin": str(data.get("origin", "")).upper().strip(),
        "destination": str(data.get("destination", "")).upper().strip(),
        "departure_utc": _to_utc_iso(str(data.get("departure_local", ""))),
        "passenger_last_name": str(data.get("passenger_last_name", "")).strip(),
        "passenger_first_name": str(data.get("passenger_first_name", "")).strip(),
        "passengers": [],
        "email": str(data.get("email", "")).strip(),
        "phone": str(data.get("phone", "")).strip(),
        "raw_text": text,
    }


def parse_ticket_text(text: str) -> tuple[dict, str]:
    """Returns (booking, method). method is 'llm' or 'regex'."""
    llm = llm_parse(text)
    if llm and llm.get("pnr") and llm.get("departure_utc"):
        return llm, "llm"
    return regex_parse(text), "regex"
