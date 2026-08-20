"""Database migration / init script.

The ORM models are SQLite- and PostgreSQL-compatible. `init_db()` uses
SQLAlchemy create_all which is idempotent (safe to run repeatedly). For
team-scale production schemas, point Alembic at the same models; the table
definitions already live in app/models.py.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import init_db, engine
from app import models  # noqa: F401  (register models)


def main():
    init_db()
    print("Database initialized/migrated at:", engine.url)


if __name__ == "__main__":
    main()
