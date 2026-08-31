from __future__ import annotations

from app import models, tasks
from app.gmail.transport import MessageDTO, ThreadDTO
from app.services import sync as sync_svc


def _thread(index: int) -> ThreadDTO:
    thread_id = f"thread-{index}"
    message_id = f"message-{index}"
    return ThreadDTO(
        gmail_thread_id=thread_id,
        history_id=str(1000 + index),
        subject=f"Subject {index}",
        snippet=f"Body {index}",
        messages=[MessageDTO(
            gmail_message_id=message_id,
            thread_id=thread_id,
            history_id=str(1000 + index),
            from_email=f"person{index}@example.com",
            to_email="owner@example.com",
            subject=f"Subject {index}",
            snippet=f"Body {index}",
            body_text=f"Body {index}",
            body_html="",
            is_incoming=True,
            received_at="2026-08-01T00:00:00Z",
            message_id_header=f"<{message_id}@example.com>",
        )],
    )


def _account(db):
    account = models.GmailAccount(
        id=1, user_id=1, email="owner@example.com", is_connected=True,
        history_id="1000",
    )
    db.add(account)
    db.flush()
    db.add(models.OAuthCredential(
        gmail_account_id=account.id,
        access_token_enc="encrypted-access",
        refresh_token_enc="encrypted-refresh",
    ))
    db.commit()
    return account


class PagedToolLayer:
    all_threads = [_thread(i) for i in range(205)]

    def __init__(self, db, account, oauth):
        self.db = db
        self.account = account

    def _transport(self):
        return self

    def get_profile(self):
        return {"email": self.account.email, "historyId": "2000"}

    def search_threads(self, query, max_results=100, page_token=None, **kwargs):
        assert query == sync_svc.INITIAL_IMPORT_QUERY
        assert kwargs.get("include_spam_trash") is False
        offset = int(page_token or 0)
        page = self.all_threads[offset:offset + max_results]
        next_offset = offset + len(page)
        return page, (str(next_offset) if next_offset < len(self.all_threads) else None)

    def list_history(self, start_history_id, page_token=None, **kwargs):
        assert start_history_id == "2000"
        return [], "2000", None

    def get_thread(self, thread_id, **kwargs):
        index = int(thread_id.split("-")[-1])
        return self.all_threads[index]


def test_initial_import_consumes_every_page_and_marks_baseline_complete(db, monkeypatch):
    account = _account(db)
    run = models.GmailSyncRun(
        owner_id=1, gmail_account_id=account.id, kind="initial_full",
        status="queued", query=sync_svc.INITIAL_IMPORT_QUERY,
        include_spam_trash=False,
    )
    db.add(run)
    db.commit()
    monkeypatch.setattr(tasks, "UnifiedEmailToolLayer", PagedToolLayer)

    result = tasks.execute_gmail_initial_import.call_local(run.id)

    db.expire_all()
    saved = db.get(models.GmailSyncRun, run.id)
    assert result["ok"] is True
    assert saved.status == "completed"
    assert saved.threads_scanned == 205
    assert saved.new_threads == 205
    assert saved.new_messages == 205
    assert saved.page_token is None
    assert sync_svc.initial_import_completed(db, account.id) is True
    assert db.query(models.EmailThread).count() == 205


def test_incremental_history_pages_fetch_only_unique_affected_threads(db, monkeypatch):
    account = _account(db)
    db.add(models.GmailSyncRun(
        owner_id=1, gmail_account_id=account.id, kind="initial_full",
        status="completed", start_history_id="900", latest_history_id="1000",
    ))
    db.commit()

    class IncrementalToolLayer(PagedToolLayer):
        fetched: list[str] = []

        def list_history(self, start_history_id, page_token=None, **kwargs):
            if not page_token:
                return ([{"messagesAdded": [{"message": {"threadId": "thread-1"}}]}],
                        "1001", "next")
            return ([{"messages": [{"threadId": "thread-1"}, {"threadId": "thread-2"}]}],
                    "1002", None)

        def get_thread(self, thread_id, **kwargs):
            self.fetched.append(thread_id)
            return super().get_thread(thread_id, **kwargs)

    monkeypatch.setattr(sync_svc, "UnifiedEmailToolLayer", IncrementalToolLayer)
    result = sync_svc.sync_incremental(db, account, account.oauth)
    db.commit()

    assert result["history_pages"] == 2
    assert result["affected_threads"] == 2
    assert result["threads"] == 2
    assert result["history_id"] == "1002"
    assert IncrementalToolLayer.fetched == ["thread-1", "thread-2"]
    assert account.history_id == "1002"


def test_incremental_sync_requires_completed_first_import(client, db):
    _account(db)
    response = client.post("/api/gmail/sync")
    assert response.status_code == 409
    assert "INITIAL_IMPORT_REQUIRED" in response.text


def test_initial_import_api_is_resumable_and_single_inflight(client, db, monkeypatch):
    account = _account(db)
    queued: list[int] = []
    monkeypatch.setattr(tasks, "enqueue_gmail_initial_import", lambda run_id: queued.append(run_id))

    first = client.post("/api/gmail/imports")
    assert first.status_code == 200
    payload = first.json()
    run_id = payload["run"]["id"]
    assert payload["created"] is True
    assert queued == [run_id]

    duplicate = client.post("/api/gmail/imports").json()
    assert duplicate["created"] is False
    assert duplicate["run"]["id"] == run_id

    paused = client.post(f"/api/gmail/imports/{run_id}/pause").json()
    assert paused["run"]["status"] == "paused"
    resumed = client.post(f"/api/gmail/imports/{run_id}/resume").json()
    assert resumed["run"]["status"] == "queued"
    assert queued == [run_id, run_id]
    cancelled = client.post(f"/api/gmail/imports/{run_id}/cancel").json()
    assert cancelled["run"]["status"] == "cancelled"
    assert sync_svc.initial_import_completed(db, account.id) is False
