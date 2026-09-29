"""Follow-up state machine + intent-driven stop."""
import json
from datetime import datetime, timedelta, timezone

from app.tools.email_tools import UnifiedEmailToolLayer
from app.services import followup as followup_svc
from app.services import approvals as approval_svc
from app.agents.orchestrator import Orchestrator
from app import models


def _setup(db):
    acct = models.GmailAccount(id=1, user_id=1, email="me@example.com", is_connected=True)
    db.add(acct)
    camp = models.Campaign(owner_id=1, name="C", status="active", daily_send_limit=5,
                           sending_window_start=0, sending_window_end=23, timezone="UTC",
                           max_follow_ups=2, follow_up_intervals_days="1,1", agent_mode="langgraph_only")
    db.add(camp)
    db.commit()
    contact = models.Contact(owner_id=1, email="lead@example.com", status="contacted")
    db.add(contact)
    db.commit()
    cc = models.CampaignContact(campaign_id=camp.id, contact_id=contact.id, status="sent", assigned_follow_ups=0)
    db.add(cc)
    db.commit()
    return acct, camp, contact, cc


def test_schedule_and_process_follow_up(db):
    acct, camp, contact, cc = _setup(db)
    thread = models.EmailThread(
        gmail_account_id=acct.id, gmail_thread_id="followup-thread",
        contact_email=contact.email, campaign_id=camp.id, subject="Original subject",
    )
    db.add(thread)
    db.flush()
    db.add(models.EmailMessage(
        thread_id=thread.id, gmail_message_id="followup-parent",
        message_id_header="<followup-parent@example.com>",
        from_email=acct.email, to_email=contact.email, subject="Original subject",
        body_text="Original outreach", is_incoming=False,
        received_at=datetime.now(timezone.utc) - timedelta(days=2),
    ))
    cc.thread_id = thread.id
    db.commit()
    # next follow-up scheduled (sequence 1 after initial outreach)
    r = followup_svc.schedule_next_follow_up(db, cc, agent="langgraph", mode="langgraph_only", is_primary=True)
    assert r["ok"] is True
    task = r["task"]
    assert task.status == "scheduled"
    # make it due
    task.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    orch = Orchestrator(db)
    outcomes = followup_svc.process_due_follow_ups(db, orch)
    db.commit()
    assert any(o.get("ok") for o in outcomes)
    # an approval for the follow-up must now exist
    ap = db.query(models.Approval).filter_by(kind="follow_up", campaign_contact_id=cc.id).first()
    assert ap is not None
    # task marked done
    assert task.status == "done"


def test_follow_up_cancelled_on_human_reply(db):
    acct, camp, contact, cc = _setup(db)
    r = followup_svc.schedule_next_follow_up(db, cc)
    task = r["task"]
    task.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    th = models.EmailThread(gmail_account_id=acct.id, gmail_thread_id="t1", contact_email=contact.email,
                            campaign_id=camp.id, has_human_reply=True)
    db.add(th)
    db.commit()
    cc.thread_id = th.id
    task.thread_id = th.id
    db.commit()
    orch = Orchestrator(db)
    outcomes = followup_svc.process_due_follow_ups(db, orch)
    db.commit()
    assert task.status == "cancelled"
    assert outcomes == []


def test_not_interested_stops_and_suppresses(db):
    acct, camp, contact, cc = _setup(db)
    # schedule a follow-up first
    followup_svc.schedule_next_follow_up(db, cc)
    db.commit()
    action = approval_svc.apply_intent_actions(db, cc, "not_interested", contact.email, 1)
    db.commit()
    assert action == "stopped"
    assert db.query(models.Suppression).filter_by(email=contact.email).first() is not None
    assert cc.status == "stopped"
    pending = db.query(models.FollowUpTask).filter_by(campaign_contact_id=cc.id, status="scheduled").count()
    assert pending == 0


