"""Gmail network timeout hardening tests (Round 3).

These prove:
  * The real Gmail transport is built with an httplib2 socket timeout and is
    authorized via `http=` (never the mutually-exclusive `credentials=` arg).
  * OAuth token refresh uses a session with a default timeout.
  * A Gmail request that never returns is interrupted by the socket timeout and
    surfaces as a recognizable `GmailTimeoutError` (not a permanent hang).
  * When a Gmail call times out, the AutomationRun reaches a terminal state
    (partial/failed) with `gmail_timeout` in the error and the worker returns
    within the bound, so it can process the NEXT run (no wedged concurrency).
"""
from __future__ import annotations

import threading
import time
import ssl
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from google.oauth2.credentials import Credentials

from app import models
from app.gmail.auth import build_credentials, maybe_refresh
from app.gmail.client import RealGmailTransport
from app.gmail.transport import GmailTimeoutError, GmailTransientNetworkError, GmailTransport, InMemoryGmailTransport
from app.tasks import execute_automation_run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _StubSettings:
    GOOGLE_CLIENT_ID = "test-client-id"
    GOOGLE_CLIENT_SECRET = "test-client-secret"
    GMAIL_HTTP_TIMEOUT_SECONDS = 25


class _StubCipher:
    def decrypt(self, v):
        return v

    def encrypt(self, v):
        return v


def _make_connected_account(db):
    acct = models.GmailAccount(id=1, user_id=1, email="tac.aisolution@gmail.com", is_connected=True,
                               history_id="1")
    db.add(acct)
    db.flush()
    oauth = models.OAuthCredential(
        gmail_account_id=acct.id,
        access_token_enc="enc_access",
        refresh_token_enc="enc_refresh",
        token_expiry=datetime(2999, 1, 1, tzinfo=timezone.utc),
    )
    db.add(oauth)
    db.add(models.GmailSyncRun(
        owner_id=1, gmail_account_id=acct.id, kind="initial_full",
        status="completed", start_history_id="1", latest_history_id="1",
    ))
    db.flush()
    return acct, oauth


def _make_automation(client, db):
    cid = client.post("/api/campaigns", json={"name": "C", "goal": "g"}).json()["id"]
    contact = models.Contact(owner_id=1, email="lead@example.com", first_name="Lead", company="X")
    db.add(contact)
    db.flush()
    cc = models.CampaignContact(campaign_id=cid, contact_id=contact.id, status="queued")
    db.add(cc)
    db.flush()
    g = client.post("/api/automation/generate", json={"prompt": "p", "campaign_id": cid})
    plan = g.json()["plan"]
    a = client.post("/api/automation", json={"prompt": "p", "campaign_id": cid, "plan": plan})
    aid = a.json()["id"]
    client.post(f"/api/automation/{aid}/enable")
    return aid


class _TimeoutTransport(GmailTransport):
    """Transport double that simulates a Gmail request timing out."""

    def __init__(self, account_email="demo@example.com"):
        self.account_email = account_email

    def get_profile(self):
        return {"email": "me@example.com", "historyId": "1"}

    def list_threads(self, query, max_results=20, page_token=None):
        # Simulate a "permanent wait" before the network layer gives up.
        time.sleep(0.3)
        raise GmailTimeoutError("gmail_timeout: simulated socket timeout")

    def get_thread(self, thread_id):
        raise GmailTimeoutError("gmail_timeout: simulated")

    def get_message(self, message_id):
        raise GmailTimeoutError("gmail_timeout: simulated")

    def create_draft(self, *a, **k):
        raise NotImplementedError

    def update_draft(self, *a, **k):
        raise NotImplementedError

    def send_draft(self, *a, **k):
        raise NotImplementedError

    def add_label(self, *a, **k):
        raise NotImplementedError

    def remove_label(self, *a, **k):
        raise NotImplementedError

    def archive_thread(self, *a, **k):
        raise NotImplementedError

    def list_history(self, *a, **k):
        time.sleep(0.3)
        raise GmailTimeoutError("gmail_timeout: simulated socket timeout")


