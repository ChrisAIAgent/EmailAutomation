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


def test_capability_checks_campaign_and_automation_ownership(client, db):
    client.post("/api/agent-takeover", json={"enabled": True, "interval_minutes": 60})
    token = "ownership-secret"
    flags.set_flag(db, takeover_svc.FLAG_TOKEN_HASH, hashlib.sha256(token.encode()).hexdigest())
    flags.set_flag(db, takeover_svc.FLAG_TOKEN_EXPIRES, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    owned_campaign = models.Campaign(
        owner_id=1, name="Owned", status="draft", sender_name="A", sender_company="B",
        product_description="P", target_audience="T", tone="professional",
    )
    foreign_campaign = models.Campaign(
        owner_id=2, name="Foreign", status="draft", sender_name="A", sender_company="B",
        product_description="P", target_audience="T", tone="professional",
    )
    foreign_automation = models.Automation(
        owner_id=2, name="Foreign automation", prompt="p", scope="global",
        plan_json="{}", status="disabled", execution_mode="semi_auto",
    )
    foreign_user = models.User(id=2, email="foreign@example.com", name="Foreign Owner")
    owned_account = models.GmailAccount(user_id=1, email="owner@example.com")
    foreign_account = models.GmailAccount(user_id=2, email="foreign@example.com")
    db.add_all([
        owned_campaign, foreign_campaign, foreign_automation, foreign_user,
        owned_account, foreign_account,
    ])
    db.flush()
    owned_thread = models.EmailThread(gmail_account_id=owned_account.id, gmail_thread_id="owned-thread")
    foreign_thread = models.EmailThread(gmail_account_id=foreign_account.id, gmail_thread_id="foreign-thread")
    db.add_all([owned_thread, foreign_thread]); db.flush()
    owned_draft = models.EmailDraft(
        gmail_account_id=owned_account.id, thread_id=owned_thread.id,
        to_email="customer@example.com", subject="Subject", body_text="Body", status="draft",
    )
    foreign_draft = models.EmailDraft(
        gmail_account_id=foreign_account.id, thread_id=foreign_thread.id,
        to_email="foreign-customer@example.com", subject="Subject", body_text="Body", status="draft",
    )
    db.add_all([owned_draft, foreign_draft]); db.flush()
    owned_approval = models.Approval(
        kind="reply", thread_id=owned_thread.id, draft_id=owned_draft.id,
        to_email=owned_draft.to_email, subject="Subject", body_text="Body", status="pending",
    )
    foreign_approval = models.Approval(
        kind="reply", thread_id=foreign_thread.id, draft_id=foreign_draft.id,
        to_email=foreign_draft.to_email, subject="Subject", body_text="Body", status="pending",
    )
    db.add_all([owned_approval, foreign_approval]); db.commit()

    owned = client.post("/api/agent-takeover/authorize", json={
        "token": token, "operation": "add_campaign_contacts", "campaign_id": owned_campaign.id,
    })
    assert owned.status_code == 200, owned.text
    removal = client.post("/api/agent-takeover/authorize", json={
        "token": token, "operation": "remove_campaign_contact", "campaign_id": owned_campaign.id,
    })
    assert removal.status_code == 200, removal.text
    foreign_campaign_reply = client.post("/api/agent-takeover/authorize", json={
        "token": token, "operation": "pause_campaign", "campaign_id": foreign_campaign.id,
    })
    assert foreign_campaign_reply.status_code == 403
    foreign_automation_reply = client.post("/api/agent-takeover/authorize", json={
        "token": token, "operation": "enable_automation", "automation_id": foreign_automation.id,
    })
    assert foreign_automation_reply.status_code == 403
    owned_thread_reply = client.post("/api/agent-takeover/authorize", json={
        "token": token, "operation": "generate_inbox_reply", "thread_id": owned_thread.id,
    })
    assert owned_thread_reply.status_code == 200, owned_thread_reply.text
    foreign_thread_reply = client.post("/api/agent-takeover/authorize", json={
        "token": token, "operation": "generate_inbox_reply", "thread_id": foreign_thread.id,
    })
    assert foreign_thread_reply.status_code == 403
    owned_revision = client.post("/api/agent-takeover/authorize", json={
        "token": token, "operation": "revise_approval", "approval_id": owned_approval.id,
    })
    assert owned_revision.status_code == 200, owned_revision.text
    foreign_revision = client.post("/api/agent-takeover/authorize", json={
        "token": token, "operation": "revise_approval", "approval_id": foreign_approval.id,
    })
    assert foreign_revision.status_code == 403


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
    assert "takeover_token=" not in create_payload["prompt"]
    assert "takeover_token=" in create_payload["system"]
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


def test_idle_session_with_mcp_error_preserves_completed_with_errors(client, db, monkeypatch):
    client.post("/api/agent-takeover", json={"enabled": True, "interval_minutes": 15})
    flags.set_flag(db, takeover_svc.FLAG_ACTIVE_SESSION, "ses_error")
    flags.set_flag(db, takeover_svc.FLAG_WORKSPACE, "ws_email")
    flags.set_flag(db, takeover_svc.FLAG_CYCLE_ID, "cycle-error")
    flags.set_flag(db, takeover_svc.FLAG_LAST_STATUS, "running")
    flags.set_flag(db, takeover_svc.FLAG_LAST_ERROR, '{"error":"mcp unavailable"}')
    flags.set_flag(db, takeover_svc.FLAG_CYCLE_HAS_ERRORS, "true")
    db.commit()

    monkeypatch.setattr(takeover_svc, "_request", lambda method, path, payload=None: {"status": {"type": "idle"}})
    result = takeover_svc.trigger_due(db)
    assert result["status"] == "not_due"
    state = takeover_svc.status(db)
    assert state["active_session_id"] == ""
    assert state["last_status"] == "completed_with_errors"
    assert state["current_stage"] == "completed_with_errors"
    assert state["cycle_has_errors"] is True
    assert state["last_error"] == '{"error":"mcp unavailable"}'
    row = db.query(models.AuditLog).filter_by(action="agent_takeover_session_completed").one()
    assert row.success is False
    assert '"outcome": "completed_with_errors"' in (row.detail or "")


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


def test_failed_takeover_telemetry_marks_cycle_with_errors(client, db):
    client.post("/api/agent-takeover", json={"enabled": True, "interval_minutes": 60})
    token = "telemetry-secret"
    flags.set_flag(db, takeover_svc.FLAG_TOKEN_HASH, hashlib.sha256(token.encode()).hexdigest())
    flags.set_flag(db, takeover_svc.FLAG_TOKEN_EXPIRES, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    flags.set_flag(db, takeover_svc.FLAG_CYCLE_ID, "cycle-failed")
    db.commit()

    response = client.post("/api/agent-takeover/telemetry", json={
        "token": token, "stage": "sync_gmail", "status": "failed",
        "detail": {"error": "mcp_http_transport"},
    })
    assert response.status_code == 200, response.text
    assert flags.get_flag(db, takeover_svc.FLAG_CYCLE_HAS_ERRORS) == "true"
    assert "mcp_http_transport" in (flags.get_flag(db, takeover_svc.FLAG_LAST_ERROR) or "")


def test_legacy_scheduler_never_directly_runs_global_scope(client, db):
    response = client.post("/api/agent-takeover", json={"enabled": False, "interval_minutes": 60})
    automation = db.get(models.Automation, response.json()["global_automation_id"])
    automation.status = "enabled"
    automation.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    assert scan_due_automations(owner_id=1) == 0
    db.refresh(automation)
    assert automation.next_run_at is None
