from app import models


def test_rejecting_approval_cancels_its_unsent_draft(client, db):
    account = models.GmailAccount(user_id=1, email="sender@example.com", is_connected=True)
    db.add(account)
    db.flush()
    draft = models.EmailDraft(
        gmail_account_id=account.id,
        to_email="customer@example.com",
        subject="Subject",
        body_text="Body",
        status="draft",
    )
    db.add(draft)
    db.flush()
    approval = models.Approval(
        kind="reply",
        draft_id=draft.id,
        to_email="customer@example.com",
        subject="Subject",
        body_text="Body",
        status="pending",
    )
    db.add(approval)
    db.commit()

    response = client.post(
        f"/api/approvals/{approval.id}/decision",
        json={"decision": "reject", "rejection_reason": "operator declined"},
    )
    assert response.status_code == 200, response.text
    db.refresh(approval)
    db.refresh(draft)
    assert approval.status == "rejected"
    assert draft.status == "cancelled"