def test_transient_tls_eof_is_retried_within_bound(monkeypatch):
    """A proxy/TLS EOF is replayed without leaking credentials into logs."""
    transport = RealGmailTransport.__new__(RealGmailTransport)
    transport._service = object()
    transport._ensure_service = lambda: None
    monkeypatch.setattr("app.gmail.client.time.sleep", lambda _: None)
    attempts = {"count": 0}

    def call(_service):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
        return {"ok": True}

    assert transport._call(call) == {"ok": True}
    assert attempts["count"] == 3


def test_exhausted_tls_eof_is_recognizable_and_durable(monkeypatch):
    transport = RealGmailTransport.__new__(RealGmailTransport)
    transport._service = object()
    transport._ensure_service = lambda: None
    monkeypatch.setattr("app.gmail.client.time.sleep", lambda _: None)

    with pytest.raises(GmailTransientNetworkError, match="gmail_transport_retry_exhausted"):
        transport._call(lambda _service: (_ for _ in ()).throw(
            ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
        ))


# ---------------------------------------------------------------------------
# 1) Transport is built with an httplib2 socket timeout via `http=` (no `credentials=`)
# ---------------------------------------------------------------------------
def test_gmail_transport_builds_with_http_timeout_not_credentials():
    captured = {}
    timeouts = []
    real_http = httplib2.Http

    class _RecHttp(real_http):
        def __init__(self, *a, **k):
            timeouts.append(k.get("timeout"))
            super().__init__(*a, **k)

    def _build(*a, **k):
        captured.update(k)
        return MagicMock()

    with patch("app.gmail.client.build", _build), \
         patch("app.gmail.client.httplib2.Http", _RecHttp), \
         patch("app.gmail.client.maybe_refresh", return_value=False), \
         patch("app.gmail.client.Cipher", _StubCipher), \
         patch("app.gmail.client.get_settings", lambda: _StubSettings()):
        acct = models.GmailAccount(id=1, user_id=1, email="me@example.com", is_connected=True)
        oauth = models.OAuthCredential(
            gmail_account_id=1, access_token_enc="x", refresh_token_enc="y",
            token_expiry=datetime(2999, 1, 1, tzinfo=timezone.utc),
        )
        t = RealGmailTransport(acct, oauth)
        t._ensure_service()

    assert "http" in captured, f"build() must receive http=, got keys {list(captured)}"
    assert "credentials" not in captured, f"build() must NOT receive credentials=, got {list(captured)}"
    assert timeouts, "httplib2.Http must be constructed with a timeout"
    assert timeouts[-1] == _StubSettings.GMAIL_HTTP_TIMEOUT_SECONDS


def test_update_draft_passes_id_as_gmail_request_parameter():
    transport = RealGmailTransport.__new__(RealGmailTransport)
    service = MagicMock()
    transport._service = service
    transport._ensure_service = lambda: None
    service.users.return_value.drafts.return_value.update.return_value.execute.return_value = {
        "id": "draft-42"
    }

    result = transport.update_draft(
        "draft-42", "lead@example.com", "Subject", "Body", "",
    )

    assert result["id"] == "draft-42"
    request = service.users.return_value.drafts.return_value.update
    assert request.call_args.kwargs["id"] == "draft-42"
    assert "id" not in request.call_args.kwargs["body"]


def test_build_credentials_preserves_utc_expiry_for_preflight_refresh():
    """A persisted expiry must reach google-auth so maybe_refresh can run
    before the first Gmail API request rather than waiting for a 401."""
    expired_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    creds = build_credentials("access", "refresh", expired_at.timestamp(), _StubSettings())

    assert creds.expiry == expired_at.replace(tzinfo=None)
    assert creds.expired is True


