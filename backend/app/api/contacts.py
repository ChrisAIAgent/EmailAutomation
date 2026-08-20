"""Contact CRM endpoints. Contacts are the shared customer source for Inbox and Campaigns."""
from __future__ import annotations

import csv
import io
import json
import re

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..schemas import ContactCreate, ContactUpdate
from .deps import ensure_owner, get_db

router = APIRouter(prefix="/api/contacts", tags=["contacts"])
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ALLOWED_CATEGORIES = {"prospect", "qualified", "customer", "partner", "won", "invalid"}
ALLOWED_INTENT = {"high", "medium", "low", "unknown"}


def _tags(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def _serialize(contact: models.Contact, db: Session) -> dict:
    campaign_count = db.query(models.CampaignContact).filter_by(
        contact_id=contact.id, membership_active=True
    ).count()
    thread_count = db.query(models.EmailThread).filter(
        models.EmailThread.contact_email == contact.email
    ).count()
    return {
        "id": contact.id, "email": contact.email,
        "first_name": contact.first_name, "last_name": contact.last_name,
        "company": contact.company, "title": contact.title, "phone": contact.phone,
        "website": contact.website, "category": contact.category or "prospect",
        "tags": _tags(contact.tags), "intent_level": contact.intent_level or "unknown",
        "notes": contact.notes, "source": contact.source, "status": contact.status,
        "lifecycle_stage": contact.lifecycle_stage or "new_customer",
        "next_action": contact.next_action,
        "manual_lock": bool(contact.manual_lock),
        "manual_updated_at": contact.manual_updated_at,
        "last_contacted_at": contact.last_contacted_at,
        "next_follow_up_at": contact.next_follow_up_at,
        "campaign_count": campaign_count, "thread_count": thread_count,
        "created_at": contact.created_at, "updated_at": contact.updated_at,
    }


def _validate(payload, require_identity: bool = True) -> tuple[str, list[str]]:
    email = str(payload.email).strip().lower()
    if require_identity and not (payload.first_name or "").strip() and not (payload.company or "").strip():
        raise HTTPException(status_code=422, detail="name_or_company_required")
    if payload.category not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=422, detail="invalid_contact_category")
    if payload.intent_level not in ALLOWED_INTENT:
        raise HTTPException(status_code=422, detail="invalid_intent_level")
    tags = sorted({str(t).strip() for t in payload.tags if str(t).strip()})
    return email, tags


@router.get("")
def list_contacts(
    q: str | None = None, category: str | None = None,
    tag: str | None = None, intent_level: str | None = None,
    db: Session = Depends(get_db),
):
    owner_id = ensure_owner(db)
    query = db.query(models.Contact).filter(models.Contact.owner_id == owner_id)
    if q:
        term = f"%{q.strip()}%"
        query = query.filter(or_(models.Contact.email.ilike(term), models.Contact.first_name.ilike(term),
                                 models.Contact.last_name.ilike(term), models.Contact.company.ilike(term)))
    if category:
        query = query.filter(models.Contact.category == category)
    if intent_level:
        query = query.filter(models.Contact.intent_level == intent_level)
    contacts = query.order_by(models.Contact.updated_at.desc()).all()
    rows = [_serialize(c, db) for c in contacts]
    if tag:
        rows = [row for row in rows if tag in row["tags"]]
    return rows


