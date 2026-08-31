from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app import models
from app.api import inbox as inbox_api
from app.gmail.transport import MessageDTO, ThreadDTO
from app.services import sync as sync_svc
from app.services.inbox_triage import TriageResult


def _msg(mid: str, *, from_email: str, to_email: str, incoming: bool) -> MessageDTO:
    return MessageDTO(
        gmail_message_id=mid,
        thread_id="gmail-thread-1",
        history_id="h1",
        from_email=from_email,
        to_email=to_email,
        subject="Re: Email Automation Solutions for TAC",
        snippet="Interested",
        body_text="I am interested. Please send more details.",
        body_html="",
        is_incoming=incoming,
        received_at="Wed, 22 Jul 2026 10:00:00 +0800",
    )


def test_sync_persists_delivery_attempt_verification_after_normal_completion(db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    db.add(account)
    db.flush()
    attempt = models.DeliveryAttempt(
        gmail_account_id=account.id,
        idempotency_key="delivery-attempt-verification",
        gmail_message_id="outbound-verified-1",
        status="gmail_sent",
    )
    db.add(attempt)
    db.commit()
    tdto = ThreadDTO(
        gmail_thread_id="delivery-attempt-thread",
        history_id="h1",
        subject="Original subject",
        snippet="Sent reply",
        messages=[_msg("outbound-verified-1", from_email=account.email, to_email="customer@example.com", incoming=False)],
    )

    class FakeToolLayer:
        def __init__(self, *_args, **_kwargs):
            pass

        def search_threads(self, **_kwargs):
            return [tdto], None

        def _transport(self):
            return SimpleNamespace(get_profile=lambda: {"historyId": "h2"})

    with patch("app.services.sync.UnifiedEmailToolLayer", FakeToolLayer):
        sync_svc.sync_inbox(db, account, oauth=None, max_results=10)
    db.commit()
    db.expire_all()
    verified = db.get(models.DeliveryAttempt, attempt.id)
    assert verified.status == "verified"
    assert verified.sync_verified_at is not None


def test_sync_clears_stale_review_when_latest_message_is_outbound(db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    contact = models.Contact(owner_id=1, email="customer@example.com", first_name="Known")
    db.add_all([account, contact])
    db.flush()
    thread = models.EmailThread(
        gmail_account_id=account.id, gmail_thread_id="sync-stale-review",
        contact_email=contact.email, intent="interested", pending_action="human_review",
    )
    db.add(thread)
    db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id, gmail_message_id="old-inbound", from_email=contact.email,
        to_email=account.email, subject="Question", body_text="Please share details", is_incoming=True,
    ))
    db.commit()
    tdto = ThreadDTO(
        gmail_thread_id="sync-stale-review", history_id="h2", subject="Question", snippet="Sent reply",
        messages=[_msg("new-outbound", from_email=account.email, to_email=contact.email, incoming=False)],
    )

    class FakeToolLayer:
        def __init__(self, *_args, **_kwargs):
            pass

        def search_threads(self, **_kwargs):
            return [tdto], None

        def _transport(self):
            return SimpleNamespace(get_profile=lambda: {"historyId": "h2"})

    with patch("app.services.sync.UnifiedEmailToolLayer", FakeToolLayer):
        sync_svc.sync_inbox(db, account, oauth=None, max_results=10)
    db.commit()
    db.refresh(thread)
    assert thread.pending_action == "no_action"
    assert db.query(models.AuditLog).filter_by(action="stale_human_review_cleared", entity_id=str(thread.id)).one()


