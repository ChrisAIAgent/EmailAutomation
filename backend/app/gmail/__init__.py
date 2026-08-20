"""Gmail package: transport factory."""
from __future__ import annotations

from ..config import get_settings
from ..security import Cipher
from .client import RealGmailTransport
from .transport import GmailTransport, InMemoryGmailTransport


_TRANSPORT_CACHE: dict[int, GmailTransport] = {}


def get_transport_for_account(account, oauth_row) -> GmailTransport:
    """Return a real transport if encrypted tokens exist and Gmail is configured.

    Transports are cached per account so Gmail drafts/state persist across calls
    (important for the in-memory demo double and avoids rebuilding the API service).
    """
    if account.id in _TRANSPORT_CACHE:
        return _TRANSPORT_CACHE[account.id]
    settings = get_settings()
    cipher = Cipher()
    if (
        settings.GOOGLE_CLIENT_ID
        and oauth_row is not None
        and oauth_row.access_token_enc
        and cipher.decrypt(oauth_row.access_token_enc)
    ):
        t: GmailTransport = RealGmailTransport(account, oauth_row)
    else:
        # Fallback: in-memory double (clearly "not connected" in UI)
        t = InMemoryGmailTransport(account_email=account.email or "demo@example.com")
    _TRANSPORT_CACHE[account.id] = t
    return t


def clear_transport_cache(account_id: int | None = None):
    if account_id is None:
        _TRANSPORT_CACHE.clear()
    else:
        _TRANSPORT_CACHE.pop(account_id, None)
