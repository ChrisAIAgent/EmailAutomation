"""Automation endpoints.

POST /api/automation/generate  -> preview a structured Plan from a prompt
POST /api/automation           -> create (disabled by default)
GET  /api/automation           -> list + OpenClaw cron status
GET  /api/automation/{id}      -> detail + recent runs + campaign metrics
POST /api/automation/{id}/enable
POST /api/automation/{id}/pause
POST /api/automation/{id}/run-now   -> manual one-shot (backend local scheduler)
POST /api/automation/tick           -> cron trigger (OpenClaw webhook; bearer-gated)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings, is_openclaw_configured
from ..schemas import (
    AutomationCreate,
    AutomationGenerateRequest,
    AutomationPlan,
    AutomationScheduleUpdate,
    GlobalAutomationUpdate,
    AutomationListOut,
)
from ..services import automation as automation_svc
from ..tasks import enqueue_run, scan_due_automations
from ..consumer_status import read_consumer_status
from ..services import flags, workspace_time
from .deps import get_db, ensure_owner

logger = logging.getLogger("api.automation")
router = APIRouter(prefix="/api/automation", tags=["automation"])


def _get_automation_or_404(db, automation_id: int, owner_id: int) -> models.Automation:
    """Fetch an Automation scoped to the current owner (no cross-owner access)."""
    a = (
        db.query(models.Automation)
        .filter_by(id=automation_id, owner_id=owner_id)
        .first()
    )
    if a is None:
        raise HTTPException(status_code=404, detail="automation not found")
    return a


def _serialize(db: Session, a: models.Automation) -> dict:
    plan = automation_svc.parse_plan(a.plan_json)
    display_timezone = workspace_time.get_timezone(db)
    global_takeover_owned = (a.scope or ("campaign" if a.campaign_id else "global")) == "global" and flags.is_agent_takeover_enabled(db)
    return {
        "id": a.id,
        "owner_id": a.owner_id,
        "name": a.name,
        "prompt": a.prompt,
        "campaign_id": a.campaign_id,
        "scope": a.scope or ("campaign" if a.campaign_id else "global"),
        "status": a.status,
        "plan": plan.model_dump(),
        "execution_mode": a.execution_mode or plan.execution_mode,
        "tick_interval_minutes": a.tick_interval_minutes,
        "next_run_at": workspace_time.utc_iso(a.next_run_at),
        "last_run_at": workspace_time.utc_iso(a.last_run_at),
        "display_timezone": display_timezone,
        "display_time": {
            "next_run_at": workspace_time.display(a.next_run_at, display_timezone),
            "last_run_at": workspace_time.display(a.last_run_at, display_timezone),
        },
        "schedule_owner": "agent_takeover" if global_takeover_owned else "huey",
        "scheduler_managed": not global_takeover_owned,
        "last_status": a.last_status,
        "created_at": a.created_at,
    }


def _serialize_run(r: models.AutomationRun) -> dict:
    timeline = []
    try:
        timeline = json.loads(r.timeline_json) if r.timeline_json else []
    except Exception:
        pass
    return {
        "id": r.id,
        "automation_id": r.automation_id,
        "trigger": r.trigger,
        "source": r.source,
        "status": r.status,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "synced_threads": r.synced_threads,
        "approvals_created": r.approvals_created,
        "drafts_created": r.drafts_created,
        "follow_ups_resolved": r.follow_ups_resolved,
        "replies_stopped": r.replies_stopped,
        "summary": r.summary,
        "timeline": timeline,
        "error": r.error,
        "execution_mode": r.execution_mode,
        "prepared_at": r.prepared_at,
        "confirmed_at": r.confirmed_at,
        "confirmation_expires_at": r.confirmation_expires_at,
        "created_at": r.created_at,
    }


@router.post("/generate")
def generate(body: AutomationGenerateRequest, db: Session = Depends(get_db)):
    if body.scope not in {"campaign", "global"}:
        raise HTTPException(status_code=400, detail="scope must be campaign or global")
    campaign = db.get(models.Campaign, body.campaign_id) if body.campaign_id else None
    if body.scope == "campaign" and not campaign:
        raise HTTPException(status_code=404, detail="campaign not found")
    plan = automation_svc.generate_plan(body.prompt)
    return {"prompt": body.prompt, "campaign_id": body.campaign_id, "scope": body.scope, "plan": plan.model_dump()}


@router.post("")
def create(body: AutomationCreate, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    if body.scope not in {"campaign", "global"}:
        raise HTTPException(status_code=400, detail="scope must be campaign or global")
    campaign = db.get(models.Campaign, body.campaign_id) if body.campaign_id else None
    if body.scope == "campaign" and not campaign:
        raise HTTPException(status_code=404, detail="campaign not found")
    try:
        a = automation_svc.create_automation(db, owner_id, body.prompt, body.campaign_id, body.plan, name=body.name, scope=body.scope)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    db.refresh(a)
    return _serialize(db, a)


@router.get("", response_model=AutomationListOut)
def list_automations(db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    s = get_settings()
    items = automation_svc.list_for_owner(db, owner_id)
    return {
        "items": [_serialize(db, a) for a in items],
        # OpenClaw is ONLY a cron/webhook trigger here. "Connected" means the
        # webhook token + (optionally) endpoint are configured. If not, the page
        # shows "未连接" and Run now falls back to the backend local scheduler.
        "openclaw_cron_configured": bool(s.AUTOMATION_WEBHOOK_TOKEN),
        "openclaw_configured": is_openclaw_configured(s),
    }


@router.get("/scheduler/status")
def scheduler_status(db: Session = Depends(get_db)):
    """Describe the Web/backend scheduler without relying on Electron IPC."""
    owner_id = ensure_owner(db)
    settings = get_settings()
    consumer = read_consumer_status()
    takeover_enabled = flags.is_agent_takeover_enabled(db)
    enabled_query = (
        db.query(models.Automation)
        .filter_by(owner_id=owner_id, status="enabled")
    )
    if takeover_enabled:
        enabled_query = enabled_query.filter(models.Automation.scope != "global")
    enabled = enabled_query.count()
    next_due_query = (
        db.query(models.Automation.next_run_at)
        .filter_by(owner_id=owner_id, status="enabled")
        .filter(models.Automation.next_run_at.isnot(None))
    )
    if takeover_enabled:
        next_due_query = next_due_query.filter(models.Automation.scope != "global")
    next_due = next_due_query.order_by(models.Automation.next_run_at.asc()).first()
    display_timezone = workspace_time.get_timezone(db)
    now = datetime.now(timezone.utc)
    return {
        "provider": "backend_huey",
        "electron_required": False,
        "scheduler_enabled": bool(consumer.get("healthy")),
        "consumer_healthy": bool(consumer.get("healthy")),
        "consumer_state": consumer.get("state"),
        "enabled_automations": enabled,
        "scan_interval_seconds": 60,
        "next_due_at": workspace_time.utc_iso(next_due[0]) if next_due else None,
        "display_timezone": display_timezone,
        "display_time": {"next_due_at": workspace_time.display(next_due[0] if next_due else None, display_timezone)},
        "agent_takeover_owns_global": takeover_enabled,
        "server_time": workspace_time.utc_iso(now),
        "server_time_display": workspace_time.display(now, display_timezone),
    }


@router.post("/scheduler/run-due")
def run_due_automations(db: Session = Depends(get_db)):
    """Run one due scan; only already-enabled due items are enqueued."""
    owner_id = ensure_owner(db)
    if not read_consumer_status().get("healthy"):
        raise HTTPException(status_code=409, detail="consumer is not healthy")
    enqueued = scan_due_automations(owner_id=owner_id)
    return {
        "ok": True,
        "provider": "backend_huey",
        "enqueued": enqueued,
        "message": "due automations enqueued" if enqueued else "no automation is due",
    }


@router.get("/global")
def get_global_automation(db: Session = Depends(get_db)):
    """Return the owner's single global Inbox switch, without requiring a Campaign."""
    owner_id = ensure_owner(db)
    item = db.query(models.Automation).filter_by(owner_id=owner_id, scope="global").first()
    return _serialize(db, item) if item else {
        "id": None, "scope": "global", "status": "disabled", "execution_mode": "full_auto",
        "tick_interval_minutes": 60, "exists": False,
    }


