"""Contact CSV import: field mapping, email validation, dedup, suppression filter."""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Optional

from .. import models
from ..config import get_settings
from ..schemas import CsvImportResult

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


def is_valid_email(email: str) -> bool:
    return bool(email) and bool(EMAIL_RE.match(email.strip()))


def _column_resolver(header: Optional[list[str]], has_header: bool):
    """Return a function that maps a csv_column identifier (name or 0-based index str) -> column index."""

    def resolve(col_id: str) -> int:
        if col_id.isdigit():
            return int(col_id)
        if has_header and header:
            try:
                return header.index(col_id)
            except ValueError:
                # case-insensitive fallback
                for i, h in enumerate(header):
                    if h.strip().lower() == col_id.strip().lower():
                        return i
                raise ValueError(f"column '{col_id}' not found in header")
        raise ValueError(f"column '{col_id}' not found")

    return resolve


def import_contacts(
    db,
    *,
    owner_id: int,
    campaign_id: int,
    csv_text: str,
    field_map: dict[str, str],
    has_header: bool = True,
    skip_allowlist_check: bool = False,
) -> CsvImportResult:
    settings = get_settings()
    reader = list(csv.reader(io.StringIO(csv_text)))
    if not reader:
        return CsvImportResult(total_rows=0, imported=0, duplicates_skipped=0, invalid_email=0, suppressed_skipped=0)

    header = reader[0] if has_header else None
    data_rows = reader[1:] if has_header else reader
    resolve = _column_resolver(header, has_header)

    target_fields = ["email", "first_name", "last_name", "company", "title", "website", "custom_fields", "timezone", "source"]

    # Auto-derive the field map from header names when the caller provides none.
    # This makes header-based CSV uploads work out of the box (UI builds its own
    # map too, but the API must be usable directly / forgiving).
    if not field_map and has_header and header:
        auto_map: dict[str, str] = {}
        for t in target_fields:
            if t in ("custom_fields", "timezone", "source"):
                continue
            for h in header:
                if h.strip().lower() == t:
                    auto_map[t] = h.strip()
                    break
        field_map = auto_map

    suppressed = {
        s.email.lower()
        for s in db.query(models.Suppression).filter_by(owner_id=owner_id).all()
    }
    seen_emails: set[str] = set()
    # already-existing contacts
    existing = {
        c.email.lower()
        for c in db.query(models.Contact).filter_by(owner_id=owner_id).all()
    }

    result = CsvImportResult(
        total_rows=len(data_rows), imported=0, duplicates_skipped=0,
        invalid_email=0, suppressed_skipped=0, rejected_rows=[],
    )

    for ridx, row in enumerate(data_rows):
        rec: dict = {}
        for target, col_id in field_map.items():
            if target not in target_fields:
                continue
            try:
                idx = resolve(col_id)
                rec[target] = row[idx].strip() if idx < len(row) else ""
            except (ValueError, IndexError):
                rec[target] = ""
        email = (rec.get("email") or "").strip().lower()
        if not email:
            result.invalid_email += 1
            result.rejected_rows.append({"row": ridx + 1, "reason": "missing email", "data": rec})
            continue
        if not is_valid_email(email):
            result.invalid_email += 1
            result.rejected_rows.append({"row": ridx + 1, "reason": "invalid email", "data": rec})
            continue
        if email in suppressed:
            result.suppressed_skipped += 1
            continue
        if email in seen_emails or email in existing:
            result.duplicates_skipped += 1
            continue
        seen_emails.add(email)

        custom = rec.get("custom_fields")
        custom_json = None
        if custom:
            try:
                custom_json = json.dumps(json.loads(custom), ensure_ascii=False)
            except Exception:
                custom_json = json.dumps({"note": custom}, ensure_ascii=False)

        contact = models.Contact(
            owner_id=owner_id,
            email=email,
            first_name=rec.get("first_name") or None,
            last_name=rec.get("last_name") or None,
            company=rec.get("company") or None,
            title=rec.get("title") or None,
            website=rec.get("website") or None,
            custom_fields=custom_json,
            source=rec.get("source") or "csv_import",
            timezone=rec.get("timezone") or None,
            status="new",
        )
        db.add(contact)
        db.flush()
        cc = models.CampaignContact(campaign_id=campaign_id, contact_id=contact.id, status="queued")
        db.add(cc)
        result.imported += 1

    db.flush()
    return result
