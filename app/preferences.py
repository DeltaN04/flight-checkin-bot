"""Traveller preferences: seat + meal + payment handoff.

Single-traveller defaults are stored as a singleton row (id=1) in the
`preferences` table, overridable per booking and via env vars.
Natural-language input ("window seat in front, veg meal") is parsed with
an LLM when available, else a deterministic regex fallback.

Pure pickers (pick_seat / pick_meal) are deliberately browser-free so they
are unit-testable; the Playwright adapters scrape the seat map / meal list
into plain dicts and call these.
"""
from __future__ import annotations

import json
import os
import re

SEAT_TYPES = ("window", "aisle", "middle", "any")
SEAT_ZONES = ("front", "middle", "back", "any")
MEAL_PREFS = ("none", "veg", "nonveg", "vegan", "jain", "any")

DEFAULTS: dict = {
    "seat_type": "window",
    "seat_zone": "front",
    "preferred_seat": "",       # e.g. "14A" — exact match wins if available & in budget
    "exit_row_ok": False,
    "skip_seat": False,          # True = keep ticket/airline seat, don't touch seat map
    "max_seat_price_inr": 1500,
    "meal_pref": "veg",
    "preferred_meal": "",       # substring match, e.g. "paneer"
    "max_meal_price_inr": 1000,
    "stop_before_payment": True,  # ALWAYS true: agent never pays, hands off at payment page
}


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def defaults_from_env() -> dict:
    d = dict(DEFAULTS)
    if os.getenv("SEAT_TYPE", "").lower() in SEAT_TYPES:
        d["seat_type"] = os.getenv("SEAT_TYPE", "").lower()
    if os.getenv("SEAT_ZONE", "").lower() in SEAT_ZONES:
        d["seat_zone"] = os.getenv("SEAT_ZONE", "").lower()
    if os.getenv("PREFERRED_SEAT"):
        d["preferred_seat"] = os.getenv("PREFERRED_SEAT", "").upper().strip()
    if os.getenv("EXIT_ROW_OK"):
        d["exit_row_ok"] = os.getenv("EXIT_ROW_OK", "").lower() == "true"
    if os.getenv("SKIP_SEAT"):
        d["skip_seat"] = os.getenv("SKIP_SEAT", "").lower() == "true"
    d["max_seat_price_inr"] = _env_int("MAX_SEAT_PRICE_INR", d["max_seat_price_inr"])
    if os.getenv("MEAL_PREF", "").lower() in MEAL_PREFS:
        d["meal_pref"] = os.getenv("MEAL_PREF", "").lower()
    if os.getenv("PREFERRED_MEAL"):
        d["preferred_meal"] = os.getenv("PREFERRED_MEAL", "").strip()
    d["max_meal_price_inr"] = _env_int("MAX_MEAL_PRICE_INR", d["max_meal_price_inr"])
    return d


def sanitize(p: dict) -> dict:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (p or {}).items() if k in DEFAULTS})
    if out.get("seat_type") not in SEAT_TYPES:
        out["seat_type"] = "any"
    if out.get("seat_zone") not in SEAT_ZONES:
        out["seat_zone"] = "any"
    if out.get("meal_pref") not in MEAL_PREFS:
        out["meal_pref"] = "any"
    out["preferred_seat"] = str(out.get("preferred_seat") or "").upper().strip()
    out["preferred_meal"] = str(out.get("preferred_meal") or "").strip()
    for k in ("max_seat_price_inr", "max_meal_price_inr"):
        try:
            out[k] = max(0, int(out[k]))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    out["exit_row_ok"] = bool(out.get("exit_row_ok"))
    out["skip_seat"] = bool(out.get("skip_seat"))
    out["stop_before_payment"] = True  # never auto-pay; handoff only
    return out


def effective_prefs(booking: dict | None = None, stored: dict | None = None) -> dict:
    """Global stored prefs overridden by per-booking columns (if any)."""
    base = dict(defaults_from_env())
    if stored:
        base.update({k: v for k, v in stored.items() if k in DEFAULTS})
    if booking:
        if booking.get("seat_pref"):
            try:
                base.update({k: v for k, v in json.loads(booking["seat_pref"]).items() if k in DEFAULTS})
            except (json.JSONDecodeError, TypeError):
                pass
        # legacy flat columns (if a booking was created with explicit fields)
        for k in ("seat_type", "seat_zone", "preferred_seat", "meal_pref", "preferred_meal"):
            if booking.get(k):
                base[k] = booking[k]
    return sanitize(base)


