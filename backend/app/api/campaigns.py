"""Campaign endpoints: CRUD, CSV import, generation, lifecycle control."""
from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..agents.orchestrator import Orchestrator
from ..config import get_settings, is_openclaw_configured
from ..exceptions import AgentUnavailableError
from ..schemas import CampaignCreate, CampaignOut, CsvImportRequest, CsvImportResult, GenerateOutreachInput
from ..services import approvals as approval_svc
from ..services import contacts as contact_svc
from ..services.xlsx import read_xlsx_rows
from .deps import get_db, ensure_owner

logger = logging.getLogger("api.campaigns")
router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


@router.get("", response_model=list[CampaignOut])
def list_campaigns(db: Session = Depends(get_db)):
    # Archived (soft-deleted) campaigns are hidden from the list but kept for audit.
    return (
        db.query(models.Campaign)
        .filter(models.Campaign.status != "archived")
        .order_by(models.Campaign.created_at.desc())
        .all()
    )


@router.post("", response_model=CampaignOut)
def create_campaign(payload: CampaignCreate, db: Session = Depends(get_db)):
    # Safety: do not allow OpenClaw to be the sole executing agent unless it is
    # actually configured. This prevents a campaign that can never run (and would
    # surface as "Agent unavailable" on every generate/analyze).
    if payload.agent_mode in ("openclaw_only", "compare") and not is_openclaw_configured():
        raise HTTPException(
            status_code=400,
            detail="OpenClaw is not configured. Set OPENCLAW_ENDPOINT (and OPENCLAW_API_KEY if required) "
                   "before using agent_mode=openclaw_only or compare. Use langgraph_only, or connect OpenClaw first.",
        )
    owner_id = ensure_owner(db)
    c = models.Campaign(owner_id=owner_id, **payload.model_dump())
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = db.get(models.Campaign, campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    return c


@router.put("/{campaign_id}", response_model=CampaignOut)
def update_campaign(campaign_id: int, payload: CampaignCreate, db: Session = Depends(get_db)):
    c = db.get(models.Campaign, campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    for k, v in payload.model_dump().items():
        setattr(c, k, v)
    db.commit()
    db.refresh(c)
    return c


@router.get("/{campaign_id}/contacts")
def list_contacts(
    campaign_id: int,
    include_removed: bool = Query(False),
    db: Session = Depends(get_db),
):
    owner_id = ensure_owner(db)
    campaign = db.query(models.Campaign).filter_by(id=campaign_id, owner_id=owner_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="campaign not found")
    query = (
        db.query(models.CampaignContact, models.Contact)
        .join(models.Contact, models.Contact.id == models.CampaignContact.contact_id)
        .filter(models.CampaignContact.campaign_id == campaign_id)
    )
    if not include_removed:
        query = query.filter(models.CampaignContact.membership_active.is_(True))
    rows = (
        query.order_by(models.CampaignContact.membership_active.desc(), models.CampaignContact.id.desc()).all()
    )
    result = []
    for cc, contact in rows:
        pending = db.query(models.Approval).filter_by(
            campaign_contact_id=cc.id, status="pending"
        ).count()
        sent = db.query(models.EmailDraft).filter_by(
            campaign_contact_id=cc.id, status="sent"
        ).count()
        result.append({
            "id": contact.id, "email": contact.email,
            "first_name": contact.first_name, "last_name": contact.last_name,
            "company": contact.company, "title": contact.title,
            "campaign_contact_status": cc.status, "campaign_contact_id": cc.id,
            "membership_active": bool(cc.membership_active),
            "removed_at": cc.removed_at, "removed_reason": cc.removed_reason,
            "pending_approval": pending > 0, "pending_approval_count": pending,
            "sent_count": sent, "assigned_follow_ups": cc.assigned_follow_ups or 0,
        })
    return result


class CampaignContactSelection(BaseModel):
    contact_ids: list[int]


@router.post("/{campaign_id}/contacts")
def add_campaign_contacts(campaign_id: int, payload: CampaignContactSelection, db: Session = Depends(get_db)):
    """Add existing CRM contacts to a campaign. Suppressed/ineligible contacts are skipped."""
    owner_id = ensure_owner(db)
    campaign = db.query(models.Campaign).filter_by(id=campaign_id, owner_id=owner_id).first()
    if not campaign or campaign.status == "archived":
        raise HTTPException(status_code=404, detail="campaign not found")
    contacts = db.query(models.Contact).filter(
        models.Contact.owner_id == owner_id, models.Contact.id.in_(payload.contact_ids)
    ).all() if payload.contact_ids else []
    suppressed = {s.email.lower() for s in db.query(models.Suppression).filter_by(owner_id=owner_id).all()}
    existing_rows = {
        row.contact_id: row
        for row in db.query(models.CampaignContact).filter_by(campaign_id=campaign_id).all()
    }
    added, skipped = [], []
    for contact in contacts:
        reason = None
        existing = existing_rows.get(contact.id)
        if existing and existing.membership_active: reason = "already_in_campaign"
        elif contact.email.lower() in suppressed: reason = "suppressed"
        elif contact.status in ("unsubscribed", "not_interested", "bounced", "archived"): reason = contact.status
        elif contact.category == "invalid": reason = "invalid_contact"
        if reason:
            skipped.append({"contact_id": contact.id, "email": contact.email, "reason": reason})
            continue
        if existing:
            existing.membership_active = True
            existing.removed_at = None
            existing.removed_reason = None
            already_sent = db.query(models.Approval).filter_by(
                campaign_contact_id=existing.id, status="approved"
            ).count() > 0
            if not already_sent:
                existing.status = "queued"
        else:
            existing = models.CampaignContact(
                campaign_id=campaign_id, contact_id=contact.id,
                status="queued", membership_active=True,
            )
            db.add(existing)
            existing_rows[contact.id] = existing
        db.flush()
        added.append(contact.id)
        db.add(models.AuditLog(
            actor="user", action="campaign_contact_added",
            entity="campaign_contact", entity_id=str(existing.id or "pending"),
            detail=f"campaign_id={campaign_id}; contact_id={contact.id}",
        ))
    db.commit()
    return {"added": len(added), "contact_ids": added, "skipped": skipped}


@router.delete("/{campaign_id}/contacts/{contact_id}")
def remove_campaign_contact(campaign_id: int, contact_id: int, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    campaign = db.query(models.Campaign).filter_by(id=campaign_id, owner_id=owner_id).first()
    contact = db.query(models.Contact).filter_by(id=contact_id, owner_id=owner_id).first()
    if not campaign or not contact:
        raise HTTPException(status_code=404, detail="campaign_contact_not_found")
    row = db.query(models.CampaignContact).filter_by(
        campaign_id=campaign_id, contact_id=contact_id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="campaign_contact_not_found")
    if not row.membership_active:
        return {
            "ok": True, "membership_active": False,
            "historical_records_preserved": True, "cancelled_pending_items": 0,
        }

    now = datetime.now(timezone.utc)
    row.membership_active = False
    row.removed_at = now
    row.removed_reason = "operator_removed_from_campaign"

    cancelled = 0
    approvals = db.query(models.Approval).filter_by(
        campaign_contact_id=row.id, status="pending"
    ).all()
    for approval in approvals:
        approval.status = "expired"
        approval.decided_by = "operator"
        approval.decided_at = now
        approval.rejection_reason = "campaign_contact_removed"
        if approval.draft_id:
            draft = db.get(models.EmailDraft, approval.draft_id)
            if draft and draft.status in ("draft", "approved"):
                draft.status = "cancelled"
        if approval.automation_run_id:
            run = db.get(models.AutomationRun, approval.automation_run_id)
            if run:
                from ..services import automation as automation_svc
                automation_svc.invalidate_frozen_run(
                    db, run, f"campaign contact {row.id} removed",
                    actor="operator",
                )
        cancelled += 1

    tasks = db.query(models.FollowUpTask).filter(
        models.FollowUpTask.campaign_contact_id == row.id,
        models.FollowUpTask.status.in_(("scheduled", "ready", "running", "paused", "failed")),
    ).all()
    for task in tasks:
        task.status = "cancelled"
        task.last_error = "campaign_contact_removed"
        cancelled += 1

    db.add(models.AuditLog(
        actor="operator", action="campaign_contact_removed",
        entity="campaign_contact", entity_id=str(row.id),
        detail=(f"campaign_id={campaign_id}; contact_id={contact_id}; "
                f"cancelled_pending_items={cancelled}; historical_records_preserved=true"),
    ))
    db.commit()
    return {
        "ok": True, "membership_active": False,
        "historical_records_preserved": True,
        "cancelled_pending_items": cancelled,
    }


@router.post("/{campaign_id}/import-csv", response_model=CsvImportResult)
def import_csv(campaign_id: int, req: CsvImportRequest, db: Session = Depends(get_db)):
    c = db.get(models.Campaign, campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    if c.status == "archived":
        raise HTTPException(status_code=409, detail="campaign_archived")
    result = contact_svc.import_contacts(
        db, owner_id=c.owner_id, campaign_id=campaign_id, csv_text=req.csv_text,
        field_map=req.field_map, has_header=req.has_header,
        skip_allowlist_check=req.skip_allowlist_check,
    )
    db.commit()
    return result


# --------------- file upload (XLSX + CSV) ---------------

EXPECTED_COLUMNS = [
    "email", "first_name", "last_name", "category", "segments", "tags", "company",
    "title", "phone", "website", "timezone", "notes", "custom_fields", "source",
]
_EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _split_list(value: str | None) -> list[str]:
    """Parse user-managed tag/segment text without changing its display case."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,，;；]", value or ""):
        item = raw.strip()
        if item and item.casefold() not in seen:
            seen.add(item.casefold())
            result.append(item)
    return result


def _custom_fields(value: str | None) -> dict | None:
    if not (value or "").strip():
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("custom_fields_must_be_json_object")
    return parsed


def _parse_upload(file_bytes: bytes, filename: str) -> tuple[list[str], list[dict]]:
    """Parse uploaded file. Returns (header, list of row dicts)."""
    lower = filename.lower()
    rows: list[list[str]] = []
    if lower.endswith(".xlsx"):
        rows = read_xlsx_rows(file_bytes)
    else:
        text = file_bytes.decode("utf-8-sig")
        reader = __import__("csv").reader(io.StringIO(text))
        rows = [r for r in reader]
    if not rows:
        return [], []
    aliases = {
        "姓名": "first_name", "名字": "first_name", "名": "first_name",
        "姓": "last_name", "姓氏": "last_name", "公司": "company", "邮箱": "email",
        "职位": "title", "电话": "phone", "网站": "website", "网址": "website",
        "标签": "tags", "备注": "notes", "时区": "timezone", "来源": "source",
        "category": "category", "system_category": "category", "系统分类": "category", "分类": "category",
        "segment": "segments", "客户分群": "segments", "客户分类": "segments",
        "行业分类": "segments", "行业": "segments",
        "自定义字段": "custom_fields",
    }
    header = [aliases.get(c.strip().lower().replace(" ", "_"), c.strip().lower().replace(" ", "_")) for c in rows[0]]
    # map header columns to expected fields
    col_idx: dict[str, int] = {}
    for i, h in enumerate(header):
        for exp in EXPECTED_COLUMNS:
            if h == exp or h.replace(" ", "_") == exp:
                col_idx[exp] = i
                break
    data = []
    for r in rows[1:]:
        row_data = {f: (r[col_idx[f]].strip() if f in col_idx and col_idx[f] < len(r) else "") for f in EXPECTED_COLUMNS}
        data.append(row_data)
    return header, data


@router.post("/{campaign_id}/upload-contacts")
async def upload_contacts(
    campaign_id: int,
    file: UploadFile = File(...),
    confirm: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Upload XLSX/CSV contact file. Returns preview always.
    Set confirm=true to actually import into DB."""
    c = db.get(models.Campaign, campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    if c.status == "archived":
        raise HTTPException(status_code=409, detail="campaign_archived")
    by = await file.read()
    header, rows = _parse_upload(by, file.filename or "")
    strict_lead = "category" in header
    compatibility_warnings = [] if strict_lead else [
        "legacy_import_missing_system_category_defaulted_to_prospect"
    ]

    # validation
    valid_rows: list[dict] = []
    invalid_rows: list[dict] = []
    emails_seen: set[str] = set()
    dup_count = 0
    for i, r in enumerate(rows):
        email = (r.get("email") or "").strip().lower()
        row_num = i + 2
        errors: list[str] = []
        if not email:
            errors.append("missing email")
        elif not _EMAIL_RE.match(email):
            errors.append("invalid email format")
        if not (r.get("first_name") or "").strip() and not (r.get("last_name") or "").strip():
            errors.append("name required")
        category = (r.get("category") or "").strip().lower()
        if strict_lead:
            if not category:
                errors.append("system_category_required")
            elif category not in {"prospect", "qualified", "customer", "partner", "won", "invalid"}:
                errors.append("invalid_system_category")
            elif category != "prospect":
                errors.append("new_lead_category_must_be_prospect")
        elif category and category not in {"prospect", "qualified", "customer", "partner", "won", "invalid"}:
            errors.append("invalid_system_category")
        try:
            r["_custom_fields"] = _custom_fields(r.get("custom_fields"))
        except (TypeError, ValueError, json.JSONDecodeError):
            errors.append("invalid custom_fields JSON object")
        if errors:
            invalid_rows.append({"row": row_num, "email": email or "(empty)", "errors": errors, **r})
            continue
        # check existing in DB
        existing = db.query(models.Contact).filter_by(owner_id=c.owner_id, email=email).first()
        if existing:
            # check if already in this campaign
            in_camp = db.query(models.CampaignContact).filter_by(
                campaign_id=campaign_id, contact_id=existing.id,
            ).first()
            if in_camp and in_camp.membership_active:
                dup_count += 1
                invalid_rows.append({"row": row_num, "email": email, "errors": ["already in campaign"], **r})
                continue
        if email in emails_seen:
            dup_count += 1
            invalid_rows.append({"row": row_num, "email": email, "errors": ["duplicate in file"], **r})
            continue
        emails_seen.add(email)
        valid_rows.append(r)

    preview = {
        "filename": file.filename,
        "header": header,
        "total_rows": len(rows),
        "valid": len(valid_rows),
        "invalid": len(invalid_rows),
        "duplicates": dup_count,
        "mode": "lead" if strict_lead else "legacy",
        "compatibility_warnings": compatibility_warnings,
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
    }

    if not confirm:
        return {"imported": 0, "preview": preview}

    # --- confirm: import into DB ---
    imported = 0
    for r in valid_rows:
        email = r.get("email", "").strip().lower()
        contact = db.query(models.Contact).filter_by(owner_id=c.owner_id, email=email).first()
        if not contact:
            contact = models.Contact(
                owner_id=c.owner_id, email=email,
                first_name=r.get("first_name") or "", last_name=r.get("last_name") or "",
                company=r.get("company") or "", title=r.get("title") or "",
                phone=r.get("phone") or None, website=r.get("website") or None,
                category=(r.get("category") or "prospect").strip().lower() or "prospect",
                tags=json.dumps(_split_list(r.get("tags")), ensure_ascii=False),
                segments=json.dumps(_split_list(r.get("segments")), ensure_ascii=False),
                timezone=r.get("timezone") or None, notes=r.get("notes") or None,
                custom_fields=(json.dumps(r.get("_custom_fields"), ensure_ascii=False)
                               if r.get("_custom_fields") else None),
                source=r.get("source") or "file_import", status="new",
                lifecycle_stage="new_customer", next_action="review",
            )
            db.add(contact)
            db.flush()
        # ensure campaign-contact row
        cc = db.query(models.CampaignContact).filter_by(
            campaign_id=campaign_id, contact_id=contact.id,
        ).first()
        if not cc:
            cc = models.CampaignContact(
                campaign_id=campaign_id, contact_id=contact.id, status="queued",
            )
            db.add(cc)
        elif not cc.membership_active:
            cc.membership_active = True
            cc.removed_at = None
            cc.removed_reason = None
            already_sent = db.query(models.Approval).filter_by(
                campaign_contact_id=cc.id, status="approved"
            ).count() > 0
            if not already_sent:
                cc.status = "queued"
        imported += 1

    if imported > 0:
        c.status = "active"
    db.commit()

    return {
        "imported": imported,
        "total_rows": len(rows),
        "duplicates_skipped": dup_count,
        "invalid": len(invalid_rows),
        "preview": preview,
    }


def _json_list(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _agent_review_enabled(db: Session, owner_id: int) -> bool:
    """Return the current workspace send authority without creating profile data."""
    profile = (
        db.query(models.AgentProfile)
        .filter_by(owner_id=owner_id)
        .first()
    )
    return bool(profile and profile.approval_mode == "agent_review")


def _generation_run_out(run: models.CampaignGenerationRun, *, reused: bool = False) -> dict:
    failures = _json_list(run.failures_json)
    send_failures = _json_list(run.send_failures_json)
    return {
        "run_id": run.id,
        "status": run.status,
        "generated": run.generated,
        "failed": run.failed,
        "approvals": _json_list(run.approvals_json),
        "failures": failures,
        "total_contacts": run.total_contacts,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "error": run.error,
        "send_status": run.send_status or "not_requested",
        "sent": run.sent or 0,
        "send_failed": run.send_failed or 0,
        "send_failures": send_failures,
        "reused": reused,
    }


def _record_generation_failure(
    db: Session, run_id: int, contact_id: int, email: str, code: str,
) -> None:
    """Persist one failed recipient without rolling back prior approvals."""
    # Keep raw driver details in server logs, not the public run-status API.
    if "database is locked" in (code or "").casefold():
        code = "draft_database_locked"
    run = db.get(models.CampaignGenerationRun, run_id)
    if run is None:
        return
    failures = _json_list(run.failures_json)
    failures.append({"contact_id": contact_id, "email": email, "code": code[:160]})
    run.failures_json = json.dumps(failures, ensure_ascii=False)
    run.failed = len(failures)
    db.commit()


def _record_generation_send_failure(
    db: Session, run_id: int, contact_id: int, email: str, code: str,
) -> None:
    """Persist an Agent-review dispatch result without touching generation results."""
    run = db.get(models.CampaignGenerationRun, run_id)
    if run is None:
        return
    failures = _json_list(run.send_failures_json)
    failures.append({"contact_id": contact_id, "email": email, "code": code[:160]})
    run.send_failures_json = json.dumps(failures, ensure_ascii=False)
    run.send_failed = len(failures)
    db.commit()


def _current_generation_run(db: Session, campaign_id: int) -> models.CampaignGenerationRun | None:
    run = (
        db.query(models.CampaignGenerationRun)
        .filter_by(campaign_id=campaign_id, status="running")
        .order_by(models.CampaignGenerationRun.started_at.desc())
        .first()
    )
    if not run:
        return None
    stale_seconds = max(300, int(get_settings().LLM_TIMEOUT_SECONDS) * 3)
    started_at = run.started_at
    if started_at and started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if started_at and started_at < datetime.now(timezone.utc) - timedelta(seconds=stale_seconds):
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.error = "generation_run_stale"
        db.commit()
        return None
    return run


@router.get("/{campaign_id}/generation-status")
def generation_status(campaign_id: int, db: Session = Depends(get_db)):
    if not db.get(models.Campaign, campaign_id):
        raise HTTPException(status_code=404, detail="campaign not found")
    active = _current_generation_run(db, campaign_id)
    if active:
        return _generation_run_out(active, reused=True)
    latest = (
        db.query(models.CampaignGenerationRun)
        .filter_by(campaign_id=campaign_id)
        .order_by(models.CampaignGenerationRun.created_at.desc())
        .first()
    )
    return _generation_run_out(latest) if latest else {
        "status": "idle", "generated": 0, "failed": 0, "approvals": [], "failures": [],
    }


@router.post("/{campaign_id}/generate")
def generate_outreach(campaign_id: int, db: Session = Depends(get_db)):
    """Generate personalized outreach for all queued contacts and queue approvals.

    In compare mode, only the PRIMARY agent's draft is executed; shadow only proposes.
    """
    c = db.get(models.Campaign, campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    if c.status != "active":
        raise HTTPException(status_code=409, detail=f"campaign_{c.status}")
    active = _current_generation_run(db, campaign_id)
    if active:
        return _generation_run_out(active, reused=True)
    queued = db.query(models.CampaignContact).filter_by(
        campaign_id=campaign_id, status="queued", membership_active=True
    ).all()
    if not queued:
        return {
            "status": "completed", "generated": 0, "failed": 0,
            "approvals": [], "failures": [], "send_status": "not_requested",
            "sent": 0, "send_failed": 0, "send_failures": [],
        }
    auto_send_requested = _agent_review_enabled(db, c.owner_id)
    run = models.CampaignGenerationRun(
        campaign_id=campaign_id,
        total_contacts=len(queued),
        send_status="running" if auto_send_requested else "not_requested",
    )
    db.add(run)
    try:
        # Commit before the slow LLM call. A client timeout can now be safely
        # observed by a later request instead of starting a duplicate batch.
        db.commit()
    except IntegrityError:
        db.rollback()
        active = _current_generation_run(db, campaign_id)
        if active:
            return _generation_run_out(active, reused=True)
        raise HTTPException(status_code=409, detail="generation_run_conflict")

    run_id = run.id
    approvals: list[int] = []
    degraded = 0
    orch = Orchestrator(db)
    for cc in queued:
        contact = db.get(models.Contact, cc.contact_id)
        email = contact.email if contact else ""
        inp = GenerateOutreachInput(campaign_id=campaign_id, contact_id=cc.contact_id, mode=c.agent_mode)
        try:
            out = orch.generate_outreach(inp)
        except AgentUnavailableError as e:
            db.rollback()
            _record_generation_failure(db, run_id, cc.contact_id, email, f"agent_unavailable:{e.agent}")
            continue
        except Exception as exc:
            logger.exception("Campaign generation failed campaign=%s contact=%s", campaign_id, cc.contact_id)
            db.rollback()
            _record_generation_failure(db, run_id, cc.contact_id, email, f"generation_failed:{type(exc).__name__}")
            continue
        # pick primary proposal
        if hasattr(out, "langgraph"):  # ComparisonView
            ad = getattr(out, c.primary_agent) or getattr(out, "langgraph")
            if ad is None:
                _record_generation_failure(db, run_id, cc.contact_id, email, "proposal_unavailable")
                continue
            from ..schemas import decision_to_proposal
            prop = decision_to_proposal(ad)
        else:
            prop = out
        if prop is None:
            _record_generation_failure(db, run_id, cc.contact_id, email, "proposal_unavailable")
            continue
        # generate_outreach flushes an AgentRun into this Session. Gmail Draft
        # creation may refresh OAuth credentials using a different, short-lived
        # Session. Commit the local decision first so SQLite does not retain a
        # writer lock while that OAuth session persists its refresh.
        try:
            db.commit()
        except Exception:
            logger.exception("Failed to persist campaign generation decision", extra={"run_id": run_id})
            _record_generation_failure(
                db, run_id, cc.contact_id, email, "agent_decision_persist_failed"
            )
            continue
        try:
            ap = approval_svc.create_outreach_approval(
                db, cc, prop, mode=c.agent_mode,
                agent=c.primary_agent, is_primary=True,
            )
            approvals.append(ap.id)
            current = db.get(models.CampaignGenerationRun, run_id)
            if current is not None:
                stored = _json_list(current.approvals_json)
                stored.append(ap.id)
                current.approvals_json = json.dumps(stored)
                current.generated = len(stored)
            if getattr(prop, "model", "") == "rule-based":
                degraded += 1
            # One recipient is one transaction: a later failure cannot erase
            # this Draft/Approval pair.
            db.commit()
            if auto_send_requested:
                # Re-read the global switch before each send. Turning Agent
                # Review off while a batch is running stops the remaining
                # dispatches but keeps their generated Drafts pending.
                if not _agent_review_enabled(db, c.owner_id):
                    current = db.get(models.CampaignGenerationRun, run_id)
                    if current is not None:
                        current.send_status = "stopped"
                        db.commit()
                    auto_send_requested = False
                else:
                    try:
                        send_result = approval_svc.decide_approval(
                            db,
                            ap.id,
                            "approve",
                            editor_email="agent:agent_review",
                            mode=c.agent_mode,
                            agent=c.primary_agent,
                            is_primary=True,
                        )
                        if send_result.get("ok"):
                            current = db.get(models.CampaignGenerationRun, run_id)
                            if current is not None:
                                current.sent = (current.sent or 0) + 1
                        else:
                            _record_generation_send_failure(
                                db, run_id, cc.contact_id, email,
                                f"send_blocked:{send_result.get('blocked') or send_result.get('error') or 'unknown'}",
                            )
                        db.commit()
                    except Exception as exc:
                        # The Draft/Approval was already committed. Preserve it
                        # as pending and record the outcome as unknown; never
                        # retry automatically after a transport or post-send
                        # exception.
                        db.rollback()
                        logger.exception(
                            "Campaign Agent-review send failed campaign=%s contact=%s",
                            campaign_id, cc.contact_id,
                        )
                        _record_generation_send_failure(
                            db, run_id, cc.contact_id, email,
                            f"send_unknown:{type(exc).__name__}",
                        )
        except approval_svc.DraftCreationError as exc:
            db.rollback()
            _record_generation_failure(db, run_id, cc.contact_id, email, exc.reason)
        except Exception as exc:
            logger.exception("Campaign draft creation failed campaign=%s contact=%s", campaign_id, cc.contact_id)
            db.rollback()
            _record_generation_failure(db, run_id, cc.contact_id, email, f"draft_creation_failed:{type(exc).__name__}")
    run = db.get(models.CampaignGenerationRun, run_id)
    if run is None:
        raise HTTPException(status_code=500, detail="generation_run_missing")
    run.status = "completed" if run.failed == 0 else ("partial" if run.generated else "failed")
    run.finished_at = datetime.now(timezone.utc)
    if degraded:
        run.error = f"rule_based_fallback:{degraded}"
    if auto_send_requested:
        if run.send_failed:
            run.send_status = "partial" if run.sent else "blocked"
        else:
            run.send_status = "completed"
    db.commit()
    return {**_generation_run_out(run), "degraded": degraded}


@router.post("/{campaign_id}/start")
def start_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = db.get(models.Campaign, campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    if c.status == "archived":
        raise HTTPException(status_code=409, detail="campaign_archived")
    c.status = "active"
    db.commit()
    return {"ok": True, "status": c.status}


@router.post("/{campaign_id}/pause")
def pause_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = db.get(models.Campaign, campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    if c.status == "archived":
        raise HTTPException(status_code=409, detail="campaign_archived")
    c.status = "paused"
    db.commit()
    return {"ok": True, "status": c.status}


@router.post("/{campaign_id}/stop")
def stop_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = db.get(models.Campaign, campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    if c.status == "archived":
        raise HTTPException(status_code=409, detail="campaign_archived")
    c.status = "stopped"
    # cancel scheduled follow-ups
    db.query(models.FollowUpTask).filter_by(campaign_id=campaign_id, status="scheduled").update({"status": "cancelled"})
    db.commit()
    return {"ok": True, "status": c.status}


@router.delete("/{campaign_id}")
def delete_campaign(campaign_id: int, db: Session = Depends(get_db)):
    """Soft-delete (archive) a campaign.

    The campaign is hidden from the list (status='archived') and its scheduled
    follow-ups are cancelled, but ALL rows (approvals, drafts, threads, contacts)
    are preserved so the audit trail of real sends is never destroyed. This is the
    safe default; a campaign with send records must never be hard-deleted.
    """
    c = db.get(models.Campaign, campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    has_sent = (
        db.query(models.Approval)
        .filter_by(campaign_id=campaign_id, status="approved")
        .first()
        is not None
    )
    now = datetime.now(timezone.utc)
    c.status = "archived"
    members = db.query(models.CampaignContact).filter_by(campaign_id=campaign_id).all()
    for member in members:
        if member.membership_active:
            member.membership_active = False
            member.removed_at = now
            member.removed_reason = "campaign_archived"

    pending = db.query(models.Approval).filter(
        models.Approval.campaign_id == campaign_id,
        models.Approval.status == "pending",
        models.Approval.campaign_contact_id.isnot(None),
    ).all()
    for approval in pending:
        approval.status = "expired"
        approval.decided_by = "system"
        approval.decided_at = now
        approval.rejection_reason = "campaign_archived"
        if approval.draft_id:
            draft = db.get(models.EmailDraft, approval.draft_id)
            if draft and draft.status in ("draft", "approved"):
                draft.status = "cancelled"
        if approval.automation_run_id:
            run = db.get(models.AutomationRun, approval.automation_run_id)
            if run:
                from ..services import automation as automation_svc
                automation_svc.invalidate_frozen_run(
                    db, run, "campaign archived", actor="system"
                )

    tasks = db.query(models.FollowUpTask).filter(
        models.FollowUpTask.campaign_id == campaign_id,
        models.FollowUpTask.status.in_(("scheduled", "ready", "running", "paused", "failed")),
    ).all()
    for task in tasks:
        task.status = "cancelled"
        task.last_error = "campaign_archived"
    db.commit()
    return {"ok": True, "deleted": True, "status": "archived", "had_send_records": has_sent}
