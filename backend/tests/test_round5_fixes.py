from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import models
from app.agents.langgraph_agent import _compose_reply
from app.services import approvals as approval_svc


def test_contact_partial_update_preserves_omitted_fields(client):
    created = client.post("/api/contacts", json={
        "email": "lead@example.com",
        "first_name": "Lin",
        "company": "Acme",
        "category": "qualified",
        "intent_level": "high",
        "tags": ["priority", "crm"],
        "next_action": "reply",
    }).json()

    response = client.put(
        f"/api/contacts/{created['id']}",
        json={"manual_lock": True},
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["manual_lock"] is True
    assert updated["email"] == "lead@example.com"
    assert updated["first_name"] == "Lin"
    assert updated["company"] == "Acme"
    assert updated["category"] == "qualified"
    assert updated["intent_level"] == "high"
    assert updated["tags"] == ["crm", "priority"]
    assert updated["next_action"] == "reply"


def test_terminal_contact_update_clears_stale_reply_action(client):
    created = client.post("/api/contacts", json={
        "email": "terminal@example.com", "first_name": "Terminal",
        "category": "qualified", "next_action": "reply",
    }).json()

    response = client.put(
        f"/api/contacts/{created['id']}",
        json={"category": "invalid", "lifecycle_stage": "stopped"},
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["lifecycle_stage"] == "stopped"
    assert updated["next_action"] == "none"


def test_generate_reply_rejects_terminal_contact_before_agent_or_draft(client, db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    contact = models.Contact(
        owner_id=1, email="terminal-reply@example.com", category="invalid",
        lifecycle_stage="stopped", next_action="reply",
    )
    db.add_all([account, contact]); db.flush()
    thread = models.EmailThread(
        gmail_account_id=account.id, gmail_thread_id="terminal-reply-thread",
        contact_email=contact.email, pending_action="reply",
    )
    db.add(thread); db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id, gmail_message_id="terminal-reply-message",
        from_email=contact.email, to_email=account.email, subject="Re: Test",
        body_text="Question", is_incoming=True,
    ))
    db.commit()

    response = client.post(f"/api/inbox/threads/{thread.id}/generate-reply")

    assert response.status_code == 409
    assert response.json()["detail"] == "contact_not_eligible_for_reply"
    assert db.query(models.Approval).count() == 0


def test_contextual_reply_preserves_subject_and_addresses_customer_questions():
    with patch(
        "app.agents.langgraph_agent._llm_reply",
        return_value=(
            "Thanks for the question. Could you tell me a bit more about your "
            "use case so I can give the most useful answer?"
        ),
    ):
        subject, body = _compose_reply(
            {"first_name": "Chris"},
            {
                "sender_name": "Chris",
                "product_description": "Email automation",
            },
            "interested",
            original_subject="Intro to TAC Email Automation",
            customer_message=(
                "What is the pricing? Can I book a demo, and do you support Chinese?"
            ),
            thread_context="Us: Intro\nCustomer: What is the pricing?",
        )

    assert subject == "Re: Intro to TAC Email Automation"
    lowered = body.lower()
    assert "pricing" in lowered
    assert "demo" in lowered
    assert "chinese" in lowered
    assert "tell me a bit more about your use case" not in lowered


def test_blocked_draft_does_not_create_empty_approval(db):
    campaign = models.Campaign(
        owner_id=1,
        name="Blocked reply",
        status="active",
        sender_name="Chris",
    )
    account = models.GmailAccount(
        user_id=1,
        email="owner@example.com",
        is_connected=True,
    )
    contact = models.Contact(
        owner_id=1,
        email="blocked@example.com",
        first_name="Blocked",
    )
    db.add_all([campaign, account, contact])
    db.flush()
    thread = models.EmailThread(
        gmail_account_id=account.id,
        gmail_thread_id="blocked-thread",
        contact_email=contact.email,
        campaign_id=campaign.id,
    )
    db.add(thread)
    db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id, gmail_message_id="blocked-parent",
        message_id_header="<blocked-parent@example.com>",
        from_email=contact.email, to_email=account.email,
        subject="Re: Test", body_text="Question", is_incoming=True,
    ))
    db.flush()

    with patch.object(
        approval_svc.UnifiedEmailToolLayer,
        "create_draft",
        return_value={"ok": False, "blocked": "recipient in suppression list"},
    ):
        with pytest.raises(approval_svc.DraftCreationError) as exc_info:
            approval_svc.create_reply_approval(
                db,
                thread=thread,
                contact=contact,
                campaign=campaign,
                cc=None,
                subject="Re: Test",
                body_text="Test",
            )

    assert exc_info.value.reason == "recipient in suppression list"
    assert db.query(models.Approval).count() == 0


