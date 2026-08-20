"""Pytest fixtures. Uses a file-based SQLite test DB; tables reset per test."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ["APP_ENCRYPTION_KEY"] = "test-encryption-key"
os.environ["ENABLE_REAL_SEND"] = "false"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["RESTRICTED_RECIPIENT_ALLOWLIST"] = ""
os.environ.pop("TEST_RECIPIENT_ALLOWLIST", None)
os.environ["LLM_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient

from app.db import init_db, SessionLocal, engine, Base
from app.main import app
from app.api import deps
from app import models


@pytest.fixture(autouse=True)
def _reset_db():
    from app.config import get_settings
    from app.gmail import clear_transport_cache
    os.environ["ENABLE_REAL_SEND"] = "false"
    os.environ["RESTRICTED_RECIPIENT_ALLOWLIST"] = ""
    os.environ.pop("TEST_RECIPIENT_ALLOWLIST", None)
    get_settings.cache_clear()
    clear_transport_cache()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    get_settings.cache_clear()
    Base.metadata.drop_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        from app.api.deps import ensure_owner
        ensure_owner(s)
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture
def client(db):
    def _get_db():
        yield db

    app.dependency_overrides[deps.get_db] = _get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def owner_id(db):
    return 1
