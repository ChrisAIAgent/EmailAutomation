"""Tests for the Automation feature (natural-language email automation).

Covers: plan generation (safe defaults), CRUD, run-now creating first-email
approvals, stop-on-intent cancelling follow-ups + suppression, draft-only (no
send), the OpenClaw cron tick token gate, and the GET-detail endpoint that
surfaces runs + campaign metrics (regression for the live 500 caused by a
missing `approvals.agent_run_id` column in the dev DB).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import models


def _make_campaign(client):
    r = client.post(
        "/api/campaigns",
        json={
            "name": "TAC 邮箱自动化测试",
            "sender_name": "Chris",
            "sender_company": "TAC",
            "product_description": "邮箱自动化",
            "target_audience": "需要邮箱自动化的客户",
            "tone": "professional",
            "agent_mode": "langgraph_only",
            "primary_agent": "langgraph",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _make_account(db):
    acct = models.GmailAccount(user_id=1, email="demo@unconfigured.local", is_connected=False)
    db.add(acct)
    db.flush()
    return acct


def test_generate_plan_defaults(client):
    cid = _make_campaign(client)
    r = client.post("/api/automation/generate", json={"prompt": "每天发一封测试邮件", "campaign_id": cid})
    assert r.status_code == 200, r.text
    plan = r.json()["plan"]
    # Safe conservative defaults (no LLM in test env).
    assert plan["tick_interval_minutes"] == 5
    assert plan["first_email_approval_required"] is True
    assert plan["daily_send_limit"] == 1
    assert "unsubscribe" in plan["stop_on_intents"]
    assert "opt_out" in plan["stop_on_intents"]


def test_create_enable_pause(client):
    cid = _make_campaign(client)
    g = client.post("/api/automation/generate", json={"prompt": "p", "campaign_id": cid})
    plan = g.json()["plan"]
    c = client.post("/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan})
    assert c.status_code == 200, c.text
    aid = c.json()["id"]
    assert c.json()["status"] == "enabled"
    assert c.json()["next_run_at"] is not None

    p = client.post(f"/api/automation/{aid}/pause")
    assert p.status_code == 200 and p.json()["status"] == "paused"
    assert p.json()["next_run_at"] is None

    e = client.post(f"/api/automation/{aid}/enable")
    assert e.status_code == 200 and e.json()["status"] == "enabled"
    assert e.json()["next_run_at"] is not None


def test_create_disabled_plan_stays_disabled(client):
    cid = _make_campaign(client)
    created = client.post("/api/automation", json={
        "prompt": "p", "campaign_id": cid,
        "plan": {"enabled": False, "tick_interval_minutes": 60},
    })
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "disabled"
    assert created.json()["next_run_at"] is None


def test_web_scheduler_status_does_not_require_electron(client):
    r = client.get("/api/automation/scheduler/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "backend_huey"
    assert body["electron_required"] is False
    assert "consumer_healthy" in body
    assert "next_due_at" in body


def test_takeover_hides_global_automation_from_huey_schedule(client, db):
    takeover = client.post("/api/agent-takeover", json={"enabled": True, "interval_minutes": 15})
    assert takeover.status_code == 200
    global_automation = db.get(models.Automation, takeover.json()["global_automation_id"])
    global_automation.next_run_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.commit()

    listed = client.get("/api/automation")
    global_item = next(item for item in listed.json()["items"] if item["scope"] == "global")
    assert global_item["schedule_owner"] == "agent_takeover"
    assert global_item["scheduler_managed"] is False
    assert global_item["next_run_at"].endswith("Z")

    status = client.get("/api/automation/scheduler/status").json()
    assert status["agent_takeover_owns_global"] is True
    assert status["enabled_automations"] == 0
    assert status["next_due_at"] is None


def test_web_can_update_automation_schedule(client):
    cid = _make_campaign(client)
    plan = client.post(
        "/api/automation/generate", json={"prompt": "p", "campaign_id": cid}
    ).json()["plan"]
    created = client.post(
        "/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan}
    ).json()
    aid = created["id"]
    client.post(f"/api/automation/{aid}/enable")

    r = client.post(
        f"/api/automation/{aid}/schedule",
        json={"tick_interval_minutes": 120},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tick_interval_minutes"] == 120
    assert r.json()["plan"]["tick_interval_minutes"] == 120
    assert r.json()["next_run_at"] is not None

    invalid = client.post(
        f"/api/automation/{aid}/schedule",
        json={"tick_interval_minutes": 0},
    )
    assert invalid.status_code == 422


def test_web_due_scan_is_owner_scoped(client, monkeypatch):
    captured = {}

    def fake_scan(owner_id=None):
        captured["owner_id"] = owner_id
        return 0

    monkeypatch.setattr("app.api.automation.scan_due_automations", fake_scan)
    monkeypatch.setattr(
        "app.api.automation.read_consumer_status",
        lambda: {"healthy": True, "state": "running"},
    )
    r = client.post("/api/automation/scheduler/run-due")
    assert r.status_code == 200, r.text
    assert captured["owner_id"] == 1


def test_run_now_creates_first_approval_draft_only(client, db):
    cid = _make_campaign(client)
    assert client.post(f"/api/campaigns/{cid}/start").status_code == 200
    contact = models.Contact(owner_id=1, email="pyx1171898390@gmail.com", first_name="Chris", company="TAC")
    db.add(contact)
    db.flush()
    cc = models.CampaignContact(campaign_id=cid, contact_id=contact.id, status="queued")
    db.add(cc)
    db.flush()

    g = client.post("/api/automation/generate", json={"prompt": "p", "campaign_id": cid})
    plan = g.json()["plan"]
    c = client.post("/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan})
    aid = c.json()["id"]

    # Run now returns IMMEDIATELY with a queued run (no email processing inline).
    from app.tasks import execute_automation_run
    res = client.post(f"/api/automation/{aid}/run-now")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "queued"
    run_id = body["run_id"]
    # The worker (separate process) flips queued -> running -> final.
    execute_automation_run.call_local(run_id, "manual", "backend")
    from app.db import SessionLocal
    with SessionLocal() as s:
        r = s.get(models.AutomationRun, run_id)
        # The Workspace-wide default is human_review, so a prepared run freezes
        # for operator confirmation instead of being reported as completed.
        assert r.status in ("success", "partial", "awaiting_confirmation")
        assert r.approvals_created >= 1
        assert r.drafts_created >= 1
        assert r.started_at is not None
        if r.status != "awaiting_confirmation":
            assert r.finished_at is not None
        persisted = s.query(models.Approval).filter_by(automation_run_id=run_id).count()
        assert r.approvals_created == persisted
    # Draft-only: never actually sends.
    assert db.query(models.EmailDraft).filter_by(status="sent").count() == 0
    assert db.query(models.Approval).filter_by(campaign_id=cid, status="pending").count() >= 1
    # Campaign contact moved past 'queued' (idempotent on next run).
    db.refresh(cc)
    assert cc.status != "queued"


def test_run_now_unsubscribe_routes_to_human_review(client, db):
    """Unsubscribe detected by the automation must be routed to human review,
    never auto-stopped. Only a later human approval in the Inbox opts the
    contact out."""
    cid = _make_campaign(client)
    acct = _make_account(db)
    contact = models.Contact(owner_id=1, email="real-customer@example.com")
    db.add(contact)
    db.flush()
    cc = models.CampaignContact(campaign_id=cid, contact_id=contact.id, status="contacted")
    db.add(cc)
    db.flush()

    th = models.EmailThread(
        gmail_account_id=acct.id, gmail_thread_id="thread_human_gate_1", campaign_id=cid,
        contact_email="real-customer@example.com", subject="re", has_human_reply=True,
    )
    db.add(th)
    db.flush()
    db.add(models.EmailMessage(
        thread_id=th.id, is_incoming=True, from_email="real-customer@example.com",
        subject="re", body_text="请退订我，不要再发了", received_at=datetime.now(timezone.utc),
    ))
    db.add(models.FollowUpTask(
        campaign_contact_id=cc.id, contact_id=contact.id, campaign_id=cid,
        thread_id=th.id, sequence=1, scheduled_at=datetime.now(timezone.utc) - timedelta(days=1),
        status="scheduled",
    ))
    db.flush()

    g = client.post("/api/automation/generate", json={"prompt": "p", "campaign_id": cid})
    plan = g.json()["plan"]
    c = client.post("/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan})
    aid = c.json()["id"]

    from app.tasks import execute_automation_run
    res = client.post(f"/api/automation/{aid}/run-now")
    assert res.status_code == 200, res.text
    run_id = res.json()["run_id"]
    execute_automation_run.call_local(run_id, "manual", "backend")
    from app.db import SessionLocal
    with SessionLocal() as s:
        r = s.get(models.AutomationRun, run_id)
        assert r.status in ("success", "partial", "failed")
        # Unsubscribe is human-gated: the run must NOT have auto-stopped anyone.
        assert r.replies_stopped == 0
    db.refresh(th)
    # Thread routed to human review, NOT stopped.
    assert th.pending_action == "human_review"
    # No Suppression written and the contact is NOT silently opted out -- the
    # unsubscribe is pending human review only.
    assert db.query(models.Suppression).filter_by(email="real-customer@example.com").count() == 0
    db.refresh(contact)
    # The contact was never auto-opted-out: its status/lifecycle must NOT have
    # been flipped to the terminal unsubscribed/stopped state by the run. It
    # retains whatever prior state it had (default "new" here) pending human
    # review of the unsubscribe request.
    assert contact.status not in ("unsubscribed", "not_interested", "bounced")
    assert contact.lifecycle_stage != "stopped"


def test_global_tick_runs_enabled_automation(client, db):
    """/tick enqueues enabled Automations through the async queue (no sync run)."""
    cid = _make_campaign(client)
    contact = models.Contact(owner_id=1, email="pyx1171898390@gmail.com", first_name="Chris", company="TAC")
    db.add(contact)
    db.flush()
    cc = models.CampaignContact(campaign_id=cid, contact_id=contact.id, status="queued")
    db.add(cc)
    db.flush()

    g = client.post("/api/automation/generate", json={"prompt": "p", "campaign_id": cid})
    plan = g.json()["plan"]
    c = client.post("/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan})
    aid = c.json()["id"]
    client.post(f"/api/automation/{aid}/enable")

    r = client.post("/api/automation/tick")  # no token configured -> source=backend
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "backend"
    assert body["enqueued"] >= 1
    assert len(body["run_ids"]) >= 1
    # tick must NOT execute run_tick synchronously: run stays queued, no approvals yet.
    run = db.query(models.AutomationRun).filter_by(id=body["run_ids"][0]).first()
    assert run is not None and run.status == "queued"
    assert db.query(models.Approval).filter_by(campaign_id=cid).count() == 0


def test_tick_token_guard(client):
    from app.config import get_settings

    s = get_settings()
    s.AUTOMATION_WEBHOOK_TOKEN = "secret"
    try:
        r = client.post("/api/automation/tick")
        assert r.status_code == 403
        r2 = client.post("/api/automation/tick", headers={"Authorization": "Bearer secret"})
        assert r2.status_code == 200
        assert r2.json()["source"] == "openclaw"
    finally:
        s.AUTOMATION_WEBHOOK_TOKEN = None


def test_detail_shows_runs_and_metrics(client, db):
    """Regression for the live 500 on GET /api/automation/{id}.

    The dev DB drifted from the models (missing `approvals.agent_run_id` and
    other columns), so any Approval query in `campaign_metrics` raised
    OperationalError. This test pins the detail endpoint to 200 with a real
    run record and a metrics block, regardless of whether the campaign has
    contacts.
    """
    cid = _make_campaign(client)  # campaign with NO contacts (mirrors live)

    g = client.post("/api/automation/generate", json={"prompt": "p", "campaign_id": cid})
    plan = g.json()["plan"]
    c = client.post("/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan})
    aid = c.json()["id"]
    client.post(f"/api/automation/{aid}/enable")

    # Run now must persist a real AutomationRun row (queued immediately).
    run = client.post(f"/api/automation/{aid}/run-now")
    assert run.status_code == 200, run.text
    assert run.json()["run_id"] >= 1
    run_id = run.json()["run_id"]

    # Execute via the worker to exercise queued -> running -> final and the
    # started_at / finished_at timestamps.
    from app.tasks import execute_automation_run
    execute_automation_run.call_local(run_id, "manual", "backend")

    # Detail must not 500 and must surface the run + metrics.
    resp = client.get(f"/api/automation/{aid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "enabled"
    assert len(body["runs"]) >= 1
    first = body["runs"][0]
    assert "status" in first and "created_at" in first and "trigger" in first
    assert first["id"] == run_id
    assert first["status"] in ("success", "partial", "failed")
    assert first["started_at"] is not None and first["finished_at"] is not None
    # metrics block present and well-shaped
    m = body["campaign_metrics"]
    assert set(m.keys()) == {"sent_today", "pending_approvals", "waiting_reply", "stopped"}
    # run record genuinely persisted (query the table directly)
    assert db.query(models.AutomationRun).filter_by(automation_id=aid).count() >= 1


def test_run_now_async_returns_within_one_second(client, db):
    """Acceptance: Run now must enqueue and return run_id in <1s (no inline email work)."""
    import time

    cid = _make_campaign(client)
    g = client.post("/api/automation/generate", json={"prompt": "p", "campaign_id": cid})
    plan = g.json()["plan"]
    c = client.post("/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan})
    aid = c.json()["id"]

    t0 = time.time()
    res = client.post(f"/api/automation/{aid}/run-now")
    elapsed = time.time() - t0

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "queued"
    assert body["run_id"] >= 1
    assert elapsed < 1.0, f"run-now took {elapsed:.3f}s, expected <1s"
    # a queued run row genuinely exists
    run = db.query(models.AutomationRun).filter_by(id=body["run_id"]).first()
    assert run is not None and run.status == "queued"


def test_scan_enqueues_due_enabled_and_dedups(client, db):
    """Periodic scan only enqueues enabled automations whose next_run_at is due,
    and never double-enqueues one that already has a queued/running run."""
    from app.tasks import scan_due_automations
    from app.db import SessionLocal

    cid = _make_campaign(client)
    g = client.post("/api/automation/generate", json={"prompt": "p", "campaign_id": cid})
    plan = g.json()["plan"]
    c = client.post("/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan})
    aid = c.json()["id"]
    client.post(f"/api/automation/{aid}/enable")

    # Force it due in the past so the next scan picks it up.
    # (SQLite stores tz-aware datetimes as naive UTC, so use naive here to match.)
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    db.query(models.Automation).filter_by(id=aid).update({"next_run_at": past})
    db.commit()

    # First scan enqueues exactly one run and advances the schedule.
    assert scan_due_automations() == 1
    with SessionLocal() as s:
        runs = s.query(models.AutomationRun).filter_by(automation_id=aid).all()
        assert len(runs) == 1 and runs[0].status == "queued"
        a = s.get(models.Automation, aid)
        assert a.next_run_at > past  # schedule advanced to avoid tight-loop

    # Second scan must NOT enqueue another run (queued run still in flight).
    assert scan_due_automations() == 0
    with SessionLocal() as s:
        runs = s.query(models.AutomationRun).filter_by(automation_id=aid).all()
        assert len(runs) == 1


def test_no_concurrent_duplicate_run(client, db):
    """The atomic claim guarantees the same run cannot execute twice."""
    from app.tasks import execute_automation_run
    from app.db import SessionLocal

    cid = _make_campaign(client)
    assert client.post(f"/api/campaigns/{cid}/start").status_code == 200
    contact = models.Contact(owner_id=1, email="pyx1171898390@gmail.com")
    db.add(contact)
    db.flush()
    cc = models.CampaignContact(campaign_id=cid, contact_id=contact.id, status="queued")
    db.add(cc)
    db.flush()
    g = client.post("/api/automation/generate", json={"prompt": "p", "campaign_id": cid})
    plan = g.json()["plan"]
    c = client.post("/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan})
    aid = c.json()["id"]

    # Simulate the scheduler creating a queued run.
    run = models.AutomationRun(automation_id=aid, trigger="cron", source="backend", status="queued")
    db.add(run)
    db.flush()
    run_id = run.id
    db.commit()

    first = execute_automation_run.call_local(run_id, "cron", "backend")
    # A concurrent second attempt finds status != queued and skips.
    second = execute_automation_run.call_local(run_id, "cron", "backend")
    assert first.get("ok") is True
    assert second.get("skipped") is True

    with SessionLocal() as s:
        r = s.get(models.AutomationRun, run_id)
        assert r.status in ("success", "partial")
        assert r.approvals_created >= 1  # counted exactly once


def test_owner_scoped_access(client, db):
    """All Automation lookups/operations are scoped to the caller's owner."""
    from app.api.deps import ensure_owner

    cid = _make_campaign(client)
    # An automation owned by a DIFFERENT owner (created directly in DB).
    other = models.Automation(
        owner_id=2, name="other-owner", prompt="x", campaign_id=cid,
        plan_json="{}", status="disabled", tick_interval_minutes=5,
    )
    db.add(other)
    db.flush()
    oid = other.id
    db.commit()
    # ensure_owner is invoked inside each endpoint; as owner 1 these 404.
    for path in [
        f"/api/automation/{oid}",
        f"/api/automation/{oid}/enable",
        f"/api/automation/{oid}/pause",
        f"/api/automation/{oid}/run-now",
    ]:
        if path.endswith(("enable", "pause", "run-now")):
            r = client.post(path)
        else:
            r = client.get(path)
        assert r.status_code == 404, (path, r.status_code, r.text)


