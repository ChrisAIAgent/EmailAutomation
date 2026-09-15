from types import SimpleNamespace

from app import models
from app.agents.orchestrator import Orchestrator
from app.agents.openclaw_adapter import OpenClawAdapter
from app.schemas import ApprovalRevisionInput, EmailProposal
from app.services import approvals as approval_svc
from app.tools.email_tools import UnifiedEmailToolLayer


def _records(db, *, with_thread: bool):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    campaign = models.Campaign(owner_id=1, name="Campaign", status="active", product_description="Email automation")
    contact = models.Contact(owner_id=1, email="customer@example.com", first_name="Chris", status="contacted")
    db.add_all([account, campaign, contact])
    db.flush()
    member = models.CampaignContact(campaign_id=campaign.id, contact_id=contact.id, status="queued")
    db.add(member)
    thread = None
    message = None
    if with_thread:
        thread = models.EmailThread(
            gmail_account_id=account.id, gmail_thread_id="gmail-thread-1",
            contact_email=contact.email, campaign_id=campaign.id, subject="Original subject",
        )
        db.add(thread)
        db.flush()
        message = models.EmailMessage(
            thread_id=thread.id, gmail_message_id="gmail-message-1",
            message_id_header="<parent@example.com>", references_header="<root@example.com>",
            from_email=contact.email, to_email=account.email, subject="Original subject",
            body_text="Can you explain the workflow?", is_incoming=True,
        )
        db.add(message)
    db.flush()
    return account, campaign, contact, member, thread, message


def _capture_draft(monkeypatch, db):
    created, updated = {}, {}

    def create_draft(self, **kwargs):
        created.update(kwargs)
        draft = models.EmailDraft(
            gmail_account_id=self.account.id, gmail_draft_id="draft-1",
            to_email=kwargs["to"], subject=kwargs["subject"], body_text=kwargs["body_text"],
            body_html=kwargs.get("body_html", ""), kind=kwargs.get("kind", "outreach"), status="draft",
        )
        db.add(draft)
        db.flush()
        return {"ok": True, "draft": draft, "gmail_draft_id": "draft-1"}

    def update_draft(self, **kwargs):
        updated.update(kwargs)
        draft = db.get(models.EmailDraft, kwargs["draft_db_id"])
        draft.subject = kwargs["subject"]
        draft.body_text = kwargs["body_text"]
        draft.body_html = kwargs.get("body_html", "")
        db.flush()
        return {"ok": True, "draft": draft}

    monkeypatch.setattr(UnifiedEmailToolLayer, "create_draft", create_draft)
    monkeypatch.setattr(UnifiedEmailToolLayer, "update_draft", update_draft)
    return created, updated


def _proposal(subject="Revised subject", body="Thanks for your question. We can confirm the workflow in a short call."):
    return EmailProposal(
        agent="langgraph", run_id="revision-run", subject=subject, body_text=body,
        intent="asking_question", confidence=1.0, summary="revised",
        recommended_action="human_review", reasoning_summary="revision", risk_level="low",
        requires_approval=True, latency_ms=5, model="test-model", prompt_version="test",
    )


