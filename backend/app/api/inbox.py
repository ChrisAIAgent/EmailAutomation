"""Inbox endpoints: threads, detail, Agent bulk sort, and AI reply generation.

The Inbox is a "smart inbox": the Agent classifies every synced thread into a
business category (valid customer / needs reply / has interest / not now /
rejected / irrelevant). For threads that need a reply, the Agent can draft a
reply (real model call); the user confirms and the reply is sent through the same
real-Gmail, policy-gated send path as outreach.
"""
from __future__ import annotations

import logging
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..config import is_internal_test_email
from ..agents.orchestrator import Orchestrator
from ..exceptions import AgentUnavailableError
from ..schemas import AnalyzeMessageInput, GenerateFollowUpInput
from ..services.inbox_triage import (
    INTENT_CATEGORY,
    _thread_category,
    assess_inbound,
    clear_stale_human_review,
    contact_needs_reply,
    intent_tags,
    recompute_contact_reply_state,
    requires_human_review,
    sales_reply_required,
)
from ..services.thread_context import build_thread_context
from ..events import publish
from .deps import get_db, ensure_owner

logger = logging.getLogger("api.inbox")
router = APIRouter(prefix="/api/inbox", tags=["inbox"])


# --- Agent intent -> business category mapping ---
# INTENT_CATEGORY lives in app.services.inbox_triage (shared with the sync layer)
# so the Inbox API and Gmail sync derive a Contact's reply state from one source.

CATEGORY_LABELS = {
    "valid_customer": "valid_customer",
    "needs_reply": "needs_reply",
    "has_interest": "has_interest",
    "not_now": "not_now",
    "rejected": "rejected",
    "irrelevant": "irrelevant",
    "awaiting_reply": "awaiting_reply",
    "filtered": "filtered",
    "unsorted": "unsorted",
    "human_review": "human_review",
}

AUTO_CONTACT_INTENTS = {
    "interested",
    "asking_question",
    "objection",
    "not_interested",
    "unsubscribe",
    "opt_out",
    "bounce",
}


def _fallback_contact_name(email: str) -> str:
    local = email.split("@", 1)[0]
    value = re.sub(r"[._+-]+", " ", local).strip()
    return value.title() or email


def _auto_contact_from_inbox(
    db: Session,
    thread: models.EmailThread,
    intent: str,
    latest: models.EmailMessage,
    *,
    human_verified: bool = False,
    initial_tags: list[str] | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    company: str | None = None,
) -> models.Contact | None:
    """Idempotently promote eligible inbound senders into the CRM."""
    if not latest.is_incoming or (not human_verified and intent not in AUTO_CONTACT_INTENTS):
        return None
    tags = set(initial_tags or [])
    # Review-lane identities are Contact candidates, not CRM Contacts. Only a
    # confirmed customer may enter Contacts; job applications, forwarded
    # identities and test content remain at the admission gate.
    if requires_human_review(list(tags)):
        return None
    # An unknown sender saying "unsubscribe" is not automatically a customer
    # record.  Suppression is handled only for an existing Contact or Campaign
    # recipient below, so this also avoids creating a CRM identity just to stop
    # a message that may be unrelated to this Workspace.
    if intent in ("not_interested", "unsubscribe", "opt_out", "bounce"):
        return None
    email = (thread.contact_email or latest.from_email or "").strip().lower()
    if not email or "@" not in email:
        return None
    account = db.get(models.GmailAccount, thread.gmail_account_id)
    if account and email == account.email.lower():
        return None
    owner_id = account.user_id if account else ensure_owner(db)
    existing = db.query(models.Contact).filter_by(owner_id=owner_id, email=email).first()
    if existing:
        return existing

    rejected = intent in ("not_interested", "unsubscribe", "opt_out", "bounce")
    status = {
        "not_interested": "not_interested",
        "unsubscribe": "unsubscribed",
        "opt_out": "unsubscribed",
        "bounce": "bounced",
    }.get(intent, "replied")
    contact = models.Contact(
        owner_id=owner_id,
        email=email,
        first_name=first_name,
        last_name=last_name,
        company=company,
        # Direct legacy callers provide a validated sales intent. Inbox triage
        # passes human_verified=True and keeps every new human as a prospect
        # until the separate sales-reply decision promotes them.
        category="qualified" if not human_verified and not rejected else "prospect",
        tags=json.dumps(sorted(set(
            ["inbox", "human"] + (initial_tags or []) + intent_tags(intent)
        )), ensure_ascii=False),
        intent_level="low" if rejected else "unknown",
        source="inbox_auto",
        status=status,
        lifecycle_stage="stopped" if rejected else "new_customer",
        next_action="none" if rejected else "review",
    )
    db.add(contact)
    db.flush()
    db.add(models.AuditLog(
        actor="langgraph",
        action="contact_created_from_human_email",
        entity="contact",
        entity_id=str(contact.id),
        detail=json.dumps({
            "thread_id": thread.id,
            "email": email,
            "intent": intent,
            "tags": _tags_json(contact.tags),
            "first_name": contact.first_name,
            "last_name": contact.last_name,
            "company": contact.company,
        }, ensure_ascii=False),
        success=True,
    ))
    return contact


_DYNAMIC_INTENT_TAGS = {
    "interested", "asking_question", "objection", "not_interested",
    "unsubscribe", "opt_out", "bounce", "out_of_office",
}

# Stable identity labels every email-sourced Contact carries.  Always present.
_STABLE_AGENT_TAGS = {"inbox", "human"}

# Topic/content labels derived from message text.  These ACCUMULATE across a
# Contact's emails: a topic raised in an early message stays relevant later, so
# re-triage must preserve previously derived content tags instead of wiping them.
# Only the current message's newly detected content tags are merged in.
_ACCUMULATING_CONTENT_TAGS = {
    "job_application", "pricing", "demo_request", "partnership",
    "support_request", "complaint", "forwarded", "scripted_content",
}

# Full Agent-owned vocabulary.  Any tag outside this set is user-owned and is
# preserved verbatim on every re-triage.  Within the vocabulary: stable identity
# tags are always present, content tags accumulate, and intent tags reflect the
# CURRENT message only (replaced, never accumulated) so a Contact is never left
# carrying contradictory historical intents.
_AGENT_MANAGED_TAGS = (
    _DYNAMIC_INTENT_TAGS | _STABLE_AGENT_TAGS | _ACCUMULATING_CONTENT_TAGS
)


