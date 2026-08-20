"""Single source of truth for resolving the Gmail account used to send/sync.

All outbound paths (campaign send, follow-up, inbox reply, automation run-tick)
and the Gmail status/sync endpoints MUST route through :func:`resolve_sending_account`
so that "the account shown to the user" is always "the account that actually
sends". Previously each call site queried ``GmailAccount`` with different filters
and ordering (ASC vs DESC, with/without OAuth join, with/without ``is_connected``),
which let a stale unconfigured placeholder row or the wrong connected account win under multi-account
state.

The resolver never returns a *non-connected* thread account for a real send —
the policy layer still blocks real sends when no OAuth credential is present.
"""
from __future__ import annotations

from .. import models


def _owner_id_from(campaign=None, thread=None, owner_id=None) -> int | None:
    if owner_id:
        return owner_id
    if campaign is not None:
        return campaign.owner_id
    return None


def resolve_sending_account(db, *, owner_id=None, campaign=None, thread=None,
                            provision: bool = True):
    """Return ``(account, oauth)`` for an outbound action.

    Priority:
      1. ``thread``'s own account **if it is connected** (inbox replies go back
         via the same account that owns the thread);
      2. owner's connected account that has an OAuth credential (``id`` ASC);
      3. owner's any connected account (``id`` ASC);
      4. owner's any account row (draft-only fallback, ``id`` ASC);
      5. when ``provision=True``: create an unconfigured placeholder account
         ``demo@unconfigured.local``
         (``is_connected=False``) account so draft-only mode still works.

    When ``provision=False`` (used by read-only status/sync probes) and nothing
    is found, returns ``(None, None)`` without writing to the DB.
    """
    owner = _owner_id_from(campaign=campaign, thread=thread, owner_id=owner_id)

    # 1. thread's own connected account (inbox reply path).
    if thread is not None and thread.gmail_account_id:
        acc = db.get(models.GmailAccount, thread.gmail_account_id)
        if acc and acc.is_connected:
            return acc, acc.oauth

    # 2. owner's connected account WITH an OAuth credential (id ASC).
    q = db.query(models.GmailAccount)
    if owner is not None:
        q = q.filter(models.GmailAccount.user_id == owner)
    acc = (
        q.join(
            models.OAuthCredential,
            models.GmailAccount.id == models.OAuthCredential.gmail_account_id,
        )
        .filter(models.GmailAccount.is_connected == True)  # noqa: E712
        .order_by(models.GmailAccount.id.asc())
        .first()
    )
    if acc:
        return acc, acc.oauth

    # 3. owner's any connected account (no OAuth row — rare/stale).
    q = db.query(models.GmailAccount).filter(
        models.GmailAccount.is_connected == True  # noqa: E712
    )
    if owner is not None:
        q = q.filter(models.GmailAccount.user_id == owner)
    acc = q.order_by(models.GmailAccount.id.asc()).first()
    if acc:
        return acc, acc.oauth

    # 4. owner's any account row (draft-only fallback).
    q = db.query(models.GmailAccount)
    if owner is not None:
        q = q.filter(models.GmailAccount.user_id == owner)
    acc = q.order_by(models.GmailAccount.id.asc()).first()
    if acc:
        return acc, acc.oauth

    # 5. provision a demo (unconnected) account — only when explicitly allowed.
    if provision:
        acc = models.GmailAccount(
            user_id=owner,
            email="demo@unconfigured.local",
            is_connected=False,
        )
        db.add(acc)
        db.flush()
        return acc, None

    return None, None
