"""app/db_placement.py — pure SQLAlchemy, no providers, plain SQLite."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.crypto import encrypt
from app.db import Base
from app.db_placement import NoDbCapacity, choose_server
from app.models import AppDatabase, DbServer


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _make_server(session, name, max_databases=25, status="active") -> DbServer:
    server = DbServer(
        name=name,
        host="mysql.example.internal",
        port=3306,
        admin_user="root",
        admin_password_encrypted=encrypt("s3cret"),
        max_databases=max_databases,
        status=status,
    )
    session.add(server)
    session.commit()
    return server


def _make_appdb(session, app_id, server) -> AppDatabase:
    db = AppDatabase(
        app_id=app_id,
        db_server_id=server.id,
        db_name=f"app_{app_id}",
        db_user=f"app_{app_id}",
        password_encrypted=encrypt("pw"),
    )
    session.add(db)
    session.commit()
    return db


def test_no_servers_raises_no_capacity(session):
    with pytest.raises(NoDbCapacity):
        choose_server(session)


def test_only_draining_or_full_servers_raises_no_capacity(session):
    _make_server(session, "a", status="draining")
    _make_server(session, "b", status="full")
    with pytest.raises(NoDbCapacity):
        choose_server(session)


def test_single_active_server_with_room_is_chosen(session):
    server = _make_server(session, "a")
    assert choose_server(session).id == server.id


def test_picks_server_with_most_free_slots(session):
    full_ish = _make_server(session, "a", max_databases=25)
    roomy = _make_server(session, "b", max_databases=25)
    for app_id in range(1, 21):
        _make_appdb(session, app_id, full_ish)
    for app_id in range(21, 23):
        _make_appdb(session, app_id, roomy)
    # full_ish has 5 free, roomy has 23 free -> roomy wins.
    assert choose_server(session).id == roomy.id


def test_ties_break_on_lowest_id(session):
    first = _make_server(session, "a")
    second = _make_server(session, "b")
    assert first.id < second.id
    assert choose_server(session).id == first.id


def test_a_server_at_capacity_is_skipped(session):
    full = _make_server(session, "a", max_databases=1)
    _make_appdb(session, 1, full)
    other = _make_server(session, "b", max_databases=1)
    assert choose_server(session).id == other.id


def test_inactive_status_is_never_chosen_even_with_room(session):
    _make_server(session, "a", max_databases=25, status="full")
    roomy = _make_server(session, "b", max_databases=1)
    assert choose_server(session).id == roomy.id
