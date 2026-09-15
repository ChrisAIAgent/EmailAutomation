import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app import models
from app.gmail.transport import parse_gmail_message
from app.services.inbox_triage import TriageResult
from app.services.inbox_triage import assess_inbound


def _account(db):
    account = models.GmailAccount(
        user_id=1,
        email="owner@example.com",
        is_connected=True,
    )
    db.add(account)
    db.flush()
    return account


def _thread_with_message(db, *, sender, subject, body, incoming=True, suffix="1"):
    account = db.query(models.GmailAccount).first() or _account(db)
    thread = models.EmailThread(
        gmail_account_id=account.id,
        gmail_thread_id=f"human-gate-{suffix}",
        contact_email=sender,
        subject=subject,
    )
    db.add(thread)
    db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id,
        gmail_message_id=f"human-message-{suffix}",
        from_email=sender if incoming else account.email,
        to_email=account.email if incoming else sender,
        subject=subject,
        body_text=body,
        is_incoming=incoming,
    ))
    db.commit()
    return thread


def test_outbound_only_thread_never_becomes_needs_reply(client, db):
    thread = _thread_with_message(
        db,
        sender="prospect@example.com",
        subject="Following up",
        body="Could you review my earlier note?",
        incoming=False,
        suffix="outbound",
    )

    with patch("app.api.inbox.Orchestrator.analyze") as analyze:
        response = client.post(f"/api/inbox/threads/{thread.id}/analyze")

    assert response.status_code == 200
    assert response.json()["category"] == "awaiting_reply"
    assert response.json()["pending_action"] == "no_action"
    assert db.query(models.Contact).count() == 0
    analyze.assert_not_called()
    reply = client.post(f"/api/inbox/threads/{thread.id}/generate-reply")
    assert reply.status_code == 409


def test_advertising_is_filtered_before_contact_creation(client, db):
    thread = _thread_with_message(
        db,
        sender="offers@example.com",
        subject="Weekly newsletter and special offer",
        body="View in browser. Manage preferences. Unsubscribe.",
        suffix="advertising",
    )

    with patch("app.api.inbox.Orchestrator.analyze") as analyze:
        response = client.post(f"/api/inbox/threads/{thread.id}/analyze")

    assert response.status_code == 200
    assert response.json()["category"] == "filtered"
    assert response.json()["pending_action"] == "no_action"
    assert db.query(models.Contact).count() == 0
    assert db.query(models.Suppression).count() == 0
    analyze.assert_not_called()


def test_human_sender_creates_contact_tags_and_audit(client, db):
    thread = _thread_with_message(
        db,
        sender="person@example.com",
        subject="Could we schedule a demo?",
        body="Hi, I would like pricing and a demo next week.",
        suffix="person",
    )
    decision = SimpleNamespace(
        intent="asking_question",
        summary="Person requested pricing and a demo.",
        recommended_action="reply",
    )

    # Contact creation now requires an actual human determination (from the LLM),
    # not the permissive offline fallback. The fallback returns review-only.
    with patch(
        "app.services.inbox_triage._llm_human_assessment",
        return_value=TriageResult(
            "human", True, 0.9, ["human", "pricing", "demo_request"], "LLM confirmed human sender"
        ),
    ), patch("app.api.inbox.Orchestrator.analyze", return_value=decision):
        response = client.post(f"/api/inbox/threads/{thread.id}/analyze")

    assert response.status_code == 200
    contact = db.query(models.Contact).filter_by(email="person@example.com").one()
    tags = set(__import__("json").loads(contact.tags))
    assert {"human", "pricing", "demo_request", "asking_question"} <= tags
    assert contact.lifecycle_stage == "needs_reply"
    assert contact.next_action == "reply"
    actions = {
        row.action
        for row in db.query(models.AuditLog).filter_by(
            entity="contact", entity_id=str(contact.id)
        )
    }
    assert "contact_created_from_human_email" in actions
    assert "contact_state_updated_from_conversation" in actions


def test_retriage_replaces_agent_tags_but_preserves_manual_tags(client, db):
    thread = _thread_with_message(
        db, sender="tags@example.com", subject="Pricing and demo", body="Could I get pricing and a demo?", suffix="tags"
    )
    contact = models.Contact(
        owner_id=1, email="tags@example.com",
        tags='["inbox", "human", "pricing", "demo_request", "legacy_custom_tag"]',
    )
    db.add(contact); db.commit()
    decision = SimpleNamespace(intent="interested", summary="Interested", recommended_action="reply")
    triage = TriageResult("human", True, 0.98, ["partnership"], "Direct person")
    with patch("app.api.inbox.assess_inbound", return_value=triage), patch(
        "app.api.inbox.Orchestrator.analyze", return_value=decision
    ):
        response = client.post(f"/api/inbox/threads/{thread.id}/analyze")

    assert response.status_code == 200
    db.refresh(contact)
    tags = set(json.loads(contact.tags))
    # Content tags now ACCUMULATE: previously derived pricing/demo_request are
    # preserved, the current message's partnership is merged in, and the current
    # intent (interested) replaces any prior intent.  User tag legacy_custom_tag
    # is untouched.
    assert {
        "inbox", "human", "pricing", "demo_request",
        "partnership", "interested", "legacy_custom_tag",
    } <= tags