def test_sync_reply_links_campaign_and_cancels_followup(db):
    account = models.GmailAccount(user_id=1, email="tac.aisolution@gmail.com", is_connected=True)
    campaign = models.Campaign(owner_id=1, name="TAC test", status="active")
    contact = models.Contact(owner_id=1, email="pyx1171898390@gmail.com", status="contacted")
    db.add_all([account, campaign, contact])
    db.flush()

    cc = models.CampaignContact(campaign_id=campaign.id, contact_id=contact.id, status="sent")
    db.add(cc)
    db.flush()
    task = models.FollowUpTask(
        campaign_contact_id=cc.id,
        contact_id=contact.id,
        campaign_id=campaign.id,
        sequence=1,
        scheduled_at=datetime.now(timezone.utc),
        status="scheduled",
    )
    db.add(task)
    db.flush()

    tdto = ThreadDTO(
        gmail_thread_id="gmail-thread-1",
        history_id="h2",
        subject="Re: Email Automation Solutions for TAC",
        snippet="Interested",
        messages=[
            _msg("sent-1", from_email=account.email, to_email=contact.email, incoming=False),
            _msg("reply-1", from_email=contact.email, to_email=account.email, incoming=True),
        ],
    )

    thread, was_new = sync_svc._upsert_thread(db, account, tdto)
    for mdto in tdto.messages:
        sync_svc._upsert_message(db, thread, mdto)
    with patch("app.services.sync.assess_inbound", return_value=TriageResult("human", True, 0.99)):
        sync_svc._detect_human_reply(db, account, thread)
    db.flush()

    assert was_new is True
    assert thread.contact_email == contact.email
    assert thread.campaign_id == campaign.id
    assert thread.has_human_reply is True
    assert contact.status == "replied"
    assert cc.status == "replied"
    assert task.status == "cancelled"


def test_sync_does_not_stop_campaign_for_filtered_inbound_mail(db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    campaign = models.Campaign(owner_id=1, name="C", status="active")
    contact = models.Contact(owner_id=1, email="customer@example.com", status="contacted")
    db.add_all([account, campaign, contact])
    db.flush()
    cc = models.CampaignContact(campaign_id=campaign.id, contact_id=contact.id, status="sent")
    db.add(cc)
    thread = models.EmailThread(
        gmail_account_id=account.id, gmail_thread_id="system-thread", campaign_id=campaign.id,
        contact_email=contact.email,
    )
    db.add(thread)
    db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id, gmail_message_id="system-message", from_email=contact.email,
        to_email=account.email, subject="Notification", body_text="Automated notice", is_incoming=True,
    ))
    db.flush()

    with patch("app.services.sync.assess_inbound", return_value=TriageResult("system", False, 0.99)):
        sync_svc._detect_human_reply(db, account, thread)

    assert thread.has_human_reply is False
    assert cc.status == "sent"


def test_inbox_summary_surfaces_stopped_campaign_contact(db):
    account = models.GmailAccount(user_id=1, email="tac@example.com", is_connected=True)
    campaign = models.Campaign(owner_id=1, name="C", status="active")
    contact = models.Contact(owner_id=1, email="optout@example.com")
    db.add_all([account, campaign, contact])
    db.flush()
    db.add(models.CampaignContact(campaign_id=campaign.id, contact_id=contact.id, status="stopped"))
    thread = models.EmailThread(
        gmail_account_id=account.id,
        gmail_thread_id="optout-thread",
        campaign_id=campaign.id,
        contact_email=contact.email,
        intent="opt_out",
    )
    db.add(thread)
    db.flush()

    summary = inbox_api._thread_summary(db, thread)
    assert summary["outreach_status"] == "stopped"


def test_new_inbound_message_reopens_thread_for_triage(db):
    account = models.GmailAccount(
        user_id=1, email="owner@example.com", is_connected=True
    )
    thread = models.EmailThread(
        gmail_account_id=1,
        gmail_thread_id="retriage-thread",
        contact_email="person@example.com",
        intent="interested",
        last_agent_summary="Old decision",
        pending_action="reply",
    )
    db.add_all([account, thread])
    db.flush()
    incoming = _msg(
        "new-inbound",
        from_email="person@example.com",
        to_email=account.email,
        incoming=True,
    )

    assert sync_svc._upsert_message(db, thread, incoming) is True

    assert thread.intent is None
    assert thread.last_agent_summary is None
    assert thread.pending_action is None


