"""Shared, side-effect-free conversation context for Agent reply analysis."""
from __future__ import annotations

from datetime import datetime, timezone

from .. import models


MAX_CONTEXT_MESSAGES = 8
MAX_CONTEXT_CHARACTERS = 10_000


def _message_time(message: models.EmailMessage) -> datetime:
    value = message.received_at or message.created_at
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def build_thread_context(db, thread: models.EmailThread) -> str:
    """Return the newest conversation window in chronological reading order.

    The latest inbound customer message is always retained, even if later
    outbound messages would otherwise push it outside the eight-message window.
    This function reads local data only and never calls Gmail.
    """
    messages = (
        db.query(models.EmailMessage)
        .filter_by(thread_id=thread.id)
        .all()
    )
    if not messages:
        return ""

    ordered = sorted(messages, key=lambda message: (_message_time(message), message.id or 0))
    selected = ordered[-MAX_CONTEXT_MESSAGES:]
    inbound = [message for message in ordered if message.is_incoming]
    latest_inbound = inbound[-1] if inbound else None
    if latest_inbound is not None and latest_inbound not in selected:
        selected = selected[1:] + [latest_inbound]
        selected.sort(key=lambda message: (_message_time(message), message.id or 0))

    parts = []
    for message in selected:
        role = "Customer" if message.is_incoming else "Us"
        timestamp = _message_time(message).isoformat()
        subject = (message.subject or "").strip()[:240]
        body = (message.body_text or "").strip()[:900]
        parts.append(f"[{timestamp}] {role}\nSubject: {subject}\n{body}".rstrip())

    context = "\n\n".join(parts)
    return context[:MAX_CONTEXT_CHARACTERS]