def test_real_person_special_content_requires_review_without_contact_or_reply(client, db):
    thread = _thread_with_message(
        db,
        sender="applicant@example.com",
        subject="Job application",
        body="Hello, this is Alex Applicant. Please find my resume attached.",
        suffix="application",
    )
    decision = SimpleNamespace(intent="asking_question", summary="Applicant", recommended_action="reply")
    triage = TriageResult(
        "human", True, 0.98, ["job_application"], "Direct person",
        first_name="Alex", last_name="Applicant", company="Example Co",
    )
    with patch("app.api.inbox.assess_inbound", return_value=triage), patch(
        "app.api.inbox.Orchestrator.analyze", return_value=decision
    ):
        response = client.post(f"/api/inbox/threads/{thread.id}/analyze")

    assert response.status_code == 200
    assert response.json()["pending_action"] == "human_review"
    assert response.json()["category"] == "human_review"
    assert response.json()["review_kind"] == "contact_admission_uncertain"
    # A verified human Inbox message is not automatically a campaign reply.
    assert response.json()["has_human_reply"] is False
    assert db.query(models.Contact).filter_by(email="applicant@example.com").first() is None
    assert db.query(models.NonCustomerFilter).filter_by(email="applicant@example.com").first() is None
    approved = client.post(f"/api/inbox/threads/{thread.id}/human-review", json={"decision": "approve"})
    assert approved.status_code == 200
    assert approved.json()["result"] == "manual_contact_entry_required"
    assert approved.json()["resolved"] is False
    assert db.query(models.Contact).filter_by(email="applicant@example.com").first() is None
    assert client.post(f"/api/inbox/threads/{thread.id}/generate-reply").status_code == 409


