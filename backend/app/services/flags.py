"""Runtime system flags, including the global pause switch."""
from __future__ import annotations

from .. import models


def get_flag(db, key: str, default=None):
    row = db.get(models.SystemFlag, key)
    return row.value if row else default


def set_flag(db, key: str, value: str):
    row = db.get(models.SystemFlag, key)
    if row is None:
        row = models.SystemFlag(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.flush()


def is_globally_paused(db) -> bool:
    return (get_flag(db, "global_pause", "false") or "false").lower() == "true"


def set_global_pause(db, paused: bool):
    set_flag(db, "global_pause", "true" if paused else "false")


def is_agent_takeover_enabled(db) -> bool:
    return (get_flag(db, "agent_takeover_enabled", "false") or "false").lower() == "true"


def is_campaign_active(db, campaign) -> bool:
    from .flags import is_globally_paused  # local import guard
    if is_globally_paused(db):
        return False
    return campaign.status == "active"
