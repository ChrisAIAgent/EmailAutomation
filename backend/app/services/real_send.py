"""Derived real-send capability for a connected customer-owned Gmail account.

OAuth connection is the persisted operator authorization for real delivery.  The
capability is deliberately derived at request time so disconnecting Gmail blocks
sends immediately without a second, drift-prone switch or a manual .env edit.
"""
from __future__ import annotations

from ..config import Settings, is_gmail_configured


def is_real_send_enabled(settings: Settings, account=None, oauth=None) -> bool:
    """True only while a configured, connected account has an OAuth credential.

    ``ENABLE_REAL_SEND`` remains a legacy/internal-test override, but is no
    longer the customer-facing activation mechanism.  The tool layer separately
    requires ``RealGmailTransport`` before it can call Gmail, so this override
    can never make an in-memory transport appear sent.
    """
    oauth = oauth if oauth is not None else getattr(account, "oauth", None)
    if settings.ENABLE_REAL_SEND:
        return True
    return bool(
        is_gmail_configured(settings)
        and account is not None
        and getattr(account, "is_connected", False)
        and oauth is not None
        and getattr(oauth, "access_token_enc", None)
    )
