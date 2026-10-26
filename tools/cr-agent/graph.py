"""Minimal Microsoft Graph client (app-only / client-credentials) for reading FRD emails."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

GRAPH = "https://graph.microsoft.com/v1.0"


@dataclass
class MailMessage:
    id: str
    subject: str
    sender: str
    received: datetime
    html: str = ""


@dataclass
class MailAttachment:
    message_id: str
    id: str
    name: str
    size: int


class Graph:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str, mailbox: str):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.mailbox = mailbox
        self._token = None
        self._token_exp = 0.0

    # ---------- auth ----------
    def _auth_header(self) -> dict:
        if not self._token or time.time() > self._token_exp - 120:
            r = requests.post(
                f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
                timeout=30,
            )
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._token_exp = time.time() + int(body.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, url: str, params: dict | None = None, extra_headers: dict | None = None, stream=False):
        headers = self._auth_header() | (extra_headers or {})
        r = requests.get(url if url.startswith("http") else GRAPH + url,
                         params=params, headers=headers, timeout=120, stream=stream)
        r.raise_for_status()
        return r

    # ---------- mail ----------
    def find_messages(self, search: str, subject_regex: str, senders: list[str], lookback_days: int) -> list[MailMessage]:
        """Search all folders, then filter subject/sender/date client-side. Returns oldest first."""
        since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        pattern = re.compile(subject_regex, re.I)
        senders_l = [s.lower() for s in senders if s]
        params = {
            "$search": f'"{search}"',
            "$top": "50",
            "$select": "id,subject,from,receivedDateTime,hasAttachments",
        }
        out: list[MailMessage] = []
        url = f"/users/{self.mailbox}/messages"
        while url:
            data = self._get(url, params=params).json()
            params = None  # nextLink already carries the query
            for m in data.get("value", []):
                received = datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00"))
                sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "").lower()
                if received < since or not pattern.search(m.get("subject", "")):
                    continue
                if senders_l and sender not in senders_l:
                    continue
                out.append(MailMessage(m["id"], m["subject"], sender, received))
            url = data.get("@odata.nextLink")
        out.sort(key=lambda m: m.received)
        return out

    def load_body(self, msg: MailMessage) -> MailMessage:
        data = self._get(f"/users/{self.mailbox}/messages/{msg.id}",
                         params={"$select": "body"},
                         extra_headers={"Prefer": 'outlook.body-content-type="html"'}).json()
        msg.html = data.get("body", {}).get("content", "")
        return msg

    def list_attachments(self, msg: MailMessage) -> list[MailAttachment]:
        data = self._get(f"/users/{self.mailbox}/messages/{msg.id}/attachments",
                         params={"$select": "id,name,size,isInline"}).json()
        out = []
        for a in data.get("value", []):
            if a.get("@odata.type") != "#microsoft.graph.fileAttachment" or a.get("isInline"):
                continue
            out.append(MailAttachment(msg.id, a["id"], a["name"], int(a.get("size", 0))))
        return out

    def download_attachment(self, att: MailAttachment, dest_path: str) -> str:
        r = self._get(f"/users/{self.mailbox}/messages/{att.message_id}/attachments/{att.id}/$value", stream=True)
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        return dest_path
