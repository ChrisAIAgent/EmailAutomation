"""Real Gmail transport via google-api-python-client with retries + token refresh."""
from __future__ import annotations

import base64
import logging
import socket
import ssl
import time
from typing import Optional

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..config import get_settings
from ..security import Cipher
from .auth import build_credentials, maybe_refresh
from .transport import (
    GmailTimeoutError,
    GmailTransientNetworkError,
    GmailTransport,
    MessageDTO,
    ThreadDTO,
    _looks_corrupt_text,
    build_mime,
    parse_gmail_raw_message,
)

logger = logging.getLogger("gmail.client")

# 429 / 5xx retry config
_MAX_RETRIES = 4
_BACKOFF = 1.5

# Network exceptions that mean "the request did not return in time" rather than
# a retriable server error. These must NOT be retried forever; they surface as a
# recognizable GmailTimeoutError so the worker can fail the run and move on.
_TIMEOUT_EXC = (socket.timeout, TimeoutError)


def _is_transient_transport_error(exc: Exception) -> bool:
    """Bounded retry eligibility for interrupted TLS/proxy connections.

    Do not retry certificate validation or OAuth errors.  EOF/reset failures
    occur before Gmail returns an application response and are safe to replay.
    """
    message = str(exc).lower()
    if "certificate verify" in message or "certificat" in message and "verify" in message:
        return False
    if isinstance(exc, (ssl.SSLError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return True
    return any(marker in message for marker in (
        "unexpected_eof_while_reading",
        "eof occurred in violation of protocol",
        "connection reset",
        "connection aborted",
        "remote end closed connection",
        "remote disconnected",
    ))


class RealGmailTransport(GmailTransport):
    def __init__(self, account, oauth_row):
        # Capture PRIMITIVES only -- never hold ORM instances across requests.
        # get_transport_for_account() caches this transport per account.id, so the
        # SAME instance is reused across many request sessions. The original ORM
        # objects belong to the request session that first built the cache entry;
        # once that session closes they become detached, and SQLAlchemy 2.0's
        # expire_on_commit then fails to refresh them with
        # "is not bound to a Session; attribute refresh operation cannot proceed".
        # Holding only primitive values sidesteps the entire class of bug; the
        # OAuth row is re-loaded from a fresh session whenever tokens are needed.
        self._account_email = getattr(account, "email", "")
        self._oauth_id = getattr(oauth_row, "id", None)
        self._oauth_snapshot = oauth_row
        self._cipher = Cipher()
        self._service = None
        self._creds = None
        self._timeout = get_settings().GMAIL_HTTP_TIMEOUT_SECONDS

    # --- credential management ---
    def _load_oauth(self):
        """Re-load the OAuth credential from a FRESH session.

        The transport is cached globally and may outlive the request session that
        created it, so we must never touch the original ORM instance. Reading by
        primary key from a short-lived session is safe and avoids detached-instance
        errors, including the GmailAccount refresh failure that this fix resolves.
        """
        from ..db import SessionLocal
        from .. import models

        if self._oauth_id is None:
            return self._oauth_snapshot
        db = SessionLocal()
        try:
            return db.get(models.OAuthCredential, self._oauth_id)
        finally:
            db.close()

    def _ensure_service(self):
        if self._service is not None:
            return
        oauth = self._load_oauth()
        access = self._cipher.decrypt(oauth.access_token_enc)
        refresh = self._cipher.decrypt(oauth.refresh_token_enc)
        expiry = oauth.token_expiry.timestamp() if oauth.token_expiry else 0.0
        creds = build_credentials(access, refresh, expiry)
        try:
            if maybe_refresh(creds, timeout=self._timeout):
                self._persist_refreshed(creds)
        except _TIMEOUT_EXC as e:
            # Token refresh network timeout -> recognizable, do not wedge.
            raise GmailTimeoutError(f"gmail_timeout: token_refresh {type(e).__name__}: {e}") from e
        except Exception as e:
            if _is_timeout(e):
                raise GmailTimeoutError(f"gmail_timeout: token_refresh {type(e).__name__}: {e}") from e
            raise
        self._creds = creds
        # Explicit socket timeout on the underlying httplib2 transport. Every
        # Gmail API call AND the on-demand token refresh (AuthorizedHttp reuses
        # this same httplib2) inherit this timeout, so neither can block forever.
        # We pass `http=` (NOT `credentials=`) so we never give build() two
        # mutually-exclusive auth arguments.
        http = httplib2.Http(timeout=self._timeout)
        auth_http = AuthorizedHttp(creds, http=http)
        self._service = build("gmail", "v1", http=auth_http, cache_discovery=False)

    def _persist_refreshed(self, creds):
        from ..db import SessionLocal
        from .. import models

        db = SessionLocal()
        try:
            row = db.get(models.OAuthCredential, self._oauth_id)
            if row:
                row.access_token_enc = self._cipher.encrypt(creds.token)
                if creds.expiry:
                    row.token_expiry = creds.expiry.replace(tzinfo=__import__("datetime").timezone.utc)
                db.commit()
        finally:
            db.close()

    def _call(self, fn):
        last = None
        last_transient = None
        for attempt in range(_MAX_RETRIES):
            try:
                self._ensure_service()
                return fn(self._service)
            except HttpError as e:
                if e.status_code in (429, 500, 502, 503, 504):
                    last = e
                    time.sleep(_BACKOFF * (attempt + 1))
                    self._service = None
                    continue
                raise
            except GmailTimeoutError:
                # Already a recognizable timeout signal -- propagate immediately.
                raise
            except _TIMEOUT_EXC as e:
                # Hard network timeout: do NOT retry forever. Surface a
                # recognizable gmail_timeout so the worker can fail the run and
                # move on to the next task.
                raise GmailTimeoutError(f"gmail_timeout: {type(e).__name__}: {e}") from e
            except Exception as e:  # network / token
                if "expired" in str(e).lower():
                    self._service = None
                    try:
                        self._ensure_service()
                        continue
                    except Exception as e2:
                        raise e2
                if _is_timeout(e):
                    raise GmailTimeoutError(f"gmail_timeout: {type(e).__name__}: {e}") from e
                if _is_transient_transport_error(e):
                    last_transient = e
                    # Keep logs credential-free: the exception class and fixed
                    # category are sufficient for diagnostics.
                    logger.warning(
                        "gmail_transient_transport_error category=tls_or_connection "
                        "attempt=%s/%s type=%s",
                        attempt + 1, _MAX_RETRIES, type(e).__name__,
                    )
                    self._service = None
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_BACKOFF * (attempt + 1))
                        continue
                    break
                raise
        if last_transient is not None:
            raise GmailTransientNetworkError(
                f"gmail_transport_retry_exhausted: {type(last_transient).__name__}"
            ) from last_transient
        raise last or RuntimeError("Gmail API retry exhausted")

    # --- interface ---
    def get_profile(self) -> dict:
        p = self._call(lambda s: s.users().getProfile(userId="me").execute())
        return p

    def list_threads(self, query, max_results=20, page_token=None, include_spam_trash=False):
        req = {"userId": "me", "q": query, "maxResults": max_results}
        if include_spam_trash:
            req["includeSpamTrash"] = True
        if page_token:
            req["pageToken"] = page_token
        res = self._call(lambda s: s.users().threads().list(**req).execute())
        threads = []
        for t in res.get("threads", []):
            try:
                threads.append(self.get_thread(t["id"]))
            except (GmailTimeoutError, GmailTransientNetworkError):
                # A single thread fetch exceeded the network timeout. The whole
                # list op is wedged; re-raise immediately so we do NOT keep
                # fetching the remaining threads (each would wait up to
                # GMAIL_HTTP_TIMEOUT_SECONDS and ~50 of them could block for
                # ~50x that long). This is a recognizable, fatal signal -- the
                # worker must fail the run and move on, not serially time out.
                raise
            except Exception:
                # Other per-thread errors (e.g. a malformed thread) are
                # non-fatal; skip the bad thread and continue with the rest.
                continue
        return threads, res.get("nextPageToken")

    def get_thread(self, thread_id) -> ThreadDTO:
        res = self._call(lambda s: s.users().threads().get(userId="me", id=thread_id, format="full").execute())
        msgs = [MessageDTO(*([None]*0)) for _ in []]  # placeholder
        from .transport import parse_gmail_message
        msgs = []
        for message in res.get("messages", []):
            parsed = parse_gmail_message(message, owner_email=self._account_email)
            if _looks_corrupt_text(parsed.subject) or _looks_corrupt_text(parsed.body_text):
                raw = self._call(
                    lambda s, message_id=message["id"]: s.users().messages().get(
                        userId="me", id=message_id, format="raw"
                    ).execute()
                )
                parsed = parse_gmail_raw_message(raw, owner_email=self._account_email)
            msgs.append(parsed)
        return ThreadDTO(
            gmail_thread_id=res["id"],
            history_id=res.get("historyId"),
            subject=msgs[-1].subject if msgs else None,
            snippet=res.get("snippet"),
            messages=msgs,
        )

    def get_message(self, message_id) -> MessageDTO:
        from .transport import parse_gmail_message
        res = self._call(lambda s: s.users().messages().get(userId="me", id=message_id, format="full").execute())
        parsed = parse_gmail_message(res, owner_email=self._account_email)
        if _looks_corrupt_text(parsed.subject) or _looks_corrupt_text(parsed.body_text):
            raw = self._call(
                lambda s: s.users().messages().get(
                    userId="me", id=message_id, format="raw"
                ).execute()
            )
            return parse_gmail_raw_message(raw, owner_email=self._account_email)
        return parsed

    def create_draft(self, to, subject, body_text, body_html, thread_id=None, in_reply_to=None, references=None) -> dict:
        raw = build_mime(to, subject, body_text, body_html, thread_id, in_reply_to, references)
        body = {"message": {"raw": raw}}
        if thread_id:
            body["message"]["threadId"] = thread_id
        res = self._call(lambda s: s.users().drafts().create(userId="me", body=body).execute())
        return res

    def update_draft(self, draft_id, to, subject, body_text, body_html, thread_id=None,
                     in_reply_to=None, references=None) -> dict:
        raw = build_mime(to, subject, body_text, body_html, thread_id, in_reply_to, references)
        body = {"id": draft_id, "message": {"raw": raw}}
        if thread_id:
            body["message"]["threadId"] = thread_id
        return self._call(lambda s: s.users().drafts().update(userId="me", body=body).execute())

    def send_draft(self, draft_id) -> dict:
        res = self._call(lambda s: s.users().drafts().send(userId="me", body={"id": draft_id}).execute())
        return res

    def add_label(self, thread_id, label):
        self._call(lambda s: s.users().threads().modify(userId="me", id=thread_id, body={"addLabelIds": [label]}).execute())

    def remove_label(self, thread_id, label):
        self._call(lambda s: s.users().threads().modify(userId="me", id=thread_id, body={"removeLabelIds": [label]}).execute())

    def archive_thread(self, thread_id):
        self._call(lambda s: s.users().threads().modify(userId="me", id=thread_id, body={"removeLabelIds": ["INBOX"]}).execute())

    def list_history(self, start_history_id, label_id=None, page_token=None):
        req = {"userId": "me", "startHistoryId": start_history_id, "maxResults": 100}
        if label_id:
            req["labelId"] = label_id
        if page_token:
            req["pageToken"] = page_token
        res = self._call(lambda s: s.users().history().list(**req).execute())
        events = res.get("history", [])
        nxt = res.get("nextPageToken")
        new_hist = res.get("historyId")
        return events, (new_hist or start_history_id), nxt


def _is_timeout(e: BaseException) -> bool:
    """True for network timeouts that httplib2/google-auth wrap as a generic error."""
    msg = str(e).lower()
    return "timed out" in msg or "timeout" in msg or "read timed out" in msg
