"""Tests for the unified account resolver (P0-1).

Pins the behaviour that every outbound path now shares: a connected account
with an OAuth credential wins over a stale demo row; a disconnected thread
account does NOT silently win for inbox replies; read-only probes never
provision a demo row.
"""
from __future__ import annotations

from app import models
from app.services.accounts import resolve_sending_account
from app.services import approvals


def _demo_account(db, user_id=1):
    acct = models.GmailAccount(user_id=user_id, email="demo@unconfigured.local", is_connected=False)
    db.add(acct)
    db.flush()
    return acct


def _connected_account(db, user_id=1, email="real@gmail.com"):
    acct = models.GmailAccount(user_id=user_id, email=email, is_connected=True)
    db.add(acct)
    db.flush()
    db.add(models.OAuthCredential(gmail_account_id=acct.id, access_token_enc="enc", refresh_token_enc="enc"))
    db.flush()
    return acct


def _campaign(db, user_id=1):
    c = models.Campaign(owner_id=user_id, name="C", status="active")
    db.add(c)
    db.flush()
    return c


def test_resolver_prefers_connected_with_oauth_over_demo(db):
    demo = _demo_account(db)
    real = _connected_account(db)
    campaign = _campaign(db)

    account, oauth = resolve_sending_account(db, campaign=campaign)

    assert account.id == real.id
    assert account.is_connected is True
    assert oauth is not None
    assert account.id != demo.id


def test_resolver_provisions_demo_only_when_allowed(db):
    campaign = _campaign(db)
    before = db.query(models.GmailAccount).count()

    account, oauth = resolve_sending_account(db, campaign=campaign, provision=True)
    assert account.is_connected is False
    assert oauth is None
    assert db.query(models.GmailAccount).count() == before + 1


def test_resolver_does_not_provision_on_readonly_probe(db):
    campaign = _campaign(db)
    before = db.query(models.GmailAccount).count()

    account, oauth = resolve_sending_account(db, campaign=campaign, provision=False)
    assert account is None
    assert oauth is None
    assert db.query(models.GmailAccount).count() == before  # no demo written


def test_resolver_uses_thread_connected_account(db):
    real = _connected_account(db)
    thread = models.EmailThread(
        gmail_account_id=real.id, gmail_thread_id="gtid-1",
        subject="x", has_human_reply=False,
    )
    db.add(thread)
    db.flush()

    account, oauth = resolve_sending_account(db, thread=thread, owner_id=1)
    assert account.id == real.id
    assert oauth is not None


def test_resolver_skips_disconnected_thread_account(db):
    # Thread points at a disconnected demo account; resolver must NOT return it
    # for a real send — it must fall back to the owner's connected account.
    demo = _demo_account(db)
    real = _connected_account(db, email="other@gmail.com")
    thread = models.EmailThread(
        gmail_account_id=demo.id, gmail_thread_id="gtid-2",
        subject="x", has_human_reply=False,
    )
    db.add(thread)
    db.flush()

    account, oauth = resolve_sending_account(db, thread=thread, owner_id=1)
    assert account.id == real.id
    assert account.is_connected is True


def test_account_for_thread_delegation_fixes_disconnected_thread(db):
    # Regression for the old _account_for_thread which returned the thread's own
    # account without checking is_connected.
    demo = _demo_account(db)
    real = _connected_account(db, email="other@gmail.com")
    thread = models.EmailThread(
        gmail_account_id=demo.id, gmail_thread_id="gtid-3",
        subject="x", has_human_reply=False,
    )
    db.add(thread)
    db.flush()

    account, oauth = approvals._account_for_thread(db, thread)
    assert account.id == real.id
    assert account.is_connected is True