def test_contact_admission_reject_filters_sender_without_email_actions(client, db):
    thread = _thread_with_message(
        db,
        sender="forwarded-review@example.com",
        subject="Fwd: unrelated supplier request",
        body="Forwarded message: please quote spare parts.",
        suffix="review-endpoint",
    )
    thread.intent = "asking_question"
    thread.pending_action = "human_review"
    db.commit()

    detail = client.get(f"/api/inbox/threads/{thread.id}")
    assert detail.status_code == 200
    assert detail.json()["review_guidance"]["kind"] == "forwarded"
    assert detail.json()["review_guidance"]["recommendation"]

    resolved = client.post(
        f"/api/inbox/threads/{thread.id}/human-review",
        json={"decision": "reject"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved"] is True
    assert resolved.json()["result"] == "non_customer_filtered"
    db.refresh(thread)
    assert thread.pending_action == "no_action"
    assert thread.intent == "filtered_non_customer"
    audit = db.query(models.AuditLog).filter_by(
        action="contact_admission_resolved", entity_id=str(thread.id)
    ).one()
    assert '"send_or_draft_created": false' in audit.detail
    assert db.query(models.Approval).count() == 0
    assert db.query(models.Suppression).count() == 0
    assert db.query(models.NonCustomerFilter).filter_by(email="forwarded-review@example.com").one()

    later = _thread_with_message(
        db, sender="forwarded-review@example.com", subject="A later note",
        body="Hello again", suffix="review-later",
    )
    assert client.post(f"/api/inbox/threads/{later.id}/analyze").json()["category"] == "filtered"


def test_contact_admission_approve_waits_for_manual_form_save(client, db):
    thread = _thread_with_message(db, sender="manual@example.com", subject="Hello", body="Business inquiry", suffix="manual-admission")
    thread.intent = "triage_review"; thread.pending_action = "human_review"; db.commit()
    response = client.post(f"/api/inbox/threads/{thread.id}/human-review", json={"decision": "approve"})
    assert response.json()["result"] == "manual_contact_entry_required"
    assert db.query(models.Contact).filter_by(email="manual@example.com").first() is None
    db.refresh(thread); assert thread.pending_action == "human_review"
    created = client.post(f"/api/inbox/threads/{thread.id}/contact", json={"first_name": "Manual", "company": "Example", "category": "qualified", "tags": []})
    assert created.status_code == 200
    db.refresh(thread); assert thread.pending_action == "no_action"


def test_agent_decide_creates_only_clear_business_contact(client, db):
    thread = _thread_with_message(db, sender="alex@example.com", subject="Pricing", body="My name is Alex Buyer. Please share pricing.", suffix="agent-admission")
    thread.intent = "asking_question"; thread.pending_action = "human_review"; db.commit()
    triage = TriageResult("human", True, 0.98, ["pricing"], "Clear business sender", first_name="Alex", last_name="Buyer", company="Buyer Co")
    with patch("app.api.inbox.assess_inbound", return_value=triage):
        response = client.post(f"/api/inbox/threads/{thread.id}/human-review", json={"decision": "agent_decide"})
    assert response.json()["result"] == "contact_created_by_agent"
    contact = db.query(models.Contact).filter_by(email="alex@example.com").one()
    assert contact.source == "inbox_agent_decide"
    assert "pricing" in contact.tags


def test_agent_decide_ignores_same_email_contact_owned_by_another_user(client, db):
    db.add(models.User(id=2, email="other-owner@example.com", name="Other Owner"))
    db.add(models.Contact(owner_id=2, email="alex@example.com", first_name="Other", category="qualified"))
    db.commit()
    thread = _thread_with_message(
        db, sender="alex@example.com", subject="Pricing", body="My name is Alex Buyer. Please share pricing.",
        suffix="agent-admission-other-owner",
    )
    thread.intent = "asking_question"; thread.pending_action = "human_review"; db.commit()
    triage = TriageResult("human", True, 0.98, ["pricing"], "Clear business sender", first_name="Alex", last_name="Buyer", company="Buyer Co")
    with patch("app.api.inbox.assess_inbound", return_value=triage):
        response = client.post(f"/api/inbox/threads/{thread.id}/human-review", json={"decision": "agent_decide"})
    assert response.status_code == 200
    assert response.json()["result"] == "contact_created_by_agent"
    assert db.query(models.Contact).filter_by(owner_id=1, email="alex@example.com").one().source == "inbox_agent_decide"


def test_agent_decide_blocks_when_latest_sender_does_not_match_thread_contact(client, db):
    thread = _thread_with_message(
        db, sender="recorded@example.com", subject="Pricing", body="My name is Alex Buyer. Please share pricing.",
        suffix="agent-admission-mismatch",
    )
    thread.intent = "asking_question"; thread.pending_action = "human_review"
    thread.messages[0].from_email = "different-sender@example.com"
    db.commit()
    response = client.post(f"/api/inbox/threads/{thread.id}/human-review", json={"decision": "agent_decide"})
    assert response.status_code == 409
    assert response.json()["detail"] == "admission_sender_mismatch"
    assert db.query(models.Contact).filter_by(owner_id=1, email="recorded@example.com").first() is None


def test_existing_contact_skips_admission_card_and_reject_is_blocked(client, db):
    db.add(models.Contact(owner_id=1, email="known@example.com", first_name="Known", category="qualified"))
    db.commit()
    thread = _thread_with_message(db, sender="known@example.com", subject="Question", body="Can you help?", suffix="known-admission")
    thread.intent = "asking_question"; thread.pending_action = "human_review"; db.commit()

    detail = client.get(f"/api/inbox/threads/{thread.id}").json()
    assert detail["is_contact"] is True
    assert detail["review_kind"] == "stale_review"
    assert detail["review_guidance"]["kind"] == "stale_review"
    rejected = client.post(f"/api/inbox/threads/{thread.id}/human-review", json={"decision": "reject"})
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "contact_admission_already_completed"
    assert db.query(models.NonCustomerFilter).filter_by(email="known@example.com").first() is None


def test_customer_threads_use_latest_message_time_and_detail_is_chronological(client, db):
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    older = _thread_with_message(db, sender="ordered@example.com", subject="Older thread", body="old", suffix="ordered-old")
    newer = _thread_with_message(db, sender="ordered@example.com", subject="Newer thread", body="new", suffix="ordered-new")
    db.add(models.Contact(owner_id=1, email="ordered@example.com", first_name="Ordered", category="qualified"))
    older.messages[0].received_at = base
    newer.messages[0].received_at = base + timedelta(days=3)
    # Thread metadata points in the opposite direction and must not control UI order.
    older.updated_at = base + timedelta(days=10)
    newer.updated_at = base + timedelta(days=4)
    db.add(models.EmailMessage(
        thread_id=newer.id, gmail_message_id="ordered-middle", from_email="owner@example.com",
        to_email="ordered@example.com", subject="Newer thread", body_text="middle",
        is_incoming=False, received_at=base + timedelta(days=2),
    ))
    db.commit()

    customer = next(row for row in client.get("/api/inbox/customers").json() if row["email"] == "ordered@example.com")
    assert [row["id"] for row in customer["threads"]] == [newer.id, older.id]
    detail = client.get(f"/api/inbox/threads/{newer.id}").json()
    assert [message["body_text"] for message in detail["messages"]] == ["middle", "new"]


def test_forwarded_and_scripted_content_require_human_review(client, db):
    decision = SimpleNamespace(intent="asking_question", summary="Review", recommended_action="reply")
    cases = [
        ("forwarded@example.com", "Fwd: Supplier question", "Forwarded message: can you quote?", ["forwarded"]),
        ("tester@example.com", "Testing", "This is test content only.", ["scripted_content"]),
    ]
    for index, (sender, subject, body, tags) in enumerate(cases):
        thread = _thread_with_message(
            db, sender=sender, subject=subject, body=body, suffix=f"manual-no-contact-{index}"
        )
        triage = TriageResult("human", True, 0.98, tags, "Manual review")
        with patch("app.api.inbox.assess_inbound", return_value=triage), patch(
            "app.api.inbox.Orchestrator.analyze", return_value=decision
        ):
            response = client.post(f"/api/inbox/threads/{thread.id}/analyze")

        assert response.status_code == 200
        assert response.json()["category"] == "human_review"
        assert response.json()["pending_action"] == "human_review"
        assert db.query(models.Contact).filter_by(email=sender).first() is None
        assert db.query(models.NonCustomerFilter).filter_by(email=sender).first() is None


def test_existing_contact_special_content_is_automatically_no_action(client, db):
    sender = "known-forwarded@example.com"
    db.add(models.Contact(owner_id=1, email=sender, first_name="Known", category="qualified"))
    db.commit()
    thread = _thread_with_message(
        db, sender=sender, subject="Fwd: unrelated request", body="Forwarded message", suffix="known-content-review"
    )
    triage = TriageResult("human", True, 0.98, ["forwarded"], "Forwarded content requires review")
    with patch("app.api.inbox.assess_inbound", return_value=triage):
        response = client.post(f"/api/inbox/threads/{thread.id}/analyze")
    assert response.status_code == 200
    assert response.json()["review_kind"] is None
    assert response.json()["pending_action"] == "no_action"
    assert db.query(models.Contact).filter_by(email=sender).one()


def test_startup_reconcile_clears_legacy_review_for_existing_contact(db):
    from app.services.inbox_triage import reconcile_existing_contact_reviews

    sender = "known-legacy-review@example.com"
    db.add(models.Contact(owner_id=1, email=sender, first_name="Known", category="qualified"))
    db.commit()
    thread = _thread_with_message(
        db, sender=sender, subject="Fwd legacy", body="Forwarded message", suffix="known-legacy-review"
    )
    thread.intent = "triage_review"
    thread.pending_action = "human_review"
    db.commit()

    assert reconcile_existing_contact_reviews(db, owner_id=1) == 1
    db.commit()
    db.refresh(thread)
    assert thread.pending_action == "no_action"
    assert db.query(models.AuditLog).filter_by(
        action="existing_contact_review_reconciled", entity_id=str(thread.id)
    ).one()


def test_sender_content_review_ignore_closes_current_items_and_skips_future_prompt(client, db):
    sender = "known-ignore@example.com"
    db.add(models.Contact(owner_id=1, email=sender, first_name="Known", category="qualified"))
    db.commit()
    first = _thread_with_message(db, sender=sender, subject="Fwd one", body="Forwarded message", suffix="ignore-one")
    second = _thread_with_message(db, sender=sender, subject="Fwd two", body="Forwarded message", suffix="ignore-two")
    for item in (first, second):
        item.intent = "triage_review"
        item.pending_action = "human_review"
    db.commit()

    result = client.post(f"/api/inbox/senders/{sender}/content-review/ignore")
    assert result.status_code == 200
    assert result.json()["resolved_threads"] == 2
    assert db.query(models.InboxReviewRule).filter_by(
        owner_id=1, email=sender, review_kind="content_uncertain", action="no_action"
    ).one()
    db.refresh(first); db.refresh(second)
    assert first.pending_action == second.pending_action == "no_action"

    future = _thread_with_message(db, sender=sender, subject="Fwd later", body="Forwarded message", suffix="ignore-later")
    triage = TriageResult("human", True, 0.98, ["forwarded"], "Forwarded content requires review")
    with patch("app.api.inbox.assess_inbound", return_value=triage):
        response = client.post(f"/api/inbox/threads/{future.id}/analyze")
    assert response.status_code == 200
    assert response.json()["pending_action"] == "no_action"
    assert response.json()["category"] != "human_review"


def test_manual_contact_admission_clears_all_current_sender_reviews(client, db):
    sender = "batch-admission@example.com"
    first = _thread_with_message(db, sender=sender, subject="Question one", body="Business inquiry", suffix="batch-one")
    second = _thread_with_message(db, sender=sender, subject="Question two", body="Business inquiry", suffix="batch-two")
    for item in (first, second):
        item.intent = "triage_review"
        item.pending_action = "human_review"
    db.commit()

    result = client.post(
        f"/api/inbox/threads/{first.id}/contact",
        json={"first_name": "Batch", "company": "Example", "category": "qualified", "tags": []},
    )
    assert result.status_code == 200
    db.refresh(first); db.refresh(second)
    assert first.pending_action == second.pending_action == "no_action"


def test_sender_payload_exposes_review_kind_counts_and_rule_state(client, db):
    sender = "review-summary@example.com"
    db.add(models.Contact(owner_id=1, email=sender, first_name="Summary", category="qualified"))
    db.add(models.InboxReviewRule(owner_id=1, email=sender, review_kind="content_uncertain", action="no_action"))
    db.commit()
    thread = _thread_with_message(db, sender=sender, subject="Review", body="Forwarded message", suffix="summary")
    thread.intent = "triage_review"; thread.pending_action = "human_review"; db.commit()
    row = next(item for item in client.get("/api/inbox/customers").json() if item["email"] == sender)
    assert row["review_counts"]["content_uncertain"] == 1
    assert row["content_review_ignore_enabled"] is True


def test_unknown_opt_out_does_not_create_contact_or_suppression(client, db):
    thread = _thread_with_message(
        db,
        sender="unknown-opt-out@example.com",
        subject="Please remove me",
        body="Please unsubscribe me from this list.",
        suffix="unknown-opt-out",
    )
    decision = SimpleNamespace(intent="unsubscribe", summary="Opt out", recommended_action="stop")
    triage = TriageResult("human", True, 0.98, [], "Direct but unknown sender")
    with patch("app.api.inbox.assess_inbound", return_value=triage), patch(
        "app.api.inbox.Orchestrator.analyze", return_value=decision
    ):
        response = client.post(f"/api/inbox/threads/{thread.id}/analyze")

    assert response.status_code == 200
    # Unsubscribe is human-gated: the Agent detects it but must NOT auto-apply.
    assert response.json()["pending_action"] == "human_review"
    assert response.json()["review_kind"] == "opt_out_confirmation"
    assert db.query(models.Contact).filter_by(email="unknown-opt-out@example.com").first() is None
    assert db.query(models.Suppression).filter_by(email="unknown-opt-out@example.com").first() is None


def test_existing_contact_opt_out_routes_to_human_review_then_apply(client, db):
    """Unsubscribe is human-gated: analyze must NOT create a Suppression; only an
    explicit human approval in the Inbox applies the opt-out."""
    db.add(models.Contact(owner_id=1, email="known-opt-out@example.com", status="contacted"))
    db.commit()
    thread = _thread_with_message(
        db,
        sender="known-opt-out@example.com",
        subject="Remove me",
        body="Please unsubscribe me.",
        suffix="known-opt-out",
    )
    decision = SimpleNamespace(intent="unsubscribe", summary="Opt out", recommended_action="stop")
    triage = TriageResult("human", True, 0.98, [], "Known direct sender")
    with patch("app.api.inbox.assess_inbound", return_value=triage), patch(
        "app.api.inbox.Orchestrator.analyze", return_value=decision
    ):
        response = client.post(f"/api/inbox/threads/{thread.id}/analyze")

    assert response.status_code == 200
    # Agent detected the opt-out but did NOT auto-apply it.
    assert response.json()["pending_action"] == "human_review"
    assert response.json()["review_kind"] == "opt_out_confirmation"
    assert db.query(models.Suppression).filter_by(email="known-opt-out@example.com").first() is None
    # Contact is still active (not silently unsubscribed by the Agent).
    assert db.query(models.Contact).filter_by(email="known-opt-out@example.com").one().status == "contacted"

    # Human approves the opt-out -> Suppression written, contact unsubscribed.
    r = client.post(f"/api/inbox/threads/{thread.id}/human-review", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "unsubscribe_applied"
    supp = db.query(models.Suppression).filter_by(email="known-opt-out@example.com").one()
    assert supp.reason == "unsubscribe"
    assert db.query(models.Contact).filter_by(email="known-opt-out@example.com").one().status == "unsubscribed"


def test_existing_contact_opt_out_rejected_keeps_contact(client, db):
    """If the human rejects the detected opt-out, the contact is retained and no
    Suppression is written."""
    db.add(models.Contact(owner_id=1, email="known-opt-out@example.com", status="contacted"))
    db.commit()
    thread = _thread_with_message(
        db,
        sender="known-opt-out@example.com",
        subject="Remove me",
        body="Please unsubscribe me.",
        suffix="known-opt-out",
    )
    decision = SimpleNamespace(intent="unsubscribe", summary="Opt out", recommended_action="stop")
    triage = TriageResult("human", True, 0.98, [], "Known direct sender")
    with patch("app.api.inbox.assess_inbound", return_value=triage), patch(
        "app.api.inbox.Orchestrator.analyze", return_value=decision
    ):
        client.post(f"/api/inbox/threads/{thread.id}/analyze")
    r = client.post(f"/api/inbox/threads/{thread.id}/human-review", json={"decision": "reject"})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "unsubscribe_rejected"
    assert db.query(models.Suppression).filter_by(email="known-opt-out@example.com").first() is None
    assert db.query(models.Contact).filter_by(email="known-opt-out@example.com").one().status == "contacted"


def test_latest_outbound_message_overrides_stale_needs_reply_category(client, db):
    thread = _thread_with_message(
        db,
        sender="customer@example.com",
        subject="Pricing question",
        body="Could you send pricing?",
        suffix="answered",
    )
    thread.intent = "asking_question"
    thread.pending_action = "reply"
    db.add(models.EmailMessage(
        thread_id=thread.id,
        gmail_message_id="human-message-answered-outbound",
        from_email="owner@example.com",
        to_email="customer@example.com",
        subject="Re: Pricing question",
        body_text="Thanks, I have sent the details.",
        is_incoming=False,
    ))
    db.commit()

    rows = client.get("/api/inbox/threads").json()
    row = next(item for item in rows if item["id"] == thread.id)

    assert row["category"] == "awaiting_reply"
    assert row["pending_action"] == "no_action"
    assert row["campaign_id"] is None


def test_list_threads_filters_by_derived_category(client, db):
    answered = _thread_with_message(
        db,
        sender="answered@example.com",
        subject="Answered question",
        body="Can you help?",
        suffix="category-filter-answered",
    )
    answered.intent = "asking_question"
    answered.pending_action = "reply"
    db.add(models.EmailMessage(
        thread_id=answered.id,
        gmail_message_id="category-filter-outbound",
        from_email="owner@example.com",
        to_email="answered@example.com",
        subject="Re: Answered question",
        body_text="Already answered.",
        is_incoming=False,
    ))
    needs_reply = _thread_with_message(
        db,
        sender="waiting@example.com",
        subject="Unanswered question",
        body="Can you help?",
        suffix="category-filter-waiting",
    )
    needs_reply.intent = "asking_question"
    needs_reply.pending_action = "reply"
    db.commit()

    awaiting_rows = client.get("/api/inbox/threads?category=awaiting_reply").json()
    needs_reply_rows = client.get("/api/inbox/threads?category=needs_reply").json()

    assert [row["id"] for row in awaiting_rows] == [answered.id]
    assert [row["id"] for row in needs_reply_rows] == [needs_reply.id]


def test_filtered_mail_never_sets_verified_human_reply(client, db):
    thread = _thread_with_message(
        db,
        sender="noreply@example.com",
        subject="Verification code",
        body="123456",
        suffix="system-reply",
    )
    response = client.post(f"/api/inbox/threads/{thread.id}/analyze")
    assert response.status_code == 200
    assert response.json()["has_human_reply"] is False


def test_chinese_scripted_content_is_always_manual_review_tag():
    with patch("app.services.inbox_triage._llm_human_assessment", return_value=None):
        triage = assess_inbound(
            is_incoming=True,
            from_email="person@example.com",
            subject="hr加入测试名单",
            body="这是测试用邮件，请不要作为客户线索处理。",
        )
    # Scripted/test content must never auto-create a Contact. Offline fallback
    # routes it to human_review (is_human is not True), so a human decides first.
    assert triage.is_human is not True
    assert triage.disposition == "review"
    assert "scripted_content" in triage.tags


def test_offline_fallback_routes_unknown_sender_to_human_review():
    """Regression: when the LLM is unavailable/fails, unverified inbound mail must
    NOT auto-create a Contact. It must route to human_review so a person decides
    before any CRM write (this previously returned is_human=True and silently
    promoted ads/newsletters into Contacts)."""
    with patch("app.services.inbox_triage._llm_human_assessment", return_value=None):
        triage = assess_inbound(
            is_incoming=True,
            from_email="some-unknown-sender@example.com",
            subject="Quick question about your product",
            body="Hi, I saw your website and wanted to ask about pricing.",
        )
    assert triage.is_human is not True
    assert triage.disposition == "review"


def test_dashboard_counts_only_triaged_contact_replies(client, db):
    human = _thread_with_message(
        db,
        sender="human@example.com",
        subject="Question",
        body="Can you help?",
        suffix="metric-human",
    )
    noise = _thread_with_message(
        db,
        sender="noreply@example.com",
        subject="Verification code",
        body="123456",
        suffix="metric-noise",
    )
    human.intent = "asking_question"
    noise.intent = "filtered_system"
    db.add(models.Contact(
        owner_id=1,
        email="human@example.com",
        first_name="Human",
        lifecycle_stage="needs_reply",
        next_action="reply",
    ))
    db.commit()

    metrics = client.get("/api/dashboard/metrics").json()

    assert metrics["replies"] == 1
    assert metrics["positive_replies"] == 1
    assert metrics["needs_reply"] == 1


def test_gmail_body_respects_declared_charset_and_repairs_mojibake():
    text = "您好，这是价格咨询。"
    encoded = base64.urlsafe_b64encode(text.encode("gb18030")).decode("ascii")
    message = {
        "id": "charset-message",
        "threadId": "charset-thread",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "person@example.com"},
                {"name": "To", "value": "owner@example.com"},
                {"name": "Subject", "value": "咨询"},
                {"name": "Content-Type", "value": "text/plain; charset=gb18030"},
            ],
            "body": {"data": encoded},
        },
    }

    parsed = parse_gmail_message(message, owner_email="owner@example.com")

    assert parsed.body_text == text
    assert parsed.is_incoming is True


