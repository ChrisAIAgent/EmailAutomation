"""Regression test for the 409 'no real Gmail account connected' bug.

Root cause: `_account_for_campaign` did `filter_by(user_id=...).first()` with no
ordering and no `is_connected` filter. When a stale demo account (is_connected=False,
no OAuth) and the real connected account (is_connected=True, with OAuth) both exist
for the same owner, `.first()` returned the demo row -> InMemoryGmailTransport ->
send_approved_draft blocked with 409 even though ENABLE_REAL_SEND=true.

This test pins the corrected behaviour: the connected account with an OAuth
credential must win.
"""
from __future__ import annotations

from app import models
from app.services import approvals


def _make_demo_account(db, user_id=1):
    acct = models.GmailAccount(user_id=user_id, email="demo@unconfigured.local", is_connected=False)
    db.add(acct)
    db.flush()
    return acct


def _make_connected_account(db, user_id=1, email="real@gmail.com"):
    acct = models.GmailAccount(user_id=user_id, email=email, is_connected=True)
    db.add(acct)
    db.flush()
    db.add(models.OAuthCredential(gmail_account_id=acct.id, access_token_enc="enc", refresh_token_enc="enc"))
    db.flush()
    return acct


def _make_campaign(db, user_id=1):
    c = models.Campaign(owner_id=user_id, name="C", status="active")
    db.add(c)
    db.flush()
    return c


def test_account_for_campaign_prefers_connected_with_oauth(db):
    # Reproduce the live state: demo account inserted first, connected account second.
    demo = _make_demo_account(db)
    real = _make_connected_account(db)
    campaign = _make_campaign(db)

    account, oauth = approvals._account_for_campaign(db, campaign)

    assert account.id == real.id
    assert account.is_connected is True
    assert oauth is not None
    assert account.id != demo.id


def test_account_for_campaign_falls_back_to_demo_when_no_real(db):
    # No connected account at all -> returns the demo row, oauth None (draft-only ok).
    demo = _make_demo_account(db)
    campaign = _make_campaign(db)

    account, oauth = approvals._account_for_campaign(db, campaign)

    assert account.id == demo.id
    assert oauth is None


def test_account_for_campaign_provisions_demo_when_none(db):
    # No account at all -> provisions a demo (unconnected) account.
    campaign = _make_campaign(db)
    before = db.query(models.GmailAccount).count()

    account, oauth = approvals._account_for_campaign(db, campaign)

    assert account.is_connected is False
    assert oauth is None
    assert db.query(models.GmailAccount).count() == before + 1
