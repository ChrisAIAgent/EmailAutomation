"""Dashboard metrics + activity feed (all from real DB records)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..agents.openclaw_adapter import OpenClawAdapter
from ..agents.langgraph_agent import LangGraphAdapter
from ..config import get_settings
from ..consumer_status import read_consumer_status
from ..services import flags as flag_svc
from ..services.accounts import resolve_sending_account
from ..services.inbox_triage import contact_needs_reply
from .deps import ensure_owner, get_db

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/readiness")
def readiness(db: Session = Depends(get_db)):
    """One read-only operating-context contract for users and Agents."""
    owner_id = ensure_owner(db)
    settings = get_settings()
    account, _oauth = resolve_sending_account(db, owner_id=owner_id, provision=False)
    consumer = read_consumer_status()
    paused = flag_svc.is_globally_paused(db)
    profile = db.query(models.AgentProfile).filter_by(owner_id=owner_id, is_active=True).first()
    pending = db.query(models.Approval).filter_by(status="pending").count()
    frozen = (db.query(models.AutomationRun)
              .filter_by(execution_mode="semi_auto", status="awaiting_confirmation")
              .order_by(models.AutomationRun.prepared_at.desc()).all())
    invalid = (db.query(models.AutomationRun)
               .filter_by(execution_mode="semi_auto", status="invalidated")
               .order_by(models.AutomationRun.updated_at.desc()).first())
    blockers = []
    if not account or not account.is_connected or not account.oauth:
        blockers.append("gmail_not_connected")
    if not consumer.get("healthy"):
        blockers.append("consumer_unhealthy")
    if paused:
        blockers.append("global_pause_enabled")
    if not profile:
        blockers.append("agent_profile_missing")
    if invalid:
        blockers.append("invalidated_semi_auto_run_requires_review")
    next_action = (
        "review_invalidated_run" if invalid else
        "resolve_frozen_confirmation" if frozen else
        "review_needs_reply" if metrics_needs_reply(db) else
        "sync_and_triage_inbox" if account and account.is_connected else
        "connect_gmail"
    )
    return {
        "status": "blocked" if blockers else "ready",
        "blockers": blockers,
        "next_action": next_action,
        "gmail": {"connected": bool(account and account.is_connected and account.oauth), "email": account.email if account else None},
        "consumer": consumer,
        "global_pause": paused,
        "real_send": settings.ENABLE_REAL_SEND,
        "profile_configured": bool(profile),
        "approval_mode": (profile.approval_mode if profile and profile.approval_mode else "human_review"),
        "pending_approvals": pending,
        "awaiting_confirmation_runs": [r.id for r in frozen],
        "latest_invalidated_run": invalid.id if invalid else None,
    }


def metrics_needs_reply(db: Session) -> int:
    # Derive live from thread direction (the single source of truth) rather than a
    # stale cached field, so the number matches what the Inbox UI shows.
    return sum(1 for c in db.query(models.Contact).all() if contact_needs_reply(db, c))


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)):
    s = get_settings()
    start_today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    connected = db.query(models.GmailAccount).filter_by(is_connected=True).count()
    total_contacts = db.query(models.Contact).count()
    active = db.query(models.Campaign).filter_by(status="active").count()
    sent_today = db.query(models.EmailDraft).filter(models.EmailDraft.status == "sent", models.EmailDraft.updated_at >= start_today).count()
    # A raw inbound Gmail message is not necessarily a customer reply. Count
    # only messages whose sender passed Inbox triage and exists in Contacts.
    replies = (
        db.query(models.EmailMessage)
        .join(models.EmailThread, models.EmailThread.id == models.EmailMessage.thread_id)
        .join(models.Contact, models.Contact.email == models.EmailThread.contact_email)
        .filter(models.EmailMessage.is_incoming.is_(True))
        .count()
    )
    # Positive means an actionable sales reply, not merely a classifier intent
    # left on a thread later moved into manual review.  Derive live from thread
    # direction (single source of truth), not the stale cached field.
    needs_reply_contacts = [c for c in db.query(models.Contact).all() if contact_needs_reply(db, c)]
    positive = len(needs_reply_contacts)
    needs_reply = len(needs_reply_contacts)
    pending = db.query(models.Approval).filter_by(status="pending").count()
    scheduled = db.query(models.FollowUpTask).filter_by(status="scheduled").count()
    failed_tasks = db.query(models.FollowUpTask).filter_by(status="failed").count()
    failed_tools = db.query(models.ToolExecution).filter_by(status="failed").count()
    # Honesty fields (read-only aggregates, no write path touched):
    # human_review must be reported separately from needs_reply (AGENTS.md gate 3).
    human_review = (
        db.query(models.Contact)
        .filter(models.Contact.next_action == "human_review")
        .count()
    )
    # Stop/unsubscribe reasons live only in the Suppression table server-side;
    # surface them so a report can show "stopped count + reason" truthfully.
    sup_rows = (
        db.query(models.Suppression.reason, func.count(models.Suppression.id))
        .group_by(models.Suppression.reason)
        .all()
    )
    by_reason = {r: c for r, c in sup_rows}
    suppressions = {"total": sum(by_reason.values()), "by_reason": by_reason}
    # Restricted mode must be visible to avoid misreading an allowlisted send
    # as a full broadcast. recipient_allowlist includes the legacy env alias.
    restricted_allowlist_configured = bool(s.recipient_allowlist)
    oc = OpenClawAdapter(db).health_check()
    lg = LangGraphAdapter(db).health_check()
    return {
        "connected_emails": connected,
        "total_contacts": total_contacts,
        "active_campaigns": active,
        "sent_today": sent_today,
        "replies": replies,
        "positive_replies": positive,
        "needs_reply": needs_reply,
        "pending_approvals": pending,
        "scheduled_follow_ups": scheduled,
        "failed_tasks": failed_tasks + failed_tools,
        "real_send_enabled": s.ENABLE_REAL_SEND,
        "draft_only": not s.ENABLE_REAL_SEND,
        "restricted_allowlist_configured": restricted_allowlist_configured,
        "human_review": human_review,
        "suppressions": suppressions,
        "openclaw_connected": oc.configured and oc.reachable,
        "langgraph_configured": lg.configured,
    }


@router.get("/activity")
def activity(db: Session = Depends(get_db), limit: int = 40):
    items = []
    for r in db.query(models.AgentRun).order_by(models.AgentRun.created_at.desc()).limit(limit).all():
        items.append(("agent_run", r.created_at, f"[{r.agent}] {r.task_type} -> {r.intent} (conf {r.confidence}%)", r.status, r.error))
    for r in db.query(models.ToolExecution).order_by(models.ToolExecution.created_at.desc()).limit(limit).all():
        detail = r.blocked_reason or r.error or ""
        items.append(("tool_exec", r.created_at, f"{r.tool_name} {'ALLOWED' if r.allowed else 'BLOCKED'} ({r.status})", r.status, detail))
    for r in db.query(models.Approval).order_by(models.Approval.created_at.desc()).limit(limit).all():
        items.append(("approval", r.created_at, f"{r.kind} to {r.to_email} [{r.agent}]", r.status, r.rejection_reason))
    for r in db.query(models.AuditLog).order_by(models.AuditLog.created_at.desc()).limit(limit).all():
        items.append(("audit", r.created_at, f"{r.actor}: {r.action}", "ok" if r.success else "fail", r.detail))
    items.sort(key=lambda x: x[1], reverse=True)
    out = []
    for cat, ts, summary, status, detail in items[:limit]:
        out.append({"category": cat, "created_at": ts, "summary": summary, "status": status, "detail": detail})
    return out