def test_generate_reply_returns_policy_reason_without_empty_approval(client, db):
    campaign = models.Campaign(
        owner_id=1,
        name="Reply campaign",
        status="active",
        sender_name="Chris",
        agent_mode="langgraph_only",
    )
    account = models.GmailAccount(
        user_id=1,
        email="owner@example.com",
        is_connected=True,
    )
    contact = models.Contact(
        owner_id=1,
        email="customer@example.com",
        first_name="Customer",
    )
    db.add_all([campaign, account, contact])
    db.flush()
    thread = models.EmailThread(
        gmail_account_id=account.id,
        gmail_thread_id="reply-policy-thread",
        contact_email=contact.email,
        campaign_id=campaign.id,
        subject="Original subject",
            pending_action="reply",
    )
    db.add(thread)
    db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id,
        gmail_message_id="reply-policy-message",
        from_email=contact.email,
        to_email=account.email,
        subject="Re: Original subject",
        body_text="What is the price?",
        is_incoming=True,
    ))
    db.commit()

    decision = SimpleNamespace(
        intent="asking_question",
        summary="Customer asked about pricing",
        recommended_action="human_review",
        reasoning_summary="Pricing question",
        risk_level="low",
        draft=SimpleNamespace(
            subject="Re: Original subject",
            body_text="Pricing depends on scope.",
        ),
    )
    with patch("app.api.inbox.Orchestrator.analyze", return_value=decision), patch(
        "app.services.approvals.create_reply_approval",
        side_effect=approval_svc.DraftCreationError("recipient in suppression list"),
    ):
        response = client.post(f"/api/inbox/threads/{thread.id}/generate-reply")

    assert response.status_code == 409
    assert response.json()["detail"] == "recipient in suppression list"
    assert db.query(models.Approval).count() == 0


def test_inbox_reply_ignores_archived_campaign_during_send(db, monkeypatch):
    """Inbox replies are standalone even when a historical Campaign is attached."""
    campaign = models.Campaign(owner_id=1, name="old", status="archived", sender_name="Old")
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=False)
    contact = models.Contact(owner_id=1, email="customer@example.com")
    db.add_all([campaign, account, contact]); db.flush()
    thread = models.EmailThread(gmail_account_id=account.id, gmail_thread_id="thread-live",
                                campaign_id=campaign.id, contact_email=contact.email, has_human_reply=True)
    db.add(thread); db.flush()
    draft = models.EmailDraft(gmail_account_id=account.id, thread_id=thread.id, to_email=contact.email,
                              subject="Re: Test", body_text="Reply", kind="reply", status="draft")
    db.add(draft); db.flush()
    approval = models.Approval(kind="reply", campaign_id=campaign.id, draft_id=draft.id,
                               thread_id=thread.id, to_email=contact.email, subject=draft.subject,
                               body_text=draft.body_text, idempotency_key="send:test", status="pending")
    db.add(approval); db.flush()
    seen = {}
    def fake_send(self, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "message_id": "gmail-message", "draft": draft}
    monkeypatch.setattr(approval_svc.UnifiedEmailToolLayer, "send_approved_draft", fake_send)

    result = approval_svc.decide_approval(db, approval.id, "approve", editor_email="operator")

    assert result["ok"] is True
    assert seen["campaign_id"] is None
    assert approval.status == "approved"