@router.post("/global")
def update_global_automation(body: GlobalAutomationUpdate, db: Session = Depends(get_db)):
    """Enable/disable the master Inbox module independently of Campaign modules."""
    owner_id = ensure_owner(db)
    if body.mode not in {"full_auto", "semi_auto"}:
        raise HTTPException(status_code=400, detail="mode must be full_auto or semi_auto")
    valid_takeover = {"recent_days", "all_business", "future_only"}
    if body.enabled and body.takeover_scope not in valid_takeover:
        raise HTTPException(status_code=400, detail="takeover_scope is required before enabling global automation")
    if body.takeover_scope == "recent_days" and not (body.takeover_days and 1 <= body.takeover_days <= 3650):
        raise HTTPException(status_code=400, detail="takeover_days must be between 1 and 3650")
    item = db.query(models.Automation).filter_by(owner_id=owner_id, scope="global").first()
    if item is None:
        plan = AutomationPlan(execution_mode=body.mode, tick_interval_minutes=body.tick_interval_minutes)
        item = automation_svc.create_automation(
            db, owner_id, "Global inbox automation", None, plan,
            name="Global Inbox Automation", scope="global",
        )
    item.execution_mode = body.mode
    item.tick_interval_minutes = max(1, min(body.tick_interval_minutes, 1440))
    plan = automation_svc.parse_plan(item.plan_json)
    plan.execution_mode = body.mode
    plan.tick_interval_minutes = item.tick_interval_minutes
    if body.enabled:
        now = datetime.now(timezone.utc)
        plan.takeover_scope = body.takeover_scope
        plan.takeover_days = body.takeover_days if body.takeover_scope == "recent_days" else None
        cutoff = (
            now - timedelta(days=body.takeover_days)
            if body.takeover_scope == "recent_days"
            else now if body.takeover_scope == "future_only" else None
        )
        plan.takeover_cutoff_at = cutoff.isoformat() if cutoff else None
    item.plan_json = json.dumps(plan.model_dump())
    automation_svc.set_status(db, item, "enabled" if body.enabled else "disabled")
    db.commit()
    db.refresh(item)
    return _serialize(db, item)