# ---------------------------------------------------------------- seat picker

def _seat_parts(code: str) -> tuple[int, str] | None:
    m = re.match(r"^\s*(\d{1,2})\s*([A-K])\s*$", (code or "").upper())
    if not m:
        return None
    return int(m.group(1)), m.group(2)


def seat_position(letter: str, letters_present: list[str] | None = None) -> str:
    """window / aisle / middle for a seat letter. Defaults to 3-3 layout (ABC DEF)."""
    L = letter.upper()
    if letters_present:
        uniq = sorted(set(letters_present))
        if L == uniq[0] or L == uniq[-1]:
            return "window"
        # 2-2 or smaller: every non-window seat borders an aisle
        if len(uniq) <= 4:
            return "aisle"
        # 3-3 etc: aisle seats are the pair around the cabin centre (C,D of ABCDEF)
        n = len(uniq)
        if n % 2 == 0:
            aisle = {uniq[n // 2 - 1], uniq[n // 2]}
        else:
            aisle = {uniq[n // 2]}
        return "aisle" if L in aisle else "middle"
    if L in ("A", "F"):
        return "window"
    if L in ("C", "D"):
        return "aisle"
    return "middle"


def pick_seat(available: list[dict], prefs: dict) -> dict | None:
    """Pick best seat from [{'code','price_inr','available','exit'(bool)}].

    Returns the chosen dict or None (keep airline auto-assign).
    """
    prefs = sanitize(prefs)
    cands = [s for s in (available or []) if s.get("available", True) and _seat_parts(s.get("code", ""))]
    if not cands:
        return None
    if not prefs["exit_row_ok"]:
        cands = [s for s in cands if not s.get("exit")]
    cands = [s for s in cands if int(s.get("price_inr", 0) or 0) <= prefs["max_seat_price_inr"]]
    if not cands:
        return None

    # exact preferred seat wins
    if prefs["preferred_seat"]:
        for s in cands:
            if s["code"].upper().replace(" ", "") == prefs["preferred_seat"].replace(" ", ""):
                return s

    rows = sorted({_seat_parts(s["code"])[0] for s in cands})  # type: ignore
    lo, hi = rows[0], rows[-1]
    span = max(1, hi - lo)

    def zone_of(row: int) -> str:
        rel = (row - lo) / span
        return "front" if rel < 0.34 else ("back" if rel > 0.66 else "middle")

    letters = [(_seat_parts(s["code"])[1]) for s in cands]  # type: ignore

    def score(s: dict) -> tuple:
        row, letter = _seat_parts(s["code"])  # type: ignore
        pos = seat_position(letter, letters)
        type_ok = prefs["seat_type"] in ("any", pos)
        zone_ok = prefs["seat_zone"] in ("any", zone_of(row))
        price = int(s.get("price_inr", 0) or 0)
        # free seats sort before paid ones with same match quality
        return (0 if type_ok else 1, 0 if zone_ok else 1, price, row, letter)

    return sorted(cands, key=score)[0]


# ---------------------------------------------------------------- meal picker

def _meal_flags(meal: dict) -> dict:
    name = f"{meal.get('name','')} {meal.get('code','')} {meal.get('desc','')}".lower()
    return {
        "vegan": "vegan" in name,
        "jain": "jain" in name,
        "veg": ("veg" in name or "paneer" in name or "dal" in name) and "non-veg" not in name and "nonveg" not in name and "chicken" not in name and "egg" not in name,
        "nonveg": any(w in name for w in ("chicken", "non-veg", "nonveg", "egg", "fish", "mutton")),
    }


def pick_meal(available: list[dict], prefs: dict) -> dict | None:
    """Pick best meal from [{'name','price_inr','available',...}]. None = skip meals."""
    prefs = sanitize(prefs)
    if prefs["meal_pref"] == "none":
        return None
    cands = [m for m in (available or []) if m.get("available", True) and (m.get("name") or m.get("code"))]
    if not cands:
        return None
    cands = [m for m in cands if int(m.get("price_inr", 0) or 0) <= prefs["max_meal_price_inr"]]
    if not cands:
        return None

    want = prefs["meal_pref"]
    want_txt = prefs["preferred_meal"].lower()

    def score(m: dict) -> tuple:
        flags = _meal_flags(m)
        name = f"{m.get('name','')} {m.get('code','')}".lower()
        if want == "any":
            diet_ok = True
        elif want == "vegan":
            diet_ok = flags["vegan"]
        elif want == "jain":
            diet_ok = flags["jain"] or flags["veg"]
        elif want == "veg":
            diet_ok = flags["veg"] or flags["vegan"] or flags["jain"]
        else:  # nonveg eats anything
            diet_ok = True
        text_ok = (want_txt in name) if want_txt else False
        price = int(m.get("price_inr", 0) or 0)
        return (0 if (text_ok or not want_txt) else 1, 0 if diet_ok else 1, price, name)

    best = sorted(cands, key=score)[0]
    # hard diet mismatch and user named a specific dish from another diet: skip
    flags = _meal_flags(best)
    if want == "vegan" and not flags["vegan"]:
        return None
    if want == "veg" and flags["nonveg"]:
        return None
    return best


# ------------------------------------------------------- natural language prefs

PREF_SYSTEM = """Convert traveller flight preferences to JSON.
Keys: seat_type (window/aisle/middle/any), seat_zone (front/middle/back/any),
preferred_seat (e.g. "14A" or ""), exit_row_ok (bool),
meal_pref (veg/nonveg/vegan/jain/none/any), preferred_meal (dish or "").
Return ONLY JSON. Missing info -> defaults: seat any/front? use "any"."""


def parse_preferences_text(text: str) -> tuple[dict, str]:
    """Returns (prefs_dict_partial, method). Empty dict if nothing understood."""
    text = (text or "").strip()
    if not text:
        return {}, "empty"
    llm = _llm_prefs(text)
    if llm:
        return llm, "llm"
    return _regex_prefs(text), "regex"


def _regex_prefs(text: str) -> dict:
    t = text.lower()
    out: dict = {}
    for st in ("window", "aisle", "middle"):
        if st in t:
            out["seat_type"] = st
            break
    for z in ("front", "middle", "back"):
        if z in t and "seat" in t or f"{z} seat" in t or f"seat {z}" in t or f"sitting in {z}" in t:
            out["seat_zone"] = z
            break
    m = re.search(r"\bseat\s*(\d{1,2}\s?[A-K])\b", text, re.I)
    if m:
        out["preferred_seat"] = m.group(1).upper().replace(" ", "")
    if "exit row" in t:
        out["exit_row_ok"] = "no exit" not in t and "not exit" not in t and "avoid exit" not in t
    if "no meal" in t or "skip meal" in t or "without meal" in t:
        out["meal_pref"] = "none"
    elif "vegan" in t:
        out["meal_pref"] = "vegan"
    elif "jain" in t:
        out["meal_pref"] = "jain"
    elif "non-veg" in t or "nonveg" in t or "chicken" in t or "egg " in t:
        out["meal_pref"] = "nonveg"
    elif "veg" in t:
        out["meal_pref"] = "veg"
    for dish in ("paneer", "dal", "biryani", "pasta", "sandwich", "wrap", "chicken", "egg"):
        if dish in t:
            out["preferred_meal"] = dish
            break
    m = re.search(r"seat.*?(\d{3,4})\s*(?:rs|inr|₹)", t) or re.search(r"(?:rs|inr|₹)\s*(\d{3,4}).*?seat", t)
    if m:
        out["max_seat_price_inr"] = int(m.group(1))
    return out


def _llm_prefs(text: str) -> dict | None:
    if os.getenv("OPENAI_API_KEY"):
        try:
            import json as _json

            from openai import OpenAI

            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"),
                            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
            resp = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[{"role": "system", "content": PREF_SYSTEM},
                          {"role": "user", "content": text[:2000]}],
                temperature=0, response_format={"type": "json_object"},
            )
            data = _json.loads(resp.choices[0].message.content or "{}")
            return {k: v for k, v in data.items() if k in DEFAULTS}
        except Exception:
            pass
    try:
        import json as _json

        import httpx

        host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        r = httpx.post(f"{host}/api/chat",
                       json={"model": os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
                             "format": "json", "stream": False,
                             "messages": [{"role": "system", "content": PREF_SYSTEM},
                                          {"role": "user", "content": text[:2000]}]},
                       timeout=30)
        if r.status_code == 200:
            data = _json.loads(r.json()["message"]["content"])
            return {k: v for k, v in data.items() if k in DEFAULTS}
    except Exception:
        pass
    return None
