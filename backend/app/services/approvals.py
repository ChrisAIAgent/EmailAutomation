"""Approval workflow + send execution (always gated by the policy engine)."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from googleapiclient.errors import HttpError

from .. import models
from ..config import get_settings
from ..security import new_idempotency_key
from ..tools.email_tools import UnifiedEmailToolLayer
from .accounts import resolve_sending_account
from .thread_context import build_thread_context

logger = logging.getLogger("approvals")


class DraftCreationError(RuntimeError):
    """A policy or transport failure prevented creation of a real draft."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ApprovalRevisionError(RuntimeError):
    """A revision failed before any Approval/Draft content was changed."""

    def __init__(self, reason: str, status_code: int = 409):
        self.reason = reason
        self.status_code = status_code
        super().__init__(reason)


def _require_draft(result):
    if result.get("ok") and result.get("draft") is not None:
        return result["draft"]
    raise DraftCreationError(
        result.get("blocked") or result.get("error") or "draft_creation_failed"
    )


def _account_for_campaign(db, campaign):
    # Delegate to the single source of truth. The resolver reproduces the
    # original fallback chain (connected+OAuth -> connected -> any -> provision
    # placeholder) but with consistent ordering and an OAuth join, so a stale placeholder row
    # can never win over the real connected account. ``provision=True`` keeps
    # draft-only mode working when no real account exists.
    return resolve_sending_account(db, campaign=campaign, provision=get_settings().ALLOW_INMEMORY_GMAIL)


def _account_for_thread(db, thread):
    """Resolve the connected Gmail account for a standalone Inbox reply.

    Falls back to the thread account's owner (or the first user) so the search
    is scoped. The resolver only returns the thread's own account when it is
    actually connected — a disconnected thread account no longer silently wins.
    """
    owner_id = None
    if thread is not None and thread.gmail_account_id:
        acc = db.get(models.GmailAccount, thread.gmail_account_id)
        if acc:
            owner_id = acc.user_id
    if owner_id is None:
        owner_id = db.query(models.User.id).order_by(models.User.id.asc()).scalar()
    return resolve_sending_account(db, thread=thread, owner_id=owner_id, provision=get_settings().ALLOW_INMEMORY_GMAIL)


def _append_reference(references: str | None, message_id: str) -> str:
    """Build the RFC References chain without duplicating the parent id."""
    values = (references or "").split()
    if message_id not in values:
        values.append(message_id)
    return " ".join(values)


def _thread_reply_context(db, tl, thread, source_message=None):
    """Return an exact Gmail reply context or fail closed.

    Gmail requires a target thread id, a matching Subject, and RFC Message-ID
    based In-Reply-To/References headers. A Gmail API message id cannot be used
    as the RFC Message-ID, so legacy rows are hydrated from Gmail on demand.
    """
    if thread is None or not thread.gmail_thread_id:
        raise DraftCreationError("gmail_thread_context_missing")

    if source_message is None:
        source_message = (
            db.query(models.EmailMessage)
            .filter_by(thread_id=thread.id)
            .order_by(models.EmailMessage.received_at.desc(), models.EmailMessage.id.desc())
            .first()
        )

    if source_message is None:
        try:
            remote = tl.get_thread(thread.gmail_thread_id, agent="system", is_primary=True,
                                   campaign_id=None)
            source_message = remote.messages[-1] if remote.messages else None
        except Exception as exc:
            raise DraftCreationError("gmail_reply_headers_unavailable") from exc

    if source_message is None:
        raise DraftCreationError("gmail_reply_headers_unavailable")

    if not getattr(source_message, "message_id_header", None):
        gmail_message_id = getattr(source_message, "gmail_message_id", None)
        if not gmail_message_id:
            raise DraftCreationError("gmail_reply_headers_unavailable")
        try:
            remote_message = tl.get_message(gmail_message_id, agent="system", is_primary=True,
                                            campaign_id=None)
        except Exception as exc:
            raise DraftCreationError("gmail_reply_headers_unavailable") from exc
        for field in ("message_id_header", "in_reply_to_header", "references_header"):
            value = getattr(remote_message, field, None)
            if value:
                setattr(source_message, field, value)
        if isinstance(source_message, models.EmailMessage):
            db.flush()

    parent_message_id = getattr(source_message, "message_id_header", None)
    subject = getattr(source_message, "subject", None) or thread.subject
    if not parent_message_id or not subject:
        raise DraftCreationError("gmail_reply_headers_unavailable")

    base_references = (
        getattr(source_message, "references_header", None)
        or getattr(source_message, "in_reply_to_header", None)
    )
    return {
        "thread_gmail_id": thread.gmail_thread_id,
        "subject": subject,
        "in_reply_to": parent_message_id,
        "references": _append_reference(base_references, parent_message_id),
        "source_message": source_message,
    }


