"""Send safety: draft-only, allowlist, idempotency, new-reply cancel, daily limit.

Policy gating (allowlist / limit / idempotency / reply-cancel) is unit-tested
against the Policy Engine directly. The "draft-only" and "no real Gmail account"
rules are integration-tested through the tool layer (which is where they live).
"""
import os
from datetime import datetime, timezone

from app.config import get_settings
from app.policy.engine import PolicyContext, evaluate
from app.tools.email_tools import UnifiedEmailToolLayer
from app import models


def _setenv(**kw):
    for k, v in kw.items():
        os.environ[k] = v
    get_settings.cache_clear()


def _setup(db):
    acct = models.GmailAccount(id=1, user_id=1, email="me@example.com", is_connected=True)
    db.add(acct)
    camp = models.Campaign(owner_id=1, name="C", status="active", daily_send_limit=5,
                           sending_window_start=0, sending_window_end=23, timezone="UTC",
                           max_follow_ups=2, follow_up_intervals_days="1,1", agent_mode="langgraph_only")
    db.add(camp)
    db.commit()
    contact = models.Contact(owner_id=1, email="lead@example.com", status="new")
    db.add(contact)
    db.commit()
    cc = models.CampaignContact(campaign_id=camp.id, contact_id=contact.id, status="queued")
    db.add(cc)
    db.commit()
    return acct, camp, contact, cc


# ---- integration: master safety switches live in the tool layer ----

def test_draft_only_blocks_send(db):
    _setenv(ENABLE_REAL_SEND="false", RESTRICTED_RECIPIENT_ALLOWLIST="lead@example.com")
    acct, camp, contact, cc = _setup(db)
    tl = UnifiedEmailToolLayer(db, acct, None)
    res = tl.create_draft(to=contact.email, subject="Hi", body_text="Body", agent="langgraph",
                          is_primary=True, campaign_id=camp.id, campaign_contact_id=cc.id, kind="outreach")
    draft = res["draft"]
    ap = models.Approval(kind="first_send", campaign_id=camp.id, campaign_contact_id=cc.id,
                         draft_id=draft.id, to_email=contact.email, subject="Hi", body_text="Body",
                         idempotency_key="send:" + str(draft.id), status="approved",
                         decided_at=datetime.now(timezone.utc))
    db.add(ap)
    db.commit()
    res = tl.send_approved_draft(draft_db_id=draft.id, approval_id=ap.id, agent="langgraph",
                                 is_primary=True, campaign_id=camp.id, idempotency_key=ap.idempotency_key,
                                 requires_approval=True)
    assert res["ok"] is False
    assert "draft-only" in res["blocked"]


def test_real_send_blocked_without_gmail(db):
    """ENABLE_REAL_SEND=true but no REAL Gmail account connected => honest block
    (must NOT silently deliver to the in-memory dev double)."""
    _setenv(ENABLE_REAL_SEND="true", RESTRICTED_RECIPIENT_ALLOWLIST="lead@example.com")
    acct, camp, contact, cc = _setup(db)
    tl = UnifiedEmailToolLayer(db, acct, None)  # no oauth -> in-memory transport
    res = tl.create_draft(to=contact.email, subject="Hi", body_text="Body", agent="langgraph",
                          is_primary=True, campaign_id=camp.id, campaign_contact_id=cc.id, kind="outreach")
    draft = res["draft"]
    ap = models.Approval(kind="first_send", campaign_id=camp.id, campaign_contact_id=cc.id,
                         draft_id=draft.id, to_email=contact.email, subject="Hi", body_text="Body",
                         idempotency_key="send:" + str(draft.id), status="approved",
                         decided_at=datetime.now(timezone.utc))
    db.add(ap)
    db.commit()
    res = tl.send_approved_draft(draft_db_id=draft.id, approval_id=ap.id, agent="langgraph",
                                 is_primary=True, campaign_id=camp.id, idempotency_key=ap.idempotency_key,
                                 requires_approval=True)
    assert res["ok"] is False
    assert "Gmail" in res["blocked"]


# ---- unit: policy engine ----
# These assert the gating rules directly, independent of the Gmail transport.

def test_allowlist_blocks_send(db):
    _setenv(ENABLE_REAL_SEND="true", RESTRICTED_RECIPIENT_ALLOWLIST="other@example.com")
    ctx = PolicyContext(agent="langgraph", mode="langgraph_only", is_primary=True,
                        tool_name="send_approved_draft", to_email="lead@example.com")
    res = evaluate(db, ctx)
    assert res.allowed is False
    assert "allowlist" in res.reason


def test_idempotent_send(db):
    _setenv(ENABLE_REAL_SEND="true", RESTRICTED_RECIPIENT_ALLOWLIST="lead@example.com")
    db.add(models.ToolExecution(idempotency_key="send:1", status="ok",
                                tool_name="send_approved_draft", gmail_account_id=1, agent="langgraph"))
    ap = models.Approval(id=1, kind="first_send", campaign_id=None, to_email="lead@example.com",
                         subject="Hi", body_text="B", idempotency_key="send:1", status="approved",
                         decided_at=datetime.now(timezone.utc))
    db.add(ap)
    db.commit()
    ctx = PolicyContext(agent="langgraph", mode="langgraph_only", is_primary=True,
                        tool_name="send_approved_draft", to_email="lead@example.com",
                        approval_id=1, idempotency_key="send:1")
    res = evaluate(db, ctx)
    assert res.allowed is False
    assert "idempotency" in res.reason


