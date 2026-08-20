"""Approval endpoints: list, decide (approve/reject)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..errors import ApiError
from ..schemas import ApprovalDecision, ApprovalInvalidation, ApprovalOut
from ..services import approvals as approval_svc
from ..services import automation as automation_svc
from .deps import get_db

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


def _contact_identity(db, approval):
    contact = None
    if approval.campaign_contact_id:
        cc = db.get(models.CampaignContact, approval.campaign_contact_id)
        contact = db.get(models.Contact, cc.contact_id) if cc else None
    if contact is None:
        contact = db.query(models.Contact).filter_by(email=approval.to_email.lower()).first()
    return {
        "contact_name": (
            " ".join(filter(None, [contact.first_name, contact.last_name])) or None
            if contact else None
        ),
        "contact_company": contact.company if contact else None,
    }


def _delivery_state(db, approval):
    attempt = (
        db.query(models.DeliveryAttempt)
        .filter_by(approval_id=approval.id)
        .order_by(models.DeliveryAttempt.created_at.desc())
        .first()
    )
    if not attempt:
        return None
    return {
        "status": attempt.status,
        "gmail_message_id": attempt.gmail_message_id,
        "send_requested_at": attempt.send_requested_at,
        "gmail_accepted_at": attempt.gmail_accepted_at,
        "sync_verified_at": attempt.sync_verified_at,
        "last_error": attempt.last_error,
    }


@router.get("", response_model=list[ApprovalOut])
def list_approvals(kind: str = None, status: str = "pending", db: Session = Depends(get_db)):
    q = db.query(models.Approval)
    if kind:
        q = q.filter_by(kind=kind)
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(models.Approval.created_at.desc()).all()
    return [
        {
            "id": a.id, "kind": a.kind, "status": a.status, "to_email": a.to_email,
            "subject": a.subject, "body_text": a.body_text,
            "recommended_action": a.recommended_action, "risk_level": a.risk_level,
            "campaign_id": a.campaign_id, "thread_id": a.thread_id,
            "quality": _parse_quality(a.quality_json),
            "agent_run_id": a.agent_run_id, "model": a.model,
            "prompt_version": a.prompt_version, "latency_ms": a.latency_ms,
            "created_at": a.created_at,
            "delivery": _delivery_state(db, a),
            **_contact_identity(db, a),
        }
        for a in rows
    ]


def _parse_quality(raw):
    if not raw:
        return None
    try:
        import json
        return json.loads(raw)
    except Exception:
        return None


@router.get("/{approval_id}", response_model=ApprovalOut)
def get_approval(approval_id: int, db: Session = Depends(get_db)):
    a = db.get(models.Approval, approval_id)
    if not a:
        raise HTTPException(status_code=404, detail="approval not found")
    return {
        "id": a.id, "kind": a.kind, "status": a.status,
        "to_email": a.to_email, "subject": a.subject, "body_text": a.body_text,
        "body_html": a.body_html, "recommended_action": a.recommended_action,
        "risk_level": a.risk_level, "campaign_id": a.campaign_id,
        "thread_id": a.thread_id, "quality": _parse_quality(a.quality_json),
        "agent_run_id": a.agent_run_id, "model": a.model,
        "prompt_version": a.prompt_version, "latency_ms": a.latency_ms,
        "created_at": a.created_at, "decided_at": a.decided_at,
        "delivery": _delivery_state(db, a),
        **_contact_identity(db, a),
    }


@router.post("/{approval_id}/invalidate")
def invalidate(approval_id: int, payload: ApprovalInvalidation, db: Session = Depends(get_db)):
    """Expire a pending approval without ever dispatching its draft."""
    approval = db.get(models.Approval, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="approval not found")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail=f"approval is already {approval.status}")
    approval.status = "expired"
    approval.decided_by = payload.editor_email or "operator"
    approval.decided_at = datetime.now(timezone.utc)
    approval.rejection_reason = payload.reason
    if approval.automation_run_id:
        run = db.get(models.AutomationRun, approval.automation_run_id)
        if run:
            automation_svc.invalidate_frozen_run(
                db, run, f"approval {approval.id} invalidated: {payload.reason}",
                actor=approval.decided_by,
            )
    if approval.draft_id:
        draft = db.get(models.EmailDraft, approval.draft_id)
        if draft and draft.status in ("draft", "approved"):
            draft.status = "cancelled"
    db.add(models.AuditLog(
        actor=approval.decided_by,
        action="approval_invalidated",
        entity="approval",
        entity_id=str(approval.id),
        detail=payload.reason,
    ))
    db.commit()
    return {"ok": True, "id": approval.id, "status": approval.status}

@router.post("/{approval_id}/decision")
def decide(approval_id: int, payload: ApprovalDecision, db: Session = Depends(get_db)):
    a = db.get(models.Approval, approval_id)
    if not a:
        raise HTTPException(status_code=404, detail="approval not found")
    if payload.decision == "approve" and a.automation_run_id:
        run = db.get(models.AutomationRun, a.automation_run_id)
        if run and run.execution_mode == "semi_auto":
            raise ApiError(
                409,
                "APPROVAL_BLOCKED",
                "semi-auto Approval must be released by confirming its frozen Agent Run",
            )
    campaign = db.get(models.Campaign, a.campaign_id) if a.campaign_id else None
    mode = campaign.agent_mode if campaign else "langgraph_only"
    agent = a.agent or (campaign.primary_agent if campaign else "langgraph")
    res = approval_svc.decide_approval(
        db, approval_id, payload.decision, editor_email=payload.editor_email,
        edited_subject=payload.edited_subject, edited_body_text=payload.edited_body_text,
        edited_body_html=payload.edited_body_html, rejection_reason=payload.rejection_reason,
        mode=mode, agent=agent, is_primary=True,
    )
    if not res.get("ok"):
        # blocked by policy (draft-only / allowlist / limit) 鈥?tell the user clearly
        raise ApiError(
            409,
            "APPROVAL_BLOCKED",
            res.get("blocked") or res.get("error") or "approval dispatch blocked",
        )
    db.commit()
    return res