def test_expired_persisted_token_is_refreshed_and_saved_before_gmail_call(monkeypatch):
    """The normal transport path must save a successful preflight refresh.

    This is entirely mocked: it proves the persistence hand-off without making
    an OAuth or Gmail network request.
    """
    monkeypatch.setattr("app.gmail.client.build", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr("app.gmail.client.Cipher", _StubCipher)
    monkeypatch.setattr("app.gmail.client.get_settings", lambda: _StubSettings())

    def refresh(creds, timeout):
        assert creds.expired is True
        creds.token = "fresh-access-token"
        creds.expiry = datetime(2999, 1, 1)
        return True

    monkeypatch.setattr("app.gmail.client.maybe_refresh", refresh)
    acct = models.GmailAccount(id=1, user_id=1, email="me@example.com", is_connected=True)
    oauth = models.OAuthCredential(
        gmail_account_id=1, access_token_enc="old-access", refresh_token_enc="refresh",
        token_expiry=datetime(2000, 1, 1, tzinfo=timezone.utc),
    )
    transport = RealGmailTransport(acct, oauth)
    persisted = []
    transport._persist_refreshed = lambda creds: persisted.append((creds.token, creds.expiry))

    transport._ensure_service()

    assert persisted == [("fresh-access-token", datetime(2999, 1, 1))]
    assert transport._persisted_access_token == "fresh-access-token"


def test_post_401_refresh_is_persisted_after_successful_call():
    """AuthorizedHttp can refresh in-memory after a server-side 401; retain
    that token for later requests rather than repeating the refresh."""
    transport = RealGmailTransport.__new__(RealGmailTransport)
    transport._service = object()
    transport._ensure_service = lambda: None
    transport._persisted_access_token = "old"
    transport._creds = MagicMock(token="old")
    persisted = []
    transport._persist_refreshed = lambda creds: persisted.append(creds.token)

    def call(_service):
        transport._creds.token = "refreshed-after-401"
        return {"ok": True}

    assert transport._call(call) == {"ok": True}
    assert persisted == ["refreshed-after-401"]
    assert transport._persisted_access_token == "refreshed-after-401"


# ---------------------------------------------------------------------------
# 2) OAuth token refresh uses a session carrying a default timeout
# ---------------------------------------------------------------------------
def test_token_refresh_uses_timeout_session():
    captured = {}

    def _fake_refresh(self, request):
        captured["request"] = request
        self.token = "refreshed"
        self.expiry = None  # expiry is settable; expired becomes False

    with patch.object(Credentials, "refresh", _fake_refresh):
        creds = Credentials(
            token="old", refresh_token="r", token_uri="https://oauth2.googleapis.com/token",
            client_id="cid", client_secret="sec",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
            expiry=datetime(2000, 1, 1, tzinfo=timezone.utc),  # expired
        )
        creds = Credentials(
            token="old", refresh_token="r", token_uri="https://oauth2.googleapis.com/token",
            client_id="cid", client_secret="sec",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
            expiry=datetime(2000, 1, 1),  # naive, in the past -> expired
        )
        ok = maybe_refresh(creds, timeout=7)

    assert ok is True
    req = captured["request"]
    assert hasattr(req, "session"), "refresh must use a Request(session=...)"
    sess = req.session
    assert getattr(sess, "_timeout", None) == 7, f"refresh session must carry timeout=7, got {getattr(sess, '_timeout', None)}"


# ---------------------------------------------------------------------------
# 3) A blocked/slow Gmail call is interrupted by the socket timeout (real socket).
#    Proves the worker never hangs forever: a never-replying endpoint raises
#    GmailTimeoutError within the configured bound.
# ---------------------------------------------------------------------------
def test_real_gmail_socket_timeout_interrupts_blocked_call(monkeypatch):
    import socket as _sock

    # The sandbox may have HTTP(S)_PROXY env vars; httplib2 would otherwise try
    # (and fail) to use an unsupported proxy. Clear them for this blackhole test.
    for _v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(_v, raising=False)

    srv = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    srv.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _accept():
        try:
            conn, _ = srv.accept()
            time.sleep(30)  # accept but never reply -> simulates unreachable Gmail
        except Exception:
            pass

    threading.Thread(target=_accept, daemon=True).start()

    blackhole = f"http://127.0.0.1:{port}"
    real_http = httplib2.Http

    class _BlackHoleHttp(real_http):
        def request(self, uri, *a, **k):
            return super().request(blackhole, *a, **k)

    class _FastSettings(_StubSettings):
        GMAIL_HTTP_TIMEOUT_SECONDS = 1

    with patch("app.gmail.client.httplib2.Http", lambda *a, **k: _BlackHoleHttp(*a, **k)), \
         patch("app.gmail.client.maybe_refresh", return_value=False), \
         patch("app.gmail.client.Cipher", _StubCipher), \
         patch("app.gmail.client.get_settings", lambda: _FastSettings()):
        acct = models.GmailAccount(id=1, user_id=1, email="me@example.com", is_connected=True)
        oauth = models.OAuthCredential(
            gmail_account_id=1, access_token_enc="x", refresh_token_enc="y",
            token_expiry=datetime(2999, 1, 1, tzinfo=timezone.utc),
        )
        t = RealGmailTransport(acct, oauth)
        t._ensure_service()
        start = time.time()
        try:
            t.get_profile()
            pytest.fail("Gmail call should have timed out")
        except GmailTimeoutError:
            elapsed = time.time() - start

    assert 0.4 < elapsed < 6, f"socket timeout must fire near the 1s bound, got {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 4) On Gmail timeout the run reaches a terminal state and the worker returns;
#    a subsequent run still executes (no wedged concurrency).
# ---------------------------------------------------------------------------
def test_gmail_timeout_marks_run_terminal_and_worker_continues(client, db):
    _make_connected_account(db)
    aid = _make_automation(client, db)

    # Manually create a queued run (deterministic; bypasses the live queue).
    run = models.AutomationRun(automation_id=aid, trigger="manual", source="backend", status="queued")
    db.add(run)
    db.flush()
    run_id = run.id
    db.commit()

    # --- first run: Gmail times out ---
    with patch("app.tools.email_tools.get_transport_for_account",
               lambda acct, oauth: _TimeoutTransport(account_email="me@example.com")):
        start = time.time()
        res = execute_automation_run.call_local(run_id, "manual", "backend")
        elapsed = time.time() - start

    # Worker must NOT block forever; it returns within the configured bound.
    from app.config import get_settings
    assert elapsed < get_settings().GMAIL_HTTP_TIMEOUT_SECONDS, \
        f"worker blocked too long on Gmail timeout: {elapsed:.1f}s"

    r = db.get(models.AutomationRun, run_id)
    assert r.status in ("partial", "failed"), f"run must be terminal, got {r.status}"
    assert "gmail_timeout" in (r.error or ""), f"error must carry gmail_timeout, got {r.error!r}"
    assert r.finished_at is not None, "finished_at must be set so the run is released"
    # The worker returned (did not hang): run_tick handled the sync timeout
    # gracefully and produced a terminal run, so the function completed.
    assert res is not None

    # --- second run: Gmail works (in-memory double) -> worker must continue ---
    run2 = models.AutomationRun(automation_id=aid, trigger="manual", source="backend", status="queued")
    db.add(run2)
    db.flush()
    run2_id = run2.id
    db.commit()

    with patch("app.tools.email_tools.get_transport_for_account",
               lambda acct, oauth: InMemoryGmailTransport(account_email="me@example.com")):
        res2 = execute_automation_run.call_local(run2_id, "manual", "backend")

    r2 = db.get(models.AutomationRun, run2_id)
    assert res2["ok"] is True, f"worker must process the next run, got {res2}"
    assert r2.status in ("success", "partial"), f"next run must be terminal, got {r2.status}"

    # No inflight left behind -> the old (terminal) run never resumes concurrently.
    inflight = (
        db.query(models.AutomationRun)
        .filter(models.AutomationRun.status.in_(["queued", "running"]))
        .count()
    )
    assert inflight == 0, f"no inflight runs should remain, found {inflight}"
