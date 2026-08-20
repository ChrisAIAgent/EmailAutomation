"""Follow-up scheduling: state machine + next-time computation."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import models
from ..schemas import GenerateFollowUpInput
from ..tools.email_tools import UnifiedEmailToolLayer
from .accounts import resolve_sending_account

logger = logging.getLogger("followup")


def follow_up_intervals(campaign) -> list[int]:
    return [int(x) for x in (campaign.follow_up_intervals_days or "3,4").split(",") if x.strip()]


def compute_follow_up_time(campaign, base: datetime) -> datetime:
    """Next business datetime in the campaign timezone, inside the send window, skipping weekends."""
    tz = ZoneInfo(campaign.timezone or "Asia/Shanghai")
    intervals = follow_up_intervals(campaign)
    days = intervals[0] if intervals else 3
    dt = base.astimezone(tz) + timedelta(days=days)
    if dt.hour < campaign.sending_window_start:
        dt = dt.replace(hour=campaign.sending_window_start, minute=0, second=0, microsecond=0)
    if dt.hour >= campaign.sending_window_end:
        dt = (dt + timedelta(days=1)).replace(hour=campaign.sending_window_start, minute=0, second=0, microsecond=0)
    while dt.weekday() >= 5:  # Sat/Sun
        dt = (dt + timedelta(days=1)).replace(hour=campaign.sending_window_start, minute=0, second=0, microsecond=0)
    return dt.astimezone(timezone.utc)


def schedule_next_follow_up(db, cc, agent="langgraph", mode="langgraph_only", is_primary=True) -> dict:
    """Schedule the next follow-up using the current assigned-follow-up sequence."""
    if not cc.membership_active:
        return {"ok": False, "error": "campaign_contact_removed"}
    campaign = db.get(models.Campaign, cc.campaign_id)
    if campaign is None:
        return {"ok": False, "error": "campaign missing"}
    if cc.assigned_follow_ups >= campaign.max_follow_ups:
        return {"ok": False, "error": "max follow-ups reached"}
    account, oauth = resolve_sending_account(db, campaign=campaign)
    tl = UnifiedEmailToolLayer(db, account, oauth)
    base = cc.contact.last_contacted_at or datetime.now(timezone.utc)
    when = compute_follow_up_time(campaign, base)
    idem = f"fu:{cc.id}:{cc.assigned_follow_ups + 1}"
    return tl.schedule_follow_up(
        campaign_contact_id=cc.id, contact_id=cc.contact_id, campaign_id=campaign.id,
        thread_db_id=cc.thread_id, sequence=cc.assigned_follow_ups + 1,
        scheduled_at=when, agent=agent, mode=mode, is_primary=is_primary, idempotency_key=idem,
    )


def schedule_first_follow_up(db, cc, agent="langgraph", mode="langgraph_only", is_primary=True) -> dict:
    """Backward-compatible alias for callers not yet migrated to schedule_next_follow_up."""
    return schedule_next_follow_up(db, cc, agent=agent, mode=mode, is_primary=is_primary)


def process_due_follow_ups(db, orchestrator) -> list[dict]:
    """Lightweight scheduler tick: for due, scheduled tasks, generate + queue an approval.

    Returns a list of outcomes. Never sends — human approval required.
    """
    from .flags import is_globally_paused
    if is_globally_paused(db):
        return []
    now = datetime.now(timezone.utc)
    due = (
        db.query(models.FollowUpTask)
        .filter(models.FollowUpTask.status == "scheduled", models.FollowUpTask.scheduled_at <= now)
        .all()
    )
    outcomes = []
    for task in due:
        campaign = db.get(models.Campaign, task.campaign_id)
        if campaign is None or campaign.status != "active":
            task.status = "cancelled"
            continue
        cc = db.get(models.CampaignContact, task.campaign_contact_id)
        if cc is None or not cc.membership_active or cc.status in ("stopped", "done"):
            task.status = "cancelled"
            if cc is not None and not cc.membership_active:
                task.last_error = "campaign_contact_removed"
            continue
        # Re-check thread for new human reply before doing anything
        if task.thread_id:
            th = db.get(models.EmailThread, task.thread_id)
            if th and th.has_human_reply:
                task.status = "cancelled"
                continue
        task.status = "running"
        try:
            mode = campaign.agent_mode
            inp = GenerateFollowUpInput(
                campaign_id=campaign.id, contact_id=task.contact_id, thread_id=task.thread_id or 0,
                sequence=task.sequence, mode=mode,
            )
            proposal = orchestrator.generate_follow_up(inp)
            # create draft + approval (primary agent only executes; shadow only proposes)
            from . import approvals as approvals_svc
            from ..schemas import decision_to_proposal
            if hasattr(proposal, "langgraph"):  # ComparisonView
                ad = getattr(proposal, campaign.primary_agent) or getattr(proposal, "langgraph")
                if ad is None:
                    task.status = "failed"
                    task.last_error = "primary agent produced no decision"
                    continue
                proposal = decision_to_proposal(ad)
            ap = approvals_svc.create_follow_up_approval(db, task, proposal, mode=mode)
            task.status = "done"
            outcomes.append({"task_id": task.id, "approval_id": ap.id, "ok": True})
        except Exception as e:
            task.status = "failed"
            task.last_error = str(e)[:400]
            outcomes.append({"task_id": task.id, "ok": False, "error": str(e)[:300]})
    db.flush()
    return outcomes
