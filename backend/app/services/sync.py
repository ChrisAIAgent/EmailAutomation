"""Gmail sync: pulls real threads/messages and stores normalized rows.

Reads only go through the Unified Email Tool Layer. Attachment bodies are NEVER
downloaded 鈥?only filename / mime / size / attachmentId metadata.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from .. import models
from ..gmail.transport import GmailTimeoutError, GmailTransientNetworkError, _looks_corrupt_text, _looks_like_html_or_css_residue, readable_email_text
from ..tools.email_tools import UnifiedEmailToolLayer
from .inbox_triage import assess_inbound, clear_stale_human_review, recompute_contact_reply_state

logger = logging.getLogger("gmail.sync")
INITIAL_IMPORT_QUERY = "in:anywhere -in:spam -in:trash"
INITIAL_IMPORT_BATCH_SIZE = 100


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Gmail date header RFC2822
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def sync_inbox(db, account, oauth, query: str = "", max_results: int = 50, include_spam_trash: bool = False) -> dict:
    tl = UnifiedEmailToolLayer(db, account, oauth)
    threads, search_meta = _search_relevant_threads(
        db, tl, query=query, max_results=max_results, include_spam_trash=include_spam_trash
    )
    stored = store_thread_batch(db, account, threads)
    # update history id
    try:
        profile = tl._transport().get_profile()
        if profile.get("historyId"):
            account.history_id = profile["historyId"]
    except Exception as e:
        # A failed historyId update must not silently stall incremental sync —
        # log it so the operator can see the cursor stopped advancing.
        logger.warning("historyId update failed: %s", e)
    db.flush()
    return {
        "threads": len(threads),
        **stored,
        **search_meta,
    }


def store_thread_batch(db, account, threads) -> dict:
    """Idempotently persist one fetched Gmail thread batch.

    This performs the existing local direction/reconciliation work only. It
    does not run AI triage, create Contacts, create Drafts or send mail.
    """
    created_threads = 0
    created_msgs = 0
    for tdto in threads:
        # A database error must fail the whole page. The import worker commits
        # the page token only after this batch succeeds, so a resumed import
        # safely retries the same page instead of silently skipping a thread.
        ct, was_new = _upsert_thread(db, account, tdto)
        if was_new:
            created_threads += 1
        for mdto in tdto.messages:
            if _upsert_message(db, ct, mdto):
                created_msgs += 1
            if not mdto.is_incoming and mdto.gmail_message_id:
                attempts = db.query(models.DeliveryAttempt).filter_by(
                    gmail_account_id=account.id,
                    gmail_message_id=mdto.gmail_message_id,
                ).all()
                for attempt in attempts:
                    attempt.status = "verified"
                    attempt.sync_verified_at = datetime.now(timezone.utc)
        # detect human reply on the thread
        _detect_human_reply(db, account, ct)
        _reconcile_pending_approvals(db, account, ct)
        clear_stale_human_review(db, ct, owner_id=account.user_id, actor="system")
        # Re-derive the contact's reply state from the live thread direction so an
        # outbound reply pulled in by this sync flips needs_reply -> awaiting_reply
        # immediately, instead of relying on a later manual re-analyze.
        if ct.contact_email:
            _contact = db.query(models.Contact).filter_by(owner_id=account.user_id, email=ct.contact_email.lower()).first()
            if _contact is not None:
                recompute_contact_reply_state(db, _contact)
    db.flush()
    return {
        "new_threads": created_threads,
        "new_messages": created_msgs,
        "failures": 0,
    }


def initial_import_completed(db, account_id: int) -> bool:
    return db.query(models.GmailSyncRun.id).filter_by(
        gmail_account_id=account_id, kind="initial_full", status="completed"
    ).first() is not None


def _history_thread_ids(events: list[dict]) -> list[str]:
    """Extract unique affected thread IDs from all Gmail history event shapes."""
    seen: set[str] = set()
    ordered: list[str] = []

    def add(message):
        if not isinstance(message, dict):
            return
        thread_id = message.get("threadId")
        if thread_id and thread_id not in seen:
            seen.add(thread_id)
            ordered.append(thread_id)

    for event in events:
        if not isinstance(event, dict):
            continue
        for message in event.get("messages", []) or []:
            add(message)
        for key in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
            for wrapper in event.get(key, []) or []:
                add(wrapper.get("message") if isinstance(wrapper, dict) else None)
        # In-memory and narrow transport doubles may expose a direct threadId.
        add(event)
    return ordered


def sync_history(db, account, oauth, start_history_id: str) -> dict:
    """Replay Gmail History from a durable cursor and persist affected threads."""
    if not start_history_id:
        raise RuntimeError("initial_import_required")
    tl = UnifiedEmailToolLayer(db, account, oauth)
    page_token = None
    latest_history_id = start_history_id
    affected: list[str] = []
    seen: set[str] = set()
    history_pages = 0
    while True:
        events, latest, page_token = tl.list_history(
            start_history_id, page_token=page_token, agent="system", is_primary=True
        )
        history_pages += 1
        if latest:
            latest_history_id = str(latest)
        for thread_id in _history_thread_ids(events):
            if thread_id not in seen:
                seen.add(thread_id)
                affected.append(thread_id)
        if not page_token:
            break

    threads = []
    failures = 0
    for thread_id in affected:
        try:
            threads.append(tl.get_thread(thread_id, agent="system", is_primary=True))
        except (GmailTimeoutError, GmailTransientNetworkError):
            # Do not advance the durable history cursor after a transient
            # network timeout; the next run must retry the same change set.
            raise
        except Exception:
            failures += 1
            logger.warning("Could not fetch changed Gmail thread %s", thread_id)
    stored = store_thread_batch(db, account, threads)
    account.history_id = latest_history_id
    db.flush()
    return {
        "threads": len(threads),
        "affected_threads": len(affected),
        "history_pages": history_pages,
        "history_id": latest_history_id,
        "new_threads": stored["new_threads"],
        "new_messages": stored["new_messages"],
        "failures": failures + stored["failures"],
    }


def sync_incremental(db, account, oauth) -> dict:
    """Run the normal post-baseline sync using Gmail History only."""
    if not initial_import_completed(db, account.id):
        raise RuntimeError("initial_import_required")
    return sync_history(db, account, oauth, account.history_id)


def _search_relevant_threads(db, tl, query: str, max_results: int, include_spam_trash: bool = False):
    """Fetch normal inbox/search results plus active campaign-contact threads.

    The application may send first, then receive a reply in the same Gmail
    thread. Depending on labels and Gmail ordering, a narrow recent query can
    miss that thread. Add focused per-contact searches for
    active campaign contacts so the "sent -> reply -> sync" path is visible.
    """
    seen: set[str] = set()
    merged = []
    meta = {"contact_queries": 0, "sent_message_queries": 0}

    def add_results(q: str, limit: int):
        nonlocal merged
        found, _ = tl.search_threads(
            query=q, max_results=limit, agent="system", is_primary=True,
            include_spam_trash=include_spam_trash,
        )
        for tdto in found:
            if tdto.gmail_thread_id not in seen:
                seen.add(tdto.gmail_thread_id)
                merged.append(tdto)

    add_results(query, max_results)
    if query:
        return merged, meta

    rows = (
        db.query(models.Contact.email)
        .join(models.CampaignContact, models.CampaignContact.contact_id == models.Contact.id)
        .join(models.Campaign, models.Campaign.id == models.CampaignContact.campaign_id)
        .filter(models.Campaign.status.in_(("active", "paused")))
        .filter(models.Contact.email.isnot(None))
        .distinct()
        .limit(25)
        .all()
    )
    for (email,) in rows:
        if email:
            meta["contact_queries"] += 1
            add_results(f"{{from:{email} to:{email}}} newer_than:30d", 10)
    sent_rows = (
        db.query(models.CampaignContact.last_message_id)
        .filter(models.CampaignContact.last_message_id.isnot(None))
        .filter(models.CampaignContact.status.in_(("sent", "following_up", "replied")))
        .distinct()
        .limit(50)
        .all()
    )
    for (message_id,) in sent_rows:
        try:
            meta["sent_message_queries"] += 1
            msg = tl.get_message(message_id, agent="system", is_primary=True)
            if msg and msg.thread_id and msg.thread_id not in seen:
                tdto = tl.get_thread(msg.thread_id, agent="system", is_primary=True)
                if tdto.gmail_thread_id not in seen:
                    seen.add(tdto.gmail_thread_id)
                    merged.append(tdto)
        except Exception:
            # A single stale/invalid message_id must not abort the whole sync.
            logger.warning("Could not fetch sent Gmail thread for message_id=%s", message_id)
            continue
    return merged, meta


def _upsert_thread(db, account, tdto):
    existing = db.query(models.EmailThread).filter_by(
        gmail_account_id=account.id, gmail_thread_id=tdto.gmail_thread_id
    ).first()
    if existing:
        existing.gmail_history_id = tdto.history_id
        existing.subject = tdto.subject
        existing.snippet = tdto.snippet
        other = _other_email(account, tdto)
        if other and not existing.contact_email:
            existing.contact_email = other
        _link_campaign(db, existing, other or existing.contact_email)
        return existing, False
    other = _other_email(account, tdto)
    ct = models.EmailThread(
        gmail_account_id=account.id,
        gmail_thread_id=tdto.gmail_thread_id,
        gmail_history_id=tdto.history_id,
        contact_email=other,
        subject=tdto.subject,
        snippet=tdto.snippet,
        has_human_reply=False,
    )
    db.add(ct)
    db.flush()
    _link_campaign(db, ct, other)
    return ct, True


def _other_email(account, tdto) -> Optional[str]:
    for m in tdto.messages:
        if m.is_incoming and m.from_email:
            return m.from_email.lower()
    for m in tdto.messages:
        if not m.is_incoming and m.to_email:
            return m.to_email.lower()
    return None


def _link_campaign(db, thread, email: Optional[str]) -> None:
    if not email:
        return
    cc = (
        db.query(models.CampaignContact)
        .join(models.Contact, models.Contact.id == models.CampaignContact.contact_id)
        .filter(models.Contact.email == email.lower())
        .order_by(models.CampaignContact.id.desc())
        .first()
    )
    if cc:
        thread.campaign_id = cc.campaign_id


def _upsert_message(db, thread, mdto) -> bool:
    existing = db.query(models.EmailMessage).filter_by(gmail_message_id=mdto.gmail_message_id).first()
    if existing:
        repaired_fields = []
        for field in ("message_id_header", "in_reply_to_header", "references_header"):
            if not getattr(existing, field, None) and getattr(mdto, field, None):
                setattr(existing, field, getattr(mdto, field))
        for field in ("subject", "snippet", "body_text", "body_html"):
            current = getattr(existing, field, None)
            refreshed = getattr(mdto, field, None)
            if _looks_corrupt_text(current) and refreshed and not _looks_corrupt_text(refreshed):
                setattr(existing, field, refreshed)
                repaired_fields.append(field)
        if repaired_fields:
            if "subject" in repaired_fields:
                thread.subject = existing.subject
            if "snippet" in repaired_fields:
                thread.snippet = existing.snippet
            if existing.is_incoming:
                thread.intent = None
                thread.last_agent_summary = None
                thread.pending_action = None
            db.add(models.AuditLog(
                actor="system",
                action="gmail_mime_decode_repaired",
                entity="email_message",
                entity_id=str(existing.id),
                detail=json.dumps({
                    "gmail_message_id": mdto.gmail_message_id,
                    "thread_id": thread.id,
                    "fields": repaired_fields,
                }, ensure_ascii=False),
            ))
        return False
    msg = models.EmailMessage(
        thread_id=thread.id,
        gmail_message_id=mdto.gmail_message_id,
        gmail_history_id=mdto.history_id,
        message_id_header=mdto.message_id_header,
        in_reply_to_header=mdto.in_reply_to_header,
        references_header=mdto.references_header,
        from_email=mdto.from_email,
        to_email=mdto.to_email,
        subject=mdto.subject,
        snippet=mdto.snippet,
        body_text=mdto.body_text,
        body_html=mdto.body_html,
        is_incoming=mdto.is_incoming,
        received_at=_parse_dt(mdto.received_at),
        attachments_meta=json.dumps(mdto.attachments_meta, ensure_ascii=False) if mdto.attachments_meta else None,
    )
    db.add(msg)
    db.flush()
    if mdto.is_incoming:
        # A new human-facing inbound event invalidates the previous thread
        # decision. The next Inbox sort must reassess the conversation and
        # update Contact tags/stage from the latest message.
        thread.intent = None
        thread.last_agent_summary = None
        thread.pending_action = None
    return True


def repair_stored_mail_bodies(db, *, limit: int = 10_000) -> int:
    """Repair local display text polluted by HTML/CSS boilerplate.

    The original HTML stays intact. This is idempotent and does not call Gmail,
    so an upgraded Workspace can read historical mail safely without re-syncing.
    """
    repaired = 0
    rows = (
        db.query(models.EmailMessage)
        .filter(models.EmailMessage.body_html.isnot(None))
        .order_by(models.EmailMessage.id.asc())
        .limit(limit)
        .all()
    )
    for message in rows:
        if not _looks_like_html_or_css_residue(message.body_text):
            continue
        readable = readable_email_text(message.body_text, message.body_html)
        if not readable or readable == (message.body_text or ""):
            continue
        message.body_text = readable
        if _looks_like_html_or_css_residue(message.snippet):
            message.snippet = readable[:200]
        db.add(models.AuditLog(
            actor="system", action="gmail_html_body_repaired", entity="email_message",
            entity_id=str(message.id),
            detail=json.dumps({"source": "local_html", "display_text_repaired": True}), success=True,
        ))
        repaired += 1
    if repaired:
        db.commit()
    return repaired


def _normalise_mail_text(value: Optional[str]) -> str:
    return " ".join((value or "").casefold().split())


def _reconcile_pending_approvals(db, account, thread) -> int:
    """Expire stale pending replies when Gmail already contains the exact outbound.

    Matching is deliberately narrow: same local Gmail thread, same recipient and
    whitespace/case-normalised plain-text body. This cannot approve or send a
    draft; it only prevents an already-sent reply from remaining actionable.
    """
    pending = db.query(models.Approval).filter_by(thread_id=thread.id, status="pending").all()
    if not pending:
        return 0
    outbound = db.query(models.EmailMessage).filter_by(thread_id=thread.id, is_incoming=False).all()
    reconciled = 0
    account_email = (account.email or "").casefold()
    for approval in pending:
        expected_body = _normalise_mail_text(approval.body_text)
        expected_recipient = (approval.to_email or "").casefold()
        if not expected_body or not expected_recipient:
            continue
        matched = next((message for message in outbound if
            (message.from_email or "").casefold() == account_email
            and (message.to_email or "").casefold() == expected_recipient
            and _normalise_mail_text(message.body_text) == expected_body), None)
        if not matched:
            continue
        approval.status = "expired"
        approval.decided_by = "system:sync_reconciliation"
        approval.decided_at = datetime.now(timezone.utc)
        approval.rejection_reason = "reconciled: matching outbound message found during Gmail sync"
        if approval.draft_id:
            draft = db.get(models.EmailDraft, approval.draft_id)
            if draft and draft.status in ("draft", "approved"):
                draft.status = "cancelled"
        db.add(models.AuditLog(
            actor="system",
            action="approval_reconciled_sent",
            entity="approval",
            entity_id=str(approval.id),
            detail=f"thread_id={thread.id}; gmail_message_id={matched.gmail_message_id or ''}",
        ))
        reconciled += 1
    return reconciled

def _detect_human_reply(db, account, thread):
    """Mark a reply only after the sender clears the same human gate as Inbox."""
    latest = (
        db.query(models.EmailMessage)
        .filter_by(thread_id=thread.id, is_incoming=True)
        .order_by(models.EmailMessage.received_at.desc(), models.EmailMessage.id.desc())
        .first()
    )
    if not latest:
        thread.has_human_reply = False
        return
    contact = (
        db.query(models.Contact).filter_by(email=(thread.contact_email or "").lower()).first()
        if thread.contact_email else None
    )
    # This hook controls campaign automation. Unknown inbound mail is classified
    # by Inbox later, but cannot stop a campaign merely because it arrived.
    if not contact or not thread.campaign_id:
        thread.has_human_reply = False
        return
    triage = assess_inbound(
        is_incoming=True,
        from_email=latest.from_email,
        subject=latest.subject,
        body=latest.body_text,
        known_relationship=True,
        db=db,
    )
    if triage.is_human is not True:
        thread.has_human_reply = False
        return
    thread.has_human_reply = True
    if contact:
        # update contact status
        if contact.status in ("new", "contacted", "following_up"):
            contact.status = "replied"
            contact.next_follow_up_at = None
        q = db.query(models.CampaignContact).filter_by(contact_id=contact.id)
        q = q.filter_by(campaign_id=thread.campaign_id)
        for cc in q.all():
            if cc.status not in ("stopped", "done", "bounced"):
                cc.status = "replied"
        # Scope follow-up cancellation to THIS campaign only — cancelling every
        # scheduled FollowUpTask for the contact across all campaigns was a
        # cross-campaign pollution bug (one reply stopped unrelated campaigns).
        db.query(models.FollowUpTask).filter_by(
            contact_id=contact.id, campaign_id=thread.campaign_id, status="scheduled"
        ).update({"status": "cancelled"})
