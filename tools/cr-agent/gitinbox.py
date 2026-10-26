"""Read FRD emails that Power Automate drops into the `frd-inbox` branch.

Layout written by the Power Automate flow (one folder per email):

    frd-inbox/<yyyyMMdd-HHmmss>-<rand>/meta.json        {"subject", "from", "received"}
    frd-inbox/<...>/email.html                           the email body (HTML)
    frd-inbox/<...>/attachments/<file name>              each attachment

Same interface as graph.Graph, so main.py can use either source.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from graph import MailAttachment, MailMessage


class GitInbox:
    def __init__(self, repo: Path, branch: str = "frd-inbox", root: str = "frd-inbox"):
        self.repo = repo
        self.ref = f"origin/{branch}"
        self.root = root.strip("/")
        subprocess.run(["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                       cwd=repo, check=True)
        out = subprocess.run(["git", "ls-tree", "-r", "--name-only", self.ref, f"{self.root}/"],
                             cwd=repo, check=True, text=True, capture_output=True).stdout
        self.paths = [p for p in out.splitlines() if p]

    def _show(self, path: str) -> bytes:
        return subprocess.run(["git", "show", f"{self.ref}:{path}"], cwd=self.repo,
                              check=True, capture_output=True).stdout

    def find_messages(self, _search: str, subject_regex: str, senders: list[str], lookback_days: int) -> list[MailMessage]:
        since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        pattern = re.compile(subject_regex, re.I)
        senders_l = [s.lower() for s in senders if s]
        out = []
        for p in self.paths:
            if not p.endswith("/meta.json"):
                continue
            folder = p[: -len("/meta.json")]
            try:
                meta = json.loads(self._show(p).decode("utf-8-sig"))
            except (json.JSONDecodeError, subprocess.CalledProcessError):
                print(f"!! unreadable {p}, skipping")
                continue
            subject = meta.get("subject", "")
            sender = str(meta.get("from", "")).lower()
            try:
                received = datetime.fromisoformat(str(meta.get("received", "")).replace("Z", "+00:00"))
                if received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
            except ValueError:
                received = datetime.now(timezone.utc)
            if received < since or not pattern.search(subject):
                continue
            if senders_l and not any(s in sender for s in senders_l):
                continue
            out.append(MailMessage(folder, subject, sender, received))
        out.sort(key=lambda m: m.received)
        return out

    def load_body(self, msg: MailMessage) -> MailMessage:
        path = f"{msg.id}/email.html"
        msg.html = self._show(path).decode("utf-8", errors="replace") if path in self.paths else ""
        return msg

    def list_attachments(self, msg: MailMessage) -> list[MailAttachment]:
        prefix = f"{msg.id}/attachments/"
        return [MailAttachment(msg.id, p, p[len(prefix):], 0)
                for p in self.paths if p.startswith(prefix) and "/" not in p[len(prefix):]]

    def download_attachment(self, att: MailAttachment, dest_path: str) -> str:
        Path(dest_path).write_bytes(self._show(att.id))
        return dest_path
