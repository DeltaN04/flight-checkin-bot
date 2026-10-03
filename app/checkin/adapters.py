"""Per-airline Playwright adapters with seat + meal selection and payment handoff.

Flow per booking (T-48h job):
  1. retrieve booking (PNR + last name)
  2. select seat per traveller prefs (pick_seat over scraped seat map)
  3. select meal per traveller prefs (pick_meal over scraped meal list)
  4. continue to the payment / review step, then STOP — never pay.
     The agent captures the payment URL + amount + screenshot and notifies
     you ("take me to payment") so you complete payment yourself.

Why stop? Airline payments need OTP / 3-D Secure on your card — an agent
cannot and should not pay on your behalf. DRY_RUN=true simulates the whole
flow (seat+meal+payment) without touching airline sites.
Airlines change DOM often; scrapers are best-effort with graceful fallback
(keep auto-assigned seat, skip meal) — the handoff notification always
tells you what was/wasn't selected.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


class CheckinError(Exception):
    def __init__(self, message: str, screenshot: str = ""):
        super().__init__(message)
        self.screenshot = screenshot


@dataclass
class CheckinResult:
    ok: bool
    message: str
    seat: str = ""
    meal: str = ""
    extras: dict = field(default_factory=dict)  # {"seat_price_inr":..,"meal_price_inr":..}
    needs_payment: bool = False
    payment_url: str = ""
    payment_amount_inr: int = 0
    boarding_pass_path: str = ""
    screenshot_path: str = ""


def _data_dir() -> Path:
    d = Path(os.getenv("DATA_DIR", "./data"))
    (d / "boarding_passes").mkdir(parents=True, exist_ok=True)
    return d


def _handoff_minutes() -> int:
    try:
        return max(0, int(os.getenv("PAYMENT_HANDOFF_MINUTES", "0")))
    except ValueError:
        return 0


class BaseCheckinAdapter:
    airline_name = "Base"
    checkin_url = ""
    # generic "continue" buttons tried after seat/meal steps (per-airline override first)
    continue_selectors: list[str] = [
        "button:has-text('Continue')", "button:has-text('Proceed')",
        "button:has-text('Next')", "button:has-text('Save')",
        "button[type='submit']",
    ]

    async def run(self, booking: dict) -> CheckinResult:
        from app import store as _store
        from app.preferences import effective_prefs

        prefs = effective_prefs(booking, _store.get_preferences(booking.get("owner_phone", "") or ""))
        if os.getenv("DRY_RUN", "false").lower() == "true":
            return await self._dry_run(booking, prefs)
        from playwright.async_api import async_playwright

        headless = os.getenv("HEADLESS", "true").lower() != "false"
        bp_dir = _data_dir() / "boarding_passes"
        shot = str(bp_dir / f"{booking['pnr']}_attempt.png")
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=headless)
                context = await browser.new_context()
                page = await context.new_page()
                try:
                    result = await self._do_checkin(page, booking, prefs)
                    result.screenshot_path = result.screenshot_path or shot
                    try:
                        await page.screenshot(path=result.screenshot_path)
                    except Exception:
                        pass
                    # headed handoff: leave the payment page open for the user
                    if result.needs_payment and not headless and _handoff_minutes() > 0:
                        result.message += (
                            f" Browser kept open at payment page for "
                            f"{_handoff_minutes()} min — complete payment there."
                        )
                        await page.wait_for_timeout(_handoff_minutes() * 60 * 1000)
                finally:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                return result
        except CheckinError as e:
            return CheckinResult(ok=False, message=str(e), screenshot_path=e.screenshot or shot)
        except Exception as e:
            return CheckinResult(ok=False, message=f"{self.airline_name} automation error: {e}", screenshot_path=shot)

    async def _do_checkin(self, page, booking: dict, prefs: dict) -> CheckinResult:
        raise NotImplementedError

    # ---------------------------------------------------------- dry run

    async def _dry_run(self, booking: dict, prefs: dict) -> CheckinResult:
        from app.preferences import pick_meal, pick_seat

        cabin = _demo_cabin()
        seat = None if prefs.get("skip_seat") else pick_seat(cabin, prefs)
        meals = _demo_meals()
        meal = pick_meal(meals, prefs)
        seat_price = int((seat or {}).get("price_inr", 0) or 0)
        meal_price = int((meal or {}).get("price_inr", 0) or 0)
        total = seat_price + meal_price
        bp = _data_dir() / "boarding_passes" / f"{booking['pnr']}_dryrun.json"
        bp.write_text(json.dumps({
            "pnr": booking["pnr"], "flight": booking.get("flight_no"),
            "seat": (seat or {}).get("code", "AUTO"),
            "meal": (meal or {}).get("name", "none"),
            "amount_inr": total, "stopped_at": "payment page (not paid)",
        }, indent=2))
        if total > 0:
            return CheckinResult(
                ok=True, seat=(seat or {}).get("code", ""), meal=(meal or {}).get("name", ""),
                extras={"seat_price_inr": seat_price, "meal_price_inr": meal_price},
                needs_payment=True, payment_url=self.checkin_url,
                payment_amount_inr=total, boarding_pass_path=str(bp),
                message=f"DRY-RUN: seat {seat['code'] if seat else 'AUTO'} (₹{seat_price}), "
                        f"meal {meal['name'] if meal else 'none'} (₹{meal_price}). "
                        f"Stopped at payment page — ₹{total} due (not paid).",
            )
        return CheckinResult(
            ok=True, seat=(seat or {}).get("code", ""), meal=(meal or {}).get("name", ""),
            boarding_pass_path=str(bp),
            message="DRY-RUN: free seat+meal selected, checked in (nothing to pay).",
        )

    # ---------------------------------------------------------- shared steps

    async def _fill_first(self, page, selectors: list[str], value: str, timeout: int = 8000):
        last_err = ""
        for sel in selectors:
            try:
                await page.wait_for_selector(sel, timeout=timeout)
                await page.fill(sel, value)
                return sel
            except Exception as e:
                last_err = str(e)[:200]
        raise CheckinError(f"Could not find input for '{value}'. Tried {selectors}. Last: {last_err}")

    async def _click_first(self, page, selectors: list[str], timeout: int = 8000):
        last_err = ""
        for sel in selectors:
            try:
                await page.wait_for_selector(sel, timeout=timeout)
                await page.click(sel)
                return sel
            except Exception as e:
                last_err = str(e)[:200]
        raise CheckinError(f"Could not find button. Tried {selectors}. Last: {last_err}")

    async def _click_if_present(self, page, selectors: list[str], timeout: int = 3000) -> bool:
        for sel in selectors:
            try:
                await page.wait_for_selector(sel, timeout=timeout)
                await page.click(sel)
                return True
            except Exception:
                continue
        return False

    async def retrieve_booking(self, page, booking: dict,
                               pnr_selectors: list[str], name_selectors: list[str],
                               submit_selectors: list[str]) -> None:
        await page.goto(self.checkin_url, timeout=30000)
        await self._fill_first(page, pnr_selectors, booking["pnr"])
        await self._fill_first(page, name_selectors, booking.get("passenger_last_name", ""))
        await self._click_first(page, submit_selectors)
        await page.wait_for_timeout(5000)
        content = (await page.content()).lower()
        if "captcha" in content:
            raise CheckinError(
                f"{self.airline_name} showed a CAPTCHA — rerun with HEADLESS=false "
                "to solve it manually, then the agent continues to seat/meal/payment.")

    async def select_all_passengers(self, page, booking: dict) -> int:
        """Ensure EVERY traveler on the PNR is ticked for check-in.

        Airlines list each passenger with a checkbox (usually pre-ticked).
        Only touches boxes whose row mentions a known traveler surname, plus
        an explicit "Select all" button — never newsletter/insurance boxes.
        Returns boxes ensured-checked (0 = nothing to do / not found).
        """
        import json as _json

        names = [booking.get("passenger_last_name", "") or ""]
        try:
            for c in _json.loads(booking.get("companions_json", "") or "[]"):
                if isinstance(c, dict) and c.get("last"):
                    names.append(c["last"])
        except Exception:
            pass
        names = [n.upper() for n in names if n]
        if await self._click_if_present(page, [
                "button:has-text('Select all')", "button:has-text('All passengers')",
                "a:has-text('Select all')",
        ], timeout=3000):
            return 99
        if not names:
            return 0
        try:
            return await page.evaluate(
                """(names) => {
                let n = 0;
                for (const b of document.querySelectorAll('input[type="checkbox"]')) {
                  const row = (b.closest('tr,li,[class*="passenger" i],[class*="traveller" i],[class*="row" i]')?.innerText || '');
                  if (!names.some(nm => row.toUpperCase().includes(nm))) continue;
                  if (!b.checked) b.click();
                  n++;
                }
                return n;
              }""", names)
        except Exception:
            return 0

    async def select_seat(self, page, prefs: dict) -> tuple[str, int]:
        """Scrape seat map → pick_seat → click. Returns (seat_code, price). '' = kept auto-assign."""
        from app.preferences import pick_seat

        if prefs.get("skip_seat"):
            return "", 0  # ticket seat already fixed — plain check-in only
        await self._click_if_present(page, [
            "button:has-text('Seat')", "a:has-text('Seat')",
            "[data-testid*='seat' i]", "button:has-text('Select seat')",
        ])
        await page.wait_for_timeout(2500)
        seats = await self._scrape_seats(page)
        if not seats:
            return "", 0  # no seat map found — keep airline auto-assign
        choice = pick_seat(seats, prefs)
        if not choice:
            return "", 0  # nothing in budget — keep auto-assign
        clicked = await self._click_seat(page, choice["code"])
        if not clicked:
            return "", 0
        await page.wait_for_timeout(1500)
        return choice["code"], int(choice.get("price_inr", 0) or 0)

    async def _scrape_seats(self, page) -> list[dict]:
        """Best-effort seat map scrape → [{'code','price_inr','available'}]."""
        try:
            return await page.evaluate(
                """() => {
                const out = [];
                const els = document.querySelectorAll(
                  '[data-seat], [data-seat-number], button[class*="seat" i], div[class*="seat" i]');
                for (const el of els) {
                  const code = (el.getAttribute('data-seat')
                    || el.getAttribute('data-seat-number')
                    || (el.innerText || '')).trim().toUpperCase().replace(/\\s+/g,'');
                  if (!/^\\d{1,2}[A-K]$/.test(code)) continue;
                  if (out.some(s => s.code === code)) continue;
                  const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true'
                    || el.className.toLowerCase().includes('occupied')
                    || el.className.toLowerCase().includes('blocked');
                  const txt = (el.getAttribute('title') || '' + ' ' + (el.innerText || ''));
                  const m = txt.replace(/,/g,'').match(/(?:Rs\\.?|INR|₹)\\s*(\\d{2,5})/i);
                  out.push({code, price_inr: m ? parseInt(m[1]) : 0,
                            available: !disabled,
                            exit: /exit/i.test(el.className || '')});
                }
                return out;
              }"""
            )
        except Exception:
            return []

    async def _click_seat(self, page, code: str) -> bool:
        code = code.upper().replace(" ", "")
        sels = [
            f"[data-seat='{code}']", f"[data-seat-number='{code}']",
            f"button:has-text('{code}')",
        ]
        return await self._click_if_present(page, sels, timeout=4000)

    async def select_meal(self, page, prefs: dict) -> tuple[str, int]:
        """Scrape meal options → pick_meal → click. Returns (meal_name, price)."""
        from app.preferences import pick_meal

        if prefs.get("meal_pref") == "none":
            return "", 0
        await self._click_if_present(page, [
            "button:has-text('Meal')", "a:has-text('Meal')",
            "button:has-text('Add-on')", "button:has-text('Extras')",
            "a:has-text('Extras')",
        ])
        await page.wait_for_timeout(2500)
        meals = await self._scrape_meals(page)
        if not meals:
            return "", 0
        choice = pick_meal(meals, prefs)
        if not choice:
            return "", 0
        clicked = await self._click_meal(page, choice)
        return (choice.get("name") or choice.get("code") or ""), int(choice.get("price_inr", 0) or 0) if clicked else ("", 0)

    async def _scrape_meals(self, page) -> list[dict]:
        try:
            return await page.evaluate(
                """() => {
                const out = [];
                const els = document.querySelectorAll(
                  '[data-meal], [class*="meal" i], [class*="addon" i], [class*="add-on" i]');
                for (const el of els) {
                  const name = ((el.innerText || '').trim().split('\\n')[0] || '').slice(0, 80);
                  if (!name || out.some(m => m.name === name)) continue;
                  if (!/(veg|nonveg|non-veg|chicken|paneer|meal|biryani|pasta|sandwich|wrap|jain|vegan)/i.test(name)) continue;
                  const txt = (el.innerText || '').replace(/,/g,'');
                  const m = txt.match(/(?:Rs\\.?|INR|₹)\\s*(\\d{2,5})/i);
                  const disabled = el.disabled || (el.className || '').toLowerCase().includes('soldout');
                  out.push({name, price_inr: m ? parseInt(m[1]) : 0, available: !disabled});
                }
                return out.slice(0, 40);
              }"""
            )
        except Exception:
            return []

    async def _click_meal(self, page, choice: dict) -> bool:
        name = (choice.get("name") or "")[:30]
        return await self._click_if_present(page, [
            f"[data-meal='{choice.get('code', '')}']" if choice.get("code") else "###none###",
            f"button:has-text('{name}')",
        ], timeout=4000)

    async def proceed_to_payment(self, page, booking: dict) -> tuple[str, int]:
        """Click Continue/Proceed until a payment step is detected. Returns (url, amount_inr)."""
        for _ in range(4):
            await self._click_if_present(page, self.continue_selectors, timeout=3000)
            await page.wait_for_timeout(2500)
            try:
                content = (await page.content()).lower()
            except Exception:
                break
            if any(w in content for w in ("pay now", "proceed to pay", "payment", "upi", "card details", "netbanking", "total payable", "amount payable")):
                break
        url = ""
        try:
            url = page.url
        except Exception:
            pass
        amount = await self._scrape_amount(page)
        shot = str(_data_dir() / "boarding_passes" / f"{booking['pnr']}_payment.png")
        try:
            await page.screenshot(path=shot, full_page=True)
        except Exception:
            shot = ""
        return url or self.checkin_url, amount

    async def _scrape_amount(self, page) -> int:
        try:
            texts = await page.evaluate(
                """() => document.body ? document.body.innerText.slice(0, 20000) : ''""")
        except Exception:
            return 0
        best = 0
        for m in re.finditer(
                r"(?:total(?: payable| amount)?|amount payable|grand total|to pay)\s*(?:Rs\.?|INR|₹)?\s*([\d,]{3,7})",
                texts, re.I):
            try:
                best = max(best, int(m.group(1).replace(",", "")))
            except ValueError:
                pass
        return best

    def _finish(self, booking: dict, seat: str, meal: str,
                seat_price: int, meal_price: int, pay_url: str, pay_amount: int) -> CheckinResult:
        total = int(pay_amount or 0) or (seat_price + meal_price)
        extras = {"seat_price_inr": seat_price, "meal_price_inr": meal_price}
        bits = []
        bits.append(f"seat {seat} (₹{seat_price})" if seat else "seat: airline auto-assign (free)")
        bits.append(f"meal {meal} (₹{meal_price})" if meal else "meal: none selected")
        if total > 0:
            return CheckinResult(
                ok=True, seat=seat, meal=meal, extras=extras, needs_payment=True,
                payment_url=pay_url, payment_amount_inr=total,
                message=f"{self.airline_name}: {' + '.join(bits)}. "
                        f"Reached payment page — ₹{total} due. Agent stopped before paying; "
                        f"complete payment to finish check-in.",
            )
        return CheckinResult(
            ok=True, seat=seat, meal=meal, extras=extras,
            message=f"{self.airline_name}: {' + '.join(bits)}. Checked in free — no payment needed.",
        )


# ------------------------------------------------------------------ airlines

class IndiGoAdapter(BaseCheckinAdapter):
    airline_name = "IndiGo"
    checkin_url = "https://www.goindigo.in/information/check-in.html"

    async def _do_checkin(self, page, booking: dict, prefs: dict) -> CheckinResult:
        await self.retrieve_booking(page, booking,
            ["input[name*='pnr' i]", "input[placeholder*='PNR' i]", "#pnr", "input[id*='booking' i]"],
            ["input[name*='last' i]", "input[placeholder*='last name' i]", "#lastName"],
            ["button:has-text('Check-in')", "button:has-text('Retrieve')", "button[type='submit']"])
        await self.select_all_passengers(page, booking)
        seat, seat_price = await self.select_seat(page, prefs)
        meal, meal_price = await self.select_meal(page, prefs)
        pay_url, pay_amount = await self.proceed_to_payment(page, booking)
        return self._finish(booking, seat, meal, seat_price, meal_price, pay_url, pay_amount)


class AirIndiaAdapter(BaseCheckinAdapter):
    airline_name = "Air India"
    checkin_url = "https://www.airindia.com/in/en/manage-bookings/check-in.html"

    async def _do_checkin(self, page, booking: dict, prefs: dict) -> CheckinResult:
        await self.retrieve_booking(page, booking,
            ["input[name*='booking' i]", "input[placeholder*='booking' i]", "input[name*='pnr' i]", "#bookingId"],
            ["input[name*='last' i]", "input[placeholder*='last' i]", "#lastName"],
            ["button:has-text('Check-in')", "button:has-text('Retrieve')", "button[type='submit']"])
        await self.select_all_passengers(page, booking)
        seat, seat_price = await self.select_seat(page, prefs)
        meal, meal_price = await self.select_meal(page, prefs)
        pay_url, pay_amount = await self.proceed_to_payment(page, booking)
        return self._finish(booking, seat, meal, seat_price, meal_price, pay_url, pay_amount)


class SpiceJetAdapter(BaseCheckinAdapter):
    airline_name = "SpiceJet"
    checkin_url = "https://www.spicejet.com/checkin"

    async def _do_checkin(self, page, booking: dict, prefs: dict) -> CheckinResult:
        await page.goto(self.checkin_url, timeout=30000)
        await self._fill_first(page, ["input[placeholder*='PNR' i]", "input[name*='pnr' i]", "#pnr"], booking["pnr"])
        last, email = booking.get("passenger_last_name", ""), booking.get("email", "")
        try:
            if last:
                await self._fill_first(page, ["input[placeholder*='last' i]", "input[name*='last' i]"], last, timeout=4000)
            elif email:
                await self._fill_first(page, ["input[type='email']", "input[placeholder*='email' i]"], email, timeout=4000)
        except CheckinError:
            pass
        await self._click_first(page, ["button:has-text('Check')", "button[type='submit']"])
        await page.wait_for_timeout(5000)
        await self.select_all_passengers(page, booking)
        seat, seat_price = await self.select_seat(page, prefs)
        meal, meal_price = await self.select_meal(page, prefs)
        pay_url, pay_amount = await self.proceed_to_payment(page, booking)
        return self._finish(booking, seat, meal, seat_price, meal_price, pay_url, pay_amount)


class AkasaAdapter(BaseCheckinAdapter):
    airline_name = "Akasa Air"
    checkin_url = "https://www.akasaair.com/check-in"

    async def _do_checkin(self, page, booking: dict, prefs: dict) -> CheckinResult:
        await self.retrieve_booking(page, booking,
            ["input[placeholder*='PNR' i]", "input[name*='pnr' i]"],
            ["input[placeholder*='last' i]", "input[name*='last' i]"],
            ["button:has-text('Check')", "button[type='submit']"])
        await self.select_all_passengers(page, booking)
        seat, seat_price = await self.select_seat(page, prefs)
        meal, meal_price = await self.select_meal(page, prefs)
        pay_url, pay_amount = await self.proceed_to_payment(page, booking)
        return self._finish(booking, seat, meal, seat_price, meal_price, pay_url, pay_amount)


class AirIndiaExpressAdapter(BaseCheckinAdapter):
    airline_name = "Air India Express"
    checkin_url = "https://www.airindiaexpress.com/manage/check-in"

    async def _do_checkin(self, page, booking: dict, prefs: dict) -> CheckinResult:
        await self.retrieve_booking(page, booking,
            ["input[name*='pnr' i]", "input[placeholder*='PNR' i]"],
            ["input[name*='last' i]", "input[placeholder*='last' i]"],
            ["button:has-text('Check')", "button[type='submit']"])
        await self.select_all_passengers(page, booking)
        seat, seat_price = await self.select_seat(page, prefs)
        meal, meal_price = await self.select_meal(page, prefs)
        pay_url, pay_amount = await self.proceed_to_payment(page, booking)
        return self._finish(booking, seat, meal, seat_price, meal_price, pay_url, pay_amount)


ADAPTERS: dict[str, BaseCheckinAdapter] = {
    "6E": IndiGoAdapter(),
    "AI": AirIndiaAdapter(),
    "SG": SpiceJetAdapter(),
    "QP": AkasaAdapter(),
    "IX": AirIndiaExpressAdapter(),
    "UK": AirIndiaAdapter(),  # Vistara merged into Air India; same flow
}


def get_adapter(code: str) -> BaseCheckinAdapter:
    code = (code or "").upper()
    if code in ADAPTERS:
        return ADAPTERS[code]
    return AirIndiaAdapter()


# ------------------------------------------------------------- demo data

def _demo_cabin() -> list[dict]:
    seats = []
    for row in (5, 6, 7, 14, 15, 28, 29, 30):
        for letter in "ABCDEF":
            price = 0 if row >= 28 else (800 if letter in "AF" else 500)
            seats.append({"code": f"{row}{letter}", "price_inr": price, "available": True})
    seats.append({"code": "15A", "price_inr": 800, "available": False})  # taken
    return seats


def _demo_meals() -> list[dict]:
    return [
        {"name": "Veg Paneer Wrap", "price_inr": 350, "available": True},
        {"name": "Veg Dal Makhani + Rice", "price_inr": 450, "available": True},
        {"name": "Chicken Biryani", "price_inr": 450, "available": True},
        {"name": "Vegan Quinoa Salad", "price_inr": 500, "available": True},
        {"name": "Jain Veg Thali", "price_inr": 450, "available": True},
    ]