# ----------------------------------------------------------------------------
# State-machine dynamic determination (plan: customer reply state machine)
# ----------------------------------------------------------------------------

def _dir_thread(db, *, contact_email, suffix, messages):
    account = db.query(models.GmailAccount).first() or _account(db)
    thread = models.EmailThread(
        gmail_account_id=account.id,
        gmail_thread_id=f"dir-{suffix}",
        contact_email=contact_email,
        subject="Pricing",
    )
    db.add(thread)
    db.flush()
    for idx, (incoming, body, day) in enumerate(messages):
        db.add(models.EmailMessage(
            thread_id=thread.id,
            gmail_message_id=f"dir-{suffix}-{idx}",
            from_email=contact_email if incoming else account.email,
            to_email=account.email if incoming else contact_email,
            subject="Pricing",
            body_text=body,
            is_incoming=incoming,
            received_at=datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(days=day),
        ))
    db.commit()
    return thread


def test_we_replied_followup_keeps_awaiting_customer_not_needs_reply(client, db):
    """改动 1: when our latest message is outbound, an inbound follow-up (ack/
    thanks) must NOT flip the contact back to needs_reply — it stays in the
    'we are awaiting the customer' state."""
    db.add(models.Contact(
        owner_id=1, email="dir-customer@example.com", first_name="Customer",
        category="qualified", lifecycle_stage="needs_reply", next_action="reply",
    ))
    db.commit()
    thread = _dir_thread(
        db, contact_email="dir-customer@example.com", suffix="followup",
        messages=[(True, "Can you send pricing?", 1), (False, "Sure, details inside.", 2)],
    )
    from app.api.inbox import _update_contact_from_human
    contact = db.query(models.Contact).filter_by(email="dir-customer@example.com").one()
    _update_contact_from_human(db, contact, thread_id=thread.id, intent="unknown", content_tags=[])
    db.commit()
    db.refresh(contact)
    assert contact.lifecycle_stage == "awaiting_reply"
    assert contact.next_action == "waiting_for_customer"


