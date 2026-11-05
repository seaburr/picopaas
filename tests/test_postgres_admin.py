"""Integration tests for the real PostgresAdmin (app/providers/postgres.py) against an actual PostgreSQL server.
Skipped unless POSTGRES_ADMIN_URL is set. CI's `postgres-integration` job and, locally,
`scripts/dev/postgres-up.sh && source .postgres-dev/postgres.env` provide a server shaped like a managed one:
TLS only, and an admin that is not a superuser (CREATEROLE + CREATEDB, owns the `postgres` database).

The server flavor is `self-managed`, so `do-managed`'s only difference (no explicit SET ROLE grant) is not exercised.
Uses app ids 90011/90012 and wipes them before and after each test so a crashed run doesn't poison the next.
"""
import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest

from app import config
from app.crypto import encrypt
from app.providers.base import DbAdminError, generate_password
from app.providers.postgres import PostgresAdmin

pytestmark = pytest.mark.postgres

POSTGRES_ADMIN_URL = os.environ.get("POSTGRES_ADMIN_URL")

if not POSTGRES_ADMIN_URL:
    pytest.skip("POSTGRES_ADMIN_URL not set; skipping real PostgreSQL integration tests", allow_module_level=True)

APP_A, APP_B = 90011, 90012


class _FakeDbServer:
    """Duck-types the DbServer attributes PostgresAdmin reads from a model row."""

    def __init__(self) -> None:
        p = urlparse(POSTGRES_ADMIN_URL)
        self.host, self.port, self.admin_user = p.hostname, p.port or 5432, p.username
        self.admin_password_encrypted = encrypt(p.password or "")
        self.ca_cert = Path(os.environ["POSTGRES_TEST_CA_FILE"]).read_text()
        self.flavor = "self-managed"


def _root_connect():
    """Superuser connection for test housekeeping and assertions the admin under test isn't allowed to make."""
    p = urlparse(os.environ["POSTGRES_TEST_ROOT_URL"])
    return psycopg.connect(host=p.hostname, port=p.port or 5432, user=p.username, password=p.password,
                           dbname="postgres", sslmode="require", autocommit=True)


def _wipe() -> None:
    with _root_connect() as conn:
        for app_id in (APP_A, APP_B):
            conn.execute(f'DROP DATABASE IF EXISTS "app_{app_id}" WITH (FORCE)')
            conn.execute(f'DROP ROLE IF EXISTS "app_{app_id}"')


@pytest.fixture(autouse=True)
def _clean_slate():
    _wipe()
    yield
    _wipe()


@pytest.fixture()
def admin() -> PostgresAdmin:
    return PostgresAdmin(_FakeDbServer())


def _connect_as(creds, dbname=None):
    return psycopg.connect(host=creds["host"], port=creds["port"], user=creds["user"], password=creds["password"],
                           dbname=dbname or creds["name"], sslmode="require", autocommit=True, connect_timeout=10)


def _make_table(creds, row_id=1):
    with _connect_as(creds) as conn:
        conn.execute("CREATE TABLE t (id INT PRIMARY KEY, payload TEXT)")
        conn.execute("INSERT INTO t VALUES (%s, repeat('x', 1000))", (row_id,))


def test_create_db_user_grants_full_access_to_own_db(admin):
    creds = admin.create_db_user(APP_A)
    assert creds["name"] == creds["user"] == f"app_{APP_A}"
    assert len(creds["password"]) >= 32
    assert creds["url"].startswith(f"postgres://app_{APP_A}:") or creds["url"].startswith(f"postgresql://app_{APP_A}:")
    _make_table(creds)  # needs the public schema handed over to the app role (PG15+ revoked CREATE from PUBLIC)
    with _connect_as(creds) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1


def test_app_role_cannot_connect_to_another_apps_database(admin):
    first, second = admin.create_db_user(APP_A), admin.create_db_user(APP_B)
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        _connect_as(second, dbname=first["name"])


def test_app_role_cannot_connect_to_the_admin_database(admin):
    creds = admin.create_db_user(APP_A)
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        _connect_as(creds, dbname="postgres")


def test_init_server_is_idempotent(admin):
    admin.init_server()
    admin.init_server()


def test_create_db_user_twice_replaces_password_and_keeps_data(admin):
    old = admin.create_db_user(APP_A)
    _make_table(old)
    new = admin.create_db_user(APP_A)
    assert new["password"] != old["password"]
    with pytest.raises(psycopg.OperationalError):
        _connect_as(old)
    with _connect_as(new) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1


def test_role_settings_are_applied(admin):
    creds = admin.create_db_user(APP_A)
    with _connect_as(creds) as conn:
        assert conn.execute("SHOW search_path").fetchone()[0] == "public"
        assert conn.execute("SELECT setting::int FROM pg_settings WHERE name = 'statement_timeout'").fetchone()[0] \
            == config.PG_STATEMENT_TIMEOUT_MS
        assert conn.execute(
            "SELECT setting::int FROM pg_settings WHERE name = 'idle_in_transaction_session_timeout'"
        ).fetchone()[0] == config.PG_IDLE_TX_TIMEOUT_MS


