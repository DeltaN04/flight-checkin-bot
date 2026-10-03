"""Pluggable notifications: console always, email/telegram/whatsapp optional."""
from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText


def _env(k: str, default: str = "") -> str:
    return os.getenv(k, default)


def notify(title: str, body: str) -> dict:
    results: dict[str, str] = {"console": "logged"}
    print(f"\n===== [notify] {title} =====\n{body}\n", flush=True)
    # Email
    if _env("SMTP_HOST") and _env("NOTIFY_EMAIL_TO"):
        try:
            msg = MIMEText(body)
            msg["Subject"] = title
            msg["From"] = _env("SMTP_FROM", _env("SMTP_USER"))
            msg["To"] = _env("NOTIFY_EMAIL_TO")
            with smtplib.SMTP(_env("SMTP_HOST"), int(_env("SMTP_PORT", "587"))) as s:
                s.starttls()
                if _env("SMTP_USER"):
                    s.login(_env("SMTP_USER"), _env("SMTP_PASS"))
                s.send_message(msg)
            results["email"] = "sent"
        except Exception as e:
            results["email"] = f"failed: {e}"
    # Telegram
    if _env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"):
        try:
            import httpx

            r = httpx.post(
                f"https://api.telegram.org/bot{_env('TELEGRAM_BOT_TOKEN')}/sendMessage",
                json={"chat_id": _env("TELEGRAM_CHAT_ID"), "text": f"{title}\n{body}"},
                timeout=20,
            )
            results["telegram"] = "sent" if r.status_code == 200 else f"failed: {r.text[:200]}"
        except Exception as e:
            results["telegram"] = f"failed: {e}"
    # WhatsApp via Twilio
    if _env("TWILIO_SID") and _env("TWILIO_TOKEN") and _env("WHATSAPP_TO"):
        try:
            from twilio.rest import Client  # type: ignore

            client = Client(_env("TWILIO_SID"), _env("TWILIO_TOKEN"))
            client.messages.create(
                from_=_env("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"),
                to=_env("WHATSAPP_TO"),
                body=f"{title}\n{body}",
            )
            results["whatsapp"] = "sent"
        except Exception as e:
            results["whatsapp"] = f"failed/skipped: {e}"
    return results