def test_we_replied_then_new_question_reopens_needs_reply(client, db):
    """A genuinely new question after our reply must re-open needs_reply."""
    db.add(models.Contact(
        owner_id=1, email="dir-customer@example.com",
        lifecycle_stage="awaiting_reply", next_action="waiting_for_customer",
    ))
    db.commit()
    thread = _dir_thread(
        db, contact_email="dir-customer@example.com", suffix="newq",
        messages=[(False, "Here are the details.", 1), (True, "Actually one more question.", 2)],
    )
    from app.api.inbox import _update_contact_from_human
    contact = db.query(models.Contact).filter_by(email="dir-customer@example.com").one()
    _update_contact_from_human(db, contact, thread_id=thread.id, intent="asking_question", content_tags=[])
    db.commit()
    db.refresh(contact)
    assert contact.lifecycle_stage == "needs_reply"
    assert contact.next_action == "reply"


def test_first_inbound_unanswered_stays_needs_reply(client, db):
    """Baseline: an unanswered first inbound still opens needs_reply (unchanged)."""
    db.add(models.Contact(owner_id=1, email="dir-customer@example.com"))
    db.commit()
    thread = _dir_thread(
        db, contact_email="dir-customer@example.com", suffix="first",
        messages=[(True, "Could you help?", 1)],
    )
    from app.api.inbox import _update_contact_from_human
    contact = db.query(models.Contact).filter_by(email="dir-customer@example.com").one()
    _update_contact_from_human(db, contact, thread_id=thread.id, intent="asking_question", content_tags=[])
    db.commit()
    db.refresh(contact)
    assert contact.lifecycle_stage == "needs_reply"
    assert contact.next_action == "reply"


