from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import event

from app import models, tasks
from app.db import engine


def _account_with_completed_import(db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    db.add(account)
    db.flush()
    db.add(models.OAuthCredential(gmail_account_id=account.id, access_token_enc="access"))
    imported = models.GmailSyncRun(
        owner_id=1, gmail_account_id=account.id, kind="initial_full", status="completed"
    )
    db.add(imported)
    db.commit()
    return account, imported


def _threads(db, account, count: int):
    rows = [
        models.EmailThread(gmail_account_id=account.id, gmail_thread_id=f"t-{index}", subject=f"S {index}")
        for index in range(count)
    ]
    db.add_all(rows)
    db.commit()
    return rows


def _triage_run(db, imported, *, status: str, total_threads: int = 2, failed_threads: int = 0):
    run = models.InboxTriageRun(
        owner_id=1,
        gmail_sync_run_id=imported.id,
        status=status,
        total_threads=total_threads,
        batch_size=50,
        failed_threads=failed_threads,
    )
    db.add(run)
    db.flush()
    return run


def test_first_triage_requires_completed_import(client, db):
    account = models.GmailAccount(user_id=1, email="owner@example.com", is_connected=True)
    db.add(account)
    db.commit()

    response = client.post("/api/inbox/initial-triage")

    assert response.status_code == 409
    assert "INITIAL_IMPORT_REQUIRED" in response.text


def test_first_triage_snapshot_is_batched_and_never_creates_send_artifacts(db, client, monkeypatch):
    account, imported = _account_with_completed_import(db)
    rows = _threads(db, account, 105)
    queued: list[int] = []
    monkeypatch.setattr(tasks, "enqueue_initial_inbox_triage", lambda run_id: queued.append(run_id))

    start = client.post("/api/inbox/initial-triage")
    assert start.status_code == 200
    run_id = start.json()["run"]["id"]
    assert start.json()["run"]["total_threads"] == 105
    assert queued == [run_id]

    from app.api import inbox

    calls: list[int] = []

    def fake_analyze(_db, thread):
        calls.append(thread.id)
        thread.intent = "interested" if thread.id % 3 else "filtered_spam"
        thread.pending_action = "reply" if thread.id % 3 else "no_action"
        _db.commit()
        return {"intent": thread.intent, "pending_action": thread.pending_action}

    monkeypatch.setattr(inbox, "_analyze_thread", fake_analyze)
    result = tasks.execute_initial_inbox_triage.call_local(run_id)
    db.expire_all()
    run = db.get(models.InboxTriageRun, run_id)

    assert result["ok"] is True
    assert run.status == "completed"
    assert run.total_threads == 105
    assert run.current_batch == 3
    assert run.processed_threads == 105
    assert run.auto_filtered == 35
    assert run.business_threads == 70
    assert len(calls) == 105
    assert db.query(models.InboxTriageRunItem).filter_by(triage_run_id=run_id, status="completed").count() == 105
    assert db.query(models.EmailDraft).count() == 0
    assert db.query(models.Approval).count() == 0
    assert db.query(models.AutomationRun).count() == 0
    assert imported.id == run.gmail_sync_run_id
    assert {row.id for row in rows} == set(calls)


def test_daily_triage_freezes_all_untriaged_threads_and_processes_batches(db, client, monkeypatch):
    account, _ = _account_with_completed_import(db)
    rows = _threads(db, account, 51)
    queued: list[int] = []
    monkeypatch.setattr(tasks, "enqueue_daily_inbox_triage", lambda run_id: queued.append(run_id))

    response = client.post("/api/inbox/daily-triage")
    assert response.status_code == 200
    run_id = response.json()["run"]["id"]
    assert response.json()["run"]["kind"] == "daily_incremental"
    assert response.json()["run"]["total_threads"] == 51
    assert queued == [run_id]

    later = models.EmailThread(gmail_account_id=account.id, gmail_thread_id="later", subject="later")
    db.add(later); db.commit()
    from app.api import inbox
    calls: list[int] = []
    def fake_analyze(_db, thread):
        calls.append(thread.id)
        thread.intent, thread.pending_action = "filtered_newsletter", "no_action"
        _db.commit()
        return {"intent": thread.intent, "pending_action": thread.pending_action}
    monkeypatch.setattr(inbox, "_analyze_thread", fake_analyze)

    result = tasks.execute_daily_inbox_triage.call_local(run_id)
    db.expire_all()
    run = db.get(models.InboxTriageRun, run_id)
    assert result["status"] == "completed"
    assert run.current_batch == 2
    assert run.processed_threads == 51
    assert later.id not in calls
    assert db.get(models.EmailThread, later.id).intent is None


def test_first_triage_skips_manual_result_and_retry_only_requeues_failures(db, monkeypatch):
    account, imported = _account_with_completed_import(db)
    rows = _threads(db, account, 2)
    rows[0].intent = "filtered_spam"
    run = models.InboxTriageRun(
        owner_id=1, gmail_sync_run_id=imported.id, status="queued", total_threads=2, batch_size=50
    )
    db.add(run)
    db.flush()
    db.add_all([
        models.InboxTriageRunItem(triage_run_id=run.id, email_thread_id=rows[0].id),
        models.InboxTriageRunItem(triage_run_id=run.id, email_thread_id=rows[1].id),
    ])
    db.commit()

    from app.api import inbox

    def broken_analyze(_db, _thread):
        raise RuntimeError("synthetic analysis failure")

    monkeypatch.setattr(inbox, "_analyze_thread", broken_analyze)
    tasks.execute_initial_inbox_triage.call_local(run.id)
    db.expire_all()
    saved = db.get(models.InboxTriageRun, run.id)

    assert saved.status == "completed"
    assert saved.skipped_threads == 1
    assert saved.failed_threads == 1
    failed = db.query(models.InboxTriageRunItem).filter_by(triage_run_id=run.id, status="failed").one()
    assert failed.email_thread_id == rows[1].id


def test_daily_snapshot_does_not_race_a_running_first_triage(db, client, monkeypatch):
    account, imported = _account_with_completed_import(db)
    rows = _threads(db, account, 2)
    run = models.InboxTriageRun(
        owner_id=1, gmail_sync_run_id=imported.id, status="running", total_threads=2, batch_size=50
    )
    db.add(run)
    db.flush()
    db.add_all([
        models.InboxTriageRunItem(triage_run_id=run.id, email_thread_id=rows[0].id, status="queued"),
        models.InboxTriageRunItem(triage_run_id=run.id, email_thread_id=rows[1].id, status="running"),
    ])
    db.commit()

    from app.api import inbox

    calls: list[int] = []

    def fake_analyze(_db, thread):
        calls.append(thread.id)
        return {"intent": "interested"}

    monkeypatch.setattr(inbox, "_analyze_thread", fake_analyze)
    response = client.post("/api/inbox/daily-triage")

    assert response.status_code == 200
    assert response.json()["created"] is False
    assert response.json()["reason"] == "TRIAGE_RUN_ACTIVE"
    assert calls == []


def test_initial_triage_pause_sets_paused(db, client):
    _, imported = _account_with_completed_import(db)
    run = _triage_run(db, imported, status="running")
    db.commit()

    response = client.post(f"/api/inbox/initial-triage/{run.id}/pause")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["run"]["status"] == "paused"
    assert response.json()["run"]["can_resume"] is True
    assert db.query(models.AuditLog).filter_by(action="initial_inbox_triage_pause").count() == 1


def test_initial_triage_pause_rejects_terminal_status(db, client):
    _, imported = _account_with_completed_import(db)
    run = _triage_run(db, imported, status="completed")
    db.commit()

    response = client.post(f"/api/inbox/initial-triage/{run.id}/pause")

    assert response.status_code == 409
    assert "initial_triage_is_completed" in response.text
    db.expire_all()
    assert db.get(models.InboxTriageRun, run.id).status == "completed"


def test_initial_triage_resume_requeues_and_resets_items(db, client, monkeypatch):
    account, imported = _account_with_completed_import(db)
    row = _threads(db, account, 1)[0]
    run = _triage_run(db, imported, status="paused")
    run.error = "worker_timeout_resumable"
    run.finished_at = datetime.now(timezone.utc)
    db.add(models.InboxTriageRunItem(triage_run_id=run.id, email_thread_id=row.id, status="running"))
    db.commit()
    queued: list[int] = []
    monkeypatch.setattr(tasks, "enqueue_initial_inbox_triage", lambda run_id: queued.append(run_id))

    response = client.post(f"/api/inbox/initial-triage/{run.id}/resume")

    assert response.status_code == 200
    assert response.json()["run"]["status"] == "queued"
    db.expire_all()
    saved = db.get(models.InboxTriageRun, run.id)
    item = db.query(models.InboxTriageRunItem).filter_by(triage_run_id=run.id).one()
    assert item.status == "queued"
    assert saved.error is None
    assert saved.finished_at is None
    assert queued == [run.id]


def test_initial_triage_cancel_sets_cancelled(db, client):
    _, imported = _account_with_completed_import(db)
    run = _triage_run(db, imported, status="running")
    db.commit()

    response = client.post(f"/api/inbox/initial-triage/{run.id}/cancel")

    assert response.status_code == 200
    assert response.json()["run"]["status"] == "cancelled"
    assert response.json()["run"]["can_cancel"] is False
    db.expire_all()
    assert db.get(models.InboxTriageRun, run.id).finished_at is not None


def test_initial_triage_retry_failed_only_requeues_failed_items(db, client, monkeypatch):
    account, imported = _account_with_completed_import(db)
    failed_row, completed_row = _threads(db, account, 2)
    run = _triage_run(db, imported, status="completed", failed_threads=1)
    db.add_all([
        models.InboxTriageRunItem(
            triage_run_id=run.id, email_thread_id=failed_row.id, status="failed", outcome="business", error="model_error"
        ),
        models.InboxTriageRunItem(
            triage_run_id=run.id, email_thread_id=completed_row.id, status="completed", outcome="business"
        ),
    ])
    db.commit()
    queued: list[int] = []
    monkeypatch.setattr(tasks, "enqueue_initial_inbox_triage", lambda run_id: queued.append(run_id))

    response = client.post(f"/api/inbox/initial-triage/{run.id}/retry-failed")

    assert response.status_code == 200
    assert response.json()["retried"] == 1
    assert response.json()["run"]["status"] == "queued"
    db.expire_all()
    items = {item.email_thread_id: item for item in db.query(models.InboxTriageRunItem).filter_by(triage_run_id=run.id)}
    assert items[failed_row.id].status == "queued"
    assert items[failed_row.id].outcome is None
    assert items[failed_row.id].error is None
    assert items[completed_row.id].status == "completed"
    assert db.get(models.InboxTriageRun, run.id).failed_threads == 0
    assert queued == [run.id]


def test_initial_triage_retry_failed_without_failures_is_rejected(db, client):
    _, imported = _account_with_completed_import(db)
    run = _triage_run(db, imported, status="completed")
    db.commit()

    response = client.post(f"/api/inbox/initial-triage/{run.id}/retry-failed")

    assert response.status_code == 409
    assert "initial_triage_no_failed_items" in response.text
    db.expire_all()
    assert db.get(models.InboxTriageRun, run.id).status == "completed"


def test_large_inbox_reads_are_batched_not_one_query_per_thread(db, client):
    """Progress refreshes must remain usable after a large first import."""
    account, _ = _account_with_completed_import(db)
    threads = []
    for index in range(180):
        thread = models.EmailThread(
            gmail_account_id=account.id,
            gmail_thread_id=f"history-{index}",
            contact_email=f"sender-{index % 12}@example.com",
            subject=f"History {index}",
        )
        db.add(thread)
        threads.append(thread)
    db.flush()
    db.add_all([
        models.EmailMessage(
            thread_id=thread.id,
            gmail_message_id=f"message-{thread.id}",
            is_incoming=True,
            received_at=datetime.now(timezone.utc),
        )
        for thread in threads
    ])
    db.commit()

    selects = 0

    def count_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal selects
        if statement.lstrip().upper().startswith("SELECT"):
            selects += 1

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        customers = client.get("/api/inbox/customers")
        stats = client.get("/api/inbox/stats")
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert customers.status_code == 200
    assert len(customers.json()) == 12
    assert stats.status_code == 200
    assert stats.json()["total"] == 180
    # Two endpoints each load threads and messages in batches, plus the one
    # owner/contact lookup. This must not grow with every imported thread.
    assert selects <= 12