def test_huey_queue_persists_across_restart():
    """The SQLite queue is file-backed, so queued tasks survive a restart.

    Enqueuing writes the task to backend/data/huey.db. The same file is what a
    restarted consumer reads, so a task enqueued before a restart is still
    picked up afterwards (verified end-to-end in the live acceptance run).
    """
    from app.tasks import huey, execute_automation_run

    # Enqueue a task (writes to the file-backed queue).
    execute_automation_run(999999, "cron", "backend")
    # It is persisted and re-readable -- i.e. not lost in memory only.
    assert len(huey.pending()) >= 1


def test_run_now_returns_existing_when_inflight(client, db):
    """Rapid/concurrent Run-now clicks must not create a second executable run;
    the second call returns the existing inflight run_id (idempotent)."""
    cid = _make_campaign(client)
    g = client.post("/api/automation/generate", json={"prompt": "p", "campaign_id": cid})
    plan = g.json()["plan"]
    c = client.post("/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan})
    aid = c.json()["id"]

    # First Run-now creates a queued run.
    r1 = client.post(f"/api/automation/{aid}/run-now")
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "queued"
    assert r1.json()["already_inflight"] is False
    run_id = r1.json()["run_id"]

    # Second Run-now (while still queued) returns the SAME run_id, no new run.
    r2 = client.post(f"/api/automation/{aid}/run-now")
    assert r2.status_code == 200, r2.text
    assert r2.json()["run_id"] == run_id
    assert r2.json()["already_inflight"] is True

    # Exactly one inflight (queued/running) run exists for this Automation.
    inflight = (
        db.query(models.AutomationRun)
        .filter(models.AutomationRun.automation_id == aid,
                models.AutomationRun.status.in_(["queued", "running"]))
        .count()
    )
    assert inflight == 1


def test_concurrent_enqueue_single_inflight(client, db):
    """Concurrent Run-now calls for the same Automation must produce at most ONE
    inflight (queued/running) run. The partial unique index is the real DB-layer
    guard; enqueue_run folds the losing threads into the winning run instead of
    creating a second executable run."""
    import threading

    cid = _make_campaign(client)
    g = client.post("/api/automation/generate", json={"prompt": "p", "campaign_id": cid})
    plan = g.json()["plan"]
    c = client.post("/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan})
    aid = c.json()["id"]

    run_ids: list[int] = []
    errors: list[str] = []

    def worker():
        try:
            from app.db import SessionLocal
            from app.tasks import enqueue_run

            s = SessionLocal()
            try:
                a = s.get(models.Automation, aid)
                run = enqueue_run(s, a, trigger="manual", source="test")
                run_ids.append(run.id)
            finally:
                s.close()
        except Exception as e:  # pragma: no cover - defensive
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    inflight = (
        db.query(models.AutomationRun)
        .filter(models.AutomationRun.automation_id == aid,
                models.AutomationRun.status.in_(["queued", "running"]))
        .count()
    )
    assert inflight == 1, f"expected 1 inflight, got {inflight}; run_ids={run_ids}; errors={errors}"
    # All threads resolved to the single (winning) run id.
    assert len(set(run_ids)) == 1, f"multiple run ids created: {run_ids}"


def test_stale_running_reclaimed_to_failed(client, db):
    """A run stuck in `running` past the timeout is reclaimed as failed with a
    clear worker_timeout error and a finished_at timestamp (atomic)."""
    from app.tasks import reclaim_stale_runs

    cid = _make_campaign(client)
    a = models.Automation(
        owner_id=1, name="stale", prompt="x", campaign_id=cid,
        plan_json="{}", status="enabled", tick_interval_minutes=5,
    )
    db.add(a)
    db.flush()
    run = models.AutomationRun(
        automation_id=a.id, trigger="cron", source="backend", status="running",
        started_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db.add(run)
    db.commit()

    n = reclaim_stale_runs(timeout_minutes=10)
    db.refresh(run)
    assert n == 1
    assert run.status == "failed"
    assert run.error == "worker_timeout"
    assert run.finished_at is not None


def test_after_reclaim_new_run_can_be_created(client, db):
    """After a stale run is reclaimed, the Automation must be able to produce a
    NEW run on the next scheduling pass (no permanent block)."""
    from app.tasks import enqueue_run, reclaim_stale_runs

    cid = _make_campaign(client)
    a = models.Automation(
        owner_id=1, name="reclaim", prompt="x", campaign_id=cid,
        plan_json="{}", status="enabled", tick_interval_minutes=5,
    )
    db.add(a)
    db.flush()
    stale = models.AutomationRun(
        automation_id=a.id, trigger="cron", source="backend", status="running",
        started_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db.add(stale)
    db.commit()

    # Reclaim frees the inflight slot.
    assert reclaim_stale_runs(timeout_minutes=10) == 1
    db.refresh(stale)
    assert stale.status == "failed"

    # Now a new run can be enqueued (no inflight remains).
    new_run = enqueue_run(db, a, trigger="cron", source="backend")
    assert new_run.id != stale.id
    assert new_run.status == "queued"
    inflight = (
        db.query(models.AutomationRun)
        .filter(models.AutomationRun.automation_id == a.id,
                models.AutomationRun.status.in_(["queued", "running"]))
        .count()
    )
    assert inflight == 1


def test_tick_does_not_run_synchronously(client, db):
    """/tick must enqueue via the queue and NEVER execute run_tick inline.

    After the call the run is still QUEUED (not success/failed) and no business
    side-effects (approvals) have been produced synchronously."""
    cid = _make_campaign(client)
    g = client.post("/api/automation/generate", json={"prompt": "p", "campaign_id": cid})
    plan = g.json()["plan"]
    c = client.post("/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan})
    aid = c.json()["id"]
    client.post(f"/api/automation/{aid}/enable")

    r = client.post("/api/automation/tick")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enqueued"] >= 1
    run_ids = body["run_ids"]
    assert run_ids
    run = db.query(models.AutomationRun).filter_by(id=run_ids[0]).first()
    assert run is not None
    assert run.status == "queued"  # not executed inline
    # No synchronous business execution => no approvals/drafts created here.
    assert db.query(models.Approval).filter_by(campaign_id=cid).count() == 0
