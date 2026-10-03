"""Gmail watcher: scan inbox for e-tickets (optional, needs OAuth).

Setup:
  1. Create Google Cloud OAuth client (Desktop), download as client_secret.json
     into the project root.
  2. GET /gmail/auth -> follow URL, paste code (first time only, stores token.json).
  3. POST /gmail/scan -> imports any new e-tickets as bookings.

Kept dependency-free at import time: google libs imported lazily so the app
runs without them until you use Gmail.
"""
from __future__ import annotations

import base64
import re

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SEARCH_QUERY = "subject:(e-ticket OR itinerary OR booking confirmation) newer_than:60d"


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from pathlib import Path

    token = Path("token.json")
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
            creds = flow.run_local_server(port=0)
        token.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def scan_inbox(max_messages: int = 20) -> list[dict]:
    """Returns list of {subject, snippet, body_text} for likely e-tickets."""
    svc = _service()
    res = svc.users().messages().list(userId="me", q=SEARCH_QUERY, maxResults=max_messages).execute()
    out = []
    for m in res.get("messages", []):
        full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        headers = {h["name"]: h["value"] for h in full["payload"].get("headers", [])}
        snippet = full.get("snippet", "")
        body_text = snippet
        # try to extract plain-text part
        try:
            for part in full["payload"].get("parts", []):
                data = (part.get("body", {}) or {}).get("data")
                if data and "text" in str(part.get("mimeType", "")):
                    body_text = base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
                    break
        except Exception:
            pass
        out.append({"subject": headers.get("Subject", ""), "snippet": snippet, "body_text": body_text})
    return out
