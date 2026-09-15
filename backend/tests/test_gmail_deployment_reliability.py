"""Offline deployment regressions: no customer Gmail or production database."""
from types import SimpleNamespace

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app import gmail, models, tasks
from app.gmail.client import RealGmailTransport
from app.gmail.transport import InMemoryGmailTransport
from app.services import sync


def http_error(status):
    return HttpError(httplib2.Response({'status': str(status)}),
                     b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}')


@pytest.mark.parametrize('cached', [False, True])
def test_unusable_credentials_never_return_demo_in_production(monkeypatch, cached):
    account = SimpleNamespace(id=17, email='owner@example.com')
    monkeypatch.setattr(gmail, 'get_settings', lambda: SimpleNamespace(
        GOOGLE_CLIENT_ID=None, ALLOW_INMEMORY_GMAIL=False))
    if cached:
        gmail._TRANSPORT_CACHE[17] = InMemoryGmailTransport(account_email=account.email)
    with pytest.raises(RuntimeError, match='gmail_credentials_unavailable'):
        gmail.get_transport_for_account(account, None)
    assert 17 not in gmail._TRANSPORT_CACHE


def test_decryption_failure_evicts_real_cache(monkeypatch):
    monkeypatch.setattr(gmail, 'get_settings', lambda: SimpleNamespace(
        GOOGLE_CLIENT_ID='id', ALLOW_INMEMORY_GMAIL=False))
    class BrokenCipher:
        def decrypt(self, value):
            raise ValueError('unreadable')
    monkeypatch.setattr(gmail, 'Cipher', BrokenCipher)
    gmail._TRANSPORT_CACHE[17] = object.__new__(RealGmailTransport)
    with pytest.raises(RuntimeError, match='gmail_credentials_unavailable'):
        gmail.get_transport_for_account(SimpleNamespace(id=17),
                                        SimpleNamespace(access_token_enc='encrypted'))
    assert 17 not in gmail._TRANSPORT_CACHE


def test_oauth_connection_replaces_cached_demo(monkeypatch):
    account = SimpleNamespace(id=17, email='owner@example.com')
    monkeypatch.setattr(gmail, 'get_settings', lambda: SimpleNamespace(ALLOW_INMEMORY_GMAIL=False))
    monkeypatch.setattr(gmail, '_real_credentials_available', lambda *args: True)
    actual = object()
    monkeypatch.setattr(gmail, 'RealGmailTransport', lambda *args: actual)
    gmail._TRANSPORT_CACHE[17] = InMemoryGmailTransport(account_email=account.email)
    assert gmail.get_transport_for_account(account, SimpleNamespace(access_token_enc='encrypted')) is actual


def test_consumer_refreshes_settings_after_desktop_oauth_import(monkeypatch):
    from functools import lru_cache
    persisted = {'client_id': None}
    @lru_cache
    def settings():
        return SimpleNamespace(GOOGLE_CLIENT_ID=persisted['client_id'], ALLOW_INMEMORY_GMAIL=False)
    assert settings().GOOGLE_CLIENT_ID is None
    persisted['client_id'] = 'new-desktop-client'
    monkeypatch.setattr(gmail, 'get_settings', settings)
    monkeypatch.setattr(gmail, '_real_credentials_available', lambda s, o: bool(s.GOOGLE_CLIENT_ID))
    actual = object()
    monkeypatch.setattr(gmail, 'RealGmailTransport', lambda *args: actual)
    assert gmail.get_transport_for_account(SimpleNamespace(id=17),
        SimpleNamespace(access_token_enc='encrypted')) is actual


@pytest.mark.parametrize('error', [http_error(403), http_error(500), ValueError('parse failed')])
def test_import_page_fails_instead_of_skipping_unreadable_thread(error):
    transport = object.__new__(RealGmailTransport)
    transport._call = lambda fn: {'threads': [{'id': 'a'}], 'nextPageToken': 'next'}
    def fail(thread_id):
        raise error
    transport.get_thread = fail
    with pytest.raises(type(error)):
        transport.list_threads('in:anywhere')


@pytest.mark.parametrize('status', [403, 500])
def test_changed_thread_failure_preserves_cursor(db, monkeypatch, status):
    account = models.GmailAccount(user_id=1, email='owner@example.com', history_id='900')
    db.add(account)
    db.commit()
    class Layer:
        def __init__(self, *args):
            pass
        def list_history(self, *args, **kwargs):
            return [{'threadId': 'a'}], '2000', None
        def get_thread(self, *args, **kwargs):
            raise http_error(status)
    monkeypatch.setattr(sync, 'UnifiedEmailToolLayer', Layer)
    with pytest.raises(HttpError):
        sync.sync_history(db, account, None, '900')
    db.rollback()
    assert db.get(models.GmailAccount, account.id).history_id == '900'