def test_clear_stale_review_resolves_known_contact_thread(client, db):
    """改动 3: a stale human_review thread that already belongs to a confirmed
    Contact can be cleared deterministically (no LLM, no apply_intent_actions)."""
    db.add(models.Contact(owner_id=1, email="dir-stale@example.com", first_name="Known"))
    db.commit()
    thread = _dir_thread(
        db, contact_email="dir-stale@example.com", suffix="stale",
        messages=[(True, "garbled ???", 1)],
    )
    thread.intent = "unknown"
    thread.pending_action = "human_review"
    db.commit()
    resp = client.post(f"/api/inbox/threads/{thread.id}/clear-stale-review")
    assert resp.status_code == 200
    assert resp.json()["review_kind"] is None
    db.refresh(thread)
    assert thread.pending_action == "no_action"


def test_latest_outbound_clears_stale_review_without_reanalyzing_old_inbound(client, db):
    db.add(models.Contact(owner_id=1, email="outbound-stale@example.com", first_name="Known"))
    db.commit()
    thread = _dir_thread(
        db, contact_email="outbound-stale@example.com", suffix="latest-outbound",
        messages=[(True, "Please share pricing", 1), (False, "Here are the details", 2)],
    )
    thread.intent = "interested"
    thread.pending_action = "human_review"
    db.commit()
    with patch("app.api.inbox.Orchestrator.analyze") as analyze:
        response = client.post(f"/api/inbox/threads/{thread.id}/analyze")
    assert response.status_code == 200
    assert response.json()["category"] == "awaiting_reply"
    assert response.json()["pending_action"] == "no_action"
    assert response.json()["review_kind"] is None
    analyze.assert_not_called()


