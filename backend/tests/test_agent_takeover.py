from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from app import models
from app.services import agent_takeover as takeover_svc
from app.services import flags
from app.tasks import scan_due_automations


def test_takeover_enable_disable_is_atomic(client, db):
    enabled = client.post("/api/agent-takeover", json={"enabled": True, "interval_minutes": 1})
    assert enabled.status_code == 200, enabled.text
    payload = enabled.json()
    assert payload["enabled"] is True
    assert payload["interval_minutes"] == 1
    assert payload["approval_mode"] == "agent_review"
    assert payload["execution_mode"] == "full_auto"
    assert payload["permissions"] == {
        "agent_review": True,
        "automatic_send": True,
        "scheduled_wakeup": True,
        "inbox_operations": True,
    }
    automation = db.query(models.Automation).filter_by(scope="global").one()
    assert automation.status == "enabled"
    assert automation.next_run_at is None

    disabled = client.post("/api/agent-takeover", json={"enabled": False, "interval_minutes": 120})
    assert disabled.status_code == 200, disabled.text
    payload = disabled.json()
    assert payload["enabled"] is False
    assert payload["approval_mode"] == "human_review"
    assert payload["execution_mode"] == "semi_auto"
    db.refresh(automation)
    assert automation.status == "disabled"
    assert flags.get_flag(db, takeover_svc.FLAG_TOKEN_HASH) == ""


