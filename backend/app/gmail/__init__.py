"""Gmail package: transport factory."""
from __future__ import annotations

from ..config import get_settings
from ..security import Cipher
from .client import RealGmailTransport
from .transport import GmailTransport, InMemoryGmailTransport


_TRANSPORT_CACHE: dict[int, GmailTransport] = {}


def _real_credentials_available(settings, oauth_row) -> bool:
    """True when usable encrypted tokens exist for this account.

    An unreadable token is reported as unavailable. The factory fails closed
    unless the caller explicitly enabled the offline test transport.
    """
    if not (settings.GOOGLE_CLIENT_ID and oauth_row is not None and oauth_row.access_token_enc):
        return False
    try:
        return bool(Cipher().decrypt(oauth_row.access_token_enc))
    except Exception:
        return False


def get_transport_for_account(account, oauth_row) -> GmailTransport:
    """Return a real transport if encrypted tokens exist and Gmail is configured.

    Transports are cached per account so Gmail drafts/state persist across calls
    (important for the in-memory demo double and avoids rebuilding the API service).
    """
    settings = get_settings()
    # The Consumer may have cached settings before Desktop OAuth was imported
    # by the Backend. Persisted tokens are the signal to reload configuration.
    if (not getattr(settings, "GOOGLE_CLIENT_ID", None)
            and oauth_row is not None and oauth_row.access_token_enc):
        refresh_settings = getattr(get_settings, "cache_clear", None)
        if refresh_settings is not None:
            refresh_settings()
            settings = get_settings()
    has_real = _real_credentials_available(settings, oauth_row)
    if not has_real and not settings.ALLOW_INMEMORY_GMAIL:
        _TRANSPORT_CACHE.pop(account.id, None)
        raise RuntimeError("gmail_credentials_unavailable: reconnect Gmail before synchronizing")

    cached = _TRANSPORT_CACHE.get(account.id)
    if cached is not None:
        # Guard against a stale demo double. It may have been cached while the
        # account was still disconnected; once real credentials exist it must
        # never keep shadowing the real transport, because the double reports a
        # fake historyId (1000) that the sync layer would persist and later
        # replay as if Gmail had returned it (-> permanent cursor_expired).
        if isinstance(cached, InMemoryGmailTransport) == (not has_real):
            return cached
        _TRANSPORT_CACHE.pop(account.id, None)

    if has_real:
        t: GmailTransport = RealGmailTransport(account, oauth_row)
    else:
        # Explicit offline test/development injection only; never a default.
        t = InMemoryGmailTransport(account_email=account.email or "demo@example.com")
    _TRANSPORT_CACHE[account.id] = t
    return t


def clear_transport_cache(account_id: int | None = None):
    if account_id is None:
        _TRANSPORT_CACHE.clear()
    else:
        _TRANSPORT_CACHE.pop(account_id, None)