def approval_owner_id(db, approval: models.Approval) -> int | None:
    """Resolve the owner for an Approval without falling back across tenants."""
    if approval.campaign_id:
        campaign = db.get(models.Campaign, approval.campaign_id)
        if campaign:
            return campaign.owner_id
    if approval.thread_id:
        thread = db.get(models.EmailThread, approval.thread_id)
        account = db.get(models.GmailAccount, thread.gmail_account_id) if thread else None
        if account:
            return account.user_id
    return None


def _revision_hash(subject: str, body_text: str) -> str:
    return hashlib.sha256(f"{subject.strip()}\n{body_text.strip()}".encode("utf-8")).hexdigest()


def _latest_successful_revision(db, approval_id: int):
    rows = (
        db.query(models.AuditLog)
        .filter_by(action="approval_revised", entity="approval", entity_id=str(approval_id), success=True)
        .order_by(models.AuditLog.created_at.desc(), models.AuditLog.id.desc())
        .all()
    )
    for row in rows:
        try:
            detail = json.loads(row.detail or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(detail, dict):
            return detail
    return None


def _revision_contact(db, approval, campaign, thread):
    if approval.campaign_contact_id:
        member = db.get(models.CampaignContact, approval.campaign_contact_id)
        if member:
            return db.get(models.Contact, member.contact_id), member
    owner_id = approval_owner_id(db, approval)
    email = (thread.contact_email if thread else None) or approval.to_email
    contact = None
    if email and owner_id is not None:
        contact = db.query(models.Contact).filter_by(owner_id=owner_id, email=email.lower()).first()
    return contact, None


def _proposal_from_revision_output(output, campaign):
    """Select the configured primary proposal in compare mode."""
    if hasattr(output, "langgraph"):
        primary = campaign.primary_agent if campaign and campaign.primary_agent in {"langgraph", "openclaw"} else "langgraph"
        decision = getattr(output, primary, None) or getattr(output, "langgraph", None) or getattr(output, "openclaw", None)
        if decision is None or not getattr(decision, "draft", None):
            return None
        from ..schemas import decision_to_proposal
        return decision_to_proposal(decision)
    return output


def revise_pending_approval(db, approval_id: int, instruction: str, *, editor_email: str | None = None, owner_id: int | None = None):
    """Revise one pending Approval and its existing Gmail Draft, never send.

    Every failure before ``update_draft`` leaves the existing content untouched;
    an update failure likewise leaves the Approval fields unchanged.  The linked
    Draft is always updated in place, so the Approval/Draft relationship and the
    original Gmail thread are retained.
    """
    normalized = (instruction or "").strip()
    if not normalized:
        raise ApprovalRevisionError("approval_revision_instruction_required", 422)
    if len(normalized) > 2_000:
        raise ApprovalRevisionError("approval_revision_instruction_too_long", 422)

    approval = db.get(models.Approval, approval_id)
    if approval is None:
        raise ApprovalRevisionError("approval_not_found", 404)
    if approval.status != "pending":
        raise ApprovalRevisionError(f"approval_not_pending:{approval.status}")
    resolved_owner_id = approval_owner_id(db, approval)
    if owner_id is not None and resolved_owner_id != owner_id:
        raise ApprovalRevisionError("approval_not_owned", 403)
    if resolved_owner_id is None:
        raise ApprovalRevisionError("approval_owner_unresolved")
    draft = db.get(models.EmailDraft, approval.draft_id) if approval.draft_id else None
    if draft is None or draft.status != "draft":
        raise ApprovalRevisionError("approval_revision_draft_unavailable")
    if not (draft.gmail_draft_id or "").strip():
        # Do not spend an LLM call on copy that cannot be written to the actual
        # Gmail Draft. The existing Approval remains pending and unchanged.
        raise ApprovalRevisionError("approval_revision_draft_remote_id_missing")

    instruction_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    current_hash = _revision_hash(approval.subject, approval.body_text or "")
    previous = _latest_successful_revision(db, approval.id)
    if previous and previous.get("instruction_hash") == instruction_hash and previous.get("new_hash") == current_hash:
        return {
            "ok": True, "approval_id": approval.id, "draft_id": draft.id,
            "status": approval.status, "kind": approval.kind,
            "subject": approval.subject, "body_text": approval.body_text,
            "revision_applied": True, "idempotent_replay": True, "sent": False,
        }

    campaign = db.get(models.Campaign, approval.campaign_id) if approval.campaign_id else None
    thread = db.get(models.EmailThread, approval.thread_id) if approval.thread_id else None
    contact, member = _revision_contact(db, approval, campaign, thread)
    if approval.campaign_contact_id and (member is None or not member.membership_active):
        raise ApprovalRevisionError("campaign_contact_removed")
    if approval.kind in {"reply", "follow_up"} and thread is None:
        raise ApprovalRevisionError("gmail_thread_context_missing")

    latest_customer_message = ""
    reply_context = None
    mode = campaign.agent_mode if campaign else "langgraph_only"
    agent = campaign.primary_agent if campaign else (approval.agent or "langgraph")
    if thread is not None:
        latest = (
            db.query(models.EmailMessage)
            .filter_by(thread_id=thread.id, is_incoming=True)
            .order_by(models.EmailMessage.received_at.desc(), models.EmailMessage.id.desc())
            .first()
        )
        latest_customer_message = latest.body_text if latest else ""
    else:
        latest = None

    from ..schemas import ApprovalRevisionInput
    revision_input = ApprovalRevisionInput(
        approval_id=approval.id, instruction=normalized, kind=approval.kind,
        current_subject=approval.subject, current_body_text=approval.body_text or "",
        campaign_id=campaign.id if campaign else None,
        contact_id=contact.id if contact else None,
        thread_id=thread.id if thread else None,
        thread_context=build_thread_context(db, thread) if thread else "",
        latest_customer_message=latest_customer_message,
        intent=approval.recommended_action or "unknown",
        recommended_action=approval.recommended_action or "human_review",
        risk_level=approval.risk_level or "low",
        mode=mode,
    )
    from ..agents.orchestrator import Orchestrator
    from ..exceptions import AgentUnavailableError
    try:
        output = Orchestrator(db).revise_approval(revision_input)
    except AgentUnavailableError as exc:
        raise ApprovalRevisionError(f"approval_revision_not_applied:agent_unavailable:{exc.agent}") from exc
    proposal = _proposal_from_revision_output(output, campaign)
    if proposal is None or not (proposal.body_text or "").strip():
        raise ApprovalRevisionError("approval_revision_not_applied", 503)

    # Re-apply deterministic identity cleanup to protect output from every
    # adapter, including external OpenClaw, before touching a Gmail Draft.
    from .agent_profile import apply_profile_to_reply, resolve_profile
    profile = resolve_profile(db, resolved_owner_id, campaign=campaign)
    try:
        body_text, _ = apply_profile_to_reply(
            proposal.body_text, profile,
            customer_message=latest_customer_message,
            customer_name=(contact.first_name if contact else "") or "",
        )
    except ValueError as exc:
        raise ApprovalRevisionError("approval_revision_not_applied:profile_validation") from exc

    # Replies and follow-ups always use refreshed Gmail threading headers and
    # the exact source thread subject.  First outreach may revise its subject.
    account, oauth = _account_for_campaign(db, campaign) if campaign else _account_for_thread(db, thread)
    tl = UnifiedEmailToolLayer(db, account, oauth)
    subject = (proposal.subject or "").strip()
    if thread is not None:
        try:
            reply_context = _thread_reply_context(db, tl, thread, latest)
        except DraftCreationError as exc:
            raise ApprovalRevisionError(exc.reason) from exc
        subject = reply_context["subject"]
    if not subject:
        raise ApprovalRevisionError("approval_revision_not_applied", 503)

    from . import quality as quality_svc
    quality_report = quality_svc.compute_quality(
        subject, body_text,
        contact={
            "first_name": contact.first_name if contact else "",
            "company": contact.company if contact else "",
            "title": contact.title if contact else "",
        },
        campaign={
            "product_description": campaign.product_description if campaign else "",
            "objective": campaign.objective if campaign else "",
            "target_audience": campaign.target_audience if campaign else "",
            "sender_company": campaign.sender_company if campaign else "",
        },
    )
    if quality_report.get("fabrication"):
        raise ApprovalRevisionError("approval_revision_not_applied:fabrication_detected", 503)

    revision_key = "revision:" + hashlib.sha256(
        f"{approval.id}\0{current_hash}\0{instruction_hash}".encode("utf-8")
    ).hexdigest()[:32]
    result = tl.update_draft(
        draft_db_id=draft.id, to=draft.to_email, subject=subject, body_text=body_text,
        body_html=proposal.body_html or "",
        thread_gmail_id=reply_context["thread_gmail_id"] if reply_context else None,
        in_reply_to=reply_context["in_reply_to"] if reply_context else None,
        references=reply_context["references"] if reply_context else None,
        agent=proposal.agent or agent, mode=mode, is_primary=True,
        campaign_id=campaign.id if campaign else None, idempotency_key=revision_key,
    )
    if not result.get("ok"):
        raise ApprovalRevisionError(
            "approval_revision_draft_update_failed:" + (result.get("blocked") or result.get("error") or "unknown")
        )

    approval.subject = subject
    approval.body_text = body_text
    approval.body_html = proposal.body_html or ""
    approval.agent = proposal.agent or agent
    approval.agent_run_id = proposal.run_id
    approval.model = proposal.model
    approval.prompt_version = proposal.prompt_version
    approval.latency_ms = proposal.latency_ms
    approval.quality_json = quality_svc.quality_to_json(quality_report)
    new_hash = _revision_hash(subject, body_text)
    db.add(models.AuditLog(
        actor=(editor_email or "agent").strip() or "agent",
        action="approval_revised", entity="approval", entity_id=str(approval.id),
        detail=json.dumps({
            "previous_hash": current_hash, "new_hash": new_hash,
            "instruction_hash": instruction_hash, "kind": approval.kind,
            "agent": approval.agent, "revision_key": revision_key,
        }, ensure_ascii=False),
        success=True,
    ))
    db.flush()
    return {
        "ok": True, "approval_id": approval.id, "draft_id": draft.id,
        "status": approval.status, "kind": approval.kind, "subject": approval.subject,
        "body_text": approval.body_text, "quality": quality_report,
        "revision_applied": True, "idempotent_replay": False, "sent": False,
    }


def create_outreach_approval(
    db, cc, proposal, mode="langgraph_only", agent="langgraph", is_primary=True,
    automation_run_id: int | None = None,
) -> models.Approval:
    """Create a real Gmail draft (allowed in draft-only mode) + a pending approval."""
    if not cc.membership_active:
        raise DraftCreationError("campaign_contact_removed")
    campaign = db.get(models.Campaign, cc.campaign_id)
    if campaign is None:
        raise DraftCreationError("campaign_not_found")
    if campaign.status != "active":
        raise DraftCreationError(f"campaign_{campaign.status}")
    contact = db.get(models.Contact, cc.contact_id)
    account, oauth = (
        _account_for_campaign(db, campaign)
        if campaign
        else resolve_sending_account(db, owner_id=contact.owner_id if contact else None)
    )
    tl = UnifiedEmailToolLayer(db, account, oauth)

    idem = new_idempotency_key("outreach", str(campaign.id), str(contact.id))
    res = tl.create_draft(
        to=contact.email,
        subject=proposal.subject, body_text=proposal.body_text, body_html=proposal.body_html,
        agent=agent, mode=mode, is_primary=is_primary,
        campaign_id=campaign.id, campaign_contact_id=cc.id,
        kind="outreach", idempotency_key=idem,
    )
    draft = _require_draft(res)
    # Transparent quality signals (deterministic, falsification-proof): computed
    # from the REAL contact + campaign records only. Surfaced to the tester so the
    # "quality scoring" rubric is populated and auditable.
    from . import quality as quality_svc
    quality_report = quality_svc.compute_quality(
        proposal.subject, proposal.body_text,
        contact={k: getattr(contact, k) for k in ("first_name", "company", "title")},
        campaign={k: getattr(campaign, k) for k in ("product_description", "objective", "target_audience", "sender_company")},
    )
    ap = models.Approval(
        kind="first_send", campaign_id=campaign.id, automation_run_id=automation_run_id,
        campaign_contact_id=cc.id,
        draft_id=draft.id, agent=agent,
        agent_run_id=proposal.run_id, model=proposal.model,
        prompt_version=proposal.prompt_version, latency_ms=proposal.latency_ms,
        quality_json=quality_svc.quality_to_json(quality_report),
        to_email=contact.email, subject=proposal.subject, body_text=proposal.body_text,
        body_html=proposal.body_html, recommended_action=proposal.recommended_action,
        risk_level=proposal.risk_level, idempotency_key=new_idempotency_key("send", str(draft.id)),
        status="pending",
    )
    db.add(ap)
    db.flush()
    cc.status = "outreach_generated"
    return ap


def create_follow_up_approval(
    db, task, proposal, mode="langgraph_only", agent="langgraph", is_primary=True,
    automation_run_id: int | None = None,
) -> models.Approval:
    cc = db.get(models.CampaignContact, task.campaign_contact_id)
    if cc is None or not cc.membership_active:
        raise DraftCreationError("campaign_contact_removed")
    campaign = db.get(models.Campaign, cc.campaign_id)
    if campaign is None:
        raise DraftCreationError("campaign_not_found")
    if campaign.status != "active":
        raise DraftCreationError(f"campaign_{campaign.status}")
    contact = db.get(models.Contact, cc.contact_id)
    account, oauth = _account_for_campaign(db, campaign)
    tl = UnifiedEmailToolLayer(db, account, oauth)

    thread = db.get(models.EmailThread, task.thread_id) if task.thread_id else None
    context = _thread_reply_context(db, tl, thread)
    idem = new_idempotency_key("followup", str(task.id))
    res = tl.create_draft(
        to=contact.email, subject=context["subject"], body_text=proposal.body_text, body_html=proposal.body_html,
        thread_gmail_id=context["thread_gmail_id"], in_reply_to=context["in_reply_to"],
        references=context["references"],
        agent=agent, mode=mode, is_primary=is_primary,
        campaign_id=campaign.id, campaign_contact_id=cc.id, kind="follow_up", idempotency_key=idem,
    )
    draft = _require_draft(res)
    draft.thread_id = thread.id
    ap = models.Approval(
        kind="follow_up", campaign_id=campaign.id, automation_run_id=automation_run_id,
        campaign_contact_id=cc.id,
        draft_id=draft.id, thread_id=task.thread_id,
        agent=agent, to_email=contact.email, subject=context["subject"],
        body_text=proposal.body_text, body_html=proposal.body_html,
        recommended_action=proposal.recommended_action, risk_level=proposal.risk_level,
        idempotency_key=new_idempotency_key("send", str(draft.id)),
        status="pending",
    )
    db.add(ap)
    db.flush()
    return ap


def create_reply_approval(
    db, *, thread, contact, campaign, cc, subject, body_text, body_html="",
    recommended_action="human_review", risk_level="low",
    agent="langgraph", mode="langgraph_only", is_primary=True,
    automation_run_id: int | None = None,
    source_message=None,
):
    """Create a reply draft + pending approval for an analyzed incoming reply.

    The record remains pending until it is released by a full-auto Agent Run or
    by the user confirmation that resumes a semi-auto Agent Run.
    """
    existing_pending = (
        db.query(models.Approval)
        .filter_by(thread_id=thread.id if thread else None, kind="reply", status="pending")
        .first()
    )
    if existing_pending:
        raise DraftCreationError("reply_approval_already_pending")

    # Inbox replies are standalone operations. Campaign context may inform the
    # generated copy, but it must not control account selection or send policy.
    account, oauth = _account_for_thread(db, thread)
    tl = UnifiedEmailToolLayer(db, account, oauth)
    gmail_thread_id = thread.gmail_thread_id if thread else None
    if gmail_thread_id:
        from ..gmail.client import RealGmailTransport
        if isinstance(tl._transport(), RealGmailTransport):
            try:
                tl.get_thread(gmail_thread_id, agent=agent, mode=mode, is_primary=is_primary,
                              campaign_id=None)
            except HttpError as exc:
                if exc.status_code != 404:
                    raise
                thread.intent = "thread_unavailable"
                thread.pending_action = "no_action"
                db.add(models.AuditLog(actor="agent", action="gmail_thread_unavailable",
                    entity="email_thread", entity_id=str(thread.id), detail=f"gmail_thread_id={gmail_thread_id}"))
                db.flush()
                raise DraftCreationError("gmail_thread_not_found") from exc
    if source_message is None and thread is not None:
        source_message = (db.query(models.EmailMessage).filter_by(thread_id=thread.id, is_incoming=True)
            .order_by(models.EmailMessage.received_at.desc(), models.EmailMessage.id.desc()).first())
    context = _thread_reply_context(db, tl, thread, source_message)
    source_message = context["source_message"]
    source_identity = (getattr(source_message, "gmail_message_id", None)
                       or getattr(source_message, "gmail_history_id", None)
                       or str(getattr(source_message, "id", "unknown")))
    content_identity = hashlib.sha256(
        f"{context['subject'].strip()}\n{body_text.strip()}".encode("utf-8")
    ).hexdigest()[:20]
    idem = new_idempotency_key(
        "reply",
        str(thread.id if thread else "x"),
        str(source_identity),
        content_identity,
    )
    res = tl.create_draft(
        to=contact.email, subject=context["subject"], body_text=body_text, body_html=body_html,
        thread_gmail_id=context["thread_gmail_id"], in_reply_to=context["in_reply_to"],
        references=context["references"], agent=agent, mode=mode, is_primary=is_primary,
        campaign_id=None, campaign_contact_id=None,
        kind="reply", idempotency_key=idem,
    )
    draft = _require_draft(res)
    draft.thread_id = thread.id
    ap = models.Approval(
        kind="reply", campaign_id=None,
        automation_run_id=automation_run_id,
        campaign_contact_id=None,
        draft_id=draft.id, thread_id=thread.id if thread else None,
        agent=agent, to_email=contact.email, subject=context["subject"],
        body_text=body_text, body_html=body_html,
        recommended_action=recommended_action, risk_level=risk_level,
        idempotency_key=new_idempotency_key("send", str(draft.id)),
        status="pending",
    )
    db.add(ap)
    db.flush()
    return ap


def decide_approval(db, approval_id, decision: str, *, editor_email=None, edited_subject=None,
                    edited_body_text=None, edited_body_html=None, rejection_reason=None,
                    mode="langgraph_only", agent="langgraph", is_primary=True):
    ap = db.get(models.Approval, approval_id)
    if ap is None:
        return {"ok": False, "error": "approval not found"}
    if ap.status != "pending":
        return {"ok": False, "error": f"already {ap.status}"}
    if ap.campaign_contact_id:
        member = db.get(models.CampaignContact, ap.campaign_contact_id)
        if member is None:
            if ap.draft_id:
                blocked_draft = db.get(models.EmailDraft, ap.draft_id)
                if blocked_draft and blocked_draft.status in ("draft", "approved"):
                    blocked_draft.status = "cancelled"
            ap.status = "expired"
            ap.decided_by = "system"
            ap.decided_at = datetime.now(timezone.utc)
            ap.rejection_reason = "campaign_contact_removed"
            db.flush()
            return {"ok": False, "blocked": "campaign_contact_removed", "status": ap.status}
        campaign = db.get(models.Campaign, member.campaign_id)
        campaign_block = (
            "campaign_not_found" if campaign is None
            else f"campaign_{campaign.status}" if campaign.status != "active"
            else None
        )
        if campaign_block:
            # Pause is reversible, so keep its frozen content pending. Archived
            # and stopped campaigns are terminal and must close stale send work.
            if campaign is not None and campaign.status == "paused":
                return {"ok": False, "blocked": campaign_block, "status": ap.status}
            if ap.draft_id:
                blocked_draft = db.get(models.EmailDraft, ap.draft_id)
                if blocked_draft and blocked_draft.status in ("draft", "approved"):
                    blocked_draft.status = "cancelled"
            ap.status = "expired"
            ap.decided_by = "system"
            ap.decided_at = datetime.now(timezone.utc)
            ap.rejection_reason = campaign_block
            db.flush()
            return {"ok": False, "blocked": campaign_block, "status": ap.status}
        if not member.membership_active:
            if ap.draft_id:
                blocked_draft = db.get(models.EmailDraft, ap.draft_id)
                if blocked_draft and blocked_draft.status in ("draft", "approved"):
                    blocked_draft.status = "cancelled"
            ap.status = "expired"
            ap.decided_by = "system"
            ap.decided_at = datetime.now(timezone.utc)
            ap.rejection_reason = "campaign_contact_removed"
            db.flush()
            return {"ok": False, "blocked": "campaign_contact_removed", "status": ap.status}
    if decision == "reject":
        ap.status = "rejected"
        ap.decided_by = editor_email
        ap.decided_at = datetime.now(timezone.utc)
        ap.rejection_reason = rejection_reason
        if ap.draft_id:
            draft = db.get(models.EmailDraft, ap.draft_id)
            if draft and draft.status in ("draft", "approved"):
                draft.status = "cancelled"
        db.flush()
        return {"ok": True, "status": "rejected"}

    # approve
    is_inbox_reply = ap.kind == "reply" and ap.thread_id is not None
    draft = db.get(models.EmailDraft, ap.draft_id) if ap.draft_id else None
    subject = edited_subject or ap.subject
    body_text = edited_body_text or ap.body_text
    body_html = edited_body_html if edited_body_html is not None else ap.body_html
    if draft and (edited_subject or edited_body_text):
        _edit_campaign = (db.get(models.Campaign, ap.campaign_id)
                          if ap.campaign_id and not is_inbox_reply else None)
        _edit_thread = db.get(models.EmailThread, ap.thread_id) if ap.thread_id else None
        account, oauth = (
            _account_for_campaign(db, _edit_campaign)
            if _edit_campaign else _account_for_thread(db, _edit_thread)
        )
        tl = UnifiedEmailToolLayer(db, account, oauth)
        reply_context = None
        if _edit_thread and ap.kind in {"reply", "follow_up"}:
            reply_context = _thread_reply_context(db, tl, _edit_thread)
            if edited_subject and edited_subject != reply_context["subject"]:
                return {"ok": False, "blocked": "reply_subject_must_match_thread", "status": ap.status}
            subject = reply_context["subject"]
        update_result = tl.update_draft(
            draft_db_id=draft.id, to=draft.to_email, subject=subject,
            body_text=body_text, body_html=body_html or "",
            thread_gmail_id=reply_context["thread_gmail_id"] if reply_context else None,
            in_reply_to=reply_context["in_reply_to"] if reply_context else None,
            references=reply_context["references"] if reply_context else None,
            agent=agent, mode=mode, is_primary=is_primary,
        )
        if not update_result.get("ok"):
            # Never send a remote Gmail Draft when the requested content update
            # was not applied. Keep the Approval pending for safe recovery.
            return {
                "ok": False,
                "blocked": "approval_draft_update_failed:" + (
                    update_result.get("blocked") or update_result.get("error") or "unknown"
                ),
                "status": ap.status,
            }

    campaign = (db.get(models.Campaign, ap.campaign_id)
                if ap.campaign_id and not is_inbox_reply else None)
    thread = db.get(models.EmailThread, ap.thread_id) if ap.thread_id else None
    account, oauth = _account_for_campaign(db, campaign) if campaign else _account_for_thread(db, thread)
    tl = UnifiedEmailToolLayer(db, account, oauth)
    cc = (db.get(models.CampaignContact, ap.campaign_contact_id)
          if ap.campaign_contact_id and not is_inbox_reply else None)
    thread = db.get(models.EmailThread, ap.thread_id) if ap.thread_id else None

    res = tl.send_approved_draft(
        draft_db_id=draft.id if draft else None,
        approval_id=ap.id, agent=agent, mode=mode, is_primary=is_primary,
        campaign_id=campaign.id if campaign else None, thread_db_id=thread.id if thread else None,
        idempotency_key=ap.idempotency_key, requires_approval=True,
    )
    if not res.get("ok"):
        # blocked (e.g. draft-only mode, allowlist, limit) — keep pending so user can retry later
        return {"ok": False, "blocked": res.get("blocked") or res.get("error"), "status": ap.status}

    ap.status = "approved"
    ap.decided_by = editor_email
    ap.decided_at = datetime.now(timezone.utc)
    db.flush()

    if is_inbox_reply and thread and thread.contact_email:
        contact = db.query(models.Contact).filter_by(
            owner_id=account.user_id, email=thread.contact_email.lower()
        ).first()
        if contact and not contact.manual_lock:
            contact.status = "replied"
            contact.lifecycle_stage = "awaiting_reply"
            contact.next_action = "waiting_for_customer"
            contact.next_follow_up_at = None

    # post-send bookkeeping
    if cc:
        contact = db.get(models.Contact, cc.contact_id)
        cc.assigned_follow_ups = (cc.assigned_follow_ups or 0) + (1 if ap.kind == "follow_up" else 0)
        if ap.kind == "first_send":
            cc.status = "sent"
            if contact:
                contact.status = "contacted"
                contact.last_contacted_at = datetime.now(timezone.utc)
                if not contact.manual_lock:
                    contact.lifecycle_stage = "awaiting_reply"
                    contact.next_action = "follow_up"
            cc.last_message_id = res.get("message_id")
            # Schedule the next follow-up (sequence 1 after the initial outreach).
            from . import followup as followup_svc
            followup_svc.schedule_next_follow_up(db, cc, agent=agent, mode=mode, is_primary=is_primary)
        elif ap.kind == "follow_up":
            cc.status = "following_up"
            if contact:
                contact.status = "following_up"
                contact.last_contacted_at = datetime.now(timezone.utc)
                if not contact.manual_lock:
                    contact.lifecycle_stage = "following_up"
                    contact.next_action = "follow_up"
            # schedule next follow-up if under max
            from . import followup as followup_svc
            if cc.assigned_follow_ups < campaign.max_follow_ups:
                followup_svc.schedule_next_follow_up(db, cc, agent=agent, mode=mode, is_primary=is_primary)
    return {
        "ok": True,
        "status": "approved",
        "message_id": res.get("message_id"),
        "thread_id": res.get("thread_id"),
        "expected_thread_id": res.get("expected_thread_id"),
        "thread_match": res.get("thread_match"),
    }


def apply_intent_actions(db, cc, intent: str, contact_email: str, owner_id: int, *, actor: str = "agent"):
    """When a reply is analyzed, apply stop/suppression/out-of-office side effects."""
    if intent in {"unsubscribe", "opt_out", "not_interested", "bounce"}:
        contact = db.query(models.Contact).filter_by(owner_id=owner_id, email=contact_email.lower()).first()
        if contact:
            from .contact_lifecycle import stop_contact_delivery
            return stop_contact_delivery(db, contact, owner_id=owner_id, intent=intent, actor=actor)
    if intent in ("unsubscribe", "opt_out", "not_interested"):
        # add suppression + permanently stop
        existing = db.query(models.Suppression).filter_by(owner_id=owner_id, email=contact_email.lower()).first()
        if not existing:
            db.add(models.Suppression(owner_id=owner_id, email=contact_email.lower(),
                                      reason="unsubscribe" if intent == "unsubscribe" else "opt_out" if intent == "opt_out" else "not_interested",
                                      source="agent"))
        if cc:
            cc.status = "stopped"
            from .contact_lifecycle import cancel_campaign_member_work
            cancel_campaign_member_work(db, cc, reason=f"reply_intent:{intent}", actor=actor)
        return "stopped"
    if intent == "bounce":
        if cc:
            cc.status = "bounced"
            from .contact_lifecycle import cancel_campaign_member_work
            cancel_campaign_member_work(db, cc, reason="reply_intent:bounce", actor=actor)
        return "bounced"
    if intent == "out_of_office":
        # reschedule follow-ups further out
        return "rescheduled"
    return "none"
