"""Contact CRM endpoints. Contacts are the shared customer source for Inbox and Campaigns."""
from __future__ import annotations

import csv
import io
import json
import re

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..schemas import ContactCreate, ContactTransition, ContactUpdate
from ..services.contact_lifecycle import ContactTransitionError, transition_contact
from ..services.xlsx import contacts_template_xlsx, read_xlsx_rows
from .deps import ensure_owner, get_db

router = APIRouter(prefix="/api/contacts", tags=["contacts"])
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ALLOWED_CATEGORIES = {"prospect", "qualified", "customer", "partner", "won", "invalid"}
ALLOWED_INTENT = {"high", "medium", "low", "unknown"}


def _string_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def _normalize_list(values: list[str]) -> list[str]:
    """Trim and de-duplicate a user-managed list while keeping input order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _split_list(value: str | None) -> list[str]:
    return _normalize_list(re.split(r"[,，;；]", value or ""))


def _custom_fields(value: str | None) -> dict | None:
    if not (value or "").strip():
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("custom_fields_must_be_json_object")
    return parsed


def _safe_custom_fields(value: str | None) -> dict | None:
    try:
        return _custom_fields(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


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
        # Keep the legacy ``category`` API field while exposing the English
        # business name used by the import template and UI.
        "system_category": contact.category or "prospect",
        "tags": _string_list(contact.tags), "segments": _string_list(contact.segments),
        "intent_level": contact.intent_level or "unknown",
        "notes": contact.notes, "source": contact.source, "status": contact.status,
        "custom_fields": _safe_custom_fields(contact.custom_fields),
        "timezone": contact.timezone,
        "lifecycle_stage": contact.lifecycle_stage or "new_customer",
        "next_action": contact.next_action,
        "manual_lock": bool(contact.manual_lock),
        "manual_updated_at": contact.manual_updated_at,
        "last_contacted_at": contact.last_contacted_at,
        "next_follow_up_at": contact.next_follow_up_at,
        "campaign_count": campaign_count, "thread_count": thread_count,
        "created_at": contact.created_at, "updated_at": contact.updated_at,
    }


def _validate(payload, require_identity: bool = True) -> tuple[str, list[str], list[str]]:
    email = str(payload.email).strip().lower()
    if require_identity and not (payload.first_name or "").strip() and not (payload.last_name or "").strip():
        raise HTTPException(status_code=422, detail="name_required")
    if payload.category not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=422, detail="invalid_contact_category")
    if payload.intent_level not in ALLOWED_INTENT:
        raise HTTPException(status_code=422, detail="invalid_intent_level")
    tags = _normalize_list(payload.tags)
    segments = _normalize_list(payload.segments)
    return email, tags, segments


@router.get("")
def list_contacts(
    q: str | None = None, category: str | None = None,
    tag: str | None = None, intent_level: str | None = None,
    segments_any: list[str] | None = Query(None),
    tags_any: list[str] | None = Query(None),
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
    wanted_segments = {
        value.strip().casefold()
        for raw in (segments_any or [])
        for value in re.split(r"[,，;；]", raw)
        if value.strip()
    }
    wanted_tags = {
        value.strip().casefold()
        for raw in ([tag] if tag else []) + (tags_any or [])
        for value in re.split(r"[,，;；]", raw)
        if value.strip()
    }
    if wanted_segments:
        rows = [row for row in rows if wanted_segments.intersection(
            {str(value).casefold() for value in row["segments"]}
        )]
    if wanted_tags:
        rows = [row for row in rows if wanted_tags.intersection(
            {str(value).casefold() for value in row["tags"]}
        )]
    return rows


@router.post("")
def create_contact(payload: ContactCreate, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    email, tags, segments = _validate(payload)
    if db.query(models.Contact).filter_by(owner_id=owner_id, email=email).first():
        raise HTTPException(status_code=409, detail="contact_email_exists")
    data = payload.model_dump(exclude={"tags", "segments", "custom_fields"})
    data.update(email=email, tags=json.dumps(tags, ensure_ascii=False),
                segments=json.dumps(segments, ensure_ascii=False),
                custom_fields=json.dumps(payload.custom_fields, ensure_ascii=False) if payload.custom_fields else None)
    contact = models.Contact(owner_id=owner_id, status="new", **data)
    db.add(contact)
    db.flush()
    db.add(models.AuditLog(
        actor="user", action="contact_created", entity="contact", entity_id=str(contact.id),
        detail=json.dumps({"category": contact.category, "intent_level": contact.intent_level}, ensure_ascii=False),
        success=True,
    ))
    db.commit()
    db.refresh(contact)
    return _serialize(contact, db)


@router.put("/{contact_id}")
def update_contact(
    contact_id: int, payload: ContactUpdate, actor: str = Query("user"),
    db: Session = Depends(get_db),
):
    owner_id = ensure_owner(db)
    contact = db.query(models.Contact).filter_by(id=contact_id, owner_id=owner_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="contact_not_found")
    before = {
        "category": contact.category,
        "intent_level": contact.intent_level,
        "tags": _string_list(contact.tags),
        "segments": _string_list(contact.segments),
        "notes": contact.notes,
        "lifecycle_stage": contact.lifecycle_stage,
        "next_action": contact.next_action,
        "manual_lock": bool(contact.manual_lock),
    }
    changes = payload.model_dump(exclude_unset=True, exclude={"reason", "override_manual_lock"})
    if not changes:
        return _serialize(contact, db)

    if contact.manual_lock and actor != "user" and not payload.override_manual_lock:
        raise HTTPException(status_code=409, detail="contact_manual_lock")

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
    resulting_first_name = (changes.get("first_name", contact.first_name) or "").strip()
    resulting_last_name = (changes.get("last_name", contact.last_name) or "").strip()
    if not resulting_first_name and not resulting_last_name:
        raise HTTPException(status_code=422, detail="name_required")

    transitioned = False
    if "category" in changes and changes["category"] in {"qualified", "customer", "invalid"} \
            and changes["category"] != contact.category:
        try:
            transition_contact(
                db, contact, owner_id=owner_id, action={
                    "qualified": "qualify", "customer": "customer", "invalid": "invalid",
                }[changes["category"]], reason=payload.reason, actor=actor,
                override_manual_lock=payload.override_manual_lock,
            )
        except ContactTransitionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
        transitioned = True

    for key, value in changes.items():
        if key in {"email", "tags", "segments", "custom_fields", "category"}:
            continue
        # A lifecycle transition owns its terminal/reply operational state.
        # Do not let the full-form UI accidentally overwrite it with stale
        # values while still allowing independent Intent/Tags/Notes edits.
        if transitioned and key in {"lifecycle_stage", "next_action"}:
            continue
        setattr(contact, key, value)
    if "tags" in changes:
        tags = _normalize_list(changes["tags"] or [])
        contact.tags = json.dumps(tags, ensure_ascii=False)
    if "segments" in changes:
        contact.segments = json.dumps(_normalize_list(changes["segments"] or []), ensure_ascii=False)
    if "custom_fields" in changes:
        custom_fields = changes["custom_fields"]
        contact.custom_fields = (
            json.dumps(custom_fields, ensure_ascii=False) if custom_fields is not None else None
        )
    # Terminal CRM decisions must clear residual reply/follow-up actions from
    # historical Inbox classification.  Without this, a stopped Contact can
    # still appear in Needs Action and expose a draft-reply entry point.
    from ..services.inbox_triage import normalize_terminal_contact_state
    normalize_terminal_contact_state(contact)
    # A human CRM edit becomes authoritative until explicitly unlocked.
    if changes.get("manual_lock") is True:
        from datetime import datetime, timezone
        contact.manual_updated_at = datetime.now(timezone.utc)
    after = {
        "category": contact.category,
        "intent_level": contact.intent_level,
        "tags": _string_list(contact.tags),
        "segments": _string_list(contact.segments),
        "notes": contact.notes,
        "lifecycle_stage": contact.lifecycle_stage,
        "next_action": contact.next_action,
        "manual_lock": bool(contact.manual_lock),
    }
    db.add(models.AuditLog(
        actor=actor if actor in {"user", "agent", "tacwork", "langgraph", "system"} else "user",
        action="contact_updated", entity="contact", entity_id=str(contact.id),
        detail=json.dumps({"before": before, "after": after, "reason": payload.reason or "",
                           "manual_lock_override": bool(payload.override_manual_lock)}, ensure_ascii=False),
        success=True,
    ))
    db.commit()
    db.refresh(contact)
    return _serialize(contact, db)


@router.post("/{contact_id}/transition")
def transition_contact_endpoint(
    contact_id: int, payload: ContactTransition, actor: str = Query("user"),
    db: Session = Depends(get_db),
):
    owner_id = ensure_owner(db)
    contact = db.query(models.Contact).filter_by(id=contact_id, owner_id=owner_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="contact_not_found")
    try:
        result = transition_contact(
            db, contact, owner_id=owner_id, action=payload.action,
            campaign_id=payload.campaign_id, intent=payload.intent,
            reason=payload.reason, actor=actor,
            override_manual_lock=payload.override_manual_lock,
        )
    except ContactTransitionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    db.commit()
    db.refresh(contact)
    return {**result, "contact": _serialize(contact, db)}


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
        rows = read_xlsx_rows(content)
    else:
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    if not rows:
        return []
    aliases = {
        "name": "first_name", "姓名": "first_name", "名字": "first_name",
        "first_name": "first_name", "名": "first_name",
        "last_name": "last_name", "姓": "last_name", "姓氏": "last_name",
        "公司": "company", "邮箱": "email", "职位": "title", "电话": "phone",
        "网站": "website", "网址": "website", "分类": "category", "标签": "tags",
        "category": "category", "system_category": "category", "系统分类": "category",
        "备注": "notes", "时区": "timezone", "来源": "source",
        "segment": "segments", "客户分群": "segments", "客户分类": "segments",
        "行业分类": "segments", "行业": "segments",
        "自定义字段": "custom_fields",
    }
    header = []
    for value in rows[0]:
        normalized = str(value).strip().lower().replace(" ", "_")
        # Older downloaded templates wrote the BOM escape sequence literally.
        # Accept those files while the frontend now emits a real UTF-8 BOM.
        normalized = normalized.removeprefix(r"\ufeff")
        header.append(aliases.get(normalized, normalized))
    return [
        {header[i]: (row[i].strip() if i < len(row) else "") for i in range(len(header))}
        for row in rows[1:]
        if any(str(value).strip() for value in row)
    ]


@router.get("/template.xlsx")
def download_contacts_template():
    """Return the English-first Excel template consumed by the contact importer."""
    return StreamingResponse(
        io.BytesIO(contacts_template_xlsx()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="contacts-import-template.xlsx"'},
    )


@router.post("/import")
async def import_contacts(
    file: UploadFile = File(...), confirm: bool = Query(False),
    mode: str | None = Query(None, pattern="^(lead|legacy)$"),
    db: Session = Depends(get_db),
):
    owner_id = ensure_owner(db)
    rows = _parse_file(await file.read(), file.filename or "")
    has_category_column = bool(rows and "category" in rows[0])
    # New templates carry an explicit category column and are strict.  Files
    # from older releases without that column stay importable in compatibility
    # mode and default to Prospect with a visible warning.
    strict_lead = mode == "lead" or (mode is None and has_category_column)
    compatibility_warnings = [] if strict_lead else [
        "legacy_import_missing_system_category_defaulted_to_prospect"
    ]
    existing = {c.email.lower() for c in db.query(models.Contact).filter_by(owner_id=owner_id).all()}
    seen: set[str] = set()
    valid, invalid, duplicates = [], [], 0
    for index, row in enumerate(rows, start=2):
        email = (row.get("email") or "").strip().lower()
        first_name = (row.get("first_name") or "").strip()
        last_name = (row.get("last_name") or "").strip()
        errors = []
        if not EMAIL_RE.match(email): errors.append("invalid_email")
        if not first_name and not last_name: errors.append("name_required")
        category = (row.get("category") or "").strip().lower()
        if strict_lead:
            if not category:
                errors.append("system_category_required")
            elif category not in ALLOWED_CATEGORIES:
                errors.append("invalid_system_category")
            elif category != "prospect":
                errors.append("new_lead_category_must_be_prospect")
        elif category and category not in ALLOWED_CATEGORIES:
            errors.append("invalid_system_category")
        try:
            custom_fields = _custom_fields(row.get("custom_fields"))
        except (TypeError, ValueError, json.JSONDecodeError):
            custom_fields = None
            errors.append("invalid_custom_fields")
        if email in seen or email in existing:
            duplicates += 1; errors.append("duplicate_email")
        if errors:
            invalid.append({"row": index, "email": email, "errors": errors})
            continue
        seen.add(email); valid.append(row | {"email": email, "_custom_fields": custom_fields})
    preview = {"filename": file.filename, "total": len(rows), "valid": len(valid),
               "invalid": len(invalid), "duplicates": duplicates, "errors": invalid,
               "mode": "lead" if strict_lead else "legacy",
               "compatibility_warnings": compatibility_warnings}
    if not confirm:
        return {"imported": 0, "preview": preview}
    imported = 0
    for row in valid:
        tags = _split_list(row.get("tags"))
        segments = _split_list(row.get("segments"))
        category = row.get("category") if row.get("category") in ALLOWED_CATEGORIES else "prospect"
        contact = models.Contact(
            owner_id=owner_id, email=row["email"], first_name=row.get("first_name") or None,
            last_name=row.get("last_name") or None, company=row.get("company") or None,
            title=row.get("title") or None, phone=row.get("phone") or None,
            website=row.get("website") or None, category=category,
            tags=json.dumps(tags, ensure_ascii=False), segments=json.dumps(segments, ensure_ascii=False),
            intent_level="unknown", notes=row.get("notes") or None,
            lifecycle_stage="new_customer", next_action="review",
            custom_fields=json.dumps(row.get("_custom_fields"), ensure_ascii=False) if row.get("_custom_fields") else None,
            timezone=row.get("timezone") or None, source=row.get("source") or "file_import", status="new",
        )
        db.add(contact); imported += 1
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="contact_import_conflict")
    return {"imported": imported, "preview": preview}
