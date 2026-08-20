"""Policy & Approval Engine.

Every Gmail write / scheduling action MUST pass through evaluate() before
executing. This enforces the master rules (recorded execution authorization, allowlist,
campaign enabled, daily limit, sending window, suppression, idempotency,
no-new-reply, shadow mode).
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from .. import models
from ..config import get_settings


@dataclasses.dataclass
class PolicyContext:
    agent: str  # langgraph | openclaw
    mode: str  # langgraph_only | openclaw_only | compare
    is_primary: bool
    tool_name: str
    to_email: Optional[str] = None
    campaign_id: Optional[int] = None
    thread_id: Optional[int] = None  # our DB thread id
    idempotency_key: Optional[str] = None
    approval_id: Optional[int] = None
    gmail_account_id: Optional[int] = None
    requires_approval: bool = True
    kind: Optional[str] = None  # outreach | follow_up | reply | first_send


@dataclasses.dataclass
class PolicyResult:
    allowed: bool
    reason: Optional[str] = None


# Tools that perform a real Gmail WRITE (must be blocked for shadow agent)
GMAIL_WRITE_TOOLS = {
    "create_draft",
    "update_draft",
    "send_approved_draft",
    "add_label",
    "remove_label",
    "archive_thread",
}
# Tools that mutate our scheduling state (must be blocked for shadow agent)
SCHEDULER_WRITE_TOOLS = {"schedule_follow_up", "cancel_follow_up"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _campaign_window_ok(db, campaign) -> tuple[bool, Optional[str]]:
    if campaign is None:
        return True, None
    try:
        tz = ZoneInfo(campaign.timezone or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    now_local = _now_utc().astimezone(tz)
    # weekend check (Mon=0 .. Sun=6)
    if now_local.weekday() >= 5:  # Sat/Sun
        return False, "outside sending window (weekend)"
    h = now_local.hour
    if not (campaign.sending_window_start <= h < campaign.sending_window_end):
        return False, f"outside sending window ({campaign.sending_window_start}-{campaign.sending_window_end} local)"
    return True, None


def evaluate(db, ctx: PolicyContext) -> PolicyResult:
    settings = get_settings()
    agent_label = ctx.agent

    # 1. Shadow mode blocks ALL Gmail writes and scheduler writes.
    if ctx.mode == "compare" and not ctx.is_primary:
        if ctx.tool_name in GMAIL_WRITE_TOOLS or ctx.tool_name in SCHEDULER_WRITE_TOOLS:
            return PolicyResult(False, "shadow agent cannot execute Gmail/scheduler writes")

    # 1b. Global pause (user toggled "pause all") blocks ALL real sends.
    if ctx.tool_name == "send_approved_draft":
        pause_flag = db.query(models.SystemFlag).filter_by(key="global_pause").first()
        if pause_flag and str(pause_flag.value).strip().lower() in ("true", "1", "yes"):
            return PolicyResult(False, "globally paused")

    # 2. Campaign must be active for SEND / labelling / archive / scheduler writes.
    # Drafting (create_draft / update_draft) is allowed in any state so users can
    # prepare outreach before launching the campaign.
    campaign = None
    if ctx.campaign_id:
        campaign = db.get(models.Campaign, ctx.campaign_id)
        if ctx.tool_name in (
            {"send_approved_draft", "add_label", "remove_label", "archive_thread"}
            | SCHEDULER_WRITE_TOOLS
        ):
            if campaign is None:
                return PolicyResult(False, "campaign not found")
            if campaign.status != "active":
                return PolicyResult(False, f"campaign is {campaign.status}, not active")

    # 3. Suppression list (only for outgoing recipient targeting)
    if ctx.to_email and ctx.tool_name in {"create_draft", "update_draft", "send_approved_draft"}:
        if db.query(models.Suppression).filter_by(
            owner_id=campaign.owner_id if campaign else None, email=ctx.to_email.lower()
        ).first() if campaign else None:
            return PolicyResult(False, "recipient in suppression list")

    # 4. Optional recipient allowlist (only when explicitly configured).
    # Production installations normally leave it empty, which means any
    # recipient may proceed to the remaining approval and safety checks.
    if ctx.tool_name == "send_approved_draft":
        allowlist = settings.recipient_allowlist
        if allowlist and ctx.to_email and ctx.to_email.lower() not in allowlist:
            return PolicyResult(False, "recipient not in configured allowlist")

    # 5. A tracked authorization record is required for every send. It may be
    # released by a user in semi-auto mode or by a full-auto Agent Run.
    if ctx.tool_name == "send_approved_draft":
        if ctx.requires_approval:
            if ctx.approval_id is None:
                return PolicyResult(False, "no approval execution authorization on record")
            ap = db.get(models.Approval, ctx.approval_id)
            if ap is None:
                return PolicyResult(False, "approval missing")
            if ap.status == "rejected":
                return PolicyResult(False, "approval was rejected")

    # 6. Idempotency: same key already executed => block
    if ctx.idempotency_key:
        delivery = (
            db.query(models.DeliveryAttempt)
            .filter_by(idempotency_key=ctx.idempotency_key)
            .first()
        )
        if delivery and delivery.status in {
            "sending", "unknown", "gmail_sent", "verified"
        }:
            return PolicyResult(
                False, f"delivery reconciliation required ({delivery.status})"
            )
        dup = (
            db.query(models.ToolExecution)
            .filter_by(idempotency_key=ctx.idempotency_key, status="ok")
            .first()
        )
        if dup and ctx.tool_name in {"send_approved_draft", "create_draft"}:
            return PolicyResult(False, "idempotency key already executed")

    # 7. New human reply on thread cancels OUTREACH/FOLLOW-UP sends.
    # A human reply is exactly when we WANT to send a reply back, so a
    # kind="reply" send is explicitly exempt (otherwise the agent could never
    # answer the customer). Guard only the cold/automated sends.
    if ctx.tool_name == "send_approved_draft" and ctx.thread_id and ctx.kind != "reply":
        th = db.get(models.EmailThread, ctx.thread_id)
        if th and th.has_human_reply:
            return PolicyResult(False, "thread has a new human reply; send cancelled")

    # 8. Daily send limit (count sent drafts today for the campaign)
    if ctx.tool_name == "send_approved_draft" and campaign:
        start = _now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = (
            db.query(models.EmailDraft)
            .filter(
                models.EmailDraft.campaign_contact_id.in_(
                    db.query(models.CampaignContact.id).filter_by(campaign_id=campaign.id)
                ),
                models.EmailDraft.status == "sent",
                models.EmailDraft.updated_at >= start,
            )
            .count()
        )
        if sent_today >= campaign.daily_send_limit:
            return PolicyResult(False, f"daily send limit reached ({campaign.daily_send_limit})")

    # 9. Sending window
    if ctx.tool_name == "send_approved_draft" and campaign:
        ok, reason = _campaign_window_ok(db, campaign)
        if not ok:
            return PolicyResult(False, reason)

    return PolicyResult(True, None)
