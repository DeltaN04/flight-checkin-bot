# Deploying the Family WhatsApp Bot

The bot uses the **Meta WhatsApp Cloud API** — official and free. Your family just
messages the bot number on WhatsApp: send ticket PDF → set taste → get check-in +
payment alerts. Each member's bookings and taste are kept separate by phone number.

## 1. Create the WhatsApp app (10 min, Meta side)

1. Go to [developers.facebook.com](https://developers.facebook.com) → **Create App** → choose
   *Other → Business* (or *Other* if Business isn't offered).
2. Add product **WhatsApp** → **API Setup**.
3. You'll get a **test number** with a temporary token (expires in 24h — fine for testing).
   Note the **Phone number ID**.
4. Under *To*, add your own + family numbers (test mode allows 5 recipients; each gets
   a code on WhatsApp to accept).
5. For permanent use: **API Setup → Add phone number** (your own business number, OTP verified),
   then create a **System User** (App Settings → Roles or Business Settings) with
   `whatsapp_business_messaging` permission and generate a **permanent token**.
   Display-name approval can take a few days — plan ahead.

## 2. Put this server on a public URL

Meta must reach `https://YOUR-HOST/webhook/whatsapp`. Pick one:

**A. VPS (recommended for 24×7):** any ₹500/mo VPS —
```bash
git clone <repo> && cd flight-checkin-agent
cp .env.example .env   # fill META_WA_*, FAMILY_PHONES
docker compose up -d --build
# point a domain at the VPS and put Caddy/Nginx in front for HTTPS
```

**B. Home Mac/PC + tunnel (easiest test):**
```bash
uvicorn app.main:app --port 8001
cloudflared tunnel --url http://localhost:8001   # prints https://xxx.trycloudflare.com
```
Use that URL as the webhook. Free, but the URL changes on restart.

**C. PaaS:** Railway / Render / Fly.io all build the `Dockerfile` directly —
set the env vars in their dashboard, deploy, use the given URL.

## 3. Connect the webhook

In Meta dashboard → WhatsApp → **Configuration → Webhook**:
- Callback URL: `https://YOUR-HOST/webhook/whatsapp`
- Verify token: exactly your `META_WA_VERIFY_TOKEN`
- Click Verify → subscribe to the **`messages`** field.

Then in `.env`:
```bash
META_WA_TOKEN=<permanent token>
META_WA_PHONE_NUMBER_ID=<phone number id>
META_WA_VERIFY_TOKEN=<same as above>
META_WA_APP_SECRET=<App Settings → Basic → App secret>
FAMILY_PHONES=+91XXXXXXXXXX,+91YYYYYYYYYY
```
Restart. Family sends **hi** → bot replies with the menu.

## 4. Family onboarding (send them this)

> Save this number as ✈️ *Flight Bot*. Send **hi**, then:
> 1. Forward your **e-ticket PDF** (screenshot works too, PDF is best)
> 2. Tell it your taste: *"window seat in front, veg meal"*
> 3. 48 hrs before the flight it checks you in and sends a **payment link** here
> 4. Pay on the airline page, then reply **paid ABC123**
> 5. *my bookings* anytime for status

Each member only sees their own flights.

## 5. The 24-hour rule (important)

WhatsApp lets the bot freely reply **within 24h of the member's last message**.
But check-in happens at T-48h — likely *outside* that window. Two options:

- **Recommended:** create one **Utility template** (WhatsApp Manager → Message templates),
  e.g. `flight_alert` with body `{{1}}\n{{2}}`, set `META_WA_TEMPLATE=flight_alert`.
  The bot then sends alerts via template (title + details as params) — always delivered.
- **Without template:** alerts still send as free text, but Meta may reject them outside
  24h. Ask family to send **hi** the day before the flight to open the window.

## 6. Notes & limits

- **Photos:** screenshot tickets are read via OCR only if the server has Tesseract
  (`apt install tesseract-ocr` + `pip install Pillow pytesseract`). PDFs always work — prefer PDF.
- **Payments:** the bot never pays (card OTP needs a human). It stops at the payment page.
- **Privacy:** `FAMILY_PHONES` allowlist blocks strangers. PNRs live in `data/agent.db` on your server.
- **Test first:** `DRY_RUN=true` simulates seat+meal+payment without touching airlines.
- **Alternative:** Twilio WhatsApp instead of Meta (paid number, same webhook idea) —
  the sender notifier in `app/notifier.py` already supports Twilio for single-user alerts.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Meta Verify fails | token mismatch; URL must be HTTPS and return challenge |
| No reply to messages | `messages` field not subscribed; check logs; token expired (test tokens last 24h) |
| Template rejected | use **Utility** category, `{{1}} {{2}}` params only |
| Bot says "not registered" | add the number to `FAMILY_PHONES`, restart |
