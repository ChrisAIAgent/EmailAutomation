"""Single source of truth for Contact lifecycle transitions.

Contacts are the shared customer pool.  Inbox triage, Campaign automation,
the Web UI and the Agent must all use the same transition side effects so a
classification change cannot leave an active Campaign member, a stale Draft,
or a scheduled follow-up behind.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import models


class ContactTransitionError(ValueError):
    """A requested Contact transition is not safe or is ambiguous."""

    def __init__(self, code: str, *, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _snapshot(contact: models.Contact) -> dict:
    def values(raw: str | None) -> list[str]:
        try:
            parsed = json.loads(raw or "[]")
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    return {
        "category": contact.category,
        "intent_level": contact.intent_level,
        "status": contact.status,
        "lifecycle_stage": contact.lifecycle_stage,
        "next_action": contact.next_action,
        "next_follow_up_at": contact.next_follow_up_at.isoformat() if contact.next_follow_up_at else None,
        "tags": values(contact.tags),
        "segments": values(contact.segments),
    }


def _cancel_campaign_work(db, member: models.CampaignContact, *, reason: str, actor: str) -> int:
    """Expire unsent work while preserving all historical rows."""
    now = datetime.now(timezone.utc)
    cancelled = 0
    approvals = db.query(models.Approval).filter_by(
        campaign_contact_id=member.id, status="pending"
    ).all()
    for approval in approvals:
        approval.status = "expired"
        approval.decided_by = actor
        approval.decided_at = now
        approval.rejection_reason = reason
        if approval.draft_id:
            draft = db.get(models.EmailDraft, approval.draft_id)
            if draft and draft.status in {"draft", "approved"}:
                draft.status = "cancelled"
        if approval.automation_run_id:
            run = db.get(models.AutomationRun, approval.automation_run_id)
            if run:
                # Import lazily to avoid the automation -> approvals -> contact
                # lifecycle import cycle during application startup.
                from . import automation as automation_svc
                automation_svc.invalidate_frozen_run(db, run, reason, actor=actor)
        cancelled += 1

    tasks = db.query(models.FollowUpTask).filter(
        models.FollowUpTask.campaign_contact_id == member.id,
        models.FollowUpTask.status.in_(("scheduled", "ready", "running", "paused", "failed")),
    ).all()
    for task in tasks:
        task.status = "cancelled"
        task.last_error = reason
        cancelled += 1
    return cancelled


def _active_members(db, contact_id: int) -> list[models.CampaignContact]:
    return db.query(models.CampaignContact).filter_by(
        contact_id=contact_id, membership_active=True
    ).all()


def _owned_campaign_member(db, owner_id: int, contact_id: int, campaign_id: int) -> models.CampaignContact:
    row = (
        db.query(models.CampaignContact)
        .join(models.Campaign, models.Campaign.id == models.CampaignContact.campaign_id)
        .filter(
            models.CampaignContact.contact_id == contact_id,
            models.CampaignContact.campaign_id == campaign_id,
            models.CampaignContact.membership_active.is_(True),
            models.Campaign.owner_id == owner_id,
        )
        .first()
    )
    if row is None:
        raise ContactTransitionError("source_campaign_membership_not_found", status_code=404)
    return row


def _choose_source_member(
    db, *, owner_id: int, contact: models.Contact, campaign_id: int | None,
    required: bool,
) -> models.CampaignContact | None:
    active = _active_members(db, contact.id)
    if campaign_id is not None:
        return _owned_campaign_member(db, owner_id, contact.id, campaign_id)
    if required and not active:
        raise ContactTransitionError("source_campaign_membership_required")
    if required and len(active) > 1:
        raise ContactTransitionError("source_campaign_required_multiple_memberships")
    return active[0] if len(active) == 1 else None


def _close_member(db, member: models.CampaignContact, *, status: str, reason: str, actor: str) -> int:
    now = datetime.now(timezone.utc)
    member.membership_active = False
    member.removed_at = now
    member.removed_reason = reason
    member.status = status
    cancelled = _cancel_campaign_work(db, member, reason=reason, actor=actor)
    return cancelled


def transition_contact(
    db,
    contact: models.Contact,
    *,
    owner_id: int,
    action: str,
    campaign_id: int | None = None,
    intent: str | None = None,
    reason: str | None = None,
    actor: str = "user",
    override_manual_lock: bool = False,
) -> dict:
    """Apply one lifecycle transition without committing the transaction.

    ``qualify`` closes only the Campaign that produced the qualifying reply;
    ``customer`` closes only an explicitly supplied Campaign; ``invalid``
    closes every active Campaign membership.  Historical Contact, mail, Draft,
    Approval, DeliveryAttempt and AuditLog rows are never deleted.
    """
    action = (action or "").strip().lower()
    if action not in {"qualify", "customer", "invalid"}:
        raise ContactTransitionError("invalid_contact_transition", status_code=422)
    if contact.owner_id != owner_id:
        raise ContactTransitionError("contact_not_owned", status_code=403)
    if contact.manual_lock and not override_manual_lock and (
        (action == "qualify" and contact.category != "qualified")
        or (action == "customer" and contact.category != "customer")
        or (action == "invalid" and contact.category != "invalid")
    ):
        raise ContactTransitionError("contact_manual_lock", status_code=409)

    before = _snapshot(contact)
    now = datetime.now(timezone.utc)
    cancelled = 0
    closed_campaign_ids: list[int] = []

    if action == "qualify":
        member = _choose_source_member(
            db, owner_id=owner_id, contact=contact, campaign_id=campaign_id, required=True,
        )
        contact.category = "qualified"
        if intent == "interested":
            contact.intent_level = "high"
        elif intent in {"asking_question", "objection"}:
            contact.intent_level = "medium"
        contact.status = "replied"
        contact.lifecycle_stage = "needs_reply"
        contact.next_action = "reply"
        if member is not None:
            cancelled = _close_member(
                db, member, status="converted", reason="qualified_conversion", actor=actor,
            )
            closed_campaign_ids.append(member.campaign_id)
    elif action == "customer":
        contact.category = "customer"
        member = _choose_source_member(
            db, owner_id=owner_id, contact=contact, campaign_id=campaign_id, required=False,
        )
        if member is not None:
            cancelled = _close_member(
                db, member, status="converted", reason="customer_conversion", actor=actor,
            )
            closed_campaign_ids.append(member.campaign_id)
    else:
        contact.category = "invalid"
        contact.status = "bounced" if contact.status == "bounced" else "not_interested"
        contact.intent_level = "low"
        contact.lifecycle_stage = "stopped"
        contact.next_action = "none"
        contact.next_follow_up_at = None
        for member in _active_members(db, contact.id):
            cancelled += _close_member(
                db, member, status="stopped", reason="invalid_transition", actor=actor,
            )
            closed_campaign_ids.append(member.campaign_id)

    after = _snapshot(contact)
    db.add(models.AuditLog(
        actor=actor,
        action=f"contact_transition_{action}",
        entity="contact",
        entity_id=str(contact.id),
        detail=json.dumps({
            "before": before,
            "after": after,
            "reason": reason or "",
            "intent": intent,
            "campaign_id": campaign_id,
            "closed_campaign_ids": closed_campaign_ids,
            "cancelled_pending_items": cancelled,
            "manual_lock_override": bool(override_manual_lock),
        }, ensure_ascii=False),
        success=True,
    ))
    return {
        "contact_id": contact.id,
        "action": action,
        "category": contact.category,
        "intent_level": contact.intent_level,
        "closed_campaign_ids": closed_campaign_ids,
        "cancelled_pending_items": cancelled,
        "manual_lock_override": bool(override_manual_lock),
    }


def cancel_campaign_member_work(db, member: models.CampaignContact, *, reason: str, actor: str) -> int:
    """Cancel unsent work for a member without changing membership history."""
    return _cancel_campaign_work(db, member, reason=reason, actor=actor)


def stop_contact_delivery(db, contact, *, owner_id: int, intent: str, actor: str) -> str:
    """Apply a confirmed stop signal even when human CRM classifications are locked.

    Callers retain the existing admission/opt-out confirmation gates. This is a
    local, transactional stop: no Gmail calls and no deletion of historical rows.
    """
    if contact.owner_id != owner_id:
        raise ContactTransitionError("contact_not_owned", status_code=403)
    if intent not in {"bounce", "not_interested", "unsubscribe", "opt_out"}:
        raise ContactTransitionError("invalid_stop_intent", status_code=422)
    before = _snapshot(contact)
    email = contact.email.strip().lower()
    suppression = db.query(models.Suppression).filter_by(owner_id=owner_id, email=email).first()
    if suppression is None:
        db.add(models.Suppression(owner_id=owner_id, email=email, reason=intent, source=actor))
    # Preserve human-owned CRM fields; outbound safety state is independent.
    if not contact.manual_lock:
        contact.category = "invalid"
        contact.intent_level = "low"
    contact.status = "bounced" if intent == "bounce" else (
        "unsubscribed" if intent in {"unsubscribe", "opt_out"} else "not_interested"
    )
    contact.lifecycle_stage = "stopped"
    contact.next_action = "none"
    contact.next_follow_up_at = None
    closed = []
    cancelled = 0
    for member in _active_members(db, contact.id):
        cancelled += _close_member(db, member, status="stopped", reason=f"reply_intent:{intent}", actor=actor)
        closed.append(member.campaign_id)
    after = _snapshot(contact)
    if before != after or closed or suppression is None:
        db.add(models.AuditLog(
            actor=actor, action="contact_delivery_stopped", entity="contact", entity_id=str(contact.id),
            detail=json.dumps({"intent": intent, "before": before, "after": after,
                               "closed_campaign_ids": closed, "cancelled_pending_items": cancelled,
                               "manual_lock_preserved": bool(contact.manual_lock)}, ensure_ascii=False),
            success=True,
        ))
    db.flush()
    return "bounced" if intent == "bounce" else "stopped"