def _update_contact_from_human(
    db: Session,
    contact: models.Contact,
    *,
    thread_id: int,
    intent: str,
    content_tags: list[str],
    first_name: str | None = None,
    last_name: str | None = None,
    company: str | None = None,
) -> None:
    """Update dynamic CRM state and retain an auditable before/after record."""
    if contact.manual_lock and intent not in {
        "not_interested", "unsubscribe", "opt_out", "bounce"
    }:
        return
    before = {
        "tags": _tags_json(contact.tags),
        "intent_level": contact.intent_level,
        "status": contact.status,
        "lifecycle_stage": contact.lifecycle_stage,
        "next_action": contact.next_action,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "company": contact.company,
    }
    # Tag reconciliation.  Split the existing tag set by ownership so a re-triage
    # is idempotent and does not churn the Contact's labels:
    #   * user-owned tags (anything outside the Agent vocabulary) are preserved;
    #   * stable identity tags (inbox/human) are always present;
    #   * content/topic tags accumulate -- previously derived ones are kept and
    #     the current message's content tags are merged in (never wiped);
    #   * intent tags reflect the CURRENT message only (replaced, not
    #     accumulated), so a Contact is never left with contradictory intents.
    old_tags = set(before["tags"])
    user_tags = old_tags - _AGENT_MANAGED_TAGS
    prev_content = old_tags & _ACCUMULATING_CONTENT_TAGS
    current_content = set(content_tags)
    current_intent = set(intent_tags(intent))
    tags = (
        user_tags
        | _STABLE_AGENT_TAGS
        | (prev_content | current_content)
        | current_intent
    )
    contact.tags = json.dumps(sorted(tags), ensure_ascii=False)

    # Do not overwrite fields that a user has already curated in the CRM.
    if not contact.first_name and first_name:
        contact.first_name = first_name.strip()
    if not contact.last_name and last_name:
        contact.last_name = last_name.strip()
    if not contact.company and company:
        contact.company = company.strip()

    # B: a contact that has already opted out / declined / bounced is terminal.
    # Even if this thread's new inbound message looks like a fresh question, we
    # must NOT auto-flip it back to needs_reply -- the original opt-out/decline
    # stands until a human explicitly revives the contact.
    # C: the operator's own test/seed addresses (real-send allowlist) must never
    # be auto-statused as if they were real customers -- a misclassification must
    # not flip the operator's own mailbox into "unsubscribed"/"stopped".
    if contact.status in {"unsubscribed", "not_interested", "bounced"} or is_internal_test_email(contact.email):
        # Only the non-status metadata above is refreshed; leave status /
        # lifecycle_stage / next_action untouched and skip the recompute so a
        # re-analysis can never reopen (or wrongly close) the contact.
        db.add(contact)
        return

    # Direction-aware reply state.  If the latest message on this thread is OURS
    # (outbound), the customer is merely following up (ack / thanks / confirmation)
    # and does NOT require a fresh reply.  Keeping that distinction stops the
    # contact from being flipped back to needs_reply on every inbound re-analysis,
    # so a multi-message conversation produces exactly one "awaiting reply" state.
    requires_reply = (
        sales_reply_required(intent, content_tags)
        or intent in {"interested", "asking_question", "objection"}
    )
    thread = db.get(models.EmailThread, thread_id) if thread_id else None
    latest_msg = max(thread.messages, key=_message_time, default=None) if thread else None
    we_awaiting_customer = latest_msg is not None and not latest_msg.is_incoming

    if intent in ("unsubscribe", "opt_out"):
        # The unsubscribe / opt-out state is HUMAN-GATED: the Agent may detect
        # the intent, but it must never directly mark the contact as
        # unsubscribed. Route the thread to human review; only an explicit human
        # approval in the Inbox applies the opt-out. The contact's status is left
        # untouched so it is never silently opted out by the Agent.
        if thread_id:
            t = db.get(models.EmailThread, thread_id)
            if t is not None:
                t.pending_action = "human_review"
                db.add(t)
        db.add(contact)
        return
    elif intent in ("not_interested", "bounce"):
        contact.intent_level = "low"
        contact.status = {"not_interested": "not_interested", "bounce": "bounced"}[intent]
        contact.lifecycle_stage = "stopped"
        contact.next_action = "none"
    elif requires_human_review(content_tags):
        # A verified person can be a valid CRM identity without being a sales
        # lead. Keep that distinction visible and stable for users and Agents.
        contact.category = "prospect"
        contact.intent_level = "unknown"
        contact.status = "replied"
        contact.lifecycle_stage = "new_customer"
        contact.next_action = "human_review"
    elif sales_reply_required(intent, content_tags):
        contact.category = "qualified"
        contact.intent_level = "high" if intent == "interested" else "medium"
        contact.status = "replied"
        contact.lifecycle_stage = "needs_reply"
        contact.next_action = "reply"
    elif intent == "interested":
        contact.category = "qualified"
        contact.intent_level = "high"
        contact.status = "replied"
        contact.lifecycle_stage = "needs_reply"
        contact.next_action = "reply"
    elif intent in ("asking_question", "objection"):
        contact.intent_level = "medium"
        contact.status = "replied"
        contact.lifecycle_stage = "needs_reply"
        contact.next_action = "reply"
    elif we_awaiting_customer and not requires_reply:
        # We already replied (latest message is outbound). The new inbound is a
        # follow-up, not a fresh request needing a reply. Keep the contact in the
        # "we are awaiting the customer" state instead of flipping it back to
        # needs_reply.  A genuinely new question after our reply still hits the
        # branches above (requires_reply True) and re-opens needs_reply correctly.
        contact.lifecycle_stage = "awaiting_reply"
        contact.next_action = "waiting_for_customer"
    else:
        contact.intent_level = contact.intent_level or "unknown"
        if contact.lifecycle_stage not in ("needs_reply", "stopped"):
            contact.lifecycle_stage = "new_customer"
            contact.next_action = "review"

    # Single source of truth: re-derive the contact's reply state from the LIVE
    # direction of ALL its threads (not just the one analyzed here).  This keeps
    # the cached lifecycle_stage/next_action consistent with _thread_category, so
    # a thread that received our outbound reply is flipped to awaiting_reply
    # instead of staying stuck in needs_reply, and Dashboard / contact-list
    # queries report the real number of customers awaiting our reply.
    recompute_contact_reply_state(db, contact)

    after = {
        "tags": _tags_json(contact.tags),
        "intent_level": contact.intent_level,
        "status": contact.status,
        "lifecycle_stage": contact.lifecycle_stage,
        "next_action": contact.next_action,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "company": contact.company,
    }
    if before != after:
        db.add(models.AuditLog(
            actor="langgraph",
            action="contact_state_updated_from_conversation",
            entity="contact",
            entity_id=str(contact.id),
            detail=json.dumps({
                "thread_id": thread_id,
                "intent": intent,
                "before": before,
                "after": after,
            }, ensure_ascii=False),
            success=True,
        ))


