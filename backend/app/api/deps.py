"""API dependencies: DB session + single-tenant demo owner."""
from __future__ import annotations

from ..db import get_db as _get_db
from .. import models


def get_db():
    yield from _get_db()


def ensure_owner(db) -> int:
    """Single-tenant local runtime: ensure a default owner exists and return its id."""
    user = db.query(models.User).filter_by(id=1).first()
    if user is None:
        user = models.User(id=1, email="owner@example.com", name="Workspace Owner", is_active=True)
        db.add(user)
        db.flush()
    return user.id