def test_revise_inbox_reply_updates_existing_draft_and_keeps_thread_headers(client, db, monkeypatch):
    _, campaign, contact, member, thread, message = _records(db, with_thread=True)
    _, updated = _capture_draft(monkeypatch, db)
    approval = approval_svc.create_reply_approval(
        db, thread=thread, contact=contact, campaign=campaign, cc=member,
        subject="Wrong subject", body_text="Original body", source_message=message,
    )
    db.commit()
    monkeypatch.setattr(Orchestrator, "revise_approval", lambda self, inp: _proposal("Changed subject"))

    response = client.post(
        f"/api/approvals/{approval.id}/revise",
        json={"instruction": "Make it concise and keep the next step."},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    db.refresh(approval)
    draft = db.get(models.EmailDraft, approval.draft_id)
    assert payload["approval_id"] == approval.id
    assert payload["status"] == "pending"
    assert payload["sent"] is False
    assert approval.subject == "Original subject"
    assert draft.subject == "Original subject"
    assert approval.body_text == draft.body_text
    assert updated["thread_gmail_id"] == "gmail-thread-1"
    assert updated["in_reply_to"] == "<parent@example.com>"
    assert updated["references"] == "<root@example.com> <parent@example.com>"
    assert db.query(models.Approval).count() == 1
    audit = db.query(models.AuditLog).filter_by(action="approval_revised", entity_id=str(approval.id)).one()
    assert "Make it concise" not in (audit.detail or "")
    assert "instruction_hash" in (audit.detail or "")


def test_revise_campaign_first_send_can_change_subject_in_place(client, db, monkeypatch):
    _, _, contact, member, _, _ = _records(db, with_thread=False)
    _, updated = _capture_draft(monkeypatch, db)
    original = SimpleNamespace(
        subject="Original subject", body_text="Original body", body_html="",
        recommended_action="human_review", risk_level="low", run_id="first", model="m", prompt_version="p", latency_ms=1,
    )
    approval = approval_svc.create_outreach_approval(db, member, original)
    db.commit()
    monkeypatch.setattr(Orchestrator, "revise_approval", lambda self, inp: _proposal("New campaign subject"))

    response = client.post(
        f"/api/approvals/{approval.id}/revise",
        json={"instruction": "Use a clearer value-oriented subject."},
    )
    assert response.status_code == 200, response.text
    db.refresh(approval)
    draft = db.get(models.EmailDraft, approval.draft_id)
    assert approval.subject == "New campaign subject"
    assert draft.subject == "New campaign subject"
    assert updated["thread_gmail_id"] is None
    assert approval.to_email == contact.email
    assert approval.status == "pending"
    assert db.query(models.Approval).count() == 1


def test_failed_revision_leaves_original_content_unchanged(client, db, monkeypatch):
    _, _, _, member, _, _ = _records(db, with_thread=False)
    _capture_draft(monkeypatch, db)
    original = SimpleNamespace(
        subject="Original subject", body_text="Original body", body_html="",
        recommended_action="human_review", risk_level="low", run_id="first", model="m", prompt_version="p", latency_ms=1,
    )
    approval = approval_svc.create_outreach_approval(db, member, original)
    db.commit()
    monkeypatch.setattr(Orchestrator, "revise_approval", lambda self, inp: None)

    response = client.post(
        f"/api/approvals/{approval.id}/revise",
        json={"instruction": "Rewrite this."},
    )
    assert response.status_code == 503
    db.refresh(approval)
    draft = db.get(models.EmailDraft, approval.draft_id)
    assert approval.subject == "Original subject"
    assert approval.body_text == "Original body"
    assert draft.subject == "Original subject"
    assert draft.body_text == "Original body"
    failure = db.query(models.AuditLog).filter_by(action="approval_revision_failed", entity_id=str(approval.id)).one()
    assert "Rewrite this" not in (failure.detail or "")


def test_revision_with_missing_remote_draft_id_preserves_pending_copy(client, db, monkeypatch):
    account, campaign, contact, member, _, _ = _records(db, with_thread=False)
    draft = models.EmailDraft(
        gmail_account_id=account.id, gmail_draft_id=None,
        campaign_contact_id=member.id, to_email=contact.email,
        subject="Original subject", body_text="Original body", status="draft",
    )
    db.add(draft)
    db.flush()
    approval = models.Approval(
        kind="first_send", campaign_id=campaign.id, campaign_contact_id=member.id,
        draft_id=draft.id, to_email=contact.email, subject=draft.subject,
        body_text=draft.body_text, status="pending",
    )
    db.add(approval)
    db.commit()
    calls = []
    monkeypatch.setattr(Orchestrator, "revise_approval", lambda self, inp: calls.append(inp) or _proposal())

    response = client.post(
        f"/api/approvals/{approval.id}/revise",
        json={"instruction": "Make this more professional."},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "approval_revision_draft_remote_id_missing"
    assert calls == []
    db.refresh(approval)
    db.refresh(draft)
    assert approval.status == "pending"
    assert approval.subject == "Original subject"
    assert draft.status == "draft"
    assert draft.body_text == "Original body"


def test_update_draft_rejects_missing_remote_draft_id_without_transport_call(db, monkeypatch):
    account, _, contact, member, _, _ = _records(db, with_thread=False)
    draft = models.EmailDraft(
        gmail_account_id=account.id, gmail_draft_id=None,
        campaign_contact_id=member.id, to_email=contact.email,
        subject="Original subject", body_text="Original body", status="draft",
    )
    db.add(draft)
    db.commit()
    called = []
    monkeypatch.setattr(
        UnifiedEmailToolLayer, "_transport",
        lambda self: SimpleNamespace(update_draft=lambda *args: called.append(args)),
    )

    result = UnifiedEmailToolLayer(db, account, None).update_draft(
        draft_db_id=draft.id, to=draft.to_email, subject="New subject",
        body_text="New body", agent="langgraph", mode="langgraph_only",
    )
    assert result == {"ok": False, "error": "draft_remote_id_missing"}
    assert called == []
    db.refresh(draft)
    assert draft.subject == "Original subject"
    assert draft.body_text == "Original body"


def test_approval_edit_does_not_send_when_draft_update_fails(db, monkeypatch):
    account, campaign, contact, member, _, _ = _records(db, with_thread=False)
    draft = models.EmailDraft(
        gmail_account_id=account.id, gmail_draft_id="draft-1",
        campaign_contact_id=member.id, to_email=contact.email,
        subject="Original subject", body_text="Original body", status="draft",
    )
    db.add(draft)
    db.flush()
    approval = models.Approval(
        kind="first_send", campaign_id=campaign.id, campaign_contact_id=member.id,
        draft_id=draft.id, to_email=contact.email, subject=draft.subject,
        body_text=draft.body_text, status="pending", idempotency_key="approval-edit-test",
    )
    db.add(approval)
    db.commit()
    sent = []
    monkeypatch.setattr(
        UnifiedEmailToolLayer, "update_draft",
        lambda self, **kwargs: {"ok": False, "error": "draft_remote_id_missing"},
    )
    monkeypatch.setattr(
        UnifiedEmailToolLayer, "send_approved_draft",
        lambda self, **kwargs: sent.append(kwargs) or {"ok": True},
    )

    result = approval_svc.decide_approval(
        db, approval.id, "approve", edited_body_text="Requested new body",
    )
    assert result == {
        "ok": False,
        "blocked": "approval_draft_update_failed:draft_remote_id_missing",
        "status": "pending",
    }
    assert sent == []
    db.refresh(approval)
    db.refresh(draft)
    assert approval.status == "pending"
    assert approval.body_text == "Original body"
    assert draft.body_text == "Original body"


def test_revision_is_idempotent_for_the_latest_same_instruction(client, db, monkeypatch):
    _, _, _, member, _, _ = _records(db, with_thread=False)
    _, updated = _capture_draft(monkeypatch, db)
    original = SimpleNamespace(
        subject="Original subject", body_text="Original body", body_html="",
        recommended_action="human_review", risk_level="low", run_id="first", model="m", prompt_version="p", latency_ms=1,
    )
    approval = approval_svc.create_outreach_approval(db, member, original)
    db.commit()
    calls = []

    def revise(self, inp):
        calls.append(inp)
        return _proposal()

    monkeypatch.setattr(Orchestrator, "revise_approval", revise)
    body = {"instruction": "Improve the call to action."}
    first = client.post(f"/api/approvals/{approval.id}/revise", json=body)
    second = client.post(f"/api/approvals/{approval.id}/revise", json=body)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["idempotent_replay"] is True
    assert len(calls) == 1
    assert updated["draft_db_id"] == approval.draft_id


def test_revision_requires_pending_owned_approval_and_bounded_instruction(client, db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    draft = models.EmailDraft(gmail_account_id=1, to_email="customer@example.com", subject="S", body_text="B", status="draft")
    db.add(account)
    db.flush()
    draft.gmail_account_id = account.id
    db.add(draft)
    db.flush()
    approval = models.Approval(kind="first_send", draft_id=draft.id, to_email=draft.to_email, subject="S", body_text="B", status="approved")
    db.add(approval)
    db.commit()

    not_pending = client.post(f"/api/approvals/{approval.id}/revise", json={"instruction": "Revise."})
    too_long = client.post(f"/api/approvals/{approval.id}/revise", json={"instruction": "x" * 2001})
    assert not_pending.status_code == 409
    assert too_long.status_code == 422


def test_openclaw_revision_forwards_current_draft_and_instruction(monkeypatch):
    captured = {}
    adapter = OpenClawAdapter()

    def call(method, payload):
        captured["method"] = method
        captured["payload"] = payload
        return SimpleNamespace(
            agent="openclaw", run_id="oc-run", intent="asking_question", confidence=1.0,
            summary="revised", recommended_action="human_review", reasoning_summary="revision",
            risk_level="low", requires_approval=True,
            draft=SimpleNamespace(subject="Subject", body_text="Body", body_html=""),
            latency_ms=1, model="openclaw", prompt_version="test",
        )

    monkeypatch.setattr(adapter, "_call", call)
    inp = ApprovalRevisionInput(
        approval_id=9, instruction="Make it concise.", kind="reply",
        current_subject="Subject", current_body_text="Original", thread_id=4,
    )
    proposal = adapter.revise_approval(inp)
    assert proposal is not None
    assert captured["method"] == "revise_approval"
    assert captured["payload"]["instruction"] == "Make it concise."
    assert captured["payload"]["current_body_text"] == "Original"
