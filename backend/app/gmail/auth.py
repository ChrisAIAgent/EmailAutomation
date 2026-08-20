"""Google OAuth 2.0 Authorization Code flow with CSRF state + refresh tokens."""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from ..config import Settings, get_settings

# Minimal scopes requested incrementally.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",  # drafts
    "https://www.googleapis.com/auth/gmail.send",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

# In-memory OAuth transaction store (demo). Each state keeps its PKCE verifier
# and expires after 10 minutes.
_STATE_STORE: dict[str, tuple[float, Optional[str]]] = {}


@dataclass
class OAuthResult:
    access_token: str
    refresh_token: Optional[str]
    token_expiry: float
    scopes: list[str]


def _client_config(settings: Settings) -> dict:
    return {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def generate_state() -> str:
    s = secrets.token_urlsafe(24)
    _STATE_STORE[s] = (time.time() + 600, None)
    return s


def verify_state(state: str) -> bool:
    transaction = _STATE_STORE.pop(state, None)
    if not transaction:
        return False
    return transaction[0] > time.time()


def _consume_transaction(state: str) -> Optional[str]:
    transaction = _STATE_STORE.pop(state, None)
    if not transaction or transaction[0] <= time.time():
        raise ValueError("Invalid or expired OAuth state (CSRF protection)")
    return transaction[1]


def build_authorization_url(state: str, settings: Optional[Settings] = None) -> str:
    settings = settings or get_settings()
    flow = Flow.from_client_config(
        _client_config(settings), scopes=SCOPES, state=state,
        autogenerate_code_verifier=True,
    )
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    url = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )[0]
    transaction = _STATE_STORE.get(state)
    if transaction:
        _STATE_STORE[state] = (transaction[0], flow.code_verifier)
    return url


def exchange_code(code: str, state: str, settings: Optional[Settings] = None) -> OAuthResult:
    settings = settings or get_settings()
    code_verifier = _consume_transaction(state)
    flow = Flow.from_client_config(_client_config(settings), scopes=SCOPES, state=state)
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    c: Credentials = flow.credentials
    return OAuthResult(
        access_token=c.token,
        refresh_token=c.refresh_token,
        token_expiry=c.expiry.timestamp() if c.expiry else 0.0,
        scopes=list(c.scopes or SCOPES),
    )


def build_credentials(
    access_token: str, refresh_token: Optional[str], token_expiry: float, settings: Optional[Settings] = None
) -> Credentials:
    settings = settings or get_settings()
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )


class _TimeoutSession(requests.Session):
    """A requests.Session that applies a default connect/read timeout to every
    request, so the OAuth token-refresh POST cannot block forever when Google's
    token endpoint is unreachable or slow.
    """

    def __init__(self, timeout: int):
        super().__init__()
        self._timeout = timeout

    def request(self, method, url, **kwargs):  # type: ignore[override]
        kwargs.setdefault("timeout", self._timeout)
        return super().request(method, url, **kwargs)


def maybe_refresh(creds: Credentials, timeout: Optional[int] = None) -> bool:
    """Refresh access token if expired. Returns True if refreshed.

    When ``timeout`` (seconds) is given, the refresh POST uses a session with a
    default timeout so a slow/unreachable token endpoint raises instead of
    blocking the worker thread indefinitely.
    """
    if creds.expired and creds.refresh_token:
        if timeout:
            creds.refresh(Request(session=_TimeoutSession(timeout)))
        else:
            creds.refresh(Request())
        return True
    return False