@router.get("/{automation_id}")
def detail(automation_id: int, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    a = _get_automation_or_404(db, automation_id, owner_id)
    runs = (
        db.query(models.AutomationRun)
        .filter_by(automation_id=a.id)
        .order_by(models.AutomationRun.created_at.desc())
        .limit(20)
        .all()
    )
    out = _serialize(db, a)
    out["runs"] = [_serialize_run(r) for r in runs]
    out["campaign_metrics"] = automation_svc.campaign_metrics(db, a.campaign_id) if a.campaign_id else None
    return out


@router.post("/{automation_id}/enable")
def enable(automation_id: int, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    a = _get_automation_or_404(db, automation_id, owner_id)
    automation_svc.set_status(db, a, "enabled")
    db.commit()
    return _serialize(db, a)


@router.post("/{automation_id}/pause")
def pause(automation_id: int, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    a = _get_automation_or_404(db, automation_id, owner_id)
    automation_svc.set_status(db, a, "paused")
    db.commit()
    return _serialize(db, a)


@router.post("/{automation_id}/schedule")
def update_schedule(
    automation_id: int,
    body: AutomationScheduleUpdate,
    db: Session = Depends(get_db),
):
    """Update cadence through the Web while preserving existing safety policy."""
    owner_id = ensure_owner(db)
    a = _get_automation_or_404(db, automation_id, owner_id)
    a.tick_interval_minutes = body.tick_interval_minutes
    plan = automation_svc.parse_plan(a.plan_json)
    plan.tick_interval_minutes = body.tick_interval_minutes
    a.plan_json = json.dumps(plan.model_dump())
    if a.status == "enabled":
        a.next_run_at = datetime.now(timezone.utc) + timedelta(
            minutes=body.tick_interval_minutes
        )
    db.commit()
    db.refresh(a)
    return _serialize(db, a)


@router.post("/{automation_id}/run-now")
def run_now(automation_id: int, mode: str | None = None, db: Session = Depends(get_db)):
    """Manual one-shot execution.

    Creates a QUEUED AutomationRun and enqueues it to the Huey worker, then
    returns immediately with the run id (does NOT wait for email processing).
    The worker (separate process) flips the run queued -> running -> final.

    Idempotency / single-inflight guarantee: if the Automation already has a
    queued/running run, this returns that EXISTING run_id instead of creating a
    second executable run -- so rapid or concurrent Run-now clicks are safe.
    """
    owner_id = ensure_owner(db)
    a = _get_automation_or_404(db, automation_id, owner_id)
    if mode is not None and mode not in {"full_auto", "semi_auto"}:
        raise HTTPException(status_code=400, detail="mode must be full_auto or semi_auto")
    # Pre-check so we can report whether this call created a new run or returned
    # an in-flight one. enqueue_run itself also enforces the single-inflight rule
    # at the DB layer (unique index), so even a concurrent click cannot create a
    # second executable run.
    existing = (
        db.query(models.AutomationRun)
        .filter(models.AutomationRun.automation_id == a.id,
                models.AutomationRun.status.in_(["queued", "running"]))
        .first()
    )
    run = enqueue_run(db, a, trigger="manual", source="backend", execution_mode=mode)
    return {
        "ok": True,
        "run_id": run.id,
        "status": run.status,
        "trigger": "manual",
        "source": "backend",
        "mode": run.execution_mode,
        "already_inflight": existing is not None,
    }


@router.post("/tick")
def tick(req: Request, db: Session = Depends(get_db)):
    """Cron trigger. OpenClaw (or any external scheduler) calls this every 5 min.

    If AUTOMATION_WEBHOOK_TOKEN is set, the request MUST carry
    `Authorization: Bearer <token>`; otherwise 403. OpenClaw never receives a Gmail
    token and never calls Gmail directly.

    The tick NEVER runs business logic synchronously. It routes every enabled
    Automation through ``enqueue_run`` (the same async queue used by the Huey
    scheduler), which enforces the single-inflight-per-Automation rule. The worker
    later flips each run queued -> running -> final. This keeps /tick instant and
    consistent with the local scheduler.
    """
    s = get_settings()
    source = "backend"
    auth = req.headers.get("Authorization", "")
    if s.AUTOMATION_WEBHOOK_TOKEN:
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else None
        if token != s.AUTOMATION_WEBHOOK_TOKEN:
            raise HTTPException(status_code=403, detail="invalid or missing automation webhook token")
        source = "openclaw"

    enqueued = 0
    run_ids: list[int] = []
    for a in db.query(models.Automation).filter_by(status="enabled").all():
        try:
            run = enqueue_run(db, a, trigger="cron", source=source)
            enqueued += 1
            run_ids.append(run.id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("tick enqueue failed for automation %s: %s", a.id, e)
    db.commit()
    return {"ok": True, "source": source, "enqueued": enqueued, "run_ids": run_ids}


@router.delete("/{automation_id}")
def delete_automation(automation_id: int, db: Session = Depends(get_db)):
    """Delete an Automation and its associated runs."""
    owner_id = ensure_owner(db)
    a = _get_automation_or_404(db, automation_id, owner_id)
    db.query(models.AutomationRun).filter_by(automation_id=a.id).delete()
    db.delete(a)
    db.commit()
    return {"ok": True, "deleted_id": automation_id}