def test_new_reply_cancels_send(db):
    _setenv(ENABLE_REAL_SEND="true", RESTRICTED_RECIPIENT_ALLOWLIST="lead@example.com")
    acct, camp, contact, cc = _setup(db)
    th = models.EmailThread(gmail_account_id=acct.id, gmail_thread_id="t1",
                            contact_email=contact.email, campaign_id=camp.id, has_human_reply=True)
    db.add(th)
    ap = models.Approval(id=1, kind="first_send", campaign_id=camp.id, campaign_contact_id=cc.id,
                         to_email=contact.email, subject="Hi", body_text="B",
                         idempotency_key="send:x", status="approved", decided_at=datetime.now(timezone.utc))
    db.add(ap)
    db.commit()
    ctx = PolicyContext(agent="langgraph", mode="langgraph_only", is_primary=True,
                        tool_name="send_approved_draft", to_email=contact.email,
                        campaign_id=camp.id, thread_id=th.id, approval_id=1, idempotency_key="send:x")
    res = evaluate(db, ctx)
    assert res.allowed is False
    assert "human reply" in res.reason


def test_daily_send_limit(db):
    _setenv(ENABLE_REAL_SEND="true", RESTRICTED_RECIPIENT_ALLOWLIST="lead@example.com")
    acct, camp, contact, cc = _setup(db)
    camp.daily_send_limit = 1
    db.commit()
    # mark one already-sent draft for this campaign today
    sent = models.EmailDraft(gmail_account_id=acct.id, campaign_contact_id=cc.id,
                             to_email=contact.email, subject="Old", body_text="x",
                             kind="outreach", status="sent", updated_at=datetime.now(timezone.utc))
    db.add(sent)
    ap = models.Approval(id=1, kind="first_send", campaign_id=camp.id, campaign_contact_id=cc.id,
                         to_email=contact.email, subject="Hi", body_text="B",
                         idempotency_key="send:y", status="approved", decided_at=datetime.now(timezone.utc))
    db.add(ap)
    db.commit()
    ctx = PolicyContext(agent="langgraph", mode="langgraph_only", is_primary=True,
                        tool_name="send_approved_draft", to_email="lead@example.com",
                        campaign_id=camp.id, approval_id=1, idempotency_key="send:y")
    res = evaluate(db, ctx)
    assert res.allowed is False
    assert "daily send limit" in res.reason


def test_reply_send_clears_needs_reply_state(db, monkeypatch):
    """改动 2: a sent reply (kind=reply) must leave the contact in
    waiting_for_customer / awaiting_reply, not stuck in needs_reply."""
    from app.gmail.client import RealGmailTransport
    from app.policy import engine as policy_engine

    class FakeRealTransport(RealGmailTransport):
        def __init__(self):
            pass

        def create_draft(self, to, subject, body_text, body_html="", thread_gmail_id=None,
                         in_reply_to=None, references=None):
            return {"id": "draft-1"}

        def send_draft(self, gmail_draft_id):
            return {"id": "sent-msg-1"}

    _setenv(ENABLE_REAL_SEND="true", RESTRICTED_RECIPIENT_ALLOWLIST="lead@example.com")
    # Keep this state-transition test independent of the real calendar. The
    # sending-window rule has dedicated policy tests; this test verifies only
    # the post-send reply transition below.
    monkeypatch.setattr(policy_engine, "_now_utc", lambda: datetime(2026, 8, 31, 12, tzinfo=timezone.utc))
    acct, camp, contact, cc = _setup(db)
    contact.lifecycle_stage = "needs_reply"
    contact.next_action = "reply"
    db.commit()
    tl = UnifiedEmailToolLayer(db, acct, None)
    monkeypatch.setattr(tl, "_transport", lambda: FakeRealTransport())
    res = tl.create_draft(to=contact.email, subject="Re: Hi", body_text="Body", agent="langgraph",
                          is_primary=True, campaign_id=camp.id, campaign_contact_id=cc.id, kind="reply")
    draft = res["draft"]
    ap = models.Approval(kind="reply", campaign_id=camp.id, campaign_contact_id=cc.id,
                         draft_id=draft.id, to_email=contact.email, subject="Re: Hi", body_text="Body",
                         idempotency_key="send:" + str(draft.id), status="approved",
                         decided_at=datetime.now(timezone.utc))
    db.add(ap)
    db.commit()
    out = tl.send_approved_draft(draft_db_id=draft.id, approval_id=ap.id, agent="langgraph",
                                 is_primary=True, campaign_id=camp.id, thread_db_id=1,
                                 idempotency_key=ap.idempotency_key, requires_approval=True)
    assert out["ok"] is True
    db.refresh(contact)
    assert contact.next_action == "waiting_for_customer"
    assert contact.lifecycle_stage == "awaiting_reply"
