"""Gmail API error-retry test (429 backoff)."""
import importlib

import app.gmail.client as client_mod
from app.gmail.client import RealGmailTransport
from app.config import get_settings
from googleapiclient.errors import HttpError
import httplib2
from unittest.mock import MagicMock
from app import models


def _make_429():
    resp = httplib2.Response({"status": 429})
    return HttpError(resp, b"rate limited")


def test_retry_on_429_then_success(db):
    # speed up backoff for the test
    client_mod._BACKOFF = 0

    acct = models.GmailAccount(id=1, user_id=1, email="me@example.com", is_connected=True)
    oauth = models.OAuthCredential(gmail_account_id=1, access_token_enc="x", refresh_token_enc="y")
    t = RealGmailTransport(acct, oauth)

    svc = MagicMock()
    svc.users.return_value.getProfile.return_value.execute.side_effect = [
        _make_429(), _make_429(), {"email": "ok@example.com", "historyId": "5"},
    ]
    # prevent _ensure_service from overwriting our mock, but restore it after retry resets
    t._ensure_service = lambda: setattr(t, "_service", svc)
    t._service = svc
    t._creds = MagicMock()

    result = t._call(lambda s: s.users().getProfile(userId="me").execute())
    assert result == {"email": "ok@example.com", "historyId": "5"}
    # it should have been attempted 3 times (2 failures + 1 success)
    assert svc.users.return_value.getProfile.return_value.execute.call_count == 3

    client_mod._BACKOFF = 1.5
