"""Regression coverage for the full-auto-first / semi-auto Agent Run contract."""
from __future__ import annotations

from app import models


def _campaign_and_automation(client, db):
    campaign = client.post("/api/campaigns", json={"name": "autonomy", "sender_name": "Chris"}).json()
    contact = models.Contact(owner_id=1, email="lead@example.com", first_name="Lead")
    db.add(contact)
    db.flush()
    db.add(models.CampaignContact(campaign_id=campaign["id"], contact_id=contact.id, status="queued"))
    db.commit()
    plan = {
        "execution_mode": "semi_auto", "tick_interval_minutes": 60,
        "daily_send_limit": 1, "stop_on_intents": ["unsubscribe", "opt_out", "bounce"],
    }
    response = client.post("/api/automation", json={"prompt": "p", "campaign_id": campaign["id"], "plan": plan})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_semi_auto_freezes_plan_then_requires_confirmation(client, db):
    from app.tasks import execute_automation_run

    automation_id = _campaign_and_automation(client, db)
    started = client.post("/api/agent-runs", json={"automation_id": automation_id, "mode": "semi_auto"})
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]
    execute_automation_run.call_local(run_id, "manual", "agent_api")

    detail = client.get(f"/api/agent-runs/{run_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "awaiting_confirmation"
    assert detail.json()["send_plan"]
    item = detail.json()["send_plan"][0]
    assert item["draft_id"]
    assert item["body_text"]

    confirmed = client.post(f"/api/agent-runs/{run_id}/confirm", json={"confirmed_by": "operator"})
    assert confirmed.status_code == 200, confirmed.text
    # The consumer dispatches the frozen plan; in draft-only test configuration
    # it reaches a terminal partial state instead of fabricating a delivery.
    from app.tasks import execute_prepared_agent_run
    execute_prepared_agent_run.call_local(run_id)
    assert client.get(f"/api/agent-runs/{run_id}").json()["status"] in {"success", "partial"}


def test_invalidated_frozen_approval_makes_run_non_confirmable(client, db):
    from app.tasks import execute_automation_run

    automation_id = _campaign_and_automation(client, db)
    run_id = client.post("/api/agent-runs", json={"automation_id": automation_id, "mode": "semi_auto"}).json()["run_id"]
    execute_automation_run.call_local(run_id, "manual", "agent_api")
    plan = client.get(f"/api/agent-runs/{run_id}").json()["send_plan"]
    approval_id = plan[0]["approval_id"]

    blocked_direct = client.post(f"/api/approvals/{approval_id}/decision", json={"decision": "approve"})
    assert blocked_direct.status_code == 409

    invalidated = client.post(f"/api/approvals/{approval_id}/invalidate", json={"reason": "corrected plan"})
    assert invalidated.status_code == 200, invalidated.text
    assert client.get(f"/api/agent-runs/{run_id}").json()["status"] == "invalidated"
    confirmed = client.post(f"/api/agent-runs/{run_id}/confirm", json={"confirmed_by": "operator"})
    assert confirmed.status_code == 409


def test_readiness_is_read_only_operating_context(client):
    response = client.get("/api/dashboard/readiness")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] in {"ready", "blocked"}
    assert "next_action" in payload
    assert "pending_approvals" in payload


def test_human_review_is_default_and_forces_agent_runs_to_semi_auto(client, db):
    automation_id = _campaign_and_automation(client, db)
    started = client.post("/api/agent-runs", json={"automation_id": automation_id})
    assert started.status_code == 200, started.text
    assert started.json()["mode"] == "semi_auto"


def test_agent_review_permits_requested_full_auto_mode(client, db):
    profile = client.get("/api/agent-profile").json()
    updated = client.put("/api/agent-profile", json={**profile, "approval_mode": "agent_review"})
    assert updated.status_code == 200, updated.text
    automation_id = _campaign_and_automation(client, db)
    started = client.post("/api/agent-runs", json={"automation_id": automation_id, "mode": "full_auto"})
    assert started.status_code == 200, started.text
    assert started.json()["mode"] == "full_auto"
