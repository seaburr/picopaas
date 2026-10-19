"""Shared pytest fixtures.

Tests run against a fresh SQLite in-memory database per test (StaticPool keeps
a single connection alive for the whole test so in-memory data isn't lost
between queries). Auth is exercised through the real HTTP endpoints
(register/login) rather than mocked — see `client` / `make_client` below.
"""
import os

os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.dependencies import get_registry
from app.main import app
from app.providers.fake import FakeRegistry

DEFAULT_PASSWORD = "correct-horse-battery"

# Comfortably under the 512MB cap, so existing tests that create apps with
# arbitrary image names don't need to know about the registry dependency.
DEFAULT_FAKE_IMAGE_SIZE_BYTES = 10 * 1024 * 1024


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def make_client(db_session):
    """Factory for independent TestClients (independent cookie jars) sharing one DB."""

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_registry] = lambda: FakeRegistry(
        default_size_bytes=DEFAULT_FAKE_IMAGE_SIZE_BYTES
    )
    clients = []

    def _make() -> TestClient:
        c = TestClient(app)
        clients.append(c)
        return c

    yield _make

    for c in clients:
        c.close()
    app.dependency_overrides.clear()


@pytest.fixture()
def client(make_client):
    return make_client()


def approve_user_directly(email: str) -> None:
    """Test helper: flip a user to 'active' straight in the shared test DB."""
    from app import models

    db = next(app.dependency_overrides[get_db]())
    user = db.query(models.User).filter(models.User.email == email.lower()).one()
    user.status = "active"
    db.commit()


def register_and_login(client: TestClient, email: str, password: str = DEFAULT_PASSWORD, approve: bool = True):
    """Registers a user (which logs them in via the session cookie) and returns the response.

    New non-first users start 'pending'; by default they are approved so ordinary tests can use
    /apps. Pass approve=False to exercise the pending state.
    """
    resp = client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    if approve and resp.json()["status"] == "pending":
        approve_user_directly(email)
    return resp
