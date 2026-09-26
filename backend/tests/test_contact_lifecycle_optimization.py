import json
from datetime import datetime, timezone
from io import BytesIO

from app import models


def _campaign(client, name="Lifecycle Campaign"):
    response = client.post("/api/campaigns", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_lead_import_requires_prospect_system_category(client):
    csv_data = (
        b"email,first_name,last_name,system_category,segments,tags\n"
        b"missing@example.com,Missing,Category,,IT,source-a\n"
        b"qualified@example.com,Qualified,Lead,qualified,IT,source-b\n"
        b"prospect@example.com,Prospect,Lead,prospect,Finance; IT,priority; event\n"
    )
    preview = client.post(
        "/api/contacts/import?confirm=false&mode=lead",
        files={"file": ("leads.csv", BytesIO(csv_data), "text/csv")},
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()["preview"]
    assert payload["valid"] == 1
    assert payload["invalid"] == 2
    assert any("system_category_required" in row["errors"] for row in payload["errors"])
    assert any("new_lead_category_must_be_prospect" in row["errors"] for row in payload["errors"])

    confirmed = client.post(
        "/api/contacts/import?confirm=true&mode=lead",
        files={"file": ("leads.csv", BytesIO(csv_data), "text/csv")},
    )
    assert confirmed.status_code == 200, confirmed.text
    row = client.get("/api/contacts").json()[0]
    assert row["category"] == "prospect"
    assert row["system_category"] == "prospect"
    assert row["segments"] == ["Finance", "IT"]
    assert row["tags"] == ["priority", "event"]
    assert row["lifecycle_stage"] == "new_customer"
    assert row["next_action"] == "review"


def test_contact_filters_and_category_intent_are_independent(client):
    contact = client.post("/api/contacts", json={
        "email": "filter@example.com", "first_name": "Filter",
        "category": "prospect", "intent_level": "unknown",
        "segments": ["IT"], "tags": ["Priority"],
    }).json()
    assert len(client.get("/api/contacts?category=prospect&segments_any=it").json()) == 1
    assert len(client.get("/api/contacts?tags_any=priority").json()) == 1

    updated = client.put(
        f"/api/contacts/{contact['id']}",
        json={"intent_level": "high"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["category"] == "prospect"
    assert updated.json()["intent_level"] == "high"

    updated = client.put(
        f"/api/contacts/{contact['id']}",
        json={"category": "customer"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["category"] == "customer"
    assert updated.json()["intent_level"] == "high"


def test_qualified_transition_closes_only_source_campaign_and_unsent_work(client, db):
    campaign_id = _campaign(client)
    contact = client.post("/api/contacts", json={
        "email": "reply@example.com", "first_name": "Reply", "category": "prospect",
        "segments": ["IT"],
    }).json()
    added = client.post(
        f"/api/campaigns/{campaign_id}/contacts",
        json={"contact_ids": [contact["id"]]},
    )
    assert added.status_code == 200, added.text
    cc = db.query(models.CampaignContact).filter_by(
        campaign_id=campaign_id, contact_id=contact["id"],
    ).one()
    account = models.GmailAccount(user_id=1, email="owner@example.com")
    db.add(account)
    db.flush()
    draft = models.EmailDraft(
        gmail_account_id=account.id, campaign_contact_id=cc.id,
        to_email="reply@example.com", subject="Hello", body_text="Hello", status="draft",
    )
    db.add(draft)
    db.flush()
    approval = models.Approval(
        kind="first_send", campaign_id=campaign_id, campaign_contact_id=cc.id,
        draft_id=draft.id, to_email="reply@example.com", subject="Hello",
        body_text="Hello", status="pending",
    )
    task = models.FollowUpTask(
        campaign_contact_id=cc.id, contact_id=contact["id"], campaign_id=campaign_id,
        scheduled_at=datetime.now(timezone.utc), status="scheduled",
    )
    db.add_all([approval, task])
    db.commit()

    response = client.post(
        f"/api/contacts/{contact['id']}/transition",
        json={"action": "qualify", "campaign_id": campaign_id,
              "intent": "interested", "reason": "explicit sales reply"},
    )
    assert response.status_code == 200, response.text
    db.refresh(cc)
    db.refresh(draft)
    db.refresh(approval)
    db.refresh(task)
    converted = client.get("/api/contacts").json()[0]
    assert converted["category"] == "qualified"
    assert converted["intent_level"] == "high"
    assert cc.membership_active is False
    assert cc.status == "converted"
    assert cc.removed_reason == "qualified_conversion"
    assert draft.status == "cancelled"
    assert approval.status == "expired"
    assert task.status == "cancelled"
    assert db.query(models.AuditLog).filter_by(action="contact_transition_qualify").count() == 1


def test_qualified_transition_requires_campaign_when_contact_has_multiple_active_campaigns(client):
    first_campaign = _campaign(client, "First")
    second_campaign = _campaign(client, "Second")
    contact = client.post("/api/contacts", json={
        "email": "multi@example.com", "first_name": "Multi", "category": "prospect",
    }).json()
    for campaign_id in (first_campaign, second_campaign):
        assert client.post(
            f"/api/campaigns/{campaign_id}/contacts", json={"contact_ids": [contact["id"]]},
        ).status_code == 200
    response = client.post(
        f"/api/contacts/{contact['id']}/transition",
        json={"action": "qualify", "intent": "asking_question"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "source_campaign_required_multiple_memberships"


def test_qualified_transition_requires_a_campaign_membership(client):
    contact = client.post("/api/contacts", json={
        "email": "inbox-only@example.com", "first_name": "Inbox",
        "category": "prospect",
    }).json()
    response = client.post(
        f"/api/contacts/{contact['id']}/transition",
        json={"action": "qualify", "intent": "interested"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "source_campaign_membership_required"
