"""Gmail reply threading contract: first sends start threads; replies continue them."""
import base64
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest

from app import models
from app.gmail.transport import build_mime, parse_gmail_message
from app.services import approvals as approval_svc
from app.tools.email_tools import UnifiedEmailToolLayer


def _records(db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    campaign = models.Campaign(owner_id=1, name="Campaign", status="active")
    contact = models.Contact(owner_id=1, email="customer@example.com", status="contacted")
    db.add_all([account, campaign, contact])
    db.flush()
    cc = models.CampaignContact(campaign_id=campaign.id, contact_id=contact.id, status="sent")
    thread = models.EmailThread(
        gmail_account_id=account.id, gmail_thread_id="gmail-thread-1",
        contact_email=contact.email, campaign_id=campaign.id, subject="Original subject",
    )
    db.add_all([cc, thread])
    db.flush()
    message = models.EmailMessage(
        thread_id=thread.id, gmail_message_id="gmail-message-1",
        message_id_header="<parent@example.com>", references_header="<root@example.com>",
        from_email=contact.email, to_email=account.email, subject="Original subject",
        body_text="Question", is_incoming=True,
    )
    db.add(message)
    db.flush()
    return account, campaign, contact, cc, thread, message


def _proposal(subject="Generated subject"):
    return SimpleNamespace(
        subject=subject, body_text="Reply body", body_html="",
        recommended_action="send", risk_level="low",
        run_id="run", model="model", prompt_version="1",
        latency_ms=1,
    )


def _capture_draft(monkeypatch, db):
    captured = {}

    def create_draft(self, **kwargs):
        captured.update(kwargs)
        draft = models.EmailDraft(
            gmail_account_id=self.account.id, gmail_draft_id="draft-1",
            to_email=kwargs["to"], subject=kwargs["subject"],
            body_text=kwargs["body_text"], body_html=kwargs.get("body_html", ""),
            kind=kwargs.get("kind", "outreach"), status="draft",
        )
        db.add(draft)
        db.flush()
        return {"ok": True, "draft": draft, "gmail_draft_id": "draft-1"}

    monkeypatch.setattr(UnifiedEmailToolLayer, "create_draft", create_draft)
    return captured


def test_inbox_reply_uses_original_thread_headers_and_subject(db, monkeypatch):
    _, campaign, contact, cc, thread, message = _records(db)
    captured = _capture_draft(monkeypatch, db)

    approval = approval_svc.create_reply_approval(
        db, thread=thread, contact=contact, campaign=campaign, cc=cc,
        subject="AI changed this", body_text="Reply body", source_message=message,
    )

    assert captured["thread_gmail_id"] == "gmail-thread-1"
    assert captured["in_reply_to"] == "<parent@example.com>"
    assert captured["references"] == "<root@example.com> <parent@example.com>"
    assert captured["subject"] == "Original subject"
    assert approval.subject == "Original subject"
    assert db.get(models.EmailDraft, approval.draft_id).thread_id == thread.id


def test_follow_up_uses_original_thread_headers_and_subject(db, monkeypatch):
    _, campaign, contact, cc, thread, _ = _records(db)
    task = models.FollowUpTask(
        campaign_contact_id=cc.id, contact_id=contact.id, campaign_id=campaign.id,
        thread_id=thread.id,
        sequence=1, scheduled_at=thread.created_at, status="scheduled",
    )
    db.add(task)
    db.flush()
    captured = _capture_draft(monkeypatch, db)

    approval_svc.create_follow_up_approval(db, task, _proposal())

    assert captured["thread_gmail_id"] == "gmail-thread-1"
    assert captured["in_reply_to"] == "<parent@example.com>"
    assert captured["references"] == "<root@example.com> <parent@example.com>"
    assert captured["subject"] == "Original subject"


def test_first_outreach_does_not_attach_to_existing_thread(db, monkeypatch):
    _, _, _, cc, _, _ = _records(db)
    captured = _capture_draft(monkeypatch, db)

    approval_svc.create_outreach_approval(db, cc, _proposal("New outreach"))

    assert captured["subject"] == "New outreach"
    assert captured.get("thread_gmail_id") is None
    assert captured.get("in_reply_to") is None
    assert captured.get("references") is None


def test_legacy_message_headers_are_hydrated_before_reply(db, monkeypatch):
    _, campaign, contact, cc, thread, message = _records(db)
    message.message_id_header = None
    message.references_header = None
    captured = _capture_draft(monkeypatch, db)
    monkeypatch.setattr(
        UnifiedEmailToolLayer, "get_message",
        lambda self, *args, **kwargs: SimpleNamespace(
            message_id_header="<remote-parent@example.com>",
            in_reply_to_header="<remote-root@example.com>", references_header=None,
        ),
    )

    approval_svc.create_reply_approval(
        db, thread=thread, contact=contact, campaign=campaign, cc=cc,
        subject="Changed", body_text="Reply", source_message=message,
    )

    assert message.message_id_header == "<remote-parent@example.com>"
    assert captured["references"] == "<remote-root@example.com> <remote-parent@example.com>"


def test_missing_rfc_headers_blocks_reply_draft(db, monkeypatch):
    _, campaign, contact, cc, thread, message = _records(db)
    message.message_id_header = None
    message.references_header = None
    monkeypatch.setattr(
        UnifiedEmailToolLayer, "get_message",
        lambda self, *args, **kwargs: SimpleNamespace(
            message_id_header=None, in_reply_to_header=None, references_header=None,
        ),
    )
    called = False

    def forbidden_create(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("create_draft must not run without RFC reply headers")

    monkeypatch.setattr(UnifiedEmailToolLayer, "create_draft", forbidden_create)

    with pytest.raises(approval_svc.DraftCreationError, match="gmail_reply_headers_unavailable"):
        approval_svc.create_reply_approval(
            db, thread=thread, contact=contact, campaign=campaign, cc=cc,
            subject="Changed", body_text="Reply", source_message=message,
        )
    assert called is False


def test_gmail_parser_and_mime_preserve_reply_headers():
    parsed = parse_gmail_message({
        "id": "api-id", "threadId": "thread-id", "historyId": "1",
        "payload": {"headers": [
            {"name": "From", "value": "customer@example.com"},
            {"name": "To", "value": "owner@example.com"},
            {"name": "Subject", "value": "Original subject"},
            {"name": "Message-ID", "value": "<parent@example.com>"},
            {"name": "In-Reply-To", "value": "<root@example.com>"},
            {"name": "References", "value": "<root@example.com>"},
        ]},
    }, owner_email="owner@example.com")
    assert parsed.message_id_header == "<parent@example.com>"
    assert parsed.in_reply_to_header == "<root@example.com>"
    assert parsed.references_header == "<root@example.com>"

    raw = build_mime(
        "customer@example.com", "Original subject", "Reply", "", "thread-id",
        "<parent@example.com>", "<root@example.com> <parent@example.com>",
    )
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
    assert message["Subject"] == "Original subject"
    assert message["In-Reply-To"] == "<parent@example.com>"
    assert message["References"] == "<root@example.com> <parent@example.com>"
