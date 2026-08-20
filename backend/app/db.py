"""Database engine and session management. Compatible with SQLite (dev) and PostgreSQL (prod)."""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


_connect_args = {}
_engine_kwargs = {}
if settings.DATABASE_URL.startswith("sqlite"):
    # check_same_thread=False lets the Huey worker (separate thread) share the
    # engine; timeout=30 makes SQLite wait instead of raising "database is locked"
    # under the concurrent-enqueue / reclaim load of the scheduler.
    _connect_args = {"check_same_thread": False, "timeout": 60}
    _engine_kwargs["poolclass"] = NullPool

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
    **_engine_kwargs,
)

if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA wal_autocheckpoint=1000")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables (dev / SQLite). For Postgres use Alembic migrations."""
    # Import models so they register on Base.metadata
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