def test_connection_limit_refuses_the_next_connection(admin):
    creds = admin.create_db_user(APP_A)
    held = [_connect_as(creds) for _ in range(config.PG_MAX_DB_USER_CONNECTIONS)]
    try:
        with pytest.raises(psycopg.OperationalError, match="too many connections for role"):
            _connect_as(creds)
        held.pop().close()  # a freed slot is usable again
        held.append(_connect_as(creds))
    finally:
        for c in held:
            c.close()


def test_connection_limit_counts_per_role_not_shared_between_apps(admin):
    first, second = admin.create_db_user(APP_A), admin.create_db_user(APP_B)
    held = [_connect_as(first) for _ in range(config.PG_MAX_DB_USER_CONNECTIONS)]
    try:
        _connect_as(second).close()
    finally:
        for c in held:
            c.close()


def test_changed_limit_applies_to_new_connections_only(admin):
    creds = admin.create_db_user(APP_A)
    held = [_connect_as(creds) for _ in range(3)]
    try:
        admin.set_user_connection_limit(APP_A, 2)  # already over the new cap: nothing is dropped
        assert all(c.execute("SELECT 1").fetchone()[0] == 1 for c in held)
        with pytest.raises(psycopg.OperationalError, match="too many connections for role"):
            _connect_as(creds)
        admin.set_user_connection_limit(APP_A, 4)
        held.append(_connect_as(creds))
    finally:
        for c in held:
            c.close()


def test_db_size_grows_with_data(admin):
    creds = admin.create_db_user(APP_A)
    before = admin.db_size_bytes(APP_A)
    assert before > 0  # a new database already has catalogs
    with _connect_as(creds) as conn:
        conn.execute("CREATE TABLE big AS SELECT g, repeat('x', 500) AS p FROM generate_series(1, 5000) g")
    assert admin.db_size_bytes(APP_A) > before


def test_db_size_of_unknown_app_raises(admin):
    with pytest.raises(DbAdminError):
        admin.db_size_bytes(APP_A)


def test_stage_1_read_only_blocks_writes_kills_sessions_and_restores(admin):
    creds = admin.create_db_user(APP_A)
    _make_table(creds)
    open_session = _connect_as(creds)
    try:
        report = admin.set_read_only(APP_A, True)
        assert report["killed"] == 1 and report["failed"] == 0
        with pytest.raises(psycopg.OperationalError):
            open_session.execute("SELECT 1")
    finally:
        open_session.close()

    with _connect_as(creds) as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO t VALUES (2, 'y')")
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1

    assert admin.set_read_only(APP_A, False) is None
    with _connect_as(creds) as conn:
        conn.execute("INSERT INTO t VALUES (2, 'y')")
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 2


def test_stage_2_locks_out_and_restore_lets_back_in(admin):
    creds = admin.create_db_user(APP_A)
    admin.set_read_only(APP_A, True, hard=True)
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        _connect_as(creds)

    admin.set_read_only(APP_A, True)  # back to stage 1: connects again, still read-only
    with _connect_as(creds) as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TABLE t (id INT)")

    admin.set_read_only(APP_A, False)
    with _connect_as(creds) as conn:
        conn.execute("CREATE TABLE t (id INT)")


def test_set_password_replaces_old_and_keeps_grants(admin):
    old = admin.create_db_user(APP_A)
    pw = generate_password()
    new = admin.set_password(APP_A, pw)
    assert new["password"] == pw
    with pytest.raises(psycopg.OperationalError):
        _connect_as(old)
    _make_table(new)


def test_rotate_password_invalidates_old(admin):
    old = admin.create_db_user(APP_A)
    new = admin.rotate_password(APP_A)
    assert new["password"] != old["password"] and new["user"] == old["user"]
    with pytest.raises(psycopg.OperationalError):
        _connect_as(old)
    _connect_as(new).close()


def test_drop_removes_database_and_role_even_with_open_sessions(admin):
    creds = admin.create_db_user(APP_A)
    open_session = _connect_as(creds)
    try:
        admin.drop(APP_A)
    finally:
        open_session.close()
    with _root_connect() as conn:
        assert conn.execute("SELECT count(*) FROM pg_database WHERE datname = %s", (creds["name"],)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s", (creds["user"],)).fetchone()[0] == 0
    admin.drop(APP_A)  # dropping again is a no-op


def test_server_health_reports_real_numbers(admin):
    creds = admin.create_db_user(APP_A)
    with _connect_as(creds):
        health = admin.server_health()
    assert health["threads_connected"] >= 2  # the app session and the admin's own
    assert health["max_connections"] >= 5
    assert health["data_size_bytes"] > 0
    assert health["uptime_seconds"] >= 0
