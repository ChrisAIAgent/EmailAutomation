from datetime import datetime, timedelta, timezone

from app import models
from app.services import followup as followup_svc


def _campaign_member(db, *, status="queued"):
    campaign = models.Campaign(owner_id=1, name="Membership", status="active")
    contact = models.Contact(
        owner_id=1, email="member@example.com", first_name="Member",
        category="qualified", status="new",
    )
    db.add_all([campaign, contact])
    db.flush()
    member = models.CampaignContact(
        campaign_id=campaign.id, contact_id=contact.id,
        status=status, membership_active=True,
    )
    db.add(member)
    db.flush()
    return campaign, contact, member


def test_remove_campaign_contact_preserves_history_and_cancels_pending(client, db):
    campaign, contact, member = _campaign_member(db, status="outreach_generated")
    account = models.GmailAccount(user_id=1, email="sender@example.com", is_connected=True)
    db.add(account)
    db.flush()
    draft = models.EmailDraft(
        gmail_account_id=account.id, campaign_contact_id=member.id,
        to_email=contact.email, subject="Hello", body_text="Body", status="draft",
    )
    db.add(draft)
    db.flush()
    approval = models.Approval(
        kind="first_send", campaign_id=campaign.id,
        campaign_contact_id=member.id, draft_id=draft.id,
        to_email=contact.email, subject="Hello", body_text="Body", status="pending",
    )
    task = models.FollowUpTask(
        campaign_contact_id=member.id, contact_id=contact.id,
        campaign_id=campaign.id, sequence=1,
        scheduled_at=datetime.now(timezone.utc), status="scheduled",
    )
    db.add_all([approval, task])
    db.commit()

    response = client.delete(f"/api/campaigns/{campaign.id}/contacts/{contact.id}")
    assert response.status_code == 200, response.text
    assert response.json()["membership_active"] is False
    assert response.json()["historical_records_preserved"] is True
    assert response.json()["cancelled_pending_items"] == 2

    db.refresh(member); db.refresh(contact); db.refresh(draft); db.refresh(approval); db.refresh(task)
    assert db.get(models.CampaignContact, member.id) is not None
    assert db.get(models.Contact, contact.id) is not None
    assert member.membership_active is False
    assert member.removed_reason == "operator_removed_from_campaign"
    assert draft.status == "cancelled"
    assert approval.status == "expired"
    assert approval.rejection_reason == "campaign_contact_removed"
    assert task.status == "cancelled"
    assert task.last_error == "campaign_contact_removed"
    assert db.query(models.Suppression).filter_by(email=contact.email).count() == 0

    assert client.get(f"/api/campaigns/{campaign.id}/contacts").json() == []
    history = client.get(
        f"/api/campaigns/{campaign.id}/contacts?include_removed=true"
    ).json()
    assert len(history) == 1
    assert history[0]["membership_active"] is False


def test_readding_removed_member_reuses_same_history_row(client, db):
    campaign, contact, member = _campaign_member(db)
    member.membership_active = False
    member.removed_at = datetime.now(timezone.utc)
    member.removed_reason = "operator_removed_from_campaign"
    db.commit()

    response = client.post(
        f"/api/campaigns/{campaign.id}/contacts",
        json={"contact_ids": [contact.id]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["added"] == 1
    db.refresh(member)
    assert member.membership_active is True
    assert member.removed_at is None
    assert member.removed_reason is None
    assert db.query(models.CampaignContact).filter_by(
        campaign_id=campaign.id, contact_id=contact.id
    ).count() == 1


def test_readding_sent_member_does_not_queue_duplicate_first_send(client, db):
    campaign, contact, member = _campaign_member(db, status="sent")
    member.membership_active = False
    member.removed_at = datetime.now(timezone.utc)
    member.removed_reason = "operator_removed_from_campaign"
    db.add(models.Approval(
        kind="first_send", campaign_id=campaign.id,
        campaign_contact_id=member.id, to_email=contact.email,
        subject="Previously sent", body_text="History", status="approved",
    ))
    db.commit()

    response = client.post(
        f"/api/campaigns/{campaign.id}/contacts",
        json={"contact_ids": [contact.id]},
    )
    assert response.status_code == 200, response.text
    db.refresh(member)
    assert member.membership_active is True
    assert member.status == "sent"
    assert db.query(models.Approval).filter_by(
        campaign_contact_id=member.id, kind="first_send"
    ).count() == 1


def test_due_follow_up_for_removed_member_is_cancelled_without_generation(db):
    campaign, contact, member = _campaign_member(db, status="sent")
    member.membership_active = False
    task = models.FollowUpTask(
        campaign_contact_id=member.id, contact_id=contact.id,
        campaign_id=campaign.id, sequence=1,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        status="scheduled",
    )
    db.add(task)
    db.commit()

    class NoGenerationExpected:
        def generate_follow_up(self, *args, **kwargs):
            raise AssertionError("removed Campaign member reached generation")

    outcomes = followup_svc.process_due_follow_ups(db, NoGenerationExpected())
    db.commit()
    db.refresh(task)
    assert outcomes == []
    assert task.status == "cancelled"
    assert task.last_error == "campaign_contact_removed"
    assert db.query(models.EmailDraft).filter_by(
        campaign_contact_id=member.id
    ).count() == 0
    assert db.query(models.Approval).filter_by(
        campaign_contact_id=member.id
    ).count() == 0


def test_campaign_limits_reject_invalid_values(client):
    bad_daily = client.post("/api/campaigns", json={"name": "Bad", "daily_send_limit": 0})
    assert bad_daily.status_code == 422
    bad_followups = client.post("/api/campaigns", json={"name": "Bad", "max_follow_ups": -1})
    assert bad_followups.status_code == 422
