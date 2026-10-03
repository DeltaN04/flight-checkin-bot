"""Background scheduler: every N minutes, run due check-ins."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from . import store
from .checkin.adapters import get_adapter
from .notifier import notify


def _pax_str(booking: dict) -> str:
    """'Aarav Sharma + Diya Sharma' — primary plus companions."""
    import json as _json

    names = [f"{booking.get('passenger_first_name', '')} {booking.get('passenger_last_name', '')}".strip()]
    try:
        for c in _json.loads(booking.get("companions_json", "") or "[]"):
            if isinstance(c, dict) and (c.get("first") or c.get("last")):
                names.append(f"{c.get('first', '')} {c.get('last', '')}".strip())
    except Exception:
        pass
    return " + ".join(n for n in names if n)


def _ping_owner(booking: dict, title: str, body: str) -> None:
    owner = (booking.get("owner_phone") or "").strip()
    if not owner:
        return
    # 1) official Meta Cloud API
    try:
        from .whatsapp import notify_owner_whatsapp

        res = notify_owner_whatsapp(owner, title, body) or ""
        if res.startswith("sent"):
            return
    except Exception as e:
        print(f"[scheduler] meta ping failed: {e}", flush=True)
    # 2) unofficial QR bridge (spare number)
    try:
        import httpx

        bridge = os.getenv("WA_BRIDGE_URL", "")
        if bridge:
            r = httpx.post(f"{bridge.rstrip('/')}/send",
                           json={"secret": os.getenv("WA_BRIDGE_SECRET", ""),
                                 "to": owner, "text": f"{title}\n{body}"},
                           timeout=20)
            if r.status_code != 200:
                print(f"[scheduler] bridge ping HTTP {r.status_code}: {r.text[:150]}", flush=True)
    except Exception as e:
        print(f"[scheduler] bridge ping failed: {e}", flush=True)


async def run_checkin_for_booking(booking: dict) -> bool:
    bid = booking["id"]
    adapter = get_adapter(booking.get("airline_code", ""))
    store.update_booking(bid, status="in_progress")
    try:
        result = await adapter.run(booking)
    except Exception as e:
        result = None
        msg = f"adapter crashed: {e}"
        store.log_attempt(bid, False, msg)
        store.update_booking(bid, status="retry", last_error=msg)
        title = f"✈️ Check-in needs you (PNR {booking['pnr']})"
        body = (f"{booking.get('airline')} {booking.get('flight_no')} "
                f"{booking.get('origin')}→{booking.get('destination')}\nError: {msg}\n"
                f"Manual link: {adapter.checkin_url}")
        notify(title, body)
        _ping_owner(booking, title, body)
        return False

    assert result is not None
    store.log_attempt(bid, result.ok, result.message, result.screenshot_path)
    if result.ok and result.needs_payment:
        import json as _json

        store.update_booking(
            bid, status="awaiting_payment", seat=result.seat, meal=result.meal,
            extras_json=_json.dumps(result.extras),
            payment_url=result.payment_url, payment_amount_inr=result.payment_amount_inr,
            payment_screenshot=result.screenshot_path, last_error="",
        )
        title = f"💳 Payment needed — PNR {booking['pnr']} (seat+meal done)"
        body = (f"{booking.get('airline')} {booking.get('flight_no')} "
                f"{booking.get('origin')}→{booking.get('destination')}\n"
                f"Passenger(s): {_pax_str(booking)}\n"
                f"Seat: {result.seat or 'auto-assign'} | Meal: {result.meal or 'none'}\n"
                f"Amount due: ₹{result.payment_amount_inr}\n"
                f"Pay here: {result.payment_url}\n"
                f"The agent selected everything and stopped at the payment page — "
                f"it never pays on its own (OTP/3-D Secure needs you).\n"
                f"After paying, reply *paid {booking['pnr']}* here.")
        notify(title, body)
        _ping_owner(booking, title, body)
        return True
    if result.ok:
        store.update_booking(
            bid, status="done", seat=result.seat, meal=result.meal,
            boarding_pass_path=result.boarding_pass_path, last_error="",
        )
        title = f"✅ Checked in! PNR {booking['pnr']}"
        body = (f"{booking.get('airline')} {booking.get('flight_no')} "
                f"{booking.get('origin')}→{booking.get('destination')}\n"
                f"Passenger(s): {_pax_str(booking)}\n"
                f"Seat: {result.seat or 'see boarding pass'} | Meal: {result.meal or 'none'}\n"
                f"{result.message}\n"
                f"Boarding pass: {result.boarding_pass_path or result.screenshot_path}")
        notify(title, body)
        _ping_owner(booking, title, body)
        return True
    else:
        store.update_booking(bid, status="retry", last_error=result.message)
        title = f"✈️ Check-in needs you (PNR {booking['pnr']})"
        body = (f"{booking.get('airline')} {booking.get('flight_no')}\n"
                f"Error: {result.message}\nManual link: {adapter.checkin_url}\n"
                f"Screenshot: {result.screenshot_path}")
        notify(title, body)
        _ping_owner(booking, title, body)
        return False


def run_due_now() -> dict:
    """Sync wrapper: find due bookings and run them. Returns summary."""
    now = datetime.now(timezone.utc)
    due = store.due_bookings(now)
    results = []
    for b in due:
        ok = asyncio.run(run_checkin_for_booking(b))
        results.append({"pnr": b["pnr"], "ok": ok})
    return {"checked_at": now.isoformat(), "due_count": len(due), "results": results}


def start_scheduler(app=None) -> None:
    """Start APScheduler interval job (call once at app startup)."""
    from apscheduler.schedulers.background import BackgroundScheduler

    minutes = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(run_due_now, "interval", minutes=minutes, id="checkin-sweep", replace_existing=True)
    sched.start()
    print(f"[scheduler] every {minutes} min", flush=True)
    if app is not None:
        app.state.scheduler = sched
    return sched
