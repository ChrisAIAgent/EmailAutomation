"""End-to-end flow via the API (uses in-memory Gmail transport + DB sync)."""
import os
from datetime import datetime, timezone

from app.config import get_settings
from app import models


def _setenv(**kw):
    for k, v in kw.items():
        os.environ[k] = v
    get_settings.cache_clear()


def test_e2e_outreach_to_compare(client, db):
    # 1. create campaign in compare mode
    r = client.post("/api/campaigns", json={
        "name": "E2E", "agent_mode": "compare", "primary_agent": "langgraph",
        "daily_send_limit": 10, "timezone": "UTC",
        "sending_window_start": 0, "sending_window_end": 23,
    })
    assert r.status_code == 200
    camp_id = r.json()["id"]

    # 2. import a real test contact
    csv = "email,first_name,company\nlead@example.com,Lead,Acme"
    r = client.post(f"/api/campaigns/{camp_id}/import-csv", json={
        "campaign_id": camp_id, "csv_text": csv,
        "field_map": {"email": "email", "first_name": "first_name", "company": "company"},
        "has_header": True,
    })
    assert r.status_code == 200 and r.json()["imported"] == 1

    # 2b. start campaign (policy requires an active campaign for Gmail writes)
    r = client.post(f"/api/campaigns/{camp_id}/start")
    assert r.status_code == 200

    # 2c. ensure a (demo) Gmail account exists to send from
    if not db.query(models.GmailAccount).first():
        db.add(models.GmailAccount(id=1, user_id=1, email="me@example.com", is_connected=True))
        db.commit()

    # 3. generate outreach -> draft + approval created
    r = client.post(f"/api/campaigns/{camp_id}/generate")
    assert r.status_code == 200
    assert r.json()["generated"] == 1
    ap_id = r.json()["approvals"][0]

    # 4. enable real send + allowlist, then approve.
    # HONESTY: with no REAL Gmail account connected, the send is BLOCKED (409)
    # instead of silently delivering to the in-memory dev double ("fake" send).
    _setenv(ENABLE_REAL_SEND="true", RESTRICTED_RECIPIENT_ALLOWLIST="lead@example.com")
    r = client.post(f"/api/approvals/{ap_id}/decision", json={"decision": "approve", "editor_email": "tester"})
    assert r.status_code == 409, r.text
    assert "Gmail" in r.text
    # approval stays pending — no delivery happened
    r = client.get(f"/api/approvals/{ap_id}")
    assert r.json()["status"] == "pending"
    # reset so later tests default to draft-only
    _setenv(ENABLE_REAL_SEND="false")

    # 5. simulate a real reply arriving (stands in for Gmail sync)
    acct = models.GmailAccount(id=1, user_id=1, email="me@example.com", is_connected=True)
    if not db.query(models.GmailAccount).first():
        db.add(acct)
        db.commit()
    th = models.EmailThread(gmail_account_id=1, gmail_thread_id="thread_e2e", contact_email="lead@example.com",
                            campaign_id=camp_id, has_human_reply=True)
    db.add(th)
    db.commit()
    msg = models.EmailMessage(thread_id=th.id, gmail_message_id="m_e2e", from_email="lead@example.com",
                              to_email="me@example.com", subject="Re: idea", body_text="Yes I am interested, what is the price?",
                              is_incoming=True, received_at=datetime.now(timezone.utc))
    db.add(msg)
    db.commit()
    # link campaign contact
    cc = db.query(models.CampaignContact).filter_by(campaign_id=camp_id).first()
    cc.thread_id = th.id
    db.commit()

    # 6. analyze the reply in compare mode (both agents, shadow failure tolerated)
    _setenv(OPENCLAW_TRANSPORT="http", OPENCLAW_ENDPOINT="http://127.0.0.1:9/task", OPENCLAW_TIMEOUT_SECONDS="1")
    r = client.post(f"/api/inbox/threads/{th.id}/analyze")
    assert r.status_code == 200, r.text
    data = r.json()
    # compare returns both slots (openclaw may be None if unreachable -> honest)
    assert "langgraph" in data
    assert data["langgraph"]["intent"] in ("interested", "asking_question", "unknown")

    # 7. dashboard reflects real data (no fake delivery happened; draft-only again)
    r = client.get("/api/dashboard/metrics")
    assert r.status_code == 200
    m = r.json()
    assert m["sent_today"] == 0
    assert m["real_send_enabled"] is False
    assert m["draft_only"] is True
    assert m["pending_approvals"] >= 1


def test_generate_without_gmail_creates_demo_drafts(client, db):
    """No Gmail account connected at all -> draft-only generation must still work
    (a demo unconnected account is auto-provisioned so drafts persist)."""
    assert db.query(models.GmailAccount).count() == 0
    r = client.post("/api/campaigns", json={"name": "NoG", "agent_mode": "langgraph_only",
                                            "primary_agent": "langgraph"})
    assert r.status_code == 200
    camp_id = r.json()["id"]
    r = client.post(f"/api/campaigns/{camp_id}/import-csv", json={
        "campaign_id": camp_id, "csv_text": "email,first_name\nlead@example.com,Lead",
        "field_map": {}, "has_header": True})
    assert r.status_code == 200 and r.json()["imported"] == 1
    r = client.post(f"/api/campaigns/{camp_id}/generate")
    assert r.status_code == 200, r.text
    assert r.json()["generated"] == 1
    acct = db.query(models.GmailAccount).first()
    assert acct is not None and acct.is_connected is False
    assert db.query(models.EmailDraft).count() == 1
