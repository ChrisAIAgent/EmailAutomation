"""Unified Email Tool Layer.

Agents MUST NOT call the Gmail SDK directly. Every read/write goes through here.
Each write tool is gated by the Policy Engine and logged in tool_executions.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from .. import models
from ..config import get_settings
from ..gmail import get_transport_for_account
from ..gmail.transport import GmailTimeoutError
from ..security import json_safe
from ..policy.engine import PolicyContext, evaluate
from ..services.real_send import is_real_send_enabled


class UnifiedEmailToolLayer:
    def __init__(self, db, gmail_account, oauth_row):
        self.db = db
        self.account = gmail_account
        self.oauth = oauth_row
        self.settings = get_settings()

    # ---- transport ----
    def _transport(self):
        if getattr(self, "_cached_transport", None) is None:
            self._cached_transport = get_transport_for_account(self.account, self.oauth)
        return self._cached_transport

    # ---- logging ----
    def _record(self, *, tool_name, agent, mode, is_primary, campaign_id, idem, allowed, blocked_reason, status, latency_ms, error=None, request_json=None, response_meta=None):
        row = models.ToolExecution(
            tool_name=tool_name,
            agent=agent,
            mode=mode,
            is_primary=is_primary,
            campaign_id=campaign_id,
            gmail_account_id=self.account.id,
            idempotency_key=idem,
            allowed=allowed,
            blocked_reason=blocked_reason,
            status=status,
            latency_ms=latency_ms,
            error=error,
            request_json=request_json,
            response_meta=response_meta,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _policy(self, tool_name, agent, mode, is_primary, to_email=None, campaign_id=None,
                thread_id=None, idem=None, approval_id=None, requires_approval=True,
                kind=None):
        ctx = PolicyContext(
            agent=agent, mode=mode, is_primary=is_primary, tool_name=tool_name,
            to_email=to_email, campaign_id=campaign_id, thread_id=thread_id,
            idempotency_key=idem, approval_id=approval_id, requires_approval=requires_approval,
            kind=kind,
        )
        return evaluate(self.db, ctx), ctx

    # ---- READ tools (allowed for both agents) ----
    def search_threads(self, query, max_results=20, agent="system", mode=None,
                       is_primary=True, campaign_id=None, include_spam_trash=False,
                       page_token=None):
        t0 = time.time()
        try:
            transport = self._transport()
            try:
                threads, nxt = transport.list_threads(
                    query, max_results, page_token=page_token,
                    include_spam_trash=include_spam_trash,
                )
            except TypeError:
                # Preserve compatibility with narrow transport doubles used by
                # older integrations; resumable production transports accept
                # page_token and include_spam_trash.
                threads, nxt = transport.list_threads(query, max_results)
            status = "ok"
            err = None
            meta = json_safe({"count": len(threads), "next": nxt})
        except GmailTimeoutError:
            # A hard Gmail network timeout is NOT a "no results" situation: re-raise
            # it so the caller (run_tick) can mark the AutomationRun partial/failed
            # with the gmail_timeout reason and the worker can move on. We still
            # record the failed execution for audit first.
            self._record(tool_name="search_threads", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None,
                         status="failed", latency_ms=int((time.time() - t0) * 1000),
                         error="gmail_timeout", response_meta=None)
            raise
        except Exception as e:
            status = "failed"
            err = str(e)[:500]
            self._record(tool_name="search_threads", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None,
                         status=status, latency_ms=int((time.time() - t0) * 1000), error=err, response_meta=None)
            raise
        self._record(tool_name="search_threads", agent=agent, mode=mode, is_primary=is_primary,
                     campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None,
                     status=status, latency_ms=int((time.time() - t0) * 1000), error=err, response_meta=meta)
        return threads, nxt

    def get_thread(self, gmail_thread_id, agent="system", mode=None, is_primary=True, campaign_id=None):
        t0 = time.time()
        try:
            th = self._transport().get_thread(gmail_thread_id)
            status = "ok"; err = None
            meta = json_safe({"messages": len(th.messages)})
        except GmailTimeoutError:
            self._record(tool_name="get_thread", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None,
                         status="failed", latency_ms=int((time.time() - t0) * 1000),
                         error="gmail_timeout", response_meta=None)
            raise
        except Exception as e:
            status = "failed"; err = str(e)[:500]
            self._record(tool_name="get_thread", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None,
                         status=status, latency_ms=int((time.time() - t0) * 1000), error=err, response_meta=None)
            raise
        self._record(tool_name="get_thread", agent=agent, mode=mode, is_primary=is_primary,
                     campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None,
                     status=status, latency_ms=int((time.time() - t0) * 1000), error=err, response_meta=meta)
        return th

    def get_message(self, gmail_message_id, agent="system", mode=None, is_primary=True, campaign_id=None):
        t0 = time.time()
        try:
            msg = self._transport().get_message(gmail_message_id)
            status = "ok"; err = None; meta = json_safe({"subject": msg.subject})
        except GmailTimeoutError:
            self._record(tool_name="get_message", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None,
                         status="failed", latency_ms=int((time.time() - t0) * 1000),
                         error="gmail_timeout", response_meta=None)
            raise
        except Exception as e:
            status = "failed"; err = str(e)[:500]
            self._record(tool_name="get_message", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None,
                         status=status, latency_ms=int((time.time() - t0) * 1000), error=err, response_meta=None)
            raise
        self._record(tool_name="get_message", agent=agent, mode=mode, is_primary=is_primary,
                     campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None,
                     status=status, latency_ms=int((time.time() - t0) * 1000), error=err, response_meta=meta)
        return msg

    def list_history(self, start_history_id, page_token=None, agent="system",
                     mode=None, is_primary=True, campaign_id=None):
        t0 = time.time()
        try:
            events, latest, nxt = self._transport().list_history(
                start_history_id, page_token=page_token
            )
            status = "ok"; err = None
            meta = json_safe({"count": len(events), "next": bool(nxt)})
        except Exception as e:
            status = "failed"; err = str(e)[:500]
            self._record(tool_name="list_history", agent=agent, mode=mode,
                         is_primary=is_primary, campaign_id=campaign_id,
                         idem=None, allowed=True, blocked_reason=None,
                         status=status, latency_ms=int((time.time() - t0) * 1000),
                         error=err, response_meta=None)
            raise
        self._record(tool_name="list_history", agent=agent, mode=mode,
                     is_primary=is_primary, campaign_id=campaign_id,
                     idem=None, allowed=True, blocked_reason=None,
                     status=status, latency_ms=int((time.time() - t0) * 1000),
                     error=err, response_meta=meta)
        return events, latest, nxt

    # ---- WRITE tools (gated) ----
    def create_draft(self, *, to, subject, body_text, body_html="", thread_gmail_id=None,
                     in_reply_to=None, references=None, agent="langgraph", mode=None,
                     is_primary=True, campaign_id=None, campaign_contact_id=None, kind="outreach",
                     idempotency_key=None):
        t0 = time.time()
        res_pol, ctx = self._policy("create_draft", agent, mode, is_primary, to_email=to,
                                    campaign_id=campaign_id, idem=idempotency_key)
        if not res_pol.allowed:
            self._record(tool_name="create_draft", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=False,
                         blocked_reason=res_pol.reason, status="blocked",
                         latency_ms=int((time.time() - t0) * 1000))
            return {"ok": False, "blocked": res_pol.reason}
        try:
            r = self._transport().create_draft(to, subject, body_text, body_html, thread_gmail_id, in_reply_to, references)
            # persist our draft record
            draft = models.EmailDraft(
                gmail_account_id=self.account.id,
                gmail_draft_id=r.get("id"),
                campaign_contact_id=campaign_contact_id,
                thread_id=None,
                to_email=to, subject=subject, body_text=body_text, body_html=body_html,
                kind=kind, agent=agent, idempotency_key=idempotency_key, status="draft",
            )
            self.db.add(draft)
            self.db.flush()
            self._record(tool_name="create_draft", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=True, blocked_reason=None,
                         status="ok", latency_ms=int((time.time() - t0) * 1000),
                         response_meta=json_safe({"draft_id": r.get("id"), "db_id": draft.id}))
            return {"ok": True, "draft": draft, "gmail_draft_id": r.get("id")}
        except Exception as e:
            self._record(tool_name="create_draft", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=True, blocked_reason=None,
                         status="failed", latency_ms=int((time.time() - t0) * 1000), error=str(e)[:500])
            return {"ok": False, "error": str(e)[:500]}

    def update_draft(self, *, draft_db_id, to, subject, body_text, body_html="",
                     thread_gmail_id=None, in_reply_to=None, references=None,
                     agent="langgraph", mode=None, is_primary=True, campaign_id=None,
                     idempotency_key=None):
        t0 = time.time()
        res_pol, ctx = self._policy("update_draft", agent, mode, is_primary, to_email=to,
                                    campaign_id=campaign_id, idem=idempotency_key)
        if not res_pol.allowed:
            self._record(tool_name="update_draft", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=False,
                         blocked_reason=res_pol.reason, status="blocked",
                         latency_ms=int((time.time() - t0) * 1000))
            return {"ok": False, "blocked": res_pol.reason}
        draft = self.db.get(models.EmailDraft, draft_db_id)
        if not draft:
            return {"ok": False, "error": "draft not found"}
        # A local Draft without its Gmail identifier cannot be updated in place.
        # Do not hand None to the Gmail client: its "missing id" response is
        # ambiguous and can lead an operator to discard otherwise valid work.
        if not (draft.gmail_draft_id or "").strip():
            error = "draft_remote_id_missing"
            self._record(tool_name="update_draft", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=True, blocked_reason=None,
                         status="failed", latency_ms=int((time.time() - t0) * 1000), error=error)
            return {"ok": False, "error": error}
        try:
            self._transport().update_draft(
                draft.gmail_draft_id, to, subject, body_text, body_html,
                thread_gmail_id, in_reply_to, references,
            )
            draft.subject = subject; draft.body_text = body_text; draft.body_html = body_html
            self.db.flush()
            self._record(tool_name="update_draft", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=True, blocked_reason=None,
                         status="ok", latency_ms=int((time.time() - t0) * 1000))
            return {"ok": True, "draft": draft}
        except Exception as e:
            self._record(tool_name="update_draft", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=True, blocked_reason=None,
                         status="failed", latency_ms=int((time.time() - t0) * 1000), error=str(e)[:500])
            return {"ok": False, "error": str(e)[:500]}

    def send_approved_draft(self, *, draft_db_id, approval_id, agent="langgraph", mode=None,
                            is_primary=True, campaign_id=None, thread_db_id=None,
                            idempotency_key=None, requires_approval=True):
        t0 = time.time()
        draft = self.db.get(models.EmailDraft, draft_db_id)
        if not draft:
            return {"ok": False, "error": "draft not found"}
        # Master safety: a verified customer-owned Gmail OAuth connection is the
        # persisted real-send authorization.  Disconnecting it blocks delivery
        # immediately; no manual environment toggle can make an in-memory
        # transport look like a sent message.
        if not is_real_send_enabled(self.settings, self.account, self.oauth):
            self._record(tool_name="send_approved_draft", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=False,
                         blocked_reason="draft-only mode (no connected real Gmail account)", status="blocked",
                         latency_ms=int((time.time() - t0) * 1000))
            # Structured code so any caller (API or Agent) can branch on the
            # honest "real send disabled" state instead of string-matching text.
            code = "NO_REAL_GMAIL" if self.settings.ENABLE_REAL_SEND else "DRAFT_ONLY_MODE"
            return {"ok": False, "blocked": "draft-only mode (no connected real Gmail account)",
                    "code": code}
        # Honesty guard: enabling real send without a REAL Gmail connection would
        # route the mail to the in-memory dev double and "fake" a delivery. Block it
        # and let the UI surface the missing configuration instead.
        from ..gmail.client import RealGmailTransport
        if not isinstance(self._transport(), RealGmailTransport):
            self._record(tool_name="send_approved_draft", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=False,
                         blocked_reason="ENABLE_REAL_SEND=true but no real Gmail account connected", status="blocked",
                         latency_ms=int((time.time() - t0) * 1000))
            return {"ok": False, "blocked": "ENABLE_REAL_SEND=true but no real Gmail account connected",
                    "code": "NO_REAL_GMAIL"}
        res_pol, ctx = self._policy("send_approved_draft", agent, mode, is_primary,
                                    to_email=draft.to_email, campaign_id=campaign_id, thread_id=thread_db_id,
                                    idem=idempotency_key, approval_id=approval_id,
                                    requires_approval=requires_approval, kind=draft.kind)
        if not res_pol.allowed:
            self._record(tool_name="send_approved_draft", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=False,
                         blocked_reason=res_pol.reason, status="blocked",
                         latency_ms=int((time.time() - t0) * 1000))
            return {"ok": False, "blocked": res_pol.reason}
        attempt = None
        if idempotency_key:
            attempt = self.db.query(models.DeliveryAttempt).filter_by(
                idempotency_key=idempotency_key
            ).first()
        if attempt is None:
            attempt = models.DeliveryAttempt(
                approval_id=approval_id,
                draft_id=draft.id,
                thread_id=thread_db_id,
                gmail_account_id=self.account.id,
                idempotency_key=idempotency_key,
                gmail_draft_id=draft.gmail_draft_id,
            )
            self.db.add(attempt)
        attempt.status = "sending"
        attempt.send_requested_at = datetime.now(timezone.utc)
        attempt.last_error = None
        self.db.flush()
        try:
            r = self._transport().send_draft(draft.gmail_draft_id)
            draft.status = "sent"
            draft.updated_at = datetime.now(timezone.utc)
            msg_id = r.get("id")
            gmail_thread_id = r.get("threadId")
            expected_thread_id = None
            if thread_db_id:
                expected_thread = self.db.get(models.EmailThread, thread_db_id)
                expected_thread_id = expected_thread.gmail_thread_id if expected_thread else None
            thread_match = (
                gmail_thread_id == expected_thread_id
                if gmail_thread_id and expected_thread_id
                else None
            )
            attempt.status = "gmail_sent"
            attempt.gmail_message_id = msg_id
            attempt.gmail_accepted_at = datetime.now(timezone.utc)
            # mark contact/campaign contact
            if draft.campaign_contact_id:
                cc = self.db.get(models.CampaignContact, draft.campaign_contact_id)
                if cc:
                    cc.status = "sent"
                    cc.last_message_id = msg_id
                    cc.thread_id = thread_db_id
                    contact = self.db.get(models.Contact, cc.contact_id)
                    if contact:
                        contact.status = "contacted"
                        contact.last_contacted_at = datetime.now(timezone.utc)
                        # A reply we just sent closes the open needs_reply loop: the
                        # contact now awaits the customer's next message, not the other
                        # way around.  This mirrors the inbox-reply approve path in
                        # services/approvals.py and keeps Dashboard/needs_reply accurate.
                        if (draft.kind == "reply" or thread_db_id) and not contact.manual_lock:
                            contact.next_action = "waiting_for_customer"
                            contact.lifecycle_stage = "awaiting_reply"
            self.db.flush()
            self._record(tool_name="send_approved_draft", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=True, blocked_reason=None,
                         status="ok", latency_ms=int((time.time() - t0) * 1000),
                         response_meta=json_safe({"message_id": msg_id, "thread_id": gmail_thread_id,
                                                  "expected_thread_id": expected_thread_id,
                                                  "thread_match": thread_match}))
            return {"ok": True, "message_id": msg_id, "thread_id": gmail_thread_id,
                    "expected_thread_id": expected_thread_id, "thread_match": thread_match,
                    "draft": draft}
        except Exception as e:
            attempt.status = "unknown" if isinstance(e, GmailTimeoutError) else "failed"
            attempt.last_error = str(e)[:500]
            status_code = getattr(e, "status_code", None)
            if status_code == 404:
                draft.status = "failed"
                approval = self.db.get(models.Approval, approval_id) if approval_id else None
                if approval and approval.status in {"pending", "approved"}:
                    approval.status = "expired"
                    approval.rejection_reason = "gmail_draft_not_found"
                    approval.decided_by = "system:gmail_reconciliation"
                    approval.decided_at = datetime.now(timezone.utc)
                self.db.add(models.AuditLog(
                    actor="system", action="gmail_draft_invalidated",
                    entity="email_draft", entity_id=str(draft.id),
                    detail="Remote Gmail draft was deleted or is no longer accessible.",
                ))
                self.db.flush()
            self._record(tool_name="send_approved_draft", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=True, blocked_reason=None,
                         status="failed", latency_ms=int((time.time() - t0) * 1000), error=str(e)[:500])
            return {"ok": False, "error": "gmail_draft_not_found" if status_code == 404 else str(e)[:500]}

    # ---- labels / archive (Gmail writes) ----
    def add_label(self, gmail_thread_id, label, agent="langgraph", mode=None, is_primary=True, campaign_id=None):
        t0 = time.time()
        res_pol, _ = self._policy("add_label", agent, mode, is_primary, campaign_id=campaign_id)
        if not res_pol.allowed:
            self._record(tool_name="add_label", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=False, blocked_reason=res_pol.reason,
                         status="blocked", latency_ms=int((time.time() - t0) * 1000))
            return {"ok": False, "blocked": res_pol.reason}
        try:
            self._transport().add_label(gmail_thread_id, label)
            self._record(tool_name="add_label", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None, status="ok",
                         latency_ms=int((time.time() - t0) * 1000))
            return {"ok": True}
        except Exception as e:
            self._record(tool_name="add_label", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None,
                         status="failed", latency_ms=int((time.time() - t0) * 1000), error=str(e)[:500])
            return {"ok": False, "error": str(e)[:500]}

    def remove_label(self, gmail_thread_id, label, agent="langgraph", mode=None, is_primary=True, campaign_id=None):
        t0 = time.time()
        res_pol, _ = self._policy("remove_label", agent, mode, is_primary, campaign_id=campaign_id)
        if not res_pol.allowed:
            self._record(tool_name="remove_label", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=False, blocked_reason=res_pol.reason,
                         status="blocked", latency_ms=int((time.time() - t0) * 1000))
            return {"ok": False, "blocked": res_pol.reason}
        try:
            self._transport().remove_label(gmail_thread_id, label)
            self._record(tool_name="remove_label", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None, status="ok",
                         latency_ms=int((time.time() - t0) * 1000))
            return {"ok": True}
        except Exception as e:
            self._record(tool_name="remove_label", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None,
                         status="failed", latency_ms=int((time.time() - t0) * 1000), error=str(e)[:500])
            return {"ok": False, "error": str(e)[:500]}

    def archive_thread(self, gmail_thread_id, agent="langgraph", mode=None, is_primary=True, campaign_id=None):
        t0 = time.time()
        res_pol, _ = self._policy("archive_thread", agent, mode, is_primary, campaign_id=campaign_id)
        if not res_pol.allowed:
            self._record(tool_name="archive_thread", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=False, blocked_reason=res_pol.reason,
                         status="blocked", latency_ms=int((time.time() - t0) * 1000))
            return {"ok": False, "blocked": res_pol.reason}
        try:
            self._transport().archive_thread(gmail_thread_id)
            self._record(tool_name="archive_thread", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None, status="ok",
                         latency_ms=int((time.time() - t0) * 1000))
            return {"ok": True}
        except Exception as e:
            self._record(tool_name="archive_thread", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None,
                         status="failed", latency_ms=int((time.time() - t0) * 1000), error=str(e)[:500])
            return {"ok": False, "error": str(e)[:500]}

    # ---- follow-up scheduling (our DB state) ----
    def schedule_follow_up(self, *, campaign_contact_id, contact_id, campaign_id, thread_db_id,
                           sequence, scheduled_at, agent="langgraph", mode=None, is_primary=True,
                           idempotency_key=None):
        t0 = time.time()
        res_pol, _ = self._policy("schedule_follow_up", agent, mode, is_primary, campaign_id=campaign_id,
                                  thread_id=thread_db_id, idem=idempotency_key)
        if not res_pol.allowed:
            self._record(tool_name="schedule_follow_up", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=idempotency_key, allowed=False,
                         blocked_reason=res_pol.reason, status="blocked",
                         latency_ms=int((time.time() - t0) * 1000))
            return {"ok": False, "blocked": res_pol.reason}
        task = models.FollowUpTask(
            campaign_contact_id=campaign_contact_id, contact_id=contact_id, campaign_id=campaign_id,
            thread_id=thread_db_id, sequence=sequence, scheduled_at=scheduled_at,
            status="scheduled", idempotency_key=idempotency_key, agent=agent,
        )
        self.db.add(task)
        self.db.flush()
        # mark contact.next_follow_up_at
        contact = self.db.get(models.Contact, contact_id)
        if contact:
            contact.next_follow_up_at = scheduled_at
        self._record(tool_name="schedule_follow_up", agent=agent, mode=mode, is_primary=is_primary,
                     campaign_id=campaign_id, idem=idempotency_key, allowed=True, blocked_reason=None,
                     status="ok", latency_ms=int((time.time() - t0) * 1000),
                     response_meta=json_safe({"task_id": task.id}))
        return {"ok": True, "task": task}

    def cancel_follow_up(self, *, task_id, agent="langgraph", mode=None, is_primary=True, campaign_id=None):
        t0 = time.time()
        res_pol, _ = self._policy("cancel_follow_up", agent, mode, is_primary, campaign_id=campaign_id)
        if not res_pol.allowed:
            self._record(tool_name="cancel_follow_up", agent=agent, mode=mode, is_primary=is_primary,
                         campaign_id=campaign_id, idem=None, allowed=False, blocked_reason=res_pol.reason,
                         status="blocked", latency_ms=int((time.time() - t0) * 1000))
            return {"ok": False, "blocked": res_pol.reason}
        task = self.db.get(models.FollowUpTask, task_id)
        if not task:
            return {"ok": False, "error": "task not found"}
        task.status = "cancelled"
        self.db.flush()
        self._record(tool_name="cancel_follow_up", agent=agent, mode=mode, is_primary=is_primary,
                     campaign_id=campaign_id, idem=None, allowed=True, blocked_reason=None, status="ok",
                     latency_ms=int((time.time() - t0) * 1000))
        return {"ok": True, "task": task}