def test_sync_repairs_existing_mime_corruption_without_creating_duplicate(db):
    account = models.GmailAccount(
        user_id=1, email="owner@example.com", is_connected=True
    )
    thread = models.EmailThread(
        gmail_account_id=1,
        gmail_thread_id="mime-repair-thread",
        subject="����",
        snippet="����",
        intent="interested",
        last_agent_summary="Old corrupt analysis",
        pending_action="reply",
    )
    db.add_all([account, thread])
    db.flush()
    existing = models.EmailMessage(
        thread_id=thread.id,
        gmail_message_id="mime-message",
        from_email="person@example.com",
        to_email=account.email,
        subject="����",
        snippet="����",
        body_text="����ϣ��ʹ�� AI",
        body_html="<p>����</p>",
        is_incoming=True,
    )
    db.add(existing)
    db.flush()
    refreshed = _msg(
        "mime-message",
        from_email="person@example.com",
        to_email=account.email,
        incoming=True,
    )
    refreshed.subject = "关于 AI 销售邮件自动化"
    refreshed.snippet = "我们希望使用 AI"
    refreshed.body_text = "我们希望使用 AI 自动处理销售邮件。"
    refreshed.body_html = "<p>我们希望使用 AI 自动处理销售邮件。</p>"

    assert sync_svc._upsert_message(db, thread, refreshed) is False

    assert existing.subject == refreshed.subject
    assert existing.body_text == refreshed.body_text
    assert thread.subject == refreshed.subject
    assert thread.intent is None
    assert db.query(models.EmailMessage).filter_by(
        gmail_message_id="mime-message"
    ).count() == 1
    db.flush()
    audit = db.query(models.AuditLog).filter_by(
        action="gmail_mime_decode_repaired"
    ).one()
    assert '"body_text"' in audit.detail


def test_sync_reconciles_pending_approval_when_exact_outbound_exists(db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    thread = models.EmailThread(
        gmail_account_id=1, gmail_thread_id="reconcile-thread", contact_email="customer@example.com"
    )
    db.add_all([account, thread])
    db.flush()
    draft = models.EmailDraft(
        gmail_account_id=account.id, thread_id=thread.id, to_email="customer@example.com",
        subject="Re: Question", body_text="Hello Customer\n\nHere is the answer.", status="draft"
    )
    approval = models.Approval(
        kind="reply", thread_id=thread.id, draft_id=None, to_email="customer@example.com",
        subject="Re: Question", body_text="Hello Customer\n\nHere is the answer.", status="pending"
    )
    sent = models.EmailMessage(
        thread_id=thread.id, gmail_message_id="sent-reply", from_email="owner@example.com",
        to_email="customer@example.com", subject="Re: Question",
        body_text=" Hello Customer\nHere is the answer. ", is_incoming=False
    )
    db.add_all([draft, approval, sent])
    db.flush()

    assert sync_svc._reconcile_pending_approvals(db, account, thread) == 1
    assert approval.status == "expired"
    assert approval.decided_by == "system:sync_reconciliation"
    assert "matching outbound" in approval.rejection_reason
    db.flush()
    audit = db.query(models.AuditLog).filter_by(action="approval_reconciled_sent").one()
    assert audit.entity_id == str(approval.id)


def test_sync_does_not_reconcile_different_outbound_body(db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    thread = models.EmailThread(
        gmail_account_id=1, gmail_thread_id="different-body-thread", contact_email="customer@example.com"
    )
    db.add_all([account, thread])
    db.flush()
    approval = models.Approval(
        kind="reply", thread_id=thread.id, to_email="customer@example.com",
        subject="Re: Question", body_text="The approved answer", status="pending"
    )
    sent = models.EmailMessage(
        thread_id=thread.id, gmail_message_id="different-body", from_email="owner@example.com",
        to_email="customer@example.com", subject="Re: Question",
        body_text="A different answer", is_incoming=False
    )
    db.add_all([approval, sent])
    db.flush()

    assert sync_svc._reconcile_pending_approvals(db, account, thread) == 0
    assert approval.status == "pending"


def test_reply_cancels_followup_only_for_that_campaign(db):
    """A reply in campaign A must NOT cancel scheduled follow-ups in campaign B
    for the same contact (cross-campaign pollution regression)."""
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    camp_a = models.Campaign(owner_id=1, name="A", status="active")
    camp_b = models.Campaign(owner_id=1, name="B", status="active")
    contact = models.Contact(owner_id=1, email="cust@example.com", status="contacted")
    db.add_all([account, camp_a, camp_b, contact])
    db.flush()
    cc_a = models.CampaignContact(campaign_id=camp_a.id, contact_id=contact.id, status="sent")
    cc_b = models.CampaignContact(campaign_id=camp_b.id, contact_id=contact.id, status="sent")
    db.add_all([cc_a, cc_b])
    db.flush()
    task_a = models.FollowUpTask(
        campaign_contact_id=cc_a.id, contact_id=contact.id, campaign_id=camp_a.id,
        sequence=1, scheduled_at=datetime.now(timezone.utc), status="scheduled",
    )
    task_b = models.FollowUpTask(
        campaign_contact_id=cc_b.id, contact_id=contact.id, campaign_id=camp_b.id,
        sequence=1, scheduled_at=datetime.now(timezone.utc), status="scheduled",
    )
    db.add_all([task_a, task_b])
    db.flush()
    thread = models.EmailThread(
        gmail_account_id=account.id, gmail_thread_id="xcamp-thread",
        campaign_id=camp_a.id, contact_email=contact.email,
    )
    db.add(thread)
    db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id, gmail_message_id="xcamp-reply", from_email=contact.email,
        to_email=account.email, subject="Re: A", body_text="Interested", is_incoming=True,
    ))
    db.flush()

    with patch("app.services.sync.assess_inbound", return_value=TriageResult("human", True, 0.99)):
        sync_svc._detect_human_reply(db, account, thread)
    db.flush()

    assert task_a.status == "cancelled"
    assert task_b.status == "scheduled"  # untouched — the bug used to cancel this too
    assert cc_a.status == "replied"
    assert cc_b.status == "sent"


