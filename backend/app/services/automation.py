"""Automation planning and execution.

This module is intentionally source-only: production must never depend on a
cached ``.pyc`` implementation. Both Global Inbox and Campaign runs use the
same Approval, policy, idempotency and dispatch services.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from .. import models
from ..config import is_internal_test_email
from ..agents.orchestrator import Orchestrator
from ..gmail.transport import GmailTimeoutError
from ..schemas import (
    AnalyzeMessageInput,
    AutomationPlan,
    GenerateOutreachInput,
    decision_to_proposal,
)
from ..security import json_safe
from . import approvals as approvals_svc
from . import flags as flag_svc
from . import followup as followup_svc
from . import sync as sync_svc
from .accounts import resolve_sending_account
from .thread_context import build_thread_context

logger = logging.getLogger("automation")

# Intents that auto-stop / suppress the contact (declined, bounced). These are
# applied immediately by the automation. Unsubscribe / opt-out are deliberately
# NOT in this set: they are routed to human review and only become effective
# after an explicit human approval (see inbox human-review resolution).
_AUTO_STOP_INTENTS = {"not_interested", "bounce"}
# Intents the Agent may *detect* but must never auto-apply. They are routed to
# the "needs human review" lane; only a human decision opts the contact out.
_HUMAN_GATED_INTENTS = {"unsubscribe", "opt_out"}


def parse_plan(plan_json: str) -> AutomationPlan:
    try:
        return AutomationPlan(**json.loads(plan_json))
    except Exception:
        return AutomationPlan()


def generate_plan(prompt: str) -> AutomationPlan:
    """Return conservative defaults.

    Natural-language plan generation is deliberately non-authoritative: the
    operator-visible structured form is the source of truth.
    """
    return AutomationPlan()


def create_automation(db, owner_id: int, prompt: str, campaign_id: int | None,
                      plan, name=None, scope: str = "campaign") -> models.Automation:
    if scope not in {"campaign", "global"}:
        raise ValueError("scope must be campaign or global")
    if scope == "campaign" and campaign_id is None:
        raise ValueError("campaign automation requires campaign_id")
    if scope == "global":
        campaign_id = None
    parsed = plan if isinstance(plan, AutomationPlan) else AutomationPlan(**plan)
    item = models.Automation(
        owner_id=owner_id,
        name=name or ("Global Inbox Automation" if scope == "global" else "Campaign Automation"),
        prompt=prompt,
        campaign_id=campaign_id,
        scope=scope,
        plan_json=json.dumps(parsed.model_dump()),
        status="disabled",
        tick_interval_minutes=parsed.tick_interval_minutes,
        execution_mode=parsed.execution_mode,
    )
    db.add(item)
    db.flush()
    return item


def list_for_owner(db, owner_id: int):
    return (
        db.query(models.Automation)
        .filter_by(owner_id=owner_id)
        .order_by(models.Automation.created_at.desc())
        .all()
    )


def set_status(db, automation, status: str):
    if status not in {"enabled", "disabled", "paused"}:
        raise ValueError("invalid automation status")
    automation.status = status
    automation.next_run_at = (
        datetime.now(timezone.utc) if status == "enabled" else None
    )
    db.flush()
    return automation


def _primary(output, campaign):
    if hasattr(output, "langgraph"):
        return getattr(output, campaign.primary_agent, None) or output.langgraph
    return output


def _reply_approval_for_message(db, thread_id: int, message):
    message_at = message.received_at or message.created_at
    if message_at is None:
        return None
    return (
        db.query(models.Approval)
        .filter(
            models.Approval.thread_id == thread_id,
            models.Approval.kind == "reply",
            models.Approval.created_at >= message_at,
        )
        .order_by(models.Approval.created_at.asc())
        .first()
    )


def _cutoff(plan: AutomationPlan):
    if not plan.takeover_cutoff_at:
        return None
    try:
        return datetime.fromisoformat(plan.takeover_cutoff_at)
    except ValueError:
        return None


def _at_or_after(value, cutoff) -> bool:
    if cutoff is None:
        return True
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return value >= cutoff


def validate_frozen_plan(db, run: models.AutomationRun) -> tuple[bool, str | None, list[models.Approval]]:
    """Validate the immutable semi-auto plan immediately before confirmation/send.

    A pending Approval elsewhere in the database is not a substitute for an
    item in the frozen plan. This prevents a replaced or invalidated Approval
    from being dispatched by an old awaiting-confirmation run.
    """
    try:
        plan = json.loads(run.execution_plan_json or "[]")
    except Exception:
        return False, "invalid_frozen_plan_json", []
    if not isinstance(plan, list) or not plan:
        return False, "frozen_plan_empty", []

    approvals: list[models.Approval] = []
    for item in plan:
        if not isinstance(item, dict) or not item.get("approval_id"):
            return False, "frozen_plan_item_invalid", []
        approval = db.get(models.Approval, item["approval_id"])
        if approval is None or approval.automation_run_id != run.id:
            return False, "frozen_plan_approval_not_owned_by_run", []
        if approval.status != "pending":
            return False, f"frozen_plan_approval_{approval.id}_{approval.status}", []
        draft = db.get(models.EmailDraft, approval.draft_id) if approval.draft_id else None
        if draft is None or draft.status != "draft":
            return False, f"frozen_plan_draft_{approval.id}_unavailable", []
        expected = {
            "to_email": approval.to_email,
            "thread_id": approval.thread_id,
            "subject": approval.subject,
            "body_text": approval.body_text,
            "draft_id": approval.draft_id,
        }
        if any(item.get(key) != value for key, value in expected.items()):
            return False, f"frozen_plan_approval_{approval.id}_changed", []
        approvals.append(approval)
    return True, None, approvals


def invalidate_frozen_run(db, run: models.AutomationRun, reason: str, *, actor: str) -> None:
    """Make an invalid semi-auto run explicitly non-confirmable and auditable."""
    if run.execution_mode != "semi_auto" or run.status not in {
        "queued", "running", "awaiting_confirmation", "confirmed"
    }:
        return
    run.status = "invalidated"
    run.error = reason
    run.finished_at = datetime.now(timezone.utc)
    db.add(models.AuditLog(
        actor=actor, action="semi_auto_run_invalidated",
        entity="automation_run", entity_id=str(run.id), detail=reason,
    ))


def dispatch_prepared_run(db, run: models.AutomationRun, *, actor: str) -> dict:
    valid, reason, approvals = validate_frozen_plan(db, run)
    if not valid:
        invalidate_frozen_run(db, run, reason or "frozen_plan_invalid", actor="system")
        db.flush()
        return {"sent": 0, "blocked": [{"reason": reason}], "timeline": []}
    sent, blocked = 0, []
    timeline = json.loads(run.timeline_json or "[]")
    for approval in approvals:
        campaign = db.get(models.Campaign, approval.campaign_id) if approval.campaign_id else None
        result = approvals_svc.decide_approval(
            db, approval.id, "approve", editor_email=actor,
            mode=campaign.agent_mode if campaign else "langgraph_only",
            agent=approval.agent or (campaign.primary_agent if campaign else "langgraph"),
            is_primary=True,
        )
        if result.get("ok"):
            sent += 1
            timeline.append({"email": approval.to_email, "action": "sent",
                             "approval_id": approval.id,
                             "gmail_message_id": result.get("message_id")})
        else:
            reason = result.get("blocked") or result.get("error")
            blocked.append({"email": approval.to_email, "approval_id": approval.id, "reason": reason})
            timeline.append({"email": approval.to_email, "action": "send_blocked",
                             "approval_id": approval.id, "reason": reason})
    run.timeline_json = json_safe(timeline)
    run.summary = f"sent={sent} blocked={len(blocked)} approvals={len(approvals)}"
    run.status = "partial" if blocked else "success"
    run.error = json_safe(blocked) if blocked else None
    db.flush()
    return {"sent": sent, "blocked": blocked, "timeline": timeline}


def _prepare_global(db, automation, run, plan, now):
    orch = Orchestrator(db)
    timeline, errors, stopped = [], [], 0
    cutoff = _cutoff(plan)
    threads = (
        db.query(models.EmailThread)
        .join(models.GmailAccount, models.GmailAccount.id == models.EmailThread.gmail_account_id)
        .filter(models.GmailAccount.user_id == automation.owner_id,
                models.EmailThread.has_human_reply.is_(True))
        .all()
    )
    # A Contact may have multiple historical threads. Prepare at most one reply
    # for the newest inbound conversation so a batch cannot duplicate-reply.
    newest_by_contact = {}
    for candidate in threads:
        latest = (db.query(models.EmailMessage)
                  .filter_by(thread_id=candidate.id, is_incoming=True)
                  .order_by(models.EmailMessage.received_at.desc(), models.EmailMessage.id.desc())
                  .first())
        key = (candidate.contact_email or "").lower()
        if latest and (key not in newest_by_contact or
                       (latest.received_at or latest.created_at) >
                       (newest_by_contact[key][1].received_at or newest_by_contact[key][1].created_at)):
            newest_by_contact[key] = (candidate, latest)
    for thread, message in newest_by_contact.values():
        contact = (
            db.query(models.Contact)
            .filter_by(owner_id=automation.owner_id, email=(thread.contact_email or "").lower())
            .first()
        )
        if contact is None or contact.next_action not in {
            "reply", "follow_up", "human_review"
        }:
            continue
        if message is None or not _at_or_after(message.received_at or message.created_at, cutoff):
            continue
        if _reply_approval_for_message(db, thread.id, message):
            timeline.append({"email": thread.contact_email, "action": "already_processed",
                             "thread_id": thread.id})
            continue
        campaign = db.get(models.Campaign, thread.campaign_id) if thread.campaign_id else None
        cc = (
            db.query(models.CampaignContact)
            .filter_by(campaign_id=campaign.id, contact_id=contact.id, membership_active=True).first()
            if campaign else None
        )
        try:
            output = orch.analyze(AnalyzeMessageInput(
                subject=message.subject or thread.subject or "",
                body_text=message.body_text or "",
                from_email=thread.contact_email,
                campaign_id=campaign.id if campaign else None,
                contact_id=contact.id,
                thread_id=thread.id,
                thread_context=build_thread_context(db, thread),
                mode=campaign.agent_mode if campaign else "langgraph_only",
            ))
            decision = _primary(output, campaign) if campaign else (
                output.langgraph if hasattr(output, "langgraph") else output
            )
            intent = decision.intent if decision else "unknown"
            thread.intent = intent
            thread.pending_action = decision.recommended_action if decision else None
            if thread.pending_action == "human_review" or contact.next_action == "human_review":
                db.add(models.AuditLog(
                    actor="agent", action="agent_resolved_human_review",
                    entity="email_thread", entity_id=str(thread.id),
                    detail=json_safe({"intent": intent, "recommended_action": thread.pending_action}),
                ))
            if intent in _HUMAN_GATED_INTENTS:
                # The unsubscribe / opt-out state is HUMAN-GATED. The Agent may
                # detect the intent, but it must never directly mark the contact
                # as unsubscribed. Route to human review; only an explicit human
                # approval (Inbox -> 需要人工处理 -> approve) applies the opt-out.
                if is_internal_test_email(thread.contact_email):
                    timeline.append({
                        "email": thread.contact_email,
                        "action": "skipped_internal_test",
                        "intent": intent,
                    })
                else:
                    thread.pending_action = "human_review"
                    db.add(models.AuditLog(
                        actor="agent", action="unsubscribe_routed_to_human_review",
                        entity="email_thread", entity_id=str(thread.id),
                        detail=json_safe({"intent": intent}), success=True,
                    ))
                    timeline.append({
                        "email": thread.contact_email,
                        "action": "routed_to_human_review",
                        "intent": intent,
                    })
            elif intent in set(plan.stop_on_intents) | _AUTO_STOP_INTENTS:
                if is_internal_test_email(thread.contact_email):
                    # Governance: never auto-stop the operator's own test address.
                    # A misclassification must not flip the operator's mailbox into
                    # "unsubscribed" / "stopped" or write a Suppression that blocks
                    # future campaigns to it.
                    timeline.append({
                        "email": thread.contact_email,
                        "action": "skipped_internal_test",
                        "intent": intent,
                    })
                else:
                    approvals_svc.apply_intent_actions(
                        db, cc, intent, thread.contact_email or "", automation.owner_id
                    )
                    stopped += 1
                    timeline.append({"email": thread.contact_email, "action": "stopped", "intent": intent})
            elif decision and decision.draft:
                approval = approvals_svc.create_reply_approval(
                    db, thread=thread, contact=contact, campaign=campaign, cc=cc,
                    subject=decision.draft.subject, body_text=decision.draft.body_text,
                    body_html=decision.draft.body_html or "",
                    recommended_action=decision.recommended_action,
                    risk_level=decision.risk_level,
                    agent=campaign.primary_agent if campaign else "langgraph",
                    mode=campaign.agent_mode if campaign else "langgraph_only",
                    automation_run_id=run.id, source_message=message,
                )
                timeline.append({"email": thread.contact_email, "action": "reply_prepared",
                                 "approval_id": approval.id, "intent": intent})
            else:
                timeline.append({"email": thread.contact_email, "action": "reviewed_no_action",
                                 "intent": intent})
            db.flush()
            db.commit()
        except approvals_svc.DraftCreationError as exc:
            if "gmail_timeout" in exc.reason:
                raise GmailTimeoutError(exc.reason) from exc
            errors.append(f"reply {thread.id}: {exc.reason}")
        except GmailTimeoutError:
            raise
        except Exception as exc:
            errors.append(f"analyze thread {thread.id}: {str(exc)[:200]}")
    return timeline, errors, stopped


def _prepare_campaign(db, automation, run, plan):
    campaign = db.get(models.Campaign, automation.campaign_id)
    if campaign is None:
        raise ValueError("campaign not found")
    orch = Orchestrator(db)
    timeline, errors, stopped = [], [], 0
    for thread in db.query(models.EmailThread).filter_by(
        campaign_id=campaign.id, has_human_reply=True
    ).all():
        message = (
            db.query(models.EmailMessage).filter_by(thread_id=thread.id, is_incoming=True)
            .order_by(models.EmailMessage.received_at.desc()).first()
        )
        contact = db.query(models.Contact).filter_by(email=thread.contact_email).first()
        cc = (
            db.query(models.CampaignContact)
            .filter_by(campaign_id=campaign.id, contact_id=contact.id, membership_active=True).first()
            if contact else None
        )
        if not message or not contact or not cc or _reply_approval_for_message(db, thread.id, message):
            continue
        try:
            decision = _primary(orch.analyze(AnalyzeMessageInput(
                subject=message.subject, body_text=message.body_text,
                from_email=thread.contact_email, campaign_id=campaign.id,
                contact_id=contact.id, thread_id=thread.id,
                thread_context=build_thread_context(db, thread),
                mode=campaign.agent_mode,
            )), campaign)
            if decision.intent in _HUMAN_GATED_INTENTS:
                # Human-gated: route to review, never auto-unsubscribe.
                if is_internal_test_email(contact.email):
                    timeline.append({
                        "email": contact.email,
                        "action": "skipped_internal_test",
                        "intent": decision.intent,
                    })
                else:
                    thread.pending_action = "human_review"
                    db.add(models.AuditLog(
                        actor="agent", action="unsubscribe_routed_to_human_review",
                        entity="email_thread", entity_id=str(thread.id),
                        detail=json_safe({"intent": decision.intent}), success=True,
                    ))
                    timeline.append({
                        "email": contact.email,
                        "action": "routed_to_human_review",
                        "intent": decision.intent,
                    })
            elif decision.intent in set(plan.stop_on_intents):
                if is_internal_test_email(contact.email):
                    timeline.append({
                        "email": contact.email,
                        "action": "skipped_internal_test",
                        "intent": decision.intent,
                    })
                else:
                    approvals_svc.apply_intent_actions(
                        db, cc, decision.intent, contact.email, automation.owner_id
                    )
                    stopped += 1
            elif decision.draft:
                approvals_svc.create_reply_approval(
                    db, thread=thread, contact=contact, campaign=campaign, cc=cc,
                    subject=decision.draft.subject, body_text=decision.draft.body_text,
                    body_html=decision.draft.body_html or "",
                    recommended_action=decision.recommended_action,
                    risk_level=decision.risk_level, agent=campaign.primary_agent,
                    mode=campaign.agent_mode, automation_run_id=run.id,
                    source_message=message,
                )
            db.commit()
        except GmailTimeoutError:
            raise
        except Exception as exc:
            errors.append(str(exc)[:200])
    for cc in db.query(models.CampaignContact).filter_by(
        campaign_id=campaign.id, status="queued", membership_active=True
    ).all():
        try:
            proposal = _primary(orch.generate_outreach(GenerateOutreachInput(
                campaign_id=campaign.id, contact_id=cc.contact_id, mode=campaign.agent_mode
            )), campaign)
            if hasattr(proposal, "draft"):
                proposal = decision_to_proposal(proposal)
            approval = approvals_svc.create_outreach_approval(
                db, cc, proposal, mode=campaign.agent_mode,
                agent=campaign.primary_agent, automation_run_id=run.id,
            )
            timeline.append({"email": approval.to_email, "action": "outreach_prepared"})
            db.commit()
        except approvals_svc.DraftCreationError as exc:
            if "gmail_timeout" in exc.reason:
                raise GmailTimeoutError(exc.reason) from exc
            errors.append(exc.reason)
        except GmailTimeoutError:
            raise
        except Exception as exc:
            errors.append(str(exc)[:200])
    outcomes = followup_svc.process_due_follow_ups(db, orch)
    timeline.extend({"action": "follow_up", **item} for item in outcomes)
    return timeline, errors, stopped, len(outcomes)


def run_tick(db, automation, trigger: str, source: str,
             run: models.AutomationRun | None = None) -> dict:
    if flag_svc.is_globally_paused(db):
        raise RuntimeError("globally paused")
    plan = parse_plan(automation.plan_json)
    now = datetime.now(timezone.utc)
    if run is None:
        run = models.AutomationRun(
            automation_id=automation.id, trigger=trigger, source=source,
            status="running", started_at=now,
            execution_mode=automation.execution_mode or plan.execution_mode,
        )
        db.add(run)
        db.flush()
    synced_threads = 0
    account, oauth = resolve_sending_account(
        db, owner_id=automation.owner_id, provision=False
    )
    if account is not None:
        sync_result = sync_svc.sync_inbox(
            db, account, oauth, max_results=50
        )
        synced_threads = int(sync_result.get("threads", 0))
        run.synced_threads = synced_threads
        db.commit()
    if (automation.scope or "campaign") == "global":
        timeline, errors, stopped = _prepare_global(db, automation, run, plan, now)
        followups = 0
    else:
        timeline, errors, stopped, followups = _prepare_campaign(
            db, automation, run, plan
        )
    approvals = db.query(models.Approval).filter_by(automation_run_id=run.id).all()
    run.approvals_created = len(approvals)
    run.drafts_created = sum(1 for approval in approvals if approval.draft_id)
    run.follow_ups_resolved = followups
    run.replies_stopped = stopped
    run.timeline_json = json_safe(timeline)
    run.execution_plan_json = json_safe([
        {"approval_id": approval.id, "draft_id": approval.draft_id,
         "to_email": approval.to_email, "thread_id": approval.thread_id,
         "subject": approval.subject, "body_text": approval.body_text,
         "kind": approval.kind}
        for approval in approvals
    ])
    run.prepared_at = now
    run.summary = f"approvals={len(approvals)} stopped={stopped}"
    if (run.execution_mode or automation.execution_mode) == "semi_auto" and approvals:
        run.status = "awaiting_confirmation"
        run.confirmation_expires_at = now + timedelta(hours=24)
    elif approvals:
        dispatch_prepared_run(db, run, actor="agent:full_auto")
        if errors:
            run.status = "partial"
            run.error = json_safe(errors)
    else:
        run.status = "partial" if errors else "success"
        run.error = json_safe(errors) if errors else None
    automation.last_run_at = now
    automation.last_status = run.status
    if automation.status == "enabled":
        automation.next_run_at = now + timedelta(minutes=automation.tick_interval_minutes)
    db.flush()
    return {
        "run_id": run.id, "approvals_created": run.approvals_created,
        "drafts_created": run.drafts_created, "replies_stopped": stopped,
        "synced_threads": synced_threads, "summary": run.summary, "timeline": timeline,
    }


def campaign_metrics(db, campaign_id: int) -> dict:
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    cc_ids = [row.id for row in db.query(models.CampaignContact).filter_by(
        campaign_id=campaign_id
    ).all()]
    sent_today = (
        db.query(models.EmailDraft)
        .filter(models.EmailDraft.campaign_contact_id.in_(cc_ids),
                models.EmailDraft.status == "sent",
                models.EmailDraft.updated_at >= today).count()
        if cc_ids else 0
    )
    return {
        "sent_today": sent_today,
        "pending_approvals": db.query(models.Approval).filter_by(
            campaign_id=campaign_id, status="pending"
        ).count(),
        "waiting_reply": (
            db.query(models.Contact)
            .join(models.CampaignContact, models.CampaignContact.contact_id == models.Contact.id)
            .filter(models.CampaignContact.campaign_id == campaign_id,
                    models.CampaignContact.membership_active.is_(True),
                    models.Contact.status == "replied").count()
        ),
        "stopped": db.query(models.CampaignContact).filter_by(
            campaign_id=campaign_id, status="stopped"
        ).count(),
    }
