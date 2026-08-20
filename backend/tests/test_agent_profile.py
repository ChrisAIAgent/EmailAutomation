from app import models
from app.services.agent_profile import apply_profile_to_reply, resolve_profile


def test_agent_profile_api_creates_and_updates_default(client, db):
    current = client.get("/api/agent-profile")
    assert current.status_code == 200
    assert current.json()["agent_name"] == "Sendy"
    assert current.json()["company_name"] == "TAC AISolution"
    assert current.json()["approval_mode"] == "human_review"

    payload = {
        "agent_name": "Sendy",
        "company_name": "TAC AISolution",
        "role": "Enterprise AI Solutions Sales Consultant",
        "tone": "professional and consultative",
        "language_policy": "match_customer",
        "signature_text": "Best regards,\nSendy\nTAC AISolution",
        "forbidden_claims": "pricing,guaranteed results",
        "unknown_answer_policy": "Confirm scope before making a commitment.",
        "allow_campaign_override": False,
        "is_active": True,
    }
    updated = client.put("/api/agent-profile", json=payload)
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["allow_campaign_override"] is False

    mode = client.put("/api/agent-profile/approval-mode", json={"approval_mode": "agent_review"})
    assert mode.status_code == 200
    assert mode.json()["approval_mode"] == "agent_review"
    assert db.query(models.AuditLog).filter_by(action="agent_approval_mode_updated").count() == 1


def test_reply_postprocessor_removes_bad_signature_and_matches_chinese(db):
    profile = resolve_profile(db, 1)
    body, flags = apply_profile_to_reply(
        "Hi Ziyi, TAC can automate sales email workflows. 顺颂商祺，1",
        profile,
        customer_message="我们希望自动处理销售邮件。",
        customer_name="Ziyi",
    )
    assert body.startswith("Ziyi，您好：")
    assert body.endswith("Best regards,\nSendy\nTAC AISolution")
    assert "顺颂商祺" not in body
    assert "invalid_model_signature_removed" in flags


def test_campaign_override_is_optional(db):
    row = resolve_profile(db, 1)
    model = db.query(models.AgentProfile).filter_by(owner_id=1).one()
    model.allow_campaign_override = True
    db.flush()
    profile = resolve_profile(db, 1, campaign={
        "sender_name": "Campaign Sender",
        "sender_company": "Campaign Company",
        "tone": "brief",
    })
    assert profile["agent_name"] == "Campaign Sender"
    assert profile["signature_text"].endswith("Campaign Sender\nCampaign Company")

    model.allow_campaign_override = False
    db.flush()
    inherited = resolve_profile(db, 1, campaign={
        "sender_name": "Ignored",
        "sender_company": "Ignored",
        "tone": "ignored",
    })
    assert inherited["agent_name"] == "Sendy"
    assert inherited["company_name"] == "TAC AISolution"


def test_numeric_campaign_identity_never_overrides_profile(db):
    resolve_profile(db, 1)
    row = db.query(models.AgentProfile).filter_by(owner_id=1).one()
    row.allow_campaign_override = True
    db.flush()
    profile = resolve_profile(db, 1, campaign={
        "sender_name": "1",
        "sender_company": "1",
        "tone": "professional",
    })
    assert profile["agent_name"] == "Sendy"
    assert profile["company_name"] == "TAC AISolution"


def test_agent_profile_update_accepts_actor_param(client, db):
    payload = {
        "agent_name": "Sendy",
        "company_name": "TAC AISolution",
        "role": "Enterprise AI Solutions Sales Consultant",
        "tone": "professional, consultative, concise, friendly",
        "language_policy": "match_customer",
        "signature_text": "Best regards,\nSendy\nTAC AISolution",
        "forbidden_claims": "pricing",
        "unknown_answer_policy": "Confirm scope before making a commitment.",
        "allow_campaign_override": False,
        "is_active": True,
    }
    resp = client.put("/api/agent-profile?actor=agent", json=payload)
    assert resp.status_code == 200
    log = db.query(models.AuditLog).filter_by(action="agent_profile_updated").order_by(models.AuditLog.id.desc()).first()
    assert log is not None
    assert log.actor == "agent"