def _category(intent: str | None) -> str:
    if not intent:
        return "unsorted"
    return INTENT_CATEGORY.get(intent, "irrelevant")


def _thread_pending_action(t: models.EmailThread) -> str | None:
    """Return an action that is consistent with the thread's current direction."""
    if _thread_category(t) == "awaiting_reply" and t.pending_action == "reply":
        return "no_action"
    return t.pending_action


def _message_time(message: models.EmailMessage):
    return message.received_at or message.created_at


def _latest_message_at(t: models.EmailThread):
    latest = max(t.messages, key=_message_time, default=None)
    return _message_time(latest) if latest is not None else t.updated_at


def _campaign_contact_status(db: Session, t: models.EmailThread) -> str | None:
    """Return the contact status in this thread's campaign, if it is known."""
    if not t.campaign_id or not t.contact_email:
        return None
    return (
        db.query(models.CampaignContact.status)
        .join(models.Contact, models.Contact.id == models.CampaignContact.contact_id)
        .filter(
            models.CampaignContact.campaign_id == t.campaign_id,
            models.Contact.email == t.contact_email,
        )
        .scalar()
    )


def _thread_summary(db: Session, t: models.EmailThread) -> dict:
    owner_id = ensure_owner(db)
    email = (t.contact_email or "").strip().lower()
    contact = (
        db.query(models.Contact).filter_by(owner_id=owner_id, email=email).first()
        if email else None
    )
    return {
        "id": t.id,
        "campaign_id": t.campaign_id,
        "subject": t.subject,
        "contact_email": t.contact_email,
        "intent": t.intent,
        "category": _thread_category(t),
        "category_label": CATEGORY_LABELS.get(_thread_category(t), "unsorted"),
        "pending_action": _thread_pending_action(t),
        "review_kind": _review_kind(t, contact),
        "snippet": t.snippet,
        "updated_at": t.updated_at,
        "latest_message_at": _latest_message_at(t),
        "has_human_reply": bool(t.has_human_reply and t.campaign_id),
        "outreach_status": _campaign_contact_status(db, t),
        "message_count": len(t.messages),
        "processed": bool(t.intent),
        "contact_id": contact.id if contact else None,
        "is_contact": contact is not None,
    }


def _contact_identity(contact: models.Contact | None, email: str) -> dict:
    return {
        "contact_id": contact.id if contact else None,
        "first_name": contact.first_name if contact else None,
        "last_name": contact.last_name if contact else None,
        "company": contact.company if contact else None,
        "email": email,
        "category": contact.category if contact else None,
        "tags": _tags_json(contact.tags) if contact else [],
        "intent_level": contact.intent_level if contact else "unknown",
        "lifecycle_stage": (contact.lifecycle_stage or "new_customer") if contact else "unclassified",
        "next_action": contact.next_action if contact else "review",
        "next_follow_up_at": contact.next_follow_up_at if contact else None,
        "manual_lock": bool(contact.manual_lock) if contact else False,
    }


