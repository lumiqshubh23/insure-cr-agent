"""Parse the CR / FRD table from Dinesh's email and match FRD files to attachments."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

CR_ID_RE = re.compile(r"^(CR|SIRS)[\s_-]*\d+", re.I)
CR_ANY_RE = re.compile(r"\b(?:CR|SIRS)[\s_-]?\d{3,}\b", re.I)
# A cell line that points to the mail instead of naming a file, e.g. "Refer mail below", "Details in email"
MAIL_REF_RE = re.compile(r"\b(e-?mail|mail|below|refer|as discussed|in the thread)\b", re.I)
DOC_EXTS = (".docx", ".doc", ".pdf", ".xlsx", ".xls", ".pptx")


@dataclass
class CR:
    cr_id: str
    summary: str = ""
    status: str = ""
    frd_date: str = ""
    files: list[str] = field(default_factory=list)
    skip_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    mail_only: bool = False      # requirement given in the mail text instead of (or as well as) an FRD file


def _fix_mojibake(s: str) -> str:
    if "â€" in s or "Ã" in s:
        try:
            return s.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


def _text(cell, sep=" ") -> str:
    return _fix_mojibake(re.sub(r"[ \t\xa0]+", " ", cell.get_text(sep)).strip())


def _col(header: list[str], *keys: str) -> int | None:
    for k in keys:
        for i, h in enumerate(header):
            if k in h:
                return i
    return None


def parse_cr_tables(html: str) -> list[CR]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[CR] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header = [_text(c).lower() for c in rows[0].find_all(["td", "th"])]
        c_cr = _col(header, "cr number", "cr no", "cr")
        c_file = _col(header, "file name", "frd file", "document")
        if c_cr is None or c_file is None:
            continue
        c_sum = _col(header, "summary", "description")
        c_status = _col(header, "frd status", "status")
        c_date = _col(header, "frd date", "date")

        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= max(c_cr, c_file):
                continue
            cr_id = _text(cells[c_cr])
            if not CR_ID_RE.match(cr_id):
                continue
            get = lambda i: _text(cells[i]) if i is not None and i < len(cells) else ""
            file_lines = [ln.strip() for ln in _text(cells[c_file], "\n").split("\n") if ln.strip()]
            cr = CR(cr_id=cr_id, summary=get(c_sum), status=get(c_status), frd_date=get(c_date))

            joined = " ".join(file_lines).lower()
            if cr.status.strip().upper() in {"NA", "N/A", ""} or "no changes" in joined:
                cr.skip_reason = f"Not for development: status='{cr.status}', note='{' '.join(file_lines)}'"
            else:
                for f in file_lines:
                    looks_like_file = f.lower().endswith(DOC_EXTS) or re.match(r"^(FRD|SIRS|CR)[\s_-]", f, re.I)
                    if MAIL_REF_RE.search(f) and not looks_like_file:
                        cr.mail_only = True   # e.g. "Refer mail below": build from the mail text
                    else:
                        cr.files.append(f)
                if not file_lines:
                    cr.mail_only = True
                own = norm(cr_id)
                for f in file_lines:
                    m = re.search(r"(CR|SIRS)[\s_-]*\d+", f, re.I)
                    if m and norm(m.group(0)) != own and not f.upper().startswith("SIRS"):
                        cr.warnings.append(
                            f"FRD file '{f}' references {m.group(0)}, not {cr_id}. Confirm with the BA that this is the right FRD.")
            found.append(cr)
    return found


def merge(crs_by_id: dict[str, CR], new: list[CR]) -> None:
    """Merge CRs from several emails in the thread (later emails repeat the same table)."""
    for cr in new:
        key = norm(cr.cr_id)
        if key not in crs_by_id:
            crs_by_id[key] = cr
            continue
        old = crs_by_id[key]
        old.mail_only = old.mail_only or cr.mail_only
        for f in cr.files:
            if f not in old.files:
                old.files.append(f)
        for w in cr.warnings:
            if w not in old.warnings:
                old.warnings.append(w)


def norm(name: str) -> str:
    """Normalise a file / CR name for comparison. Only strips real document extensions (keeps 'V1.0')."""
    s = name.strip()
    low = s.lower()
    for ext in DOC_EXTS:
        if low.endswith(ext):
            s = s[: -len(ext)]
            break
    return re.sub(r"[\s_\-.()]+", "", _fix_mojibake(s)).lower()


def match_files(expected: list[str], attachment_names: list[str]) -> tuple[dict[str, str], list[str]]:
    """Return ({expected_name: attachment_name}, [missing expected names])."""
    by_norm = {norm(a): a for a in attachment_names}
    matched, missing = {}, []
    for exp in expected:
        n = norm(exp)
        hit = by_norm.get(n)
        if not hit:  # tolerate small suffix differences, e.g. '_Final' or a trailing date
            cands = [a for k, a in by_norm.items() if k.startswith(n) or n.startswith(k)]
            hit = cands[0] if len(cands) == 1 else None
        if hit:
            matched[exp] = hit
        else:
            missing.append(exp)
    return matched, missing


def branch_name(cr_id: str, prefix: str = "") -> str:
    b = re.sub(r"\s+", "-", cr_id.strip())
    b = re.sub(r"[^A-Za-z0-9._/-]", "", b)
    return f"{prefix}{b}"


def mail_text(html: str) -> str:
    """Plain text of a mail body (for spotting CR ids and for Claude)."""
    return _fix_mojibake(BeautifulSoup(html or "", "html.parser").get_text("\n"))


def crs_only_in_text(text: str, known: dict[str, CR]) -> list[CR]:
    """CR ids mentioned in the mail text but in no CR table: built from the mail text alone, flagged for review."""
    out, seen = [], set()
    for m in CR_ANY_RE.finditer(text):
        raw = re.sub(r"\s+", "-", m.group(0).strip())
        key = norm(raw)
        if key in known or key in seen:
            continue
        seen.add(key)
        out.append(CR(cr_id=raw.upper(), summary="(described in mail text only)", mail_only=True,
                      warnings=[f"{raw.upper()} appears only in the mail text (no CR table row). Built from the mail; confirm scope with the BA."]))
    return out
