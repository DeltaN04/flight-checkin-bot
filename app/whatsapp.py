"""Meta WhatsApp Cloud API transport (free, official).

Setup is documented in DEPLOY.md. Env:
  META_WA_TOKEN            long-lived user/system-user token
  META_WA_PHONE_NUMBER_ID  sending number's ID
  META_WA_VERIFY_TOKEN     any secret string you invent (webhook verify)
  META_WA_APP_SECRET       Meta app secret (webhook signature check, optional but recommended)
  META_WA_TEMPLATE         approved utility template name for 24h+ alerts (optional)
  META_WA_TEMPLATE_LANG    template language, default 'en'
  FAMILY_PHONES            comma-separated digits allowlist; empty = anyone (warns)
"""
from __future__ import annotations

import hashlib
import hmac
import os

GRAPH = "https://graph.facebook.com/v21.0"


def _env(k: str, default: str = "") -> str:
    return os.getenv(k, default)


def configured() -> bool:
    return bool(_env("META_WA_TOKEN") and _env("META_WA_PHONE_NUMBER_ID"))


def family_allowlist() -> set[str]:
    import re as _re

    return {_re.sub(r"\D", "", p) for p in _env("FAMILY_PHONES", "").split(",") if p.strip()}


def is_allowed(phone: str) -> bool:
    allow = family_allowlist()
    if not allow:
        return True
    import re as _re

    return _re.sub(r"\D", "", phone or "") in allow


def verify_token_ok(token: str) -> bool:
    expected = _env("META_WA_VERIFY_TOKEN")
    return bool(expected) and hmac.compare_digest(token or "", expected)


def verify_signature(raw_body: bytes, header: str) -> bool:
    """Check X-Hub-Signature-256. No secret configured → skip (warn)."""
    secret = _env("META_WA_APP_SECRET")
    if not secret:
        print("[whatsapp] warning: META_WA_APP_SECRET not set, skipping signature check", flush=True)
        return True
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + digest, header)


def _post(path: str, payload: dict) -> dict:
    import httpx

    r = httpx.post(
        f"{GRAPH}/{_env('META_WA_PHONE_NUMBER_ID')}{path}",
        headers={"Authorization": f"Bearer {_env('META_WA_TOKEN')}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def send_text(to: str, body: str) -> dict:
    return _post("/messages", {
        "messaging_product": "whatsapp", "to": to,
        "type": "text", "text": {"body": body[:4096]},
    })


def send_menu(to: str, name: str = "there") -> dict:
    return _post("/messages", {
        "messaging_product": "whatsapp", "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": f"Hi {name}! ✈️ What do you want to do?"},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "ADD", "title": "➕ Add ticket"}},
                {"type": "reply", "reply": {"id": "BOOKINGS", "title": "📋 My bookings"}},
                {"type": "reply", "reply": {"id": "PREFS", "title": "⭐ My taste"}},
            ]},
        },
    })


def send_template(to: str, title: str, body: str) -> dict:
    """Business-initiated alert via approved template (works outside the 24h window)."""
    name = _env("META_WA_TEMPLATE")
    if not name:
        raise RuntimeError("META_WA_TEMPLATE not set")
    return _post("/messages", {
        "messaging_product": "whatsapp", "to": to,
        "type": "template",
        "template": {
            "name": name, "language": {"code": _env("META_WA_TEMPLATE_LANG", "en")},
            "components": [{"type": "body", "parameters": [
                {"type": "text", "text": title[:200]},
                {"type": "text", "text": body[:900]},
            ]}],
        },
    })


def notify_owner_whatsapp(owner: str, title: str, body: str) -> str:
    """Best-effort owner ping: template if configured (24h+ safe), else free text."""
    if not configured() or not owner:
        return "skipped (whatsapp not configured)"
    try:
        if _env("META_WA_TEMPLATE"):
            send_template(owner, title, body)
            return "sent (template)"
        send_text(owner, f"{title}\n{body}")
        return "sent (text)"
    except Exception as e:
        print(f"[whatsapp] notify failed for {owner}: {e}", flush=True)
        return f"failed: {e}"


def parse_inbound(payload: dict) -> list[dict]:
    """Flatten Meta webhook → [{'from','kind','text','media_id','mime','filename','button_id'}].

    kind: 'text' | 'media' | 'button'.
    """
    out = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for msg in value.get("messages", []) or []:
                sender = msg.get("from", "")
                mtype = msg.get("type", "")
                if mtype == "text":
                    out.append({"from": sender, "kind": "text",
                                "text": (msg.get("text", {}) or {}).get("body", "")})
                elif mtype in ("image", "document"):
                    media = msg.get(mtype, {}) or {}
                    out.append({"from": sender, "kind": "media",
                                "media_id": media.get("id", ""),
                                "mime": media.get("mime_type", ""),
                                "filename": media.get("filename", "") or "ticket",
                                "text": (msg.get("caption", "") or "")})
                elif mtype == "button":
                    out.append({"from": sender, "kind": "button",
                                "button_id": (msg.get("button", {}) or {}).get("payload", ""),
                                "text": (msg.get("button", {}) or {}).get("text", "")})
                elif mtype == "interactive":
                    reply = ((msg.get("interactive", {}) or {}).get("button_reply", {})) or {}
                    out.append({"from": sender, "kind": "button",
                                "button_id": reply.get("id", ""), "text": reply.get("title", "")})
    return out


def download_media(media_id: str) -> tuple[bytes, str]:
    """Returns (bytes, mime). Two-step: media URL, then bytes."""
    import httpx

    headers = {"Authorization": f"Bearer {_env('META_WA_TOKEN')}"}
    r = httpx.get(f"{GRAPH}/{media_id}", headers=headers, timeout=30)
    r.raise_for_status()
    url = r.json().get("url", "")
    r2 = httpx.get(url, headers=headers, timeout=60)
    r2.raise_for_status()
    mime = r2.headers.get("content-type", "").split(";")[0].strip()
    return r2.content, mime
