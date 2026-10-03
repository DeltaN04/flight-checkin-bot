"""SQLite store for bookings + check-in attempts + traveller preferences."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# New columns added after v1 — applied idempotently via ALTER TABLE.
BOOKING_EXTRA_COLUMNS = {
    "owner_phone": "TEXT NOT NULL DEFAULT ''",        # WhatsApp wa_id (digits) of the family member
    "seat_pref": "TEXT NOT NULL DEFAULT ''",          # JSON per-booking pref overrides
    "meal": "TEXT NOT NULL DEFAULT ''",               # chosen meal name
    "extras_json": "TEXT NOT NULL DEFAULT ''",        # JSON: seat/meal picks + prices
    "payment_url": "TEXT NOT NULL DEFAULT ''",        # handoff: airline payment/checkout URL
    "payment_amount_inr": "INTEGER NOT NULL DEFAULT 0",
    "payment_screenshot": "TEXT NOT NULL DEFAULT ''", # screenshot at payment step
    "companions_json": "TEXT NOT NULL DEFAULT ''",    # JSON [{first,last}] fellow travelers
}


def _db_path() -> Path:
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads").mkdir(exist_ok=True, parents=True)
    (data_dir / "boarding_passes").mkdir(exist_ok=True, parents=True)
    return data_dir / "agent.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pnr TEXT NOT NULL,
            airline TEXT NOT NULL,
            airline_code TEXT NOT NULL DEFAULT '',
            flight_no TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT '',
            destination TEXT NOT NULL DEFAULT '',
            departure_utc TEXT NOT NULL,
            passenger_last_name TEXT NOT NULL DEFAULT '',
            passenger_first_name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            raw_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'scheduled',
            seat TEXT NOT NULL DEFAULT '',
            boarding_pass_path TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL,
            at_utc TEXT NOT NULL,
            ok INTEGER NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            screenshot_path TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wa_sessions (
            owner TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'idle',
            draft_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    _migrate_preferences(conn)
    # idempotent migrations for bookings columns
    existing = {r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()}
    for col, ddl in BOOKING_EXTRA_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE bookings ADD COLUMN {col} {ddl}")
    conn.commit()
    conn.close()


def _migrate_preferences(conn: sqlite3.Connection) -> None:
    """Migrate singleton preferences (id=1) to per-owner keyed table."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(preferences)").fetchall()}
    if not cols:
        conn.execute(
            """
            CREATE TABLE preferences (
                owner TEXT PRIMARY KEY,
                data_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        return
    if "owner" in cols:
        return
    old = conn.execute("SELECT data_json FROM preferences WHERE id=1").fetchone()
    conn.execute("DROP TABLE preferences")
    conn.execute(
        """
        CREATE TABLE preferences (
            owner TEXT PRIMARY KEY,
            data_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    if old and old[0] and old[0] != "{}":
        conn.execute(
            "INSERT INTO preferences (owner, data_json, updated_at) VALUES ('',?,?)",
            (old[0], now_iso()),
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_booking(b: dict) -> int:
    seat_pref = b.get("seat_pref", "")
    if isinstance(seat_pref, dict):
        seat_pref = json.dumps(seat_pref)
    companions = b.get("passengers") or b.get("companions") or []
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO bookings
        (pnr, airline, airline_code, flight_no, origin, destination, departure_utc,
         passenger_last_name, passenger_first_name, email, phone, source, raw_text,
         status, owner_phone, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            b.get("pnr", "").upper().strip(),
            b.get("airline", ""),
            b.get("airline_code", "").upper().strip(),
            b.get("flight_no", "").upper().strip(),
            b.get("origin", "").upper().strip(),
            b.get("destination", "").upper().strip(),
            b.get("departure_utc", ""),
            b.get("passenger_last_name", ""),
            b.get("passenger_first_name", ""),
            b.get("email", ""),
            b.get("phone", ""),
            b.get("source", "manual"),
            b.get("raw_text", "")[:20000],
            "scheduled",
            normalize_phone(b.get("owner_phone", "")),
            now_iso(),
            now_iso(),
        ),
    )
    conn.commit()
    bid = cur.lastrowid
    conn.close()
    if seat_pref:
        update_booking(int(bid), seat_pref=seat_pref)
    if companions:
        # primary traveler stays in first/last columns; everyone else goes
        # to companions (match on first+last, not surname — families share those)
        primary = (str(b.get("passenger_first_name", "")).upper(),
                   str(b.get("passenger_last_name", "")).upper())
        rest = [c for c in companions
                if isinstance(c, dict) and c.get("last")
                and (str(c.get("first", "")).upper(), str(c.get("last", "")).upper()) != primary]
        if rest:
            update_booking(int(bid), companions_json=json.dumps(rest))
    return int(bid)


def normalize_phone(raw: str | None) -> str:
    """Normalize a phone/wa_id to digits only (e.g. '+91 98...' -> '9198...')."""
    import re as _re

    return _re.sub(r"\D", "", str(raw or ""))


def list_bookings(owner: str | None = None) -> list[dict]:
    conn = get_conn()
    if owner:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE owner_phone=? ORDER BY departure_utc ASC",
            (normalize_phone(owner),),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM bookings ORDER BY departure_utc ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def bookings_for_owner(owner: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
    conn = get_conn()
    if statuses:
        q = f"SELECT * FROM bookings WHERE owner_phone=? AND status IN ({','.join('?' * len(statuses))}) ORDER BY departure_utc ASC"
        rows = conn.execute(q, (normalize_phone(owner), *statuses)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE owner_phone=? ORDER BY departure_utc ASC",
            (normalize_phone(owner),),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_booking(bid: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
    conn.close()
    return dict(row) if row else None


_ACTIVE_STATUSES = ("scheduled", "retry", "in_progress", "awaiting_payment")


def find_active_booking(pnr: str, owner: str = "") -> dict | None:
    """Existing trackable booking for (pnr, owner), to avoid double scheduling."""
    conn = get_conn()
    row = conn.execute(
        f"SELECT * FROM bookings WHERE pnr=? AND owner_phone=? AND status IN ({','.join('?' * len(_ACTIVE_STATUSES))})"
        " ORDER BY id ASC LIMIT 1",
        (pnr.upper().strip(), normalize_phone(owner), *_ACTIVE_STATUSES),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_booking(bid: int, **fields) -> None:
    fields["updated_at"] = now_iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn = get_conn()
    conn.execute(f"UPDATE bookings SET {sets} WHERE id=?", (*fields.values(), bid))
    conn.commit()
    conn.close()


def log_attempt(booking_id: int, ok: bool, message: str, screenshot_path: str = "") -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO attempts (booking_id, at_utc, ok, message, screenshot_path) VALUES (?,?,?,?,?)",
        (booking_id, now_iso(), 1 if ok else 0, message[:5000], screenshot_path),
    )
    conn.execute(
        "UPDATE bookings SET attempts = attempts + 1, updated_at=? WHERE id=?",
        (now_iso(), booking_id),
    )
    conn.commit()
    conn.close()


def due_bookings(now_utc: datetime, window_open_hours: float = 48.0, window_close_hours: float = 1.0) -> list[dict]:
    """Bookings whose departure is within [now+close, now+open] and still scheduled/retry."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM bookings WHERE status IN ('scheduled','retry')"
    ).fetchall()
    conn.close()
    due = []
    for r in rows:
        d = dict(r)
        try:
            dep = datetime.fromisoformat(d["departure_utc"])
            if dep.tzinfo is None:
                dep = dep.replace(tzinfo=timezone.utc)
            delta_h = (dep - now_utc).total_seconds() / 3600.0
            if window_close_hours <= delta_h <= window_open_hours:
                d["_hours_to_departure"] = round(delta_h, 2)
                due.append(d)
        except Exception:
            continue
    return due


# ---------------------------------------------------------- preferences

def get_preferences(owner: str = "") -> dict:
    """Stored prefs for one family member (owner = WhatsApp digits, '' = default)."""
    from .preferences import defaults_from_env, sanitize

    owner = normalize_phone(owner)
    conn = get_conn()
    row = conn.execute("SELECT data_json FROM preferences WHERE owner=?", (owner,)).fetchone()
    conn.close()
    stored: dict = {}
    if row and row[0]:
        try:
            stored = json.loads(row[0])
        except json.JSONDecodeError:
            stored = {}
    merged = dict(defaults_from_env())
    merged.update({k: v for k, v in stored.items()})
    return sanitize(merged)


def save_preferences(partial: dict, owner: str = "") -> dict:
    from .preferences import sanitize

    owner = normalize_phone(owner)
    conn = get_conn()
    row = conn.execute("SELECT data_json FROM preferences WHERE owner=?", (owner,)).fetchone()
    current: dict = {}
    if row and row[0]:
        try:
            current = json.loads(row[0])
        except json.JSONDecodeError:
            current = {}
    current.update({k: v for k, v in (partial or {}).items()})
    clean = sanitize(current)
    conn.execute(
        "INSERT INTO preferences (owner, data_json, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(owner) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
        (owner, json.dumps(clean), now_iso()),
    )
    conn.commit()
    conn.close()
    return clean


# ---------------------------------------------------------- wa sessions

def get_session(owner: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT state, draft_json FROM wa_sessions WHERE owner=?", (normalize_phone(owner),)
    ).fetchone()
    conn.close()
    if not row:
        return {"state": "idle", "draft": {}}
    try:
        draft = json.loads(row[1] or "{}")
    except json.JSONDecodeError:
        draft = {}
    return {"state": row[0] or "idle", "draft": draft}


def set_session(owner: str, state: str, draft: dict | None = None) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO wa_sessions (owner, state, draft_json, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(owner) DO UPDATE SET state=excluded.state, draft_json=excluded.draft_json, "
        "updated_at=excluded.updated_at",
        (normalize_phone(owner), state, json.dumps(draft or {}), now_iso()),
    )
    conn.commit()
    conn.close()


def clear_session(owner: str) -> None:
    set_session(owner, "idle", {})
