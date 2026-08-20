"""Global inbox automation is independent from Campaign automation."""
from __future__ import annotations

from app import models


def test_global_automation_can_be_created_without_campaign(client):
    response = client.post("/api/automation", json={
        "name": "Global inbox", "prompt": "follow triaged business mail",
        "scope": "global", "campaign_id": None,
        "plan": {"execution_mode": "full_auto", "tick_interval_minutes": 60},
    })
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["scope"] == "global"
    assert item["campaign_id"] is None


def test_global_automation_run_does_not_require_campaign(client, db):
    automation = client.post("/api/automation", json={
        "prompt": "global inbox", "scope": "global",
        "plan": {"execution_mode": "semi_auto", "tick_interval_minutes": 60},
    }).json()
    run = client.post("/api/agent-runs", json={"automation_id": automation["id"], "mode": "semi_auto"}).json()
    from app.tasks import execute_automation_run
    execute_automation_run.call_local(run["run_id"], "manual", "agent_api")
    stored = db.get(models.AutomationRun, run["run_id"])
    # No eligible reply means there is no frozen recipient/content plan to
    # confirm. A semi-auto run must never expose an empty confirmation.
    assert stored.status == "success"


def test_global_switch_is_independent_from_campaign_automations(client):
    initial = client.get("/api/automation/global")
    assert initial.status_code == 200
    assert initial.json()["status"] == "disabled"

    enabled = client.post("/api/automation/global", json={
        "enabled": True, "mode": "full_auto", "takeover_scope": "future_only"
    })
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["scope"] == "global"
    assert enabled.json()["status"] == "enabled"

    disabled = client.post("/api/automation/global", json={"enabled": False, "mode": "semi_auto"})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["execution_mode"] == "semi_auto"


def test_global_enable_requires_takeover_scope(client):
    response = client.post("/api/automation/global", json={
        "enabled": True, "mode": "full_auto"
    })
    assert response.status_code == 400
    assert "takeover_scope" in response.json()["detail"]


def test_global_recent_days_persists_cutoff(client):
    response = client.post("/api/automation/global", json={
        "enabled": True, "mode": "full_auto",
        "takeover_scope": "recent_days", "takeover_days": 14,
    })
    assert response.status_code == 200, response.text
    plan = response.json()["plan"]
    assert plan["takeover_scope"] == "recent_days"
    assert plan["takeover_days"] == 14
    assert plan["takeover_cutoff_at"]


def test_global_full_auto_agent_resolves_human_review(client, db, monkeypatch):
    """Full-auto turns Human Review into an auditable Agent decision, not a user queue."""
    from types import SimpleNamespace
    from app.agents.orchestrator import Orchestrator
    from app.services import automation as automation_svc
    from app.tasks import execute_automation_run

    account = models.GmailAccount(user_id=1, email="demo@unconfigured.local", is_connected=False)
    contact = models.Contact(owner_id=1, email="review@example.com", next_action="human_review", tags='["forwarded"]')
    db.add_all([account, contact]); db.flush()
    thread = models.EmailThread(gmail_account_id=account.id, gmail_thread_id="review-thread", contact_email=contact.email,
                                has_human_reply=True, pending_action="human_review")
    db.add(thread); db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id, gmail_message_id="review-parent",
        message_id_header="<review-parent@example.com>",
        is_incoming=True, from_email=contact.email,
        subject="Need a reply", body_text="Can we talk?",
    )); db.commit()

    decision = SimpleNamespace(intent="interested", recommended_action="reply", risk_level="low",
        draft=SimpleNamespace(subject="Re: Need a reply", body_text="Thanks for reaching out.", body_html=""))
    captured = {}
    def analyze(self, payload):
        captured["thread_context"] = payload.thread_context
        return SimpleNamespace(langgraph=decision)
    monkeypatch.setattr(Orchestrator, "analyze", analyze)
    automation = client.post("/api/automation", json={"scope": "global", "prompt": "global", "plan": {"execution_mode": "full_auto"}}).json()
    run = client.post("/api/agent-runs", json={"automation_id": automation["id"], "mode": "full_auto"}).json()
    execute_automation_run.call_local(run["run_id"], "manual", "agent_api")

    assert db.query(models.AuditLog).filter_by(action="agent_resolved_human_review", entity_id=str(thread.id)).count() == 1
    assert db.query(models.Approval).filter_by(automation_run_id=run["run_id"]).count() == 1
    assert "Customer" in captured["thread_context"]
    assert "Can we talk?" in captured["thread_context"]