def test_opt_out_stops_and_suppresses(db):
    _, _, contact, cc = _setup(db)
    followup_svc.schedule_next_follow_up(db, cc)
    db.commit()

    action = approval_svc.apply_intent_actions(db, cc, "opt_out", contact.email, 1)
    db.commit()

    assert action == "stopped"
    assert cc.status == "stopped"
    suppression = db.query(models.Suppression).filter_by(email=contact.email).first()
    assert suppression is not None
    assert suppression.reason == "opt_out"
    assert db.query(models.FollowUpTask).filter_by(
        campaign_contact_id=cc.id, status="scheduled"
    ).count() == 0


def test_manual_lock_does_not_block_bounce_stop_and_repeat_is_idempotent(db):
    acct, camp, contact, cc = _setup(db)
    contact.category = "customer"
    contact.intent_level = "high"
    contact.tags = json.dumps(["user-tag"])
    contact.segments = json.dumps(["enterprise"])
    contact.manual_lock = True
    contact.lifecycle_stage = "following_up"
    contact.next_action = "follow_up"
    contact.next_follow_up_at = datetime.now(timezone.utc) + timedelta(days=1)

    pending_draft = models.EmailDraft(
        gmail_account_id=acct.id, campaign_contact_id=cc.id,
        to_email=contact.email, subject="Pending", body_text="Pending body", status="draft",
    )
    sent_draft = models.EmailDraft(
        gmail_account_id=acct.id, campaign_contact_id=cc.id,
        to_email=contact.email, subject="Sent", body_text="Sent body", status="sent",
    )
    db.add_all([pending_draft, sent_draft])
    db.flush()
    pending_approval = models.Approval(
        kind="first_send", campaign_id=camp.id, campaign_contact_id=cc.id,
        draft_id=pending_draft.id, to_email=contact.email,
        subject="Pending", body_text="Pending body", status="pending",
    )
    historical_approval = models.Approval(
        kind="first_send", campaign_id=camp.id, campaign_contact_id=cc.id,
        draft_id=sent_draft.id, to_email=contact.email,
        subject="Sent", body_text="Sent body", status="approved",
    )
    task = models.FollowUpTask(
        campaign_contact_id=cc.id, contact_id=contact.id, campaign_id=camp.id,
        sequence=1, scheduled_at=datetime.now(timezone.utc), status="scheduled",
    )
    db.add_all([pending_approval, historical_approval, task])
    db.commit()

    assert approval_svc.apply_intent_actions(db, cc, "bounce", contact.email, 1) == "bounced"
    db.commit()
    db.refresh(contact); db.refresh(cc); db.refresh(pending_draft)
    db.refresh(sent_draft); db.refresh(pending_approval); db.refresh(historical_approval); db.refresh(task)

    assert contact.status == "bounced"
    assert contact.lifecycle_stage == "stopped"
    assert contact.next_action == "none" and contact.next_follow_up_at is None
    assert contact.manual_lock is True
    assert contact.category == "customer" and contact.intent_level == "high"
    assert contact.tags == json.dumps(["user-tag"])
    assert contact.segments == json.dumps(["enterprise"])
    assert cc.membership_active is False and cc.status == "stopped"
    assert pending_approval.status == "expired" and pending_draft.status == "cancelled"
    assert historical_approval.status == "approved" and sent_draft.status == "sent"
    assert task.status == "cancelled"
    assert db.query(models.Suppression).filter_by(owner_id=1, email=contact.email).count() == 1
    assert db.query(models.AuditLog).filter_by(action="contact_delivery_stopped", entity_id=str(contact.id)).count() == 1

    # Reprocessing the same bounce remains successful without duplicating audit/suppression rows.
    assert approval_svc.apply_intent_actions(db, cc, "bounce", contact.email, 1) == "bounced"
    db.commit()
    assert db.query(models.Suppression).filter_by(owner_id=1, email=contact.email).count() == 1
    assert db.query(models.AuditLog).filter_by(action="contact_delivery_stopped", entity_id=str(contact.id)).count() == 1


def test_legacy_first_follow_up_alias_preserves_next_sequence(db):
    _, _, _, cc = _setup(db)
    cc.assigned_follow_ups = 1
    db.commit()
    result = followup_svc.schedule_first_follow_up(db, cc)
    assert result["ok"] is True
    assert result["task"].sequence == 2