def test_clear_stale_review_refuses_unknown_sender(client, db):
    """改动 3: an unknown sender with no outbound cannot be auto-cleared."""
    account = db.query(models.GmailAccount).first() or _account(db)
    thread = models.EmailThread(
        gmail_account_id=account.id, gmail_thread_id="dir-unknown-stale",
        contact_email="dir-unknown@example.com", subject="x", intent="unknown",
    )
    db.add(thread)
    db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id, gmail_message_id="dir-unknown-stale-0",
        from_email="dir-unknown@example.com", to_email=account.email,
        subject="x", body_text="?", is_incoming=True,
        received_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    ))
    thread.pending_action = "human_review"
    db.commit()
    resp = client.post(f"/api/inbox/threads/{thread.id}/clear-stale-review")
    assert resp.status_code == 409
    assert "stale_review_not_clearable" in resp.json()["detail"]
    db.refresh(thread)
    assert thread.pending_action == "human_review"


def test_clear_stale_review_refuses_non_human_review(client, db):
    account = db.query(models.GmailAccount).first() or _account(db)
    thread = models.EmailThread(
        gmail_account_id=account.id, gmail_thread_id="dir-notreview",
        contact_email="dir-any@example.com", subject="x",
    )
    db.add(thread)
    db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id, gmail_message_id="dir-notreview-0",
        from_email="dir-any@example.com", to_email=account.email,
        subject="x", body_text="?", is_incoming=True,
        received_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    ))
    thread.pending_action = "reply"
    db.commit()
    resp = client.post(f"/api/inbox/threads/{thread.id}/clear-stale-review")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "not_human_review"


# ----------------------------------------------------------------------------
# A+B fix: contact tag accumulation + idempotent re-triage (no churn)
# ----------------------------------------------------------------------------

def test_content_tags_accumulate_across_emails_and_retriage_is_idempotent(client, db):
    """A+B fix: a Contact's content tags accumulate across separate emails instead
    of being reset to the latest email's perspective, and re-analyzing the same
    message is a no-op (idempotent) -- no tag churn, no extra AuditLog."""
    from app.api.inbox import _update_contact_from_human

    db.add(models.Contact(
        owner_id=1, email="acc@example.com", first_name="Acc",
        tags=json.dumps(["inbox", "human"]),
    ))
    db.commit()
    contact = db.query(models.Contact).filter_by(email="acc@example.com").one()

    # Email 1 mentions pricing.
    _update_contact_from_human(
        db, contact, thread_id=1, intent="asking_question", content_tags=["pricing"],
    )
    db.commit(); db.refresh(contact)
    assert "pricing" in set(json.loads(contact.tags))

    # Email 2 mentions a demo -- pricing must survive, demo_request added.
    _update_contact_from_human(
        db, contact, thread_id=1, intent="interested", content_tags=["demo_request"],
    )
    db.commit(); db.refresh(contact)
    tags_after_2 = set(json.loads(contact.tags))
    assert {"pricing", "demo_request", "interested", "inbox", "human"} <= tags_after_2

    # Re-analyzing the same latest email (idempotent) must not change tags or
    # emit a duplicate AuditLog.
    audit_before = db.query(models.AuditLog).filter_by(
        entity="contact", entity_id=str(contact.id),
        action="contact_state_updated_from_conversation",
    ).count()
    _update_contact_from_human(
        db, contact, thread_id=1, intent="interested", content_tags=["demo_request"],
    )
    db.commit(); db.refresh(contact)
    tags_after_repeat = set(json.loads(contact.tags))
    assert tags_after_repeat == tags_after_2
    audit_after = db.query(models.AuditLog).filter_by(
        entity="contact", entity_id=str(contact.id),
        action="contact_state_updated_from_conversation",
    ).count()
    assert audit_after == audit_before


def test_analyze_endpoint_accumulates_content_tags_across_two_messages(client, db):
    """End-to-end via the analyze endpoint: two emails in a thread accumulate
    content tags on the Contact rather than resetting to the last email."""
    contact = models.Contact(owner_id=1, email="acc2@example.com", first_name="Acc2")
    db.add(contact); db.commit()

    thread = _thread_with_message(
        db, sender="acc2@example.com", subject="Pricing", body="Could I get pricing?", suffix="acc2-a",
    )
    # Pin an explicit timeline so the second inbound is unambiguously "latest".
    thread.messages[0].received_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    db.commit()
    decision_a = SimpleNamespace(intent="asking_question", summary="Pricing", recommended_action="reply")
    triage_a = TriageResult("human", True, 0.98, ["pricing"], "Direct person")
    with patch("app.api.inbox.assess_inbound", return_value=triage_a), patch(
        "app.api.inbox.Orchestrator.analyze", return_value=decision_a
    ):
        client.post(f"/api/inbox/threads/{thread.id}/analyze")

    # A second, later inbound message about a demo.
    account = db.query(models.GmailAccount).first()
    db.add(models.EmailMessage(
        thread_id=thread.id, gmail_message_id="acc2-b",
        from_email="acc2@example.com", to_email=account.email,
        subject="Re: Pricing", body_text="Also interested in a demo.",
        is_incoming=True, received_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
    ))
    db.commit()
    decision_b = SimpleNamespace(intent="interested", summary="Demo", recommended_action="reply")
    triage_b = TriageResult("human", True, 0.98, ["demo_request"], "Direct person")
    with patch("app.api.inbox.assess_inbound", return_value=triage_b), patch(
        "app.api.inbox.Orchestrator.analyze", return_value=decision_b
    ):
        client.post(f"/api/inbox/threads/{thread.id}/analyze")

    db.refresh(contact)
    tags = set(json.loads(contact.tags))
    # Both pricing (from message 1) and demo_request (from message 2) survive.
    assert {"pricing", "demo_request", "interested", "inbox", "human"} <= tags