def _tags_json(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _review_kind(t: models.EmailThread, contact: models.Contact | None) -> str | None:
    if _thread_pending_action(t) != "human_review":
        return None
    if t.intent in {"unsubscribe", "opt_out"}:
        return "opt_out_confirmation"
    # A known Contact does not repeat Contact admission, but special content
    # still needs a message-level human decision.
    if t.intent == "triage_review":
        return "content_uncertain"
    latest = max(t.messages, key=_message_time, default=None)
    if contact is not None or (latest is not None and not latest.is_incoming):
        return "stale_review"
    return "contact_admission_uncertain"


def _review_guidance(t: models.EmailThread, contact: models.Contact | None) -> dict | None:
    """Explain the Contact-admission decision for a review-lane sender."""
    if _thread_pending_action(t) != "human_review":
        return None
    kind = _review_kind(t, contact)
    # Unsubscribe / opt-out review: human must decide whether to honor the opt-out.
    # The Agent may have detected the intent, but it must never auto-apply it.
    if kind == "opt_out_confirmation":
        return {
            "kind": kind,
            "title": "客户请求退订 / 停止联系",
            "reason": "Agent 在邮件中检测到退订意图（unsubscribe / opt-out）。按规则，退订只能由人工确认后生效，Agent 不会自动退订。",
            "recommendation": "确认对方要退订请点「批准」，将其标记为 unsubscribed 并写入发送抑制名单；如误判或对方仍想继续，点「拒绝」保留联系人。",
        }
    if kind == "stale_review":
        return {
            "kind": kind,
            "title": "Stale review",
            "reason": "This sender is already a Contact or the latest message was sent by us.",
            "recommendation": "Clear this obsolete review. It will not create a reply, Draft, Approval, or Gmail action.",
        }
    if kind == "content_uncertain":
        return {
            "kind": kind,
            "title": "内容需要人工确认",
            "reason": t.last_agent_summary or "邮件内容无法安全判断为客户业务或非客户内容。",
            "recommendation": (
                "先确认这是否属于当前客户关系。未知发件人可选择人工录入或拒绝；"
                "已有联系人可标记为无需动作。不会自动生成回复或发送邮件。"
            ),
        }
    tags = set(_tags_json(contact.tags)) if contact else set()
    latest_text = "\n".join(filter(None, [t.subject or ""] + [m.body_text or m.snippet or "" for m in t.messages[-2:]])).lower()
    title = "是否将此发件人创建为联系人？"
    if "job_application" in tags or any(word in latest_text for word in ("job application", "resume", "求职", "应聘")):
        return {"kind": "job_application", "title": title, "reason": "Agent 识别为求职/招聘来信，不属于当前销售客户链路。", "recommendation": "建议拒绝进入联系人；如确需保留，可由你人工录入。"}
    if "scripted_content" in tags or "test" in latest_text or "测试" in latest_text:
        return {"kind": "scripted_content", "title": title, "reason": "识别为自动化/脚本生成内容（非真实业务客户），不能自动进入客户运营。", "recommendation": "建议拒绝进入联系人。"}
    if "forwarded" in tags or any(word in latest_text for word in ("fwd:", "fw:", "forwarded message", "转发")):
        return {"kind": "forwarded", "title": title, "reason": "这是转发邮件，不能从被转发内容推断当前发件人的客户身份。", "recommendation": "如你认识当前发件人，请人工录入；否则建议拒绝。"}
    return {"kind": "ambiguous", "title": title, "reason": t.last_agent_summary or "Agent 无法安全确认该发件人是否为业务客户。", "recommendation": "身份明确时可交给 Agent 建联；否则请人工录入或拒绝。"}


@router.get("/customers")
def list_inbox_customers(db: Session = Depends(get_db)):
    """Contact-centric Inbox: one row per sender, with all Gmail threads grouped."""
    threads = (
        db.query(models.EmailThread)
        .filter(models.EmailThread.contact_email.isnot(None))
        .all()
    )
    grouped: dict[str, list[models.EmailThread]] = {}
    for thread in threads:
        email = (thread.contact_email or "").strip().lower()
        if email:
            grouped.setdefault(email, []).append(thread)

    owner_id = ensure_owner(db)
    result = []
    for email, rows in grouped.items():
        rows.sort(key=_latest_message_at, reverse=True)
        contact = db.query(models.Contact).filter_by(owner_id=owner_id, email=email).first()
        latest = rows[0]
        categories = [_thread_category(row) for row in rows]
        campaign_ids = {row.campaign_id for row in rows if row.campaign_id}
        campaigns = (
            db.query(models.Campaign).filter(models.Campaign.id.in_(campaign_ids)).all()
            if campaign_ids else []
        )
        identity = _contact_identity(contact, email)
        category = _thread_category(latest)
        if not contact and category == "filtered":
            identity["lifecycle_stage"] = "filtered"
            identity["next_action"] = "none"
        elif not contact and category == "awaiting_reply":
            identity["lifecycle_stage"] = "awaiting_reply"
            identity["next_action"] = "none"
        needs_reply = bool(contact and contact.lifecycle_stage == "needs_reply" and contact.next_action == "reply")
        stopped = bool(contact and (
            contact.lifecycle_stage == "stopped"
            or contact.status in ("not_interested", "unsubscribed", "bounced", "archived")
        ))
        if stopped:
            priority = 10
        elif needs_reply:
            priority = 100
        elif contact and contact.lifecycle_stage == "new_customer":
            priority = 80
        elif contact and contact.next_action == "follow_up":
            priority = 70
        elif category == "unsorted":
            priority = 60
        else:
            priority = 30
        result.append({
            **identity,
            "latest_category": category,
            "categories": sorted(set(categories)),
            "latest_subject": latest.subject,
            "latest_snippet": latest.snippet,
            "last_activity_at": _latest_message_at(latest),
            "thread_count": len(rows),
            "message_count": sum(len(row.messages) for row in rows),
            "needs_reply": needs_reply,
            "priority": priority,
            "campaigns": [{"id": c.id, "name": c.name} for c in campaigns],
            "threads": [_thread_summary(db, row) for row in rows],
        })
    return sorted(result, key=lambda row: (row["priority"], row["last_activity_at"]), reverse=True)


@router.get("/threads")
def list_threads(
    db: Session = Depends(get_db),
    limit: int = 100,
    category: str | None = None,
):
    threads = db.query(models.EmailThread).all()
    threads.sort(key=_latest_message_at, reverse=True)
    if category:
        threads = [thread for thread in threads if _thread_category(thread) == category]
    threads = threads[:limit]
    return [_thread_summary(db, t) for t in threads]


@router.get("/stats")
def inbox_stats(db: Session = Depends(get_db)):
    """Counts per category + unprocessed total, for the smart-inbox sidebar."""
    threads = db.query(models.EmailThread).all()
    counts: dict[str, int] = {}
    unprocessed = 0
    for t in threads:
        cat = _thread_category(t)
        counts[cat] = counts.get(cat, 0) + 1
        if not t.intent:
            unprocessed += 1
    return {"counts": counts, "unprocessed": unprocessed, "total": len(threads)}


@router.get("/threads/{thread_id}")
def thread_detail(thread_id: int, db: Session = Depends(get_db)):
    t = db.get(models.EmailThread, thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="thread not found")
    # Latest AgentRun for this thread -> reasoning + model (Agent judgment proof).
    run = (
        db.query(models.AgentRun)
        .filter_by(thread_id=t.id)
        .order_by(models.AgentRun.created_at.desc())
        .first()
    )
    owner_id = ensure_owner(db)
    email = (t.contact_email or "").strip().lower()
    contact = (
        db.query(models.Contact).filter_by(owner_id=owner_id, email=email).first()
        if email else None
    )
    return {
        "id": t.id,
        "gmail_thread_id": t.gmail_thread_id,
        "subject": t.subject,
        "contact_email": t.contact_email,
        "campaign_id": t.campaign_id,
        "intent": t.intent,
        "category": _thread_category(t),
        "category_label": CATEGORY_LABELS.get(_thread_category(t), "unsorted"),
        "pending_action": _thread_pending_action(t),
        "review_kind": _review_kind(t, contact),
        "agent_summary": t.last_agent_summary,
        "agent_reasoning": run.reasoning_summary if run else None,
        "agent_model": run.model if run else None,
        "agent_action": run.recommended_action if run else None,
        "review_guidance": _review_guidance(t, contact),
        "has_human_reply": bool(t.has_human_reply and t.campaign_id),
        "outreach_status": _campaign_contact_status(db, t),
        "contact_id": contact.id if contact else None,
        "is_contact": contact is not None,
        "messages": [
            {
                "id": m.id, "from_email": m.from_email, "to_email": m.to_email,
                "subject": m.subject, "snippet": m.snippet, "body_text": m.body_text,
                "is_incoming": m.is_incoming, "received_at": m.received_at,
                "attachments": _atts(m.attachments_meta),
            }
            for m in sorted(t.messages, key=_message_time)
        ],
    }


class HumanReviewDecisionBody(BaseModel):
    decision: str  # approve | reject | agent_decide
    reason: str | None = None


@router.post("/threads/{thread_id}/human-review")
def resolve_human_review(thread_id: int, payload: HumanReviewDecisionBody, db: Session = Depends(get_db)):
    """Resolve Contact admission only; never draft, approve, or send."""
    if payload.decision not in {"approve", "reject", "agent_decide"}:
        raise HTTPException(status_code=422, detail="invalid_human_review_decision")
    t = db.get(models.EmailThread, thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="thread not found")
    if _thread_pending_action(t) != "human_review":
        raise HTTPException(status_code=409, detail="thread_not_waiting_for_human_review")

    email = (t.contact_email or "").strip().lower()

    # --- Unsubscribe / opt-out review (HUMAN-GATED layer) ---
    # The Agent may *detect* an unsubscribe / opt-out intent, but it must never
    # directly mark the contact as unsubscribed. This endpoint is the ONLY
    # approved entry point: an explicit human approval here applies the opt-out
    # (writes the Suppression + flips the contact status). Reject / agent_decide
    # keep the contact and never opt it out.
    if t.intent in {"unsubscribe", "opt_out"}:
        if not email:
            raise HTTPException(status_code=422, detail="thread_contact_email_not_found")
        owner_id = ensure_owner(db)
        from ..services import approvals as approvals_svc
        contact = db.query(models.Contact).filter_by(owner_id=owner_id, email=email).first()
        if payload.decision == "approve":
            cc = db.query(models.CampaignContact).filter_by(contact_id=contact.id).first() if contact else None
            approvals_svc.apply_intent_actions(db, cc, t.intent, email, owner_id)
            t.pending_action = "no_action"
            t.last_agent_summary = "退订已人工批准，联系人已 opt-out。"
            db.add(models.AuditLog(
                actor="user", action="unsubscribe_applied_after_review", entity="email_thread",
                entity_id=str(t.id), detail=json.dumps({"intent": t.intent, "result": "unsubscribe_applied"}, ensure_ascii=False), success=True,
            ))
            db.commit()
            return {**_thread_summary(db, t), "resolved": True, "result": "unsubscribe_applied",
                    "message": "已退订：联系人标记为 unsubscribed，并写入发送抑制名单。"}
        # reject / agent_decide -> keep the contact, do NOT opt out.
        t.pending_action = "no_action"
        t.last_agent_summary = "退订被人工拒绝，联系人保留，未写入抑制名单。"
        db.add(models.AuditLog(
            actor="user", action="unsubscribe_rejected_after_review", entity="email_thread",
            entity_id=str(t.id), detail=json.dumps({"intent": t.intent, "result": "unsubscribe_rejected"}, ensure_ascii=False), success=True,
        ))
        db.commit()
        return {**_thread_summary(db, t), "resolved": True, "result": "unsubscribe_rejected",
                "message": "已拒绝退订：联系人保持原状态。"}

    owner_id = ensure_owner(db)
    contact = (
        db.query(models.Contact).filter_by(owner_id=owner_id, email=email).first()
        if email else None
    )
    guidance = _review_guidance(t, contact) or {}
    if guidance.get("kind") == "content_uncertain" and contact is not None:
        if payload.decision != "approve":
            raise HTTPException(status_code=409, detail="content_review_requires_manual_no_action_confirmation")
        t.pending_action = "no_action"
        t.last_agent_summary = "Operator confirmed no action for uncertain non-sales content."
        db.add(models.AuditLog(
            actor="user", action="inbox_content_review_resolved", entity="email_thread",
            entity_id=str(t.id),
            detail=json.dumps({"result": "no_action", "contact_id": contact.id}, ensure_ascii=False),
            success=True,
        ))
        db.commit()
        return {
            **_thread_summary(db, t), "resolved": True,
            "result": "content_review_no_action",
            "message": "Content review recorded. The Contact was kept; no reply, Draft, Approval, or Gmail operation was created.",
        }
    if contact is not None:
        raise HTTPException(status_code=409, detail="contact_admission_already_completed")
    if payload.decision == "approve":
        return {**_thread_summary(db, t), "resolved": False, "result": "manual_contact_entry_required"}

    if payload.decision == "reject":
        rejected = db.query(models.NonCustomerFilter).filter_by(owner_id=owner_id, email=email).first()
        if not rejected:
            db.add(models.NonCustomerFilter(
                owner_id=owner_id, email=email,
                reason=(payload.reason or guidance.get("kind") or "operator_rejected")[:200],
                source="inbox_review", created_by="user",
            ))
        for row in db.query(models.EmailThread).filter_by(contact_email=email).all():
            row.intent = "filtered_non_customer"
            row.pending_action = "no_action"
            row.has_human_reply = False
        result = "non_customer_filtered"
    else:
        latest = max((m for m in t.messages if m.is_incoming), key=lambda m: m.received_at or m.created_at, default=None)
        if latest is None:
            raise HTTPException(status_code=409, detail="agent_decide_requires_inbound_message")
        latest_email = (latest.from_email or "").strip().lower()
        if not latest_email or latest_email != email:
            raise HTTPException(status_code=409, detail="admission_sender_mismatch")
        triage = assess_inbound(
            is_incoming=True, from_email=latest.from_email,
            subject=latest.subject, body=latest.body_text,
            known_relationship=False, db=db,
        )
        blocked_tags = {"forwarded", "scripted_content", "job_application"}
        identity_clear = bool(triage.first_name or triage.company)
        business_relevant = t.intent in {"interested", "asking_question", "objection"}
        if triage.is_human is not True or blocked_tags.intersection(triage.tags) or not identity_clear or not business_relevant:
            t.last_agent_summary = "Agent cannot create this Contact yet: explicit sender name or company and a direct business relationship are required."
            db.add(models.AuditLog(
                actor="agent", action="contact_admission_deferred", entity="email_thread",
                entity_id=str(t.id), detail=json.dumps({"decision": "agent_decide", "identity_clear": identity_clear, "business_relevant": business_relevant, "tags": triage.tags, "send_or_draft_created": False}, ensure_ascii=False), success=True,
            ))
            db.commit()
            return {**_thread_summary(db, t), "resolved": False, "result": "insufficient_contact_evidence", "message": t.last_agent_summary}
        contact = db.query(models.Contact).filter_by(owner_id=owner_id, email=email).first()
        if not contact:
            contact = models.Contact(
                owner_id=owner_id, email=email,
                first_name=triage.first_name, last_name=triage.last_name,
                company=triage.company, category="qualified",
                tags=json.dumps(sorted(set(triage.tags + intent_tags(t.intent))), ensure_ascii=False),
                intent_level="unknown", lifecycle_stage="new_customer",
                next_action="review", source="inbox_agent_decide", status="new",
            )
            db.add(contact)
        t.pending_action = "no_action"
        result = "contact_created_by_agent"
    db.add(models.AuditLog(actor="user" if payload.decision == "reject" else "agent", action="contact_admission_resolved", entity="email_thread", entity_id=str(t.id), detail=json.dumps({"decision": payload.decision, "review_kind": guidance.get("kind", "ambiguous"), "result": result, "send_or_draft_created": False}, ensure_ascii=False), success=True))
    db.commit()
    return {**_thread_summary(db, t), "resolved": True, "result": result, "message": "Contact-admission decision saved. No reply, Draft, Approval, or Gmail operation was created."}


class AddThreadContactBody(BaseModel):
    first_name: str | None = None
    company: str | None = None
    category: str = "qualified"
    tags: list[str] = []


@router.post("/threads/{thread_id}/contact")
def add_thread_contact(thread_id: int, payload: AddThreadContactBody, db: Session = Depends(get_db)):
    """Promote an Inbox sender into the shared CRM contact pool."""
    import json
    t = db.get(models.EmailThread, thread_id)
    if not t or not t.contact_email:
        raise HTTPException(status_code=404, detail="thread_contact_email_not_found")
    owner_id = ensure_owner(db)
    contact = db.query(models.Contact).filter_by(owner_id=owner_id, email=t.contact_email.lower()).first()
    if contact:
        return {"created": False, "contact_id": contact.id}
    if not (payload.first_name or "").strip() and not (payload.company or "").strip():
        raise HTTPException(status_code=422, detail="name_or_company_required")
    if payload.category not in ("prospect", "qualified", "customer", "partner", "won", "invalid"):
        raise HTTPException(status_code=422, detail="invalid_contact_category")
    contact = models.Contact(
        owner_id=owner_id, email=t.contact_email.lower(),
        first_name=(payload.first_name or "").strip() or None,
        company=(payload.company or "").strip() or None,
        category=payload.category, tags=json.dumps(sorted(set(payload.tags)), ensure_ascii=False),
        source="inbox", status="replied" if t.has_human_reply else "new",
    )
    db.add(contact); db.flush()
    t.pending_action = "no_action"
    db.add(models.AuditLog(
        actor="user", action="contact_admission_resolved", entity="email_thread",
        entity_id=str(t.id), detail=json.dumps({"decision": "approve", "result": "contact_created_manually", "contact_id": contact.id, "send_or_draft_created": False}, ensure_ascii=False), success=True,
    ))
    db.commit(); db.refresh(contact)
    return {"created": True, "contact_id": contact.id}


@router.get("/non-customer-filters")
def list_non_customer_filters(db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    rows = db.query(models.NonCustomerFilter).filter_by(owner_id=owner_id).order_by(
        models.NonCustomerFilter.created_at.desc()
    ).all()
    return [{"id": row.id, "email": row.email, "reason": row.reason, "source": row.source, "created_by": row.created_by, "created_at": row.created_at} for row in rows]


@router.delete("/non-customer-filters/{filter_id}")
def delete_non_customer_filter(filter_id: int, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    row = db.query(models.NonCustomerFilter).filter_by(id=filter_id, owner_id=owner_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="non_customer_filter_not_found")
    db.delete(row)
    db.add(models.AuditLog(actor="user", action="non_customer_filter_removed", entity="non_customer_filter", entity_id=str(filter_id), detail=json.dumps({"email": row.email}, ensure_ascii=False), success=True))
    db.commit()
    return {"deleted": True, "email": row.email}


def _atts(meta):
    import json
    if not meta:
        return []
    try:
        return json.loads(meta)
    except Exception:
        return []


def _contact_id(db, thread):
    if not thread.contact_email:
        return None
    account = db.get(models.GmailAccount, thread.gmail_account_id)
    owner_id = account.user_id if account else ensure_owner(db)
    c = db.query(models.Contact).filter_by(owner_id=owner_id, email=thread.contact_email.lower()).first()
    return c.id if c else None


def _analyze_thread(db: Session, t: models.EmailThread) -> dict:
    """Run the Agent on a thread's latest incoming message and persist the result.

    Returns the persisted summary dict. Calls the REAL model (LangGraph); never
    hardcodes a result.
    """
    messages = list(t.messages)
    incoming = [m for m in messages if m.is_incoming]
    if not messages:
        raise HTTPException(status_code=400, detail="no message to analyze")
    account = db.get(models.GmailAccount, t.gmail_account_id)
    owner_id = account.user_id if account else ensure_owner(db)
    latest_message = max(messages, key=_message_time)
    # Once we have sent the latest message, there is no customer reply to act on.
    # Do not re-analyze an older inbound message into another reply or review.
    if not latest_message.is_incoming and t.intent not in {"unsubscribe", "opt_out"}:
        clear_stale_human_review(db, t, owner_id=owner_id, actor="system")
        t.pending_action = "no_action"
        if not t.intent:
            t.intent = "outbound_only"
            t.last_agent_summary = "Latest message was sent by us; awaiting customer reply."
        db.commit()
        return _thread_summary(db, t)
    if not incoming:
        # Direction gate: an outbound-only thread is waiting for a reply. It is
        # never valid input for customer-intent analysis or reply generation.
        t.intent = "outbound_only"
        t.last_agent_summary = "Outbound-only thread; no customer message received."
        t.pending_action = "no_action"
        t.has_human_reply = False
        db.commit()
        return _thread_summary(db, t)

    latest = max(
        incoming,
        key=lambda message: message.received_at or message.created_at,
    )
    campaign = db.get(models.Campaign, t.campaign_id) if t.campaign_id else None
    if campaign is not None and account is not None and campaign.owner_id != account.user_id:
        campaign = None
    if t.campaign_id and campaign is None:
        stale_campaign_id = t.campaign_id
        t.campaign_id = None
        db.add(models.AuditLog(
            actor="agent",
            action="campaign_context_unavailable",
            entity="email_thread",
            entity_id=str(t.id),
            detail=(
                f"Campaign {stale_campaign_id} no longer exists; "
                "fell back to the owner Agent Profile."
            ),
        ))
    existing_contact = (
        db.query(models.Contact).filter_by(owner_id=owner_id, email=(t.contact_email or "").lower()).first()
        if t.contact_email else None
    )
    sender_email = (t.contact_email or latest.from_email or "").strip().lower()
    if existing_contact is None and sender_email and db.query(models.NonCustomerFilter).filter_by(
        owner_id=owner_id, email=sender_email
    ).first():
        t.intent = "filtered_non_customer"
        t.last_agent_summary = "Sender is on the operator-managed non-customer filter list."
        t.pending_action = "no_action"
        t.has_human_reply = False
        db.commit()
        return _thread_summary(db, t)
    known_relationship = existing_contact is not None or campaign is not None
    triage = assess_inbound(
        is_incoming=latest.is_incoming,
        from_email=latest.from_email,
        subject=latest.subject,
        body=latest.body_text,
        known_relationship=known_relationship,
        db=db,
    )
    if triage.filtered:
        t.intent = f"filtered_{triage.disposition}" if triage.disposition != "outbound" else "outbound_only"
        t.last_agent_summary = triage.reason
        t.pending_action = "no_action"
        t.has_human_reply = False
        db.commit()
        return _thread_summary(db, t)
    if requires_human_review(triage.tags):
        # Special content is not safe to auto-filter from a heuristic tag alone.
        # Only definite system, advertising, and spam mail reaches the automatic
        # filtered branch above; uncertain content stays visible for review.
        t.intent = "triage_review"
        t.last_agent_summary = triage.reason or "Content requires a human business-context decision."
        t.pending_action = "human_review"
        t.has_human_reply = False
        db.add(models.AuditLog(
            actor="agent", action="inbox_content_review_required", entity="email_thread",
            entity_id=str(t.id), detail=f"tags={','.join(sorted(triage.tags))}", success=True,
        ))
        db.commit()
        return _thread_summary(db, t)
    if triage.is_human is not True and existing_contact is None:
        t.intent = "triage_review"
        t.last_agent_summary = triage.reason
        t.pending_action = "human_review"
        t.has_human_reply = False
        db.commit()
        return _thread_summary(db, t)

    mode = campaign.agent_mode if campaign else "langgraph_only"
    inp = AnalyzeMessageInput(
        thread_id=t.id, subject=latest.subject, body_text=latest.body_text,
        from_email=latest.from_email, campaign_id=t.campaign_id,
        contact_id=_contact_id(db, t), thread_context=build_thread_context(db, t),
        mode=mode,
    )
    orch = Orchestrator(db)
    try:
        out = orch.analyze(inp)
    except AgentUnavailableError as e:
        raise HTTPException(status_code=409, detail=f"Agent unavailable: {e.agent}")
    decision_obj = out.langgraph if hasattr(out, "langgraph") else out
    intent = decision_obj.intent if decision_obj else None
    if intent:
        t.intent = intent
        t.last_agent_summary = decision_obj.summary if decision_obj else None
        # has_human_reply is a campaign-automation gate, not a generic
        # "human inbound exists" flag.  Keep it false for Inbox-only replies.
        t.has_human_reply = bool(t.campaign_id)
        # Unsubscribe / opt-out is human-gated: the Agent may detect it, but the
        # contact must never be auto-unsubscribed. Route to human review; only an
        # explicit human approval in the Inbox applies the opt-out.
        recommended_action = decision_obj.recommended_action if decision_obj else "review"
        t.pending_action = (
            "human_review" if intent in {"unsubscribe", "opt_out"}
            else ("reply" if sales_reply_required(intent, triage.tags)
                  else ("no_action" if existing_contact is not None and recommended_action == "human_review"
                        else recommended_action))
        )
        contact = _auto_contact_from_inbox(
            db,
            t,
            intent,
            latest,
            human_verified=True,
            initial_tags=triage.tags,
            first_name=triage.first_name,
            last_name=triage.last_name,
            company=triage.company,
        )
        if contact is None and t.contact_email:
            contact = db.query(models.Contact).filter_by(owner_id=owner_id, email=t.contact_email.lower()).first()
        if contact:
            _update_contact_from_human(
                db,
                contact,
                thread_id=t.id,
                intent=intent,
                content_tags=triage.tags,
                first_name=triage.first_name,
                last_name=triage.last_name,
                company=triage.company,
            )
        cc = (
            db.query(models.CampaignContact)
            .filter_by(contact_id=contact.id, campaign_id=t.campaign_id)
            .first()
            if (contact and t.campaign_id) else None
        )
        from ..services import approvals as approval_svc
        # Stop/suppression is meaningful only for a verified existing Contact
        # or a Campaign recipient.  Unknown mail that happens to contain an
        # opt-out phrase must not manufacture a Contact or Suppression record.
        # Unsubscribe / opt-out is human-gated: never auto-apply here. The thread
        # is already routed to human_review and only a human approval in the
        # Inbox applies the opt-out + Suppression.
        if known_relationship and intent not in {"unsubscribe", "opt_out"}:
            approval_svc.apply_intent_actions(
                db, cc, intent, t.contact_email, campaign.owner_id if campaign else 1
            )
    db.commit()
    summary = _thread_summary(db, t)
    if hasattr(out, "langgraph"):
        # Preserve the comparison contract for callers that explicitly chose
        # compare mode while keeping the persisted thread summary as the base
        # response used by the Inbox UI.
        summary["langgraph"] = (
            out.langgraph.model_dump() if out.langgraph is not None else None
        )
        summary["openclaw"] = (
            out.openclaw.model_dump() if out.openclaw is not None else None
        )
    return summary


@router.post("/threads/{thread_id}/analyze")
def analyze_thread(thread_id: int, db: Session = Depends(get_db)):
    t = db.get(models.EmailThread, thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="thread not found")
    summary = _analyze_thread(db, t)
    publish("inbox", {"action": "analyze", "thread_id": thread_id})
    return summary


@router.post("/threads/{thread_id}/clear-stale-review")
def clear_stale_review(thread_id: int, db: Session = Depends(get_db)):
    """Deterministically clear a *stale* human_review thread WITHOUT calling the LLM.

    human_review is meant for unknown inbound senders awaiting Contact-admission.
    If the thread already belongs to a confirmed Contact, or its latest message is
    our own outbound reply, the review is obsolete: the thread should simply show
    as awaiting_reply (pending_action=no_action). This gives the Agent a safe, side-
    effect-free exit for the legacy human_review threads that resolve_human_review
    refuses (it rejects already-known Contacts) and that re-running analyze would
    wrongly re-process.

    Returns 409 if the thread is not in human_review, or if it is a genuinely
    unknown sender with no outbound yet (those still need a human decision).
    """
    t = db.get(models.EmailThread, thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="thread not found")
    if t.pending_action != "human_review":
        raise HTTPException(
            status_code=409,
            detail="not_human_review",
        )
    account = db.get(models.GmailAccount, t.gmail_account_id)
    owner_id = account.user_id if account else ensure_owner(db)
    if not clear_stale_human_review(db, t, owner_id=owner_id, actor="user"):
        raise HTTPException(
            status_code=409,
            detail="stale_review_not_clearable:unknown sender,no outbound; needs manual human review",
        )
    db.commit()
    return _thread_summary(db, t)


@router.post("/sort")
def sort_inbox(db: Session = Depends(get_db), limit: int = 50):
    """Agent one-click sort: classify every unprocessed thread in the inbox.

    Only threads without an intent yet are (re)analyzed, so re-running is cheap
    and idempotent. Eligible inbound senders are also promoted into Contacts;
    irrelevant/unknown/marketing mail is excluded.
    """
    threads = (
        db.query(models.EmailThread)
        .filter(models.EmailThread.intent.is_(None))
        .order_by(models.EmailThread.updated_at.desc())
        .limit(limit)
        .all()
    )
    results = []
    failed = 0
    for t in threads:
        try:
            results.append(_analyze_thread(db, t))
        except HTTPException as e:
            logger.warning("sort failed for thread %s: %s", t.id, e.detail)
            failed += 1
            continue
    publish("inbox", {"action": "sort", "sorted": len(results), "failed": failed})
    return {"sorted": len(results), "failed": failed, "results": results}


class ReplyBody(BaseModel):
    subject: str | None = None
    body_text: str | None = None


@router.post("/threads/{thread_id}/generate-reply")
def generate_reply(thread_id: int, db: Session = Depends(get_db)):
    """Agent drafts a reply to a thread (real model). Creates a PENDING reply
    approval + a real Gmail draft in the thread. The user then confirms via the
    standard approval decision endpoint, which sends through the policy-gated
    real-Gmail path (so the allowlist + idempotency still apply).
    """
    t = db.get(models.EmailThread, thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="thread not found")
    incoming = [m for m in t.messages if m.is_incoming]
    if not incoming:
        raise HTTPException(status_code=409, detail="outbound_only_thread_has_no_message_to_reply_to")
    if t.pending_action == "human_review":
        raise HTTPException(status_code=409, detail="thread_requires_human_review")
    if t.pending_action != "reply":
        raise HTTPException(status_code=409, detail="thread_not_eligible_for_reply")
    latest_message = max(
        t.messages,
        key=lambda message: message.received_at or message.created_at,
    )
    if not latest_message.is_incoming:
        raise HTTPException(status_code=409, detail="awaiting_customer_reply")
    latest = max(
        incoming,
        key=lambda message: message.received_at or message.created_at,
    )

    # Campaign replies use their existing Campaign. Daily Inbox replies remain
    # standalone and must not be forced into an unrelated active Campaign.
    campaign = db.get(models.Campaign, t.campaign_id) if t.campaign_id else None

    # Resolve an existing CRM contact for context, but never silently add an
    # Inbox sender to Contacts or a marketing Campaign. Those are explicit user
    # actions in the CRM workflow.
    contact = None
    if t.contact_email:
        owner_id = (
            db.get(models.GmailAccount, t.gmail_account_id).user_id
            if db.get(models.GmailAccount, t.gmail_account_id) else None
        )
        contact = db.query(models.Contact).filter_by(owner_id=owner_id, email=t.contact_email).first()
    cc = None
    if contact is not None and campaign is not None and t.campaign_id == campaign.id:
        cc = db.query(models.CampaignContact).filter_by(campaign_id=campaign.id, contact_id=contact.id).first()

    mode = campaign.agent_mode if campaign else "langgraph_only"
    thread_context = build_thread_context(db, t)
    # 1) Try analyze (composes a contextual reply for interested/asking_question).
    inp = AnalyzeMessageInput(
        thread_id=t.id, subject=latest.subject, body_text=latest.body_text,
        from_email=latest.from_email, campaign_id=campaign.id if campaign else None,
        contact_id=contact.id if contact else None, thread_context=thread_context,
        mode=mode,
    )
    orch = Orchestrator(db)
    try:
        out = orch.analyze(inp)
    except AgentUnavailableError as e:
        raise HTTPException(status_code=409, detail=f"Agent unavailable: {e.agent}")
    decision = out.langgraph if hasattr(out, "langgraph") else out
    intent = decision.intent if decision else None
    if intent:
        t.intent = intent
        t.last_agent_summary = decision.summary if decision else None
        t.pending_action = decision.recommended_action if decision else None

    subject = None
    body_text = None
    if decision and getattr(decision, "draft", None):
        subject = decision.draft.subject
        body_text = decision.draft.body_text
    if not subject or not body_text:
        # 2) Fallback: generate a follow-up/reply with thread context.
        if campaign is None:
            raise HTTPException(status_code=503, detail="Inbox reply agent returned no draft")
        try:
            prop = orch.generate_follow_up(GenerateFollowUpInput(
                campaign_id=campaign.id, contact_id=contact.id if contact else 0,
                thread_id=t.id, sequence=(cc.assigned_follow_ups or 0) + 1, mode=mode,
                last_message_body=latest.body_text,
            ))
        except AgentUnavailableError as e:
            raise HTTPException(status_code=409, detail=f"Agent unavailable: {e.agent}")
        if prop is None:
            raise HTTPException(status_code=503, detail="Agent returned no reply draft")
        subject = subject or prop.subject
        body_text = body_text or prop.body_text

    from ..services import approvals as approval_svc
    try:
        ap = approval_svc.create_reply_approval(
            db, thread=t, contact=contact, campaign=campaign, cc=cc,
            subject=subject, body_text=body_text, body_html="",
            recommended_action=decision.recommended_action if decision else "human_review",
            risk_level=decision.risk_level if decision else "low",
            agent="langgraph", mode=mode, is_primary=True,
            source_message=latest,
        )
    except approval_svc.DraftCreationError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    db.commit()
    return {
        "approval_id": ap.id,
        "subject": ap.subject,
        "body_text": ap.body_text,
        "intent": intent,
        "reasoning": decision.reasoning_summary if decision else None,
        "category": _category(intent),
        "message": "Reply draft generated. Confirm in Approvals to send through Gmail.",
    }


def approval_svc_apply(db, cc, intent, email, owner_id):
    from ..services import approvals as approval_svc
    return approval_svc.apply_intent_actions(db, cc, intent, email, owner_id)
