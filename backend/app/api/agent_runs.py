"""Dual-mode Agent Run API.

Full-auto runs send after the normal policy checks. Semi-auto runs prepare and
freeze a batch, then require one explicit confirmation before dispatch.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..schemas import AgentRunConfirm, AgentRunCreate, AgentRunOut
from ..tasks import enqueue_run, execute_prepared_agent_run
from ..services import automation as automation_svc
from .deps import ensure_owner, get_db

router = APIRouter(prefix="/api/agent-runs", tags=["agent-runs"])


def _run_or_404(db: Session, run_id: int, owner_id: int) -> models.AutomationRun:
    row = (
        db.query(models.AutomationRun)
        .join(models.Automation, models.Automation.id == models.AutomationRun.automation_id)
        .filter(models.AutomationRun.id == run_id, models.Automation.owner_id == owner_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    return row


def _serialize(run: models.AutomationRun) -> dict:
    try:
        plan = json.loads(run.execution_plan_json or "[]")
    except Exception:
        plan = []
    try:
        timeline = json.loads(run.timeline_json or "[]")
    except Exception:
        timeline = []
    return {
        "id": run.id, "automation_id": run.automation_id, "mode": run.execution_mode,
        "status": run.status, "summary": run.summary, "error": run.error,
        "started_at": run.started_at, "finished_at": run.finished_at,
        "synced_threads": run.synced_threads, "approvals_created": run.approvals_created,
        "drafts_created": run.drafts_created, "replies_stopped": run.replies_stopped,
        "timeline": timeline,
        "prepared_at": run.prepared_at, "confirmed_at": run.confirmed_at,
        "confirmed_by": run.confirmed_by, "confirmation_expires_at": run.confirmation_expires_at,
        "send_plan": plan,
    }


@router.post("")
def start(body: AgentRunCreate, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    if body.mode not in {"full_auto", "semi_auto"}:
        raise HTTPException(status_code=400, detail="mode must be full_auto or semi_auto")
    automation = db.query(models.Automation).filter_by(id=body.automation_id, owner_id=owner_id).first()
    if automation is None:
        raise HTTPException(status_code=404, detail="automation not found")
    run = enqueue_run(db, automation, trigger="manual", source="agent_api", execution_mode=body.mode)
    return {"ok": True, "run_id": run.id, "mode": run.execution_mode, "status": run.status}


@router.get("/{run_id}", response_model=AgentRunOut)
def detail(run_id: int, db: Session = Depends(get_db)):
    return _serialize(_run_or_404(db, run_id, ensure_owner(db)))


@router.post("/{run_id}/confirm")
def confirm(run_id: int, body: AgentRunConfirm, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id, ensure_owner(db))
    if run.execution_mode != "semi_auto" or run.status != "awaiting_confirmation":
        raise HTTPException(status_code=409, detail="run is not awaiting confirmation")
    expires_at = run.confirmation_expires_at
    if expires_at and expires_at.tzinfo is None:
        # SQLite returns naive values even for timezone-aware model columns.
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at and expires_at < datetime.now(timezone.utc):
        run.status = "expired"
        db.commit()
        raise HTTPException(status_code=409, detail="prepared send plan expired")
    valid, reason, _approvals = automation_svc.validate_frozen_plan(db, run)
    if not valid:
        automation_svc.invalidate_frozen_run(db, run, reason or "frozen_plan_invalid", actor="system")
        db.commit()
        raise HTTPException(status_code=409, detail=f"prepared send plan is invalid: {reason}")
    run.status = "confirmed"
    run.confirmed_at = datetime.now(timezone.utc)
    run.confirmed_by = body.confirmed_by or "user"
    db.commit()
    execute_prepared_agent_run(run.id)
    return {"ok": True, "run_id": run.id, "status": "confirmed"}


@router.post("/{run_id}/cancel")
def cancel(run_id: int, db: Session = Depends(get_db)):
    run = _run_or_404(db, run_id, ensure_owner(db))
    if run.status not in {"awaiting_confirmation", "confirmed"}:
        raise HTTPException(status_code=409, detail="only a prepared run can be cancelled")
    run.status = "cancelled"
    for approval in db.query(models.Approval).filter_by(automation_run_id=run.id, status="pending"):
        approval.status = "rejected"
        approval.rejection_reason = "agent run cancelled before send"
    db.commit()
    return {"ok": True, "run_id": run.id, "status": "cancelled"}