# ----------------------------------------------------------------------------
# "Needs reply" single source of truth: cached state derived from live threads
# ----------------------------------------------------------------------------

def test_recompute_flips_outbound_only_contact_to_awaiting(client, db):
    """The reported false-positive: all threads are outbound-latest (we replied,
    awaiting the customer) yet the cached field was stuck at needs_reply/reply.
    recompute must correct it to awaiting_reply/waiting_for_customer."""
    from app.services.inbox_triage import recompute_contact_reply_state

    db.add(models.Contact(
        owner_id=1, email="recompute-out@example.com",
        lifecycle_stage="needs_reply", next_action="reply",
    ))
    db.commit()
    _dir_thread(
        db, contact_email="recompute-out@example.com", suffix="ro",
        messages=[
            (False, "Here are the details.", 1),
            (True, "Thanks!", 2),
            (False, "You're welcome.", 3),
        ],
    )
    contact = db.query(models.Contact).filter_by(email="recompute-out@example.com").one()
    changed = recompute_contact_reply_state(db, contact)
    db.commit()
    db.refresh(contact)
    assert changed is True
    assert contact.lifecycle_stage == "awaiting_reply"
    assert contact.next_action == "waiting_for_customer"


def test_recompute_flags_latest_inbound_thread_as_needs_reply(client, db):
    """A contact whose latest message is an inbound question must be (re)opened
    as needs_reply/reply, even if it was previously awaiting the customer."""
    from app.services.inbox_triage import recompute_contact_reply_state

    db.add(models.Contact(
        owner_id=1, email="recompute-in@example.com",
        lifecycle_stage="awaiting_reply", next_action="waiting_for_customer",
    ))
    db.commit()
    _dir_thread(
        db, contact_email="recompute-in@example.com", suffix="ri",
        messages=[
            (False, "We sent a proposal.", 1),
            (True, "Can you clarify pricing?", 2),
        ],
    )
    # The latest inbound message must be classified for _thread_category to map
    # it to needs_reply (mirrors how a synced+analyzed thread carries intent).
    thread = db.query(models.EmailThread).filter_by(contact_email="recompute-in@example.com").one()
    thread.intent = "asking_question"
    db.commit()
    contact = db.query(models.Contact).filter_by(email="recompute-in@example.com").one()
    changed = recompute_contact_reply_state(db, contact)
    db.commit()
    db.refresh(contact)
    assert changed is True
    assert contact.lifecycle_stage == "needs_reply"
    assert contact.next_action == "reply"


def test_recompute_keeps_stopped_and_human_review_protected(client, db):
    """Terminal/review states must never be downgraded by recompute, even when a
    thread's latest message would otherwise imply needs_reply."""
    from app.services.inbox_triage import recompute_contact_reply_state

    stopped = models.Contact(
        owner_id=1, email="recompute-stop@example.com",
        lifecycle_stage="stopped", next_action="none",
    )
    hr = models.Contact(
        owner_id=1, email="recompute-hr@example.com",
        lifecycle_stage="new_customer", next_action="human_review",
    )
    db.add_all([stopped, hr])
    db.commit()
    _dir_thread(
        db, contact_email="recompute-stop@example.com", suffix="rs",
        messages=[(True, "Please remove me, also a question", 1)],
    )
    _dir_thread(
        db, contact_email="recompute-hr@example.com", suffix="rh",
        messages=[(True, "A question", 1)],
    )
    db.commit()
    s = db.query(models.Contact).filter_by(email="recompute-stop@example.com").one()
    h = db.query(models.Contact).filter_by(email="recompute-hr@example.com").one()
    assert recompute_contact_reply_state(db, s) is False
    assert recompute_contact_reply_state(db, h) is False
    db.refresh(s)
    db.refresh(h)
    assert s.lifecycle_stage == "stopped" and s.next_action == "none"
    assert h.lifecycle_stage == "new_customer" and h.next_action == "human_review"


def test_dashboard_needs_reply_uses_live_direction_not_cache(client, db):
    """Dashboard /metrics needs_reply must reflect live thread direction, so a
    contact whose cache says needs_reply but whose threads are all outbound is
    NOT counted, while a contact whose cache says awaiting but has a live inbound
    question IS counted."""
    human = _thread_with_message(
        db, sender="human@example.com", subject="Question", body="Can you help?",
        suffix="metric-live",
    )
    human.intent = "asking_question"
    # Real contact: latest thread is inbound asking_question -> truly needs reply.
    # Cache says awaiting_reply, so a STALE cached count would MISS it.
    db.add(models.Contact(
        owner_id=1, email="human@example.com",
        first_name="Human", lifecycle_stage="awaiting_reply",
        next_action="waiting_for_customer",
    ))
    # Stale cached needs_reply/reply contact with NO matching inbound thread.
    db.add(models.Contact(
        owner_id=1, email="stale-needs@example.com",
        first_name="Stale", lifecycle_stage="needs_reply", next_action="reply",
    ))
    db.commit()

    metrics = client.get("/api/dashboard/metrics").json()
    # human@example.com -> live inbound needs_reply => counted (1)
    # stale-needs@example.com -> no thread at all => NOT counted by live check
    assert metrics["needs_reply"] == 1
    assert metrics["positive_replies"] == 1
