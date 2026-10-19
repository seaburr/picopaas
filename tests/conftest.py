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
from app.main import app

DEFAULT_PASSWORD = "correct-horse-battery"


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


def register_and_login(client: TestClient, email: str, password: str = DEFAULT_PASSWORD):
    """Registers a user (which logs them in via the session cookie) and returns the response."""
    resp = client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp
