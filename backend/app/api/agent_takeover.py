"""Workspace-level TACWork Agent Takeover switch."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy.orm import Session

from .. import models
from ..consumer_status import read_consumer_status
from ..services import agent_takeover as takeover_svc
from ..services import agent_providers as provider_svc
from ..services import workspace_time
from ..services.agent_profile import get_or_create_profile
from ..services.automation import create_automation, parse_plan
from ..schemas import AutomationPlan
from .deps import ensure_owner, get_db

router = APIRouter(prefix="/api/agent-takeover", tags=["agent-takeover"])


class AgentTakeoverUpdate(BaseModel):
    enabled: bool
    interval_minutes: StrictInt = Field(default=60, ge=takeover_svc.MIN_INTERVAL_MINUTES, le=takeover_svc.MAX_INTERVAL_MINUTES, description="Agent Takeover cadence in whole minutes (1-1440).")
    display_timezone: str | None = None
    scope: str | None = Field(default=None, pattern="^(inbox|campaign|all)$")


class WorkspaceDisplayTimezoneUpdate(BaseModel):
    display_timezone: str


class AgentTakeoverAuthorize(BaseModel):
    token: str
    operation: str
    requested_scope: str | None = Field(default=None, pattern="^(campaign|global)$")
    automation_id: int | None = None
    campaign_id: int | None = None
    thread_id: int | None = None
    approval_id: int | None = None
    contact_id: int | None = None


class AgentTakeoverTelemetry(BaseModel):
    token: str
    stage: str
    status: str
    detail: dict | None = None


def _global_automation(db: Session, owner_id: int):
    return db.query(models.Automation).filter_by(owner_id=owner_id, scope="global").first()


def _out(db: Session, owner_id: int) -> dict:
    state = takeover_svc.status(db)
    now = datetime.now(timezone.utc)
    state = {
        **state,
        "next_run_at": workspace_time.utc_iso(state["next_run_at"]),
        "last_run_at": workspace_time.utc_iso(state["last_run_at"]),
        "last_completed_at": workspace_time.utc_iso(state["last_completed_at"]),
    }
    profile = db.query(models.AgentProfile).filter_by(owner_id=owner_id).first()
    automation = _global_automation(db, owner_id)
    consumer = read_consumer_status()
    return {
        **state,
        "approval_mode": (profile.approval_mode if profile else "human_review") or "human_review",
        "execution_mode": automation.execution_mode if automation else "semi_auto",
        "global_automation_id": automation.id if automation else None,
        "consumer_healthy": bool(consumer.get("healthy")),
        "consumer_state": consumer.get("state"),
        "server_time": workspace_time.utc_iso(now),
        "server_time_display": workspace_time.display(now, state["display_timezone"]),
        "permissions": {
            "agent_review": bool(state["enabled"]),
            "automatic_send": bool(state["enabled"]),
            "scheduled_wakeup": bool(state["enabled"]),
            "inbox_operations": bool(state["enabled"]),
        },
    }


@router.get("")
def get_agent_takeover(db: Session = Depends(get_db)):
    return _out(db, ensure_owner(db))


@router.post("")
def update_agent_takeover(body: AgentTakeoverUpdate, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    effective_scope = body.scope or takeover_svc.scope_value(db)
    if body.enabled:
        try:
            provider_svc.require_capability(
                provider_svc.get_selected_provider(db),
                provider_svc.CAP_SCHEDULED_TAKEOVER,
            )
        except provider_svc.AgentProviderError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
    profile = get_or_create_profile(db, owner_id)
    automation = _global_automation(db, owner_id)
    if automation is None:
        plan = AutomationPlan(
            execution_mode="semi_auto", tick_interval_minutes=body.interval_minutes,
            takeover_scope="future_only",
            takeover_cutoff_at=datetime.now(timezone.utc).isoformat(),
        )
        automation = create_automation(
            db, owner_id, "Global inbox automation", None, plan,
            name="Global Inbox Automation", scope="global",
        )

    old_enabled = takeover_svc.status(db)["enabled"]
    profile.approval_mode = "agent_review" if body.enabled else "human_review"
    profile.version += 1
    automation.execution_mode = "full_auto" if body.enabled else "semi_auto"
    automation.tick_interval_minutes = body.interval_minutes
    automation.status = "enabled" if body.enabled else "disabled"
    # TACWork owns cadence while takeover is enabled.  Keep this NULL so the
    # legacy Automation scanner cannot directly enqueue the Global run.
    automation.next_run_at = None
    plan = parse_plan(automation.plan_json)
    plan.execution_mode = automation.execution_mode
    plan.tick_interval_minutes = body.interval_minutes
    if not plan.takeover_scope:
        plan.takeover_scope = "future_only"
        plan.takeover_cutoff_at = datetime.now(timezone.utc).isoformat()
    automation.plan_json = json.dumps(plan.model_dump())
    try:
        takeover_svc.configure(
            db, enabled=body.enabled, interval_minutes=body.interval_minutes,
            display_timezone=body.display_timezone, scope=effective_scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(models.AuditLog(
        actor="user", action="agent_takeover_updated", entity="agent_takeover",
        detail=json.dumps({
            "enabled": f"{old_enabled}->{body.enabled}",
            "interval_minutes": body.interval_minutes,
            "approval_mode": profile.approval_mode,
            "execution_mode": automation.execution_mode,
            "scope": effective_scope,
        }),
    ))
    db.commit()
    return _out(db, owner_id)


@router.post("/display-timezone")
def update_workspace_display_timezone(body: WorkspaceDisplayTimezoneUpdate, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    try:
        workspace_time.set_timezone(db, body.display_timezone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return _out(db, owner_id)


@router.post("/run-now")
def run_agent_takeover_now(db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    if not takeover_svc.status(db)["enabled"]:
        raise HTTPException(status_code=409, detail="agent_takeover_disabled")
    result = takeover_svc.trigger_due(db, force=True)
    return {**result, "takeover": _out(db, owner_id)}


@router.post("/authorize")
def authorize_agent_takeover(body: AgentTakeoverAuthorize, db: Session = Depends(get_db)):
    """Validate one scheduled-session capability without expanding its scope."""
    owner_id = ensure_owner(db)
    campaign_operations = {
        "create_campaign", "add_campaign_contacts", "update_campaign", "generate_campaign_outreach",
        "start_campaign", "pause_campaign", "stop_campaign", "remove_campaign_contact",
    }
    contact_operations = {"update_contact", "transition_contact"}
    automation_operations = {
        "enable_automation", "pause_automation", "schedule_automation", "start_agent_run",
    }
    automation_plan_operations = {"generate_automation_plan", "create_automation"}
    allowed = {
        "sync_gmail", "sort_inbox", "start_daily_triage", "control_daily_triage",
        "create_campaign", "generate_automation_plan", "create_automation",
        "generate_inbox_reply", "revise_approval",
        *campaign_operations, *automation_operations, *contact_operations,
    }
    if body.operation not in allowed or not takeover_svc.token_is_valid(db, body.token):
        raise HTTPException(status_code=403, detail="invalid_or_expired_agent_takeover_grant")
    active_scope = takeover_svc.scope_value(db)
    if body.operation in campaign_operations or body.operation in contact_operations:
        if active_scope not in {"campaign", "all"}:
            raise HTTPException(status_code=403, detail="agent_takeover_scope_does_not_allow_campaign_operations")
    if body.operation in campaign_operations and body.operation != "create_campaign":
        if body.campaign_id is None:
            raise HTTPException(status_code=422, detail="agent_takeover_campaign_required")
    if body.operation in automation_plan_operations:
        is_campaign_automation = body.requested_scope == "campaign" or body.campaign_id is not None
        if is_campaign_automation and active_scope not in {"campaign", "all"}:
            raise HTTPException(status_code=403, detail="agent_takeover_scope_does_not_allow_campaign_operations")
    if body.campaign_id is not None:
        campaign = db.get(models.Campaign, body.campaign_id)
        if not campaign or campaign.owner_id != owner_id:
            raise HTTPException(status_code=403, detail="agent_takeover_campaign_not_owned")
    if body.operation in contact_operations:
        if body.contact_id is None:
            raise HTTPException(status_code=422, detail="agent_takeover_contact_required")
        contact = db.get(models.Contact, body.contact_id)
        if not contact or contact.owner_id != owner_id:
            raise HTTPException(status_code=403, detail="agent_takeover_contact_not_owned")
    if body.operation == "generate_inbox_reply":
        if body.thread_id is None:
            raise HTTPException(status_code=422, detail="agent_takeover_thread_required")
        thread = db.get(models.EmailThread, body.thread_id)
        account = db.get(models.GmailAccount, thread.gmail_account_id) if thread else None
        if not thread or not account or account.user_id != owner_id:
            raise HTTPException(status_code=403, detail="agent_takeover_thread_not_owned")
    if body.operation == "revise_approval":
        if body.approval_id is None:
            raise HTTPException(status_code=422, detail="agent_takeover_approval_required")
        approval = db.get(models.Approval, body.approval_id)
        from ..services.approvals import approval_owner_id
        if not approval or approval_owner_id(db, approval) != owner_id:
            raise HTTPException(status_code=403, detail="agent_takeover_approval_not_owned")
    if body.operation in automation_operations:
        if body.automation_id is None:
            raise HTTPException(status_code=422, detail="agent_takeover_automation_required")
        automation = db.get(models.Automation, body.automation_id)
        if not automation or automation.owner_id != owner_id:
            raise HTTPException(status_code=403, detail="agent_takeover_automation_not_owned")
        if automation.scope == "campaign" and active_scope not in {"campaign", "all"}:
            raise HTTPException(status_code=403, detail="agent_takeover_scope_does_not_allow_campaign_operations")
    if body.operation == "start_agent_run":
        if automation.execution_mode != "full_auto" or automation.status != "enabled":
            raise HTTPException(status_code=409, detail="automation_not_ready_for_takeover")
    return {"authorized": True, "operation": body.operation}


@router.post("/telemetry")
def record_agent_takeover_telemetry(body: AgentTakeoverTelemetry, db: Session = Depends(get_db)):
    """Record sanitized scheduled-cycle stages for support diagnostics."""
    ensure_owner(db)
    if not takeover_svc.token_is_valid(db, body.token):
        raise HTTPException(status_code=403, detail="invalid_or_expired_agent_takeover_grant")
    allowed_stages = {
        "read_operating_state", "read_inbox", "read_daily_triage",
        "sync_gmail", "sort_inbox", "start_daily_triage", "control_daily_triage",
        "poll_daily_triage", "create_campaign", "add_campaign_contacts",
        "remove_campaign_contact", "generate_inbox_reply",
        "revise_approval",
        "update_campaign", "generate_campaign_outreach", "start_campaign",
        "pause_campaign", "stop_campaign", "generate_automation_plan",
        "create_automation", "enable_automation", "pause_automation",
        "schedule_automation", "start_agent_run", "poll_agent_run",
    }
    allowed_statuses = {"started", "success", "failed"}
    if body.stage not in allowed_stages or body.status not in allowed_statuses:
        raise HTTPException(status_code=422, detail="invalid_takeover_telemetry")
    from ..services import flags
    flags.set_flag(db, takeover_svc.FLAG_CURRENT_STAGE, f"{body.stage}:{body.status}")
    if body.status == "success":
        flags.set_flag(db, takeover_svc.FLAG_LAST_SUCCESS_STAGE, body.stage)
    elif body.status == "failed":
        flags.set_flag(db, takeover_svc.FLAG_LAST_ERROR, json.dumps(body.detail or {})[:500])
        flags.set_flag(db, takeover_svc.FLAG_CYCLE_HAS_ERRORS, "true")
    cycle_id = flags.get_flag(db, takeover_svc.FLAG_CYCLE_ID)
    db.add(models.AuditLog(
        actor=takeover_svc.status(db).get("session_provider") or provider_svc.selected_provider_id(db),
        action=f"agent_takeover_{body.stage}_{body.status}",
        entity="agent_takeover", entity_id=cycle_id,
        detail=json.dumps(body.detail or {}), success=body.status != "failed",
    ))
    db.commit()
    return {"ok": True, "cycle_id": cycle_id, "stage": body.stage, "status": body.status}