def test_replay_failure_resumes_without_rescanning(db, monkeypatch):
    account = models.GmailAccount(user_id=1, email='owner@example.com',
                                  is_connected=True, history_id='900')
    db.add(account)
    db.flush()
    db.add(models.OAuthCredential(gmail_account_id=account.id, access_token_enc='encrypted'))
    run = models.GmailSyncRun(owner_id=1, gmail_account_id=account.id,
                             kind='initial_full', status='queued')
    db.add(run)
    db.commit()
    scans = []
    replay_attempts = []
    class Layer:
        def __init__(self, *args):
            pass
        def _transport(self):
            return self
        def get_profile(self):
            return {'historyId': '2000'}
        def search_threads(self, *args, **kwargs):
            scans.append(kwargs.get('page_token'))
            return [], None
    def replay(session, acct, oauth, cursor):
        replay_attempts.append(cursor)
        if len(replay_attempts) == 1:
            raise http_error(403)
        acct.history_id = '2001'
        return dict(history_id='2001', threads=0, new_threads=0, new_messages=0, failures=0)
    monkeypatch.setattr(tasks, 'UnifiedEmailToolLayer', Layer)
    monkeypatch.setattr(sync, 'sync_history', replay)
    assert tasks.execute_gmail_initial_import.call_local(run.id)['ok'] is False
    db.expire_all()
    assert run.scan_completed is True
    assert account.history_id == '900'
    run.status = 'queued'
    db.commit()
    assert tasks.execute_gmail_initial_import.call_local(run.id)['ok'] is True
    db.expire_all()
    assert scans == [None]
    assert replay_attempts == ['2000', '2000']
    assert run.status == 'completed'
    assert account.history_id == '2001'


def test_quota_backoff_retries_then_succeeds(monkeypatch):
    from app.gmail import client
    waits = []
    monkeypatch.setattr(client.time, 'sleep', waits.append)
    transport = object.__new__(RealGmailTransport)
    transport._service = None
    transport._ensure_service = lambda: None
    calls = []
    def request(service):
        calls.append(1)
        if len(calls) == 1:
            raise http_error(403)
        return 'ok'
    assert transport._call(request) == 'ok'
    assert waits == [65.0]


def test_production_import_fails_without_usable_credentials(db, monkeypatch):
    account = models.GmailAccount(user_id=1, email='owner@example.com',
                                  is_connected=True, history_id='900')
    db.add(account)
    db.flush()
    db.add(models.OAuthCredential(gmail_account_id=account.id, access_token_enc='unreadable'))
    run = models.GmailSyncRun(owner_id=1, gmail_account_id=account.id,
                             kind='initial_full', status='queued')
    db.add(run)
    db.commit()
    monkeypatch.setattr(gmail, 'get_settings', lambda: SimpleNamespace(
        GOOGLE_CLIENT_ID=None, ALLOW_INMEMORY_GMAIL=False))
    result = tasks.execute_gmail_initial_import.call_local(run.id)
    db.expire_all()
    assert not result['ok']
    assert run.status == 'failed'
    assert 'gmail_credentials_unavailable' in run.error
    assert not run.scan_completed
    assert account.history_id == '900'
    assert db.query(models.EmailThread).count() == 0
    assert not sync.initial_import_completed(db, account.id)


def test_legacy_run_additive_migration_preserves_checkpoint(db):
    from sqlalchemy import text
    from app import migrations
    account = models.GmailAccount(user_id=1, email='owner@example.com')
    db.add(account)
    db.flush()
    run = models.GmailSyncRun(owner_id=1, gmail_account_id=account.id,
                             kind='initial_full', status='failed',
                             threads_scanned=100, page_token='next-page')
    db.add(run)
    db.commit()
    run_id = run.id
    # Reproduce the pre-upgrade schema in the disposable test database only.
    db.execute(text('ALTER TABLE gmail_sync_runs DROP COLUMN scan_completed'))
    db.commit()
    added = migrations.run()
    db.expire_all()
    saved = db.get(models.GmailSyncRun, run_id)
    assert any(t == 'gmail_sync_runs' and c == 'scan_completed' for t, c, _ in added)
    assert saved.scan_completed is None
    assert saved.page_token == 'next-page' and saved.threads_scanned == 100
    assert saved.status == 'failed'