def test_takeover_persists_workspace_display_timezone_and_returns_local_times(client):
    response = client.post("/api/agent-takeover", json={
        "enabled": True, "interval_minutes": 15, "display_timezone": "Asia/Shanghai",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["display_timezone"] == "Asia/Shanghai"
    assert body["display_time"]["next_run_at"]["utc"].endswith("Z")
    assert "Asia/Shanghai" in body["display_time"]["next_run_at"]["local"]

    invalid = client.post("/api/agent-takeover/display-timezone", json={"display_timezone": "Not/AZone"})
    assert invalid.status_code == 422


def test_takeover_accepts_any_whole_minute_in_configured_range(client):
    for minutes in (1, 2, 17, 120, 1439, 1440):
        response = client.post("/api/agent-takeover", json={"enabled": False, "interval_minutes": minutes})
        assert response.status_code == 200, response.text
        assert response.json()["interval_minutes"] == minutes


def test_takeover_rejects_invalid_minute_values(client):
    for value in (0, -1, 1441, 1.5, True, "15"):
        response = client.post("/api/agent-takeover", json={"enabled": False, "interval_minutes": value})
        assert response.status_code == 422


def test_changing_disabled_interval_does_not_start_takeover(client):
    response = client.post("/api/agent-takeover", json={"enabled": False, "interval_minutes": 17})
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["next_run_at"] is None
    assert body["active_session_id"] == ""


def test_capability_can_start_owned_enabled_full_auto_runs(client, db):
    response = client.post("/api/agent-takeover", json={"enabled": True, "interval_minutes": 60})
    global_id = response.json()["global_automation_id"]
    token = "scheduled-session-secret"
    flags.set_flag(db, takeover_svc.FLAG_TOKEN_HASH, hashlib.sha256(token.encode()).hexdigest())
    flags.set_flag(db, takeover_svc.FLAG_TOKEN_EXPIRES, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    campaign = models.Campaign(
        owner_id=1, name="Campaign", status="draft", sender_name="A", sender_company="B",
        product_description="P", target_audience="T", tone="professional",
    )
    db.add(campaign); db.flush()
    campaign_automation = models.Automation(
        owner_id=1, name="Campaign automation", prompt="p", campaign_id=campaign.id,
        scope="campaign", plan_json="{}", status="enabled", execution_mode="full_auto",
    )
    db.add(campaign_automation); db.commit()

    allowed = client.post("/api/agent-takeover/authorize", json={
        "token": token, "operation": "start_agent_run", "automation_id": global_id,
    })
    assert allowed.status_code == 200
    campaign_allowed = client.post("/api/agent-takeover/authorize", json={
        "token": token, "operation": "start_agent_run", "automation_id": campaign_automation.id,
    })
    assert campaign_allowed.status_code == 200
    campaign_automation.execution_mode = "semi_auto"; db.commit()
    denied = client.post("/api/agent-takeover/authorize", json={
        "token": token, "operation": "start_agent_run", "automation_id": campaign_automation.id,
    })
    assert denied.status_code == 409


def test_due_tick_creates_fresh_tacwork_session_and_records_stage(client, db, monkeypatch):
    client.post("/api/agent-takeover", json={
        "enabled": True, "interval_minutes": 1, "display_timezone": "Asia/Shanghai",
    })
    calls = []

    def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        if path == "/status":
            return {"activeWorkspaceId": "ws_email"}
        if path == "/workspace/ws_email/sessions":
            return {"item": {"id": "ses_scheduled"}, "started": True}
        raise AssertionError(path)

    monkeypatch.setattr(takeover_svc, "_request", fake_request)
    result = takeover_svc.trigger_due(db, force=True)
    assert result == {"status": "running", "session_id": "ses_scheduled", "workspace_id": "ws_email"}
    state = takeover_svc.status(db)
    assert state["cycle_id"]
    assert state["current_stage"] == "agent_running"
    assert state["last_success_stage"] == "session_create"
    create_payload = calls[-1][2]
    assert "Cycle ID:" in create_payload["prompt"]
    assert "takeover_token=" in create_payload["prompt"]
    assert "Workspace display timezone: Asia/Shanghai" in create_payload["prompt"]
    assert "raw UTC timestamps" in create_payload["prompt"]
    assert db.query(models.AuditLog).filter_by(action="agent_takeover_session_started").count() == 1


def test_stale_tacwork_session_is_cleared_and_replaced_in_same_cycle(client, db, monkeypatch):
    client.post("/api/agent-takeover", json={"enabled": True, "interval_minutes": 15})
    flags.set_flag(db, takeover_svc.FLAG_ACTIVE_SESSION, "ses_missing")
    flags.set_flag(db, takeover_svc.FLAG_WORKSPACE, "ws_email")
    db.commit()

    def fake_request(method, path, payload=None):
        if path.endswith("/snapshot?limit=20"):
            raise RuntimeError("tacwork_http_404: session_not_found")
        if path == "/status":
            return {"activeWorkspaceId": "ws_email"}
        if path == "/workspace/ws_email/sessions":
            return {"item": {"id": "ses_fresh"}}
        raise AssertionError(path)

    monkeypatch.setattr(takeover_svc, "_request", fake_request)
    result = takeover_svc.trigger_due(db, force=True)
    assert result["session_id"] == "ses_fresh"
    assert takeover_svc.status(db)["active_session_id"] == "ses_fresh"
    assert db.query(models.AuditLog).filter_by(
        action="agent_takeover_stale_session_recovered"
    ).count() == 1


def test_idle_session_is_completed_before_the_next_due_cycle(client, db, monkeypatch):
    client.post("/api/agent-takeover", json={"enabled": True, "interval_minutes": 15})
    flags.set_flag(db, takeover_svc.FLAG_ACTIVE_SESSION, "ses_done")
    flags.set_flag(db, takeover_svc.FLAG_WORKSPACE, "ws_email")
    flags.set_flag(db, takeover_svc.FLAG_CYCLE_ID, "cycle-done")
    flags.set_flag(db, takeover_svc.FLAG_LAST_STATUS, "running")
    db.commit()

    monkeypatch.setattr(takeover_svc, "_request", lambda method, path, payload=None: {"status": {"type": "idle"}})
    result = takeover_svc.trigger_due(db)
    assert result["status"] == "not_due"
    state = takeover_svc.status(db)
    assert state["active_session_id"] == ""
    assert state["last_status"] == "completed"
    assert state["current_stage"] == "completed"
    assert state["last_completed_at"] is not None
    assert db.query(models.AuditLog).filter_by(action="agent_takeover_session_completed").count() == 1


def test_takeover_disable_clears_active_session_state(client, db):
    client.post("/api/agent-takeover", json={"enabled": True, "interval_minutes": 15})
    flags.set_flag(db, takeover_svc.FLAG_ACTIVE_SESSION, "ses_old")
    flags.set_flag(db, takeover_svc.FLAG_WORKSPACE, "ws_email")
    db.commit()
    client.post("/api/agent-takeover", json={"enabled": False, "interval_minutes": 15})
    assert takeover_svc.status(db)["active_session_id"] == ""
    assert takeover_svc.status(db)["workspace_id"] == ""


def test_takeover_telemetry_is_sanitized_and_traceable(client, db):
    client.post("/api/agent-takeover", json={"enabled": True, "interval_minutes": 60})
    token = "telemetry-secret"
    flags.set_flag(db, takeover_svc.FLAG_TOKEN_HASH, hashlib.sha256(token.encode()).hexdigest())
    flags.set_flag(db, takeover_svc.FLAG_TOKEN_EXPIRES, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    flags.set_flag(db, takeover_svc.FLAG_CYCLE_ID, "cycle-123")
    db.commit()
    response = client.post("/api/agent-takeover/telemetry", json={
        "token": token, "stage": "sync_gmail", "status": "success",
        "detail": {"threads": 2, "messages": 3},
    })
    assert response.status_code == 200, response.text
    row = db.query(models.AuditLog).filter_by(action="agent_takeover_sync_gmail_success").one()
    assert row.entity_id == "cycle-123"
    assert token not in (row.detail or "")
    assert flags.get_flag(db, takeover_svc.FLAG_LAST_SUCCESS_STAGE) == "sync_gmail"


def test_legacy_scheduler_never_directly_runs_global_scope(client, db):
    response = client.post("/api/agent-takeover", json={"enabled": False, "interval_minutes": 60})
    automation = db.get(models.Automation, response.json()["global_automation_id"])
    automation.status = "enabled"
    automation.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    assert scan_due_automations(owner_id=1) == 0
    db.refresh(automation)
    assert automation.next_run_at is None
