# Flight Auto Check-in Agent (India)

LLM-powered agent that:
1. **Ingests** your ticket (PDF/screenshot upload, Gmail forward/watch, or manual PNR entry)
2. **Parses** PNR, airline, flight, departure time, passengers with an LLM (+ regex fallback, works offline)
3. **Schedules** a job for T-48h (airline window: 48h → 1-2h before departure)
4. **Auto check-ins** via Playwright browser automation (IndiGo, Air India, SpiceJet, Akasa, Air India Express)
5. **Notifies** you (Email + Telegram + WhatsApp via Twilio/CallMeBot + console)

> Reality check: Indian carriers have **no public check-in API**. This uses controlled browser automation against their public web check-in pages. CAPTCHA / OTP / seat-paywalls / schedule changes can block full-auto — so the agent retries and falls back to a "click-this-prefilled-link" notification.

## Quickstart

```bash
cd flight-checkin-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # for real browser check-in
cp .env.example .env          # fill keys
uvicorn app.main:app --reload --port 8001
# UI → http://localhost:8001
```

## How to use

1. Open UI, set **My preferences** once (seat + meal, or type e.g. *"window seat in front, veg paneer meal"*).
2. Upload ticket / forward Gmail / enter PNR (per-booking seat/meal overrides optional).
3. Agent shows parsed booking + scheduled check-in time (departure − 48h).
4. At T-48h it launches headless Chromium, retrieves the booking, clicks your
   preferred **seat** (exact seat > type+zone, within your max ₹; else keeps free auto-assign)
   and **meal** (diet match + dish + max ₹; skipped if `none`), then continues to the
   **payment/review step and stops** — capturing amount + payment URL + screenshot.
5. You get Email/Telegram/WhatsApp:
   - 💳 **Payment needed** (seat 5A + meal + ₹1150 due + pay link) → pay yourself, then hit **Paid** (or `POST /api/bookings/{id}/paid`).
   - ✅ **Checked in** (only when seat+meal were free — nothing to pay).
   - ✈️ **Needs you** (CAPTCHA/selector breakage → prefilled manual link).

> The agent **never pays**: card OTP / 3-D Secure legally requires you.
> Tip: run with `HEADLESS=false PAYMENT_HANDOFF_MINUTES=15` on your own machine and the
> browser stays open at the payment page for you to pay directly.

## Airline support

| Airline | Code | Web check-in URL | ID needed |
|---|---|---|---|
| IndiGo | 6E | https://www.goindigo.in/information/check-in.html | PNR + Last name |
| Air India | AI | https://www.airindia.com/in/en/manage-bookings/check-in.html | PNR + Last name |
| SpiceJet | SG | https://www.spicejet.com/checkin | PNR + Last name/email |
| Akasa Air | QP | https://www.akasaair.com/check-in | PNR + Last name |
| Air India Express | IX | https://www.airindiaexpress.com/manage/check-in | PNR + Last name |

Each has an adapter in `app/checkin/`. They share `BaseCheckinAdapter` (Playwright). Selectors are best-effort — airlines change DOM often; update selector in one file.

## LLM parsing

`app/parser_llm.py` tries in order:
1. OpenAI-compatible API (`OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL`) — works with OpenAI, Together, OpenRouter, local LM Studio.
2. Ollama local (`OLLAMA_MODEL`, default `llama3.1:8b`).
3. Deterministic regex fallback (PNR `[A-Z0-9]{6}`, flight `6E 123`, dates, etc.) — always works offline.

So ticket upload works even with no API key.

## WhatsApp bot (family use)

Each family member chats with the bot — separate bookings + taste per phone number:

- Send **hi** → menu. Send the **e-ticket PDF** (screenshot ok, PDF best) → scheduled.
- Or one line: ``ABC123 Sharma 2026-12-20 14:35 6E 2345 DEL BOM``
- Taste in plain words: *window seat in front, veg meal* · *my bookings* · *paid ABC123*
- At T-48h the member gets the check-in / 💳 payment alert **on their own WhatsApp**.

Setup: Meta WhatsApp Cloud API (free, official) → see **DEPLOY.md**
(webhook `GET/POST /webhook/whatsapp`, `FAMILY_PHONES` allowlist, Docker deploy,
24h-window templates). `DRY_RUN=true` simulates everything offline.

## QR bridge (no Meta setup — spare number)

Unofficial alternative: pair a **spare** number by scanning a QR. Same bot brain,
same per-member bookings — but unofficial, so never use anyone's main number
(ban risk, dropped sessions).

```bash
cd wa-bridge && npm install
./start-bridge.sh        # prints QR → scan with the SPARE number (Linked Devices)
# in another terminal:
./start.sh               # the Python API (BOT_URL, default :8001)
```

How it connects: bridge forwards incoming texts/photos/PDFs to
`POST /api/wa/process` + `/api/wa/upload` (shared `WA_BRIDGE_SECRET`), and the
scheduler pushes T-48h alerts back via the bridge `/send` endpoint whenever Meta
is not configured. Groups are ignored — family chats 1:1 with the spare number.
If the session drops, restart the bridge; if logged out, delete `wa-bridge/auth/`
and rescan.

## Scheduler

APScheduler `IntervalTrigger(minutes=5)` in-process. On boot it reloads pending bookings from SQLite. For production use a persistent worker (systemd/docker) — see `.env.example`.

## Project layout

```
flight-checkin-agent/
  app/main.py            FastAPI: upload / manual / prefs / gmail-scan / run-due / paid
  app/store.py           SQLite (data/agent.db) + bookings + attempts + preferences
  app/preferences.py     seat/meal prefs, natural-language parsing, pick_seat/pick_meal
  app/parser_llm.py      PDF→text→LLM→Booking
  app/scheduler.py       due-job loop (dep − 48h) + payment-handoff notify
  app/notifier.py        email + telegram + whatsapp + console
  app/gmail_watcher.py   Gmail API scan for e-tickets
  app/checkin/*.py       per-airline Playwright adapters (retrieve → seat → meal → payment stop)
  app/bot.py             WhatsApp conversation logic (commands, one-liner PNR, uploads)
  app/whatsapp.py        Meta Cloud API transport (send/verify/webhook parse/media)
  web/templates/index.html  UI: prefs + upload/manual + bookings + pay links
  Dockerfile, docker-compose.yml, DEPLOY.md  deploy + family onboarding
  data/                  db + uploads + boarding passes
```

## Disclaimer

For personal use. Respect airline ToS. Store PNRs securely; `data/` is gitignored.
