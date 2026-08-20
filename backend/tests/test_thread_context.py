from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import models
from app.services.thread_context import (
    MAX_CONTEXT_CHARACTERS,
    build_thread_context,
)


def test_context_is_chronological_bounded_and_keeps_latest_inbound(db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=False)
    db.add(account)
    db.flush()
    thread = models.EmailThread(
        gmail_account_id=account.id,
        gmail_thread_id="context-thread",
        contact_email="customer@example.com",
    )
    db.add(thread)
    db.flush()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.add(models.EmailMessage(
        thread_id=thread.id,
        gmail_message_id="customer-source",
        is_incoming=True,
        from_email="customer@example.com",
        subject="Original customer request",
        body_text="Customer context that must remain visible. " + ("x" * 4_000),
        received_at=start,
    ))
    for index in range(8):
        db.add(models.EmailMessage(
            thread_id=thread.id,
            gmail_message_id=f"out-{index}",
            is_incoming=False,
            from_email="owner@example.com",
            subject=f"Outbound {index}",
            body_text=f"Response {index}",
            received_at=start + timedelta(minutes=index + 1),
        ))
    db.flush()

    context = build_thread_context(db, thread)

    assert "Original customer request" in context
    assert "Customer context that must remain visible" in context
    assert context.index("Original customer request") < context.index("Outbound 7")
    assert context.count("Subject:") == 8
    assert len(context) <= MAX_CONTEXT_CHARACTERS


def test_campaign_automation_passes_shared_context_to_agent(db, monkeypatch):
    from app.agents.orchestrator import Orchestrator
    from app.services import automation as automation_svc

    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=False)
    campaign = models.Campaign(owner_id=1, name="Context campaign", status="active")
    contact = models.Contact(owner_id=1, email="customer@example.com")
    db.add_all([account, campaign, contact])
    db.flush()
    db.add(models.CampaignContact(
        campaign_id=campaign.id, contact_id=contact.id, status="replied"
    ))
    thread = models.EmailThread(
        gmail_account_id=account.id,
        gmail_thread_id="campaign-context-thread",
        campaign_id=campaign.id,
        contact_email=contact.email,
        has_human_reply=True,
    )
    db.add(thread)
    db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id,
        gmail_message_id="campaign-inbound",
        is_incoming=True,
        from_email=contact.email,
        subject="Integration question",
        body_text="Can this connect to our CRM?",
        received_at=datetime.now(timezone.utc),
    ))
    db.commit()

    captured = {}
    decision = SimpleNamespace(intent="unknown", draft=None)
    def analyze(self, payload):
        captured["thread_context"] = payload.thread_context
        return decision
    monkeypatch.setattr(Orchestrator, "analyze", analyze)
    monkeypatch.setattr(automation_svc.followup_svc, "process_due_follow_ups", lambda *_: [])

    automation_svc._prepare_campaign(
        db,
        SimpleNamespace(campaign_id=campaign.id, owner_id=1),
        SimpleNamespace(id=1),
        SimpleNamespace(stop_on_intents=[]),
    )

    assert "Integration question" in captured["thread_context"]
    assert "Can this connect to our CRM?" in captured["thread_context"]
