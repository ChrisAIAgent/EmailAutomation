from datetime import datetime, timedelta
from types import SimpleNamespace

from app import models
from app.services.automation import _reply_approval_for_message


def _approval(thread_id: int, created_at: datetime):
    return models.Approval(
        thread_id=thread_id,
        kind="reply",
        status="approved",
        to_email="customer@example.com",
        subject="Re: Question",
        body_text="Reply",
        created_at=created_at,
    )


def test_reply_approval_marks_same_inbound_message_processed(db):
    now = datetime.utcnow()
    approval = _approval(thread_id=41, created_at=now + timedelta(seconds=1))
    db.add(approval)
    db.commit()
    message = SimpleNamespace(received_at=now, created_at=now)

    assert _reply_approval_for_message(db, 41, message).id == approval.id


def test_newer_inbound_message_is_not_blocked_by_old_approval(db):
    now = datetime.utcnow()
    approval = _approval(thread_id=42, created_at=now)
    db.add(approval)
    db.commit()
    message = SimpleNamespace(
        received_at=now + timedelta(seconds=1),
        created_at=now + timedelta(seconds=1),
    )

    assert _reply_approval_for_message(db, 42, message) is None