@router.post("")
def create_contact(payload: ContactCreate, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    email, tags = _validate(payload)
    if db.query(models.Contact).filter_by(owner_id=owner_id, email=email).first():
        raise HTTPException(status_code=409, detail="contact_email_exists")
    data = payload.model_dump(exclude={"tags", "custom_fields"})
    data.update(email=email, tags=json.dumps(tags, ensure_ascii=False),
                custom_fields=json.dumps(payload.custom_fields, ensure_ascii=False) if payload.custom_fields else None)
    contact = models.Contact(owner_id=owner_id, status="new", **data)
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return _serialize(contact, db)


@router.put("/{contact_id}")
def update_contact(contact_id: int, payload: ContactUpdate, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    contact = db.query(models.Contact).filter_by(id=contact_id, owner_id=owner_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="contact_not_found")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return _serialize(contact, db)

    if "email" in changes:
        if changes["email"] is None:
            raise HTTPException(status_code=422, detail="email_required")
        email = str(changes["email"]).strip().lower()
        conflict = db.query(models.Contact).filter(
            models.Contact.owner_id == owner_id, models.Contact.email == email,
            models.Contact.id != contact_id,
        ).first()
        if conflict:
            raise HTTPException(status_code=409, detail="contact_email_exists")
        contact.email = email

    if "category" in changes and changes["category"] not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=422, detail="invalid_contact_category")
    if "intent_level" in changes and changes["intent_level"] not in ALLOWED_INTENT:
        raise HTTPException(status_code=422, detail="invalid_intent_level")

    for key, value in changes.items():
        if key in {"email", "tags", "custom_fields"}:
            continue
        setattr(contact, key, value)
    if "tags" in changes:
        tags = sorted({str(t).strip() for t in (changes["tags"] or []) if str(t).strip()})
        contact.tags = json.dumps(tags, ensure_ascii=False)
    if "custom_fields" in changes:
        custom_fields = changes["custom_fields"]
        contact.custom_fields = (
            json.dumps(custom_fields, ensure_ascii=False) if custom_fields is not None else None
        )
    # A human CRM edit becomes authoritative until explicitly unlocked.
    if changes.get("manual_lock") is True:
        from datetime import datetime, timezone
        contact.manual_updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(contact)
    return _serialize(contact, db)


@router.delete("/{contact_id}")
def delete_contact(contact_id: int, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    contact = db.query(models.Contact).filter_by(id=contact_id, owner_id=owner_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="contact_not_found")
    if db.query(models.CampaignContact).filter_by(contact_id=contact.id).first():
        contact.category = "invalid"
        contact.status = "archived"
        db.commit()
        return {"ok": True, "archived": True}
    db.delete(contact)
    db.commit()
    return {"ok": True, "archived": False}


def _parse_file(content: bytes, filename: str) -> list[dict]:
    rows: list[list[str]] = []
    if filename.lower().endswith(".xlsx"):
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(content), read_only=True).active
        rows = [[str(v).strip() if v is not None else "" for v in row] for row in ws.iter_rows(values_only=True)]
    else:
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    if not rows:
        return []
    aliases = {"name": "first_name", "姓名": "first_name", "公司": "company", "邮箱": "email",
               "职位": "title", "电话": "phone", "分类": "category", "标签": "tags", "备注": "notes"}
    header = []
    for value in rows[0]:
        normalized = str(value).strip().lower().replace(" ", "_")
        # Older downloaded templates wrote the BOM escape sequence literally.
        # Accept those files while the frontend now emits a real UTF-8 BOM.
        normalized = normalized.removeprefix(r"\ufeff")
        header.append(aliases.get(normalized, normalized))
    return [{header[i]: (row[i].strip() if i < len(row) else "") for i in range(len(header))} for row in rows[1:]]


@router.post("/import")
async def import_contacts(file: UploadFile = File(...), confirm: bool = Query(False), db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    rows = _parse_file(await file.read(), file.filename or "")
    existing = {c.email.lower() for c in db.query(models.Contact).filter_by(owner_id=owner_id).all()}
    seen: set[str] = set()
    valid, invalid, duplicates = [], [], 0
    for index, row in enumerate(rows, start=2):
        email = (row.get("email") or "").strip().lower()
        name = (row.get("first_name") or "").strip()
        company = (row.get("company") or "").strip()
        errors = []
        if not EMAIL_RE.match(email): errors.append("invalid_email")
        if not name and not company: errors.append("name_or_company_required")
        if email in seen or email in existing:
            duplicates += 1; errors.append("duplicate_email")
        if errors:
            invalid.append({"row": index, "email": email, "errors": errors})
            continue
        seen.add(email); valid.append(row | {"email": email})
    preview = {"filename": file.filename, "total": len(rows), "valid": len(valid),
               "invalid": len(invalid), "duplicates": duplicates, "errors": invalid}
    if not confirm:
        return {"imported": 0, "preview": preview}
    imported = 0
    for row in valid:
        raw_tags = row.get("tags") or ""
        tags = [v.strip() for v in re.split(r"[,，;；]", raw_tags) if v.strip()]
        category = row.get("category") if row.get("category") in ALLOWED_CATEGORIES else "prospect"
        contact = models.Contact(
            owner_id=owner_id, email=row["email"], first_name=row.get("first_name") or None,
            last_name=row.get("last_name") or None, company=row.get("company") or None,
            title=row.get("title") or None, phone=row.get("phone") or None,
            category=category, tags=json.dumps(tags, ensure_ascii=False),
            intent_level="unknown", notes=row.get("notes") or None,
            source="file_import", status="new",
        )
        db.add(contact); imported += 1
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="contact_import_conflict")
    return {"imported": imported, "preview": preview}