def test_sync_skips_bad_sent_message_without_aborting(db):
    """One stale/invalid message_id must not abort the whole sync (P0-4)."""
    campaign = models.Campaign(owner_id=1, name="C", status="active")
    contact_a = models.Contact(owner_id=1, email="a@example.com")
    contact_b = models.Contact(owner_id=1, email="b@example.com")
    db.add_all([campaign, contact_a, contact_b])
    db.flush()
    db.add(models.CampaignContact(
        campaign_id=campaign.id, contact_id=contact_a.id, status="sent", last_message_id="bad",
    ))
    db.add(models.CampaignContact(
        campaign_id=campaign.id, contact_id=contact_b.id, status="sent", last_message_id="good",
    ))
    db.flush()

    class FakeTL:
        def search_threads(self, query, max_results, agent, is_primary, include_spam_trash=False):
            return [], {}

        def get_message(self, message_id, agent, is_primary):
            if message_id == "bad":
                raise RuntimeError("boom")
            return SimpleNamespace(thread_id="gtid-good", gmail_message_id=message_id)

        def get_thread(self, thread_id, agent, is_primary):
            return ThreadDTO(
                gmail_thread_id=thread_id, history_id="h", subject="s", snippet="", messages=[],
            )

    merged, meta = sync_svc._search_relevant_threads(db, FakeTL(), query="", max_results=50)

    assert len(merged) == 1
    assert merged[0].gmail_thread_id == "gtid-good"
    assert meta["sent_message_queries"] == 2  # both attempted, bad skipped


def test_local_html_body_repair_is_idempotent_and_never_calls_gmail(db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    db.add(account); db.flush()
    thread = models.EmailThread(gmail_account_id=account.id, gmail_thread_id="html-repair", contact_email="sender@example.com")
    db.add(thread); db.flush()
    message = models.EmailMessage(
        thread_id=thread.id, gmail_message_id="html-repair-message", from_email="sender@example.com",
        to_email=account.email, subject="Update",
        body_text="@import url('fonts'); body { color: red; } Useful update",
        body_html="<html><head><style>body { color: red; }</style></head><body><p>Useful update</p></body></html>",
        is_incoming=True,
    )
    db.add(message); db.commit()

    assert sync_svc.repair_stored_mail_bodies(db) == 1
    db.refresh(message)
    assert message.body_text == "Useful update"
    assert "color: red" not in message.body_text
    assert sync_svc.repair_stored_mail_bodies(db) == 0
    assert db.query(models.AuditLog).filter_by(action="gmail_html_body_repaired", entity_id=str(message.id)).count() == 1
