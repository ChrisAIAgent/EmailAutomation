"""Safety + foundation acceptance tests (Round 1 of the real-usage phase).

Covers:
- Policy engine send gating (all 10 spec conditions, incl. global pause).
- Draft-only mode blocks real send at the tool layer.
- Campaign creation round-trips all 13 spec fields.
- CSV import: validation / dedup / suppression / statistics.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.policy.engine import evaluate, PolicyContext
from app import models
from app.config import Settings


def make_campaign(db, **kw):
    status = kw.pop("status", "active")
    c = models.Campaign(owner_id=1, name="C", status=status, **kw)
    db.add(c)
    db.flush()
    return c


def _patch_settings(monkeypatch, allowlist="a@b.com"):
    import app.policy.engine as eng

    monkeypatch.setattr(
        eng, "get_settings",
        lambda: Settings(RESTRICTED_RECIPIENT_ALLOWLIST=allowlist, ENABLE_REAL_SEND=True),
    )
    # Pin "now" to a Tuesday 10:00 UTC so the sending-window/weekend checks are deterministic.
    fixed = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(eng, "_now_utc", lambda: fixed)


def _ctx(**kw):
    base = dict(
        agent="langgraph", mode="langgraph_only", is_primary=True,
        tool_name="send_approved_draft", requires_approval=True,
    )
    base.update(kw)
    return PolicyContext(**base)


# --- 1. campaign must be active ---
def test_send_blocked_when_campaign_not_active(db):
    c = make_campaign(db, status="draft")
    res = evaluate(db, _ctx(campaign_id=c.id, to_email="a@b.com"))
    assert res.allowed is False
    assert "not active" in res.reason


# --- 2. suppression list ---
def test_send_blocked_when_suppressed(db):
    c = make_campaign(db)
    db.add(models.Suppression(owner_id=1, email="sup@x.com", reason="manual", source="t"))
    db.flush()
    res = evaluate(db, _ctx(campaign_id=c.id, to_email="sup@x.com"))
    assert res.allowed is False
    assert "suppression" in res.reason


# --- 3. recipient allowlist ---
def test_send_blocked_when_not_in_allowlist(db, monkeypatch):
    c = make_campaign(db)
    _patch_settings(monkeypatch)
    res = evaluate(db, _ctx(campaign_id=c.id, to_email="any@x.com"))
    assert res.allowed is False
    assert "allowlist" in res.reason


def test_empty_allowlist_does_not_restrict_recipient(db, monkeypatch):
    c = make_campaign(db, timezone="UTC", sending_window_start=0, sending_window_end=24)
    ap = models.Approval(
        kind="first_send",
        campaign_id=c.id,
        to_email="customer@example.com",
        subject="s",
        body_text="b",
        status="pending",
    )
    db.add(ap)
    db.flush()
    _patch_settings(monkeypatch, allowlist="")

    res = evaluate(
        db,
        _ctx(
            campaign_id=c.id,
            to_email="customer@example.com",
            approval_id=ap.id,
        ),
    )

    assert res.allowed is True, res.reason


def test_send_allowed_when_in_allowlist_and_all_conditions_met(db, monkeypatch):
    c = make_campaign(db, timezone="UTC", sending_window_start=0, sending_window_end=24)
    ap = models.Approval(kind="first_send", campaign_id=c.id, to_email="a@b.com", subject="s", body_text="b", status="pending")
    db.add(ap)
    db.flush()
    _patch_settings(monkeypatch)
    res = evaluate(db, _ctx(campaign_id=c.id, to_email="a@b.com", approval_id=ap.id))
    assert res.allowed is True, res.reason


# --- 4. approval required ---
def test_send_blocked_without_approval(db, monkeypatch):
    c = make_campaign(db, timezone="UTC", sending_window_start=0, sending_window_end=24)
    _patch_settings(monkeypatch)
    res = evaluate(db, _ctx(campaign_id=c.id, to_email="a@b.com"))  # no approval_id
    assert res.allowed is False
    assert "approval" in res.reason


# --- 5. idempotency ---
def test_send_blocked_idempotency(db, monkeypatch):
    c = make_campaign(db, timezone="UTC", sending_window_start=0, sending_window_end=24)
    ap = models.Approval(kind="first_send", campaign_id=c.id, to_email="a@b.com", subject="s", body_text="b", status="pending")
    db.add(ap)
    db.add(models.ToolExecution(tool_name="send_approved_draft", idempotency_key="idem1", status="ok"))
    db.flush()
    _patch_settings(monkeypatch)
    res = evaluate(db, _ctx(campaign_id=c.id, to_email="a@b.com", approval_id=ap.id, idempotency_key="idem1"))
    assert res.allowed is False
    assert "idempotency" in res.reason


# --- 6. new human reply cancels send ---
def test_send_blocked_new_reply(db, monkeypatch):
    c = make_campaign(db, timezone="UTC", sending_window_start=0, sending_window_end=24)
    ap = models.Approval(kind="first_send", campaign_id=c.id, to_email="a@b.com", subject="s", body_text="b", status="pending")
    th = models.EmailThread(gmail_account_id=1, gmail_thread_id="t1", has_human_reply=True)
    db.add(ap)
    db.add(th)
    db.flush()
    _patch_settings(monkeypatch)
    res = evaluate(db, _ctx(campaign_id=c.id, to_email="a@b.com", approval_id=ap.id, thread_id=th.id))
    assert res.allowed is False
    assert "reply" in res.reason


# --- 7. daily send limit ---
def test_send_blocked_daily_limit(db, monkeypatch):
    c = make_campaign(db, timezone="UTC", sending_window_start=0, sending_window_end=24, daily_send_limit=1)
    ap = models.Approval(kind="first_send", campaign_id=c.id, to_email="a@b.com", subject="s", body_text="b", status="pending")
    cc = models.CampaignContact(campaign_id=c.id, contact_id=1, status="queued")
    db.add(ap)
    db.add(cc)
    db.flush()
    db.add(models.EmailDraft(gmail_account_id=1, to_email="a@b.com", subject="s", body_text="b", status="sent", campaign_contact_id=cc.id))
    db.flush()
    _patch_settings(monkeypatch)
    res = evaluate(db, _ctx(campaign_id=c.id, to_email="a@b.com", approval_id=ap.id))
    assert res.allowed is False
    assert "daily" in res.reason


# --- 8. sending window ---
def test_send_blocked_out_of_window(db, monkeypatch):
    c = make_campaign(db, timezone="UTC", sending_window_start=9, sending_window_end=9)
    ap = models.Approval(kind="first_send", campaign_id=c.id, to_email="a@b.com", subject="s", body_text="b", status="pending")
    db.add(ap)
    db.flush()
    _patch_settings(monkeypatch)
    res = evaluate(db, _ctx(campaign_id=c.id, to_email="a@b.com", approval_id=ap.id))
    assert res.allowed is False
    assert "window" in res.reason


# --- 9. global pause (spec: 未暂停) ---
def test_send_blocked_global_pause(db, monkeypatch):
    c = make_campaign(db, timezone="UTC", sending_window_start=0, sending_window_end=24)
    ap = models.Approval(kind="first_send", campaign_id=c.id, to_email="a@b.com", subject="s", body_text="b", status="pending")
    db.add(ap)
    db.add(models.SystemFlag(key="global_pause", value="true"))
    db.flush()
    _patch_settings(monkeypatch)
    res = evaluate(db, _ctx(campaign_id=c.id, to_email="a@b.com", approval_id=ap.id))
    assert res.allowed is False
    assert "paused" in res.reason


# --- 10. shadow agent cannot execute writes ---
def test_shadow_cannot_send(db):
    c = make_campaign(db)
    res = evaluate(db, PolicyContext(
        agent="openclaw", mode="compare", is_primary=False,
        tool_name="send_approved_draft", campaign_id=c.id, to_email="a@b.com",
    ))
    assert res.allowed is False
    assert "shadow" in res.reason


# --- tool layer: draft-only blocks real send (no ENABLE_REAL_SEND) ---
def test_email_tools_send_blocked_in_draft_only(db):
    account = models.GmailAccount(user_id=1, email="x@x.com", is_connected=True)
    db.add(account)
    db.flush()
    draft = models.EmailDraft(gmail_account_id=account.id, to_email="a@b.com", subject="s", body_text="b", status="draft")
    db.add(draft)
    db.flush()
    from app.tools.email_tools import UnifiedEmailToolLayer

    tl = UnifiedEmailToolLayer(db, account, None)
    res = tl.send_approved_draft(
        draft_db_id=draft.id, approval_id=1, campaign_id=None,
        idempotency_key="k", requires_approval=True,
    )
    assert res.get("ok") is False
    assert "draft-only" in (res.get("blocked") or "")


# --- foundation: Campaign create round-trips all 13 spec fields ---
def test_campaign_create_all_fields(client):
    payload = {
        "name": "TAC 邮箱自动化测试",
        "objective": "获客",
        "product_description": "邮箱自动化",
        "target_audience": "需要邮箱自动化的客户",
        "sender_name": "Chris",
        "sender_company": "TAC",
        "tone": "professional",
        "primary_agent": "langgraph",
        "agent_mode": "langgraph_only",
        "daily_send_limit": 1,
        "sending_window_start": 9,
        "sending_window_end": 18,
        "timezone": "Asia/Shanghai",
        "max_follow_ups": 2,
        "follow_up_intervals_days": "3,4",
        "approval_mode": "human_confirm",
        "confidence_threshold": 70,
    }
    r = client.post("/api/campaigns", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    for k, v in payload.items():
        assert data[k] == v, (k, data[k], v)
    assert data["status"] == "draft"


# --- foundation: CSV import validation / dedup / suppression / stats ---
def test_csv_import_features(db):
    from app.services import contacts as contact_svc

    c = models.Campaign(owner_id=1, name="C", status="active")
    db.add(c)
    db.add(models.Suppression(owner_id=1, email="suppressed@x.com", reason="manual", source="t"))
    db.add(models.Contact(owner_id=1, email="dup@x.com"))
    db.flush()

    csv_text = (
        "email,first_name,company\n"
        "valid@x.com,Chris,TAC\n"
        "bad-email,Tom,Co\n"
        "valid@x.com,Chris2,TAC2\n"
        "suppressed@x.com,Sup,Co\n"
        "dup@x.com,Dup,Co\n"
        ",NoEmail,Co\n"
    )
    res = contact_svc.import_contacts(
        db, owner_id=1, campaign_id=c.id, csv_text=csv_text, field_map={}, has_header=True,
    )
    assert res.imported == 1, res
    assert res.invalid_email == 2, res          # bad-email + empty
    assert res.duplicates_skipped == 2, res     # valid@x.com (2nd) + dup@x.com (existing)
    assert res.suppressed_skipped == 1, res     # suppressed@x.com
    assert len(res.rejected_rows) == 2, res     # only invalid rows are reported
