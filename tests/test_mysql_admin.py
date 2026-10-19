"""Integration tests for the real MySQLAdmin (app/providers/mysql.py) against
an actual MySQL server. Skipped unless MYSQL_ADMIN_URL is set — CI's
`mysql-integration` job (.github/workflows/ci.yml) runs a `mysql:8.4` service
container and sets it; locally, run a MySQL container yourself and export
MYSQL_ADMIN_URL=mysql://root:<password>@127.0.0.1:3306 to opt in.

Uses app_ids in a high, dedicated range (90001/90002) and cleans them up
before and after each test so a crashed previous run doesn't poison the next.
"""
import os
from urllib.parse import urlparse

import pymysql
import pytest

from app.crypto import encrypt
from app.providers.mysql import MySQLAdminClient

pytestmark = pytest.mark.mysql

MYSQL_ADMIN_URL = os.environ.get("MYSQL_ADMIN_URL")

if not MYSQL_ADMIN_URL:
    pytest.skip(
        "MYSQL_ADMIN_URL not set; skipping real MySQL integration tests", allow_module_level=True
    )

_TEST_APP_IDS = (90001, 90002)


class _FakeDbServer:
    """Duck-types the DbServer attributes MySQLAdminClient reads from a model row."""

    def __init__(self, host: str, port: int, admin_user: str, admin_password: str) -> None:
        self.host = host
        self.port = port
        self.admin_user = admin_user
        self.admin_password_encrypted = encrypt(admin_password)


def _parsed():
    return urlparse(MYSQL_ADMIN_URL)


def _server() -> _FakeDbServer:
    p = _parsed()
    return _FakeDbServer(
        host=p.hostname, port=p.port or 3306, admin_user=p.username, admin_password=p.password or ""
    )


def _admin_connect():
    p = _parsed()
    return pymysql.connect(
        host=p.hostname, port=p.port or 3306, user=p.username, password=p.password or "", autocommit=True
    )


def _wipe_test_apps() -> None:
    conn = _admin_connect()
    try:
        with conn.cursor() as cur:
            for app_id in _TEST_APP_IDS:
                cur.execute(f"DROP DATABASE IF EXISTS `app_{app_id}`")
                cur.execute(f"DROP USER IF EXISTS 'app_{app_id}'@'%'")
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean_slate():
    _wipe_test_apps()
    yield
    _wipe_test_apps()


@pytest.fixture()
def admin() -> MySQLAdminClient:
    return MySQLAdminClient(_server())


def _connect_as(creds, database=None):
    return pymysql.connect(
        host=creds["host"],
        port=creds["port"],
        user=creds["user"],
        password=creds["password"],
        database=database,
        autocommit=True,
    )


def test_create_db_user_grants_full_access_to_own_db(admin):
    creds = admin.create_db_user(90001)
    assert creds["name"] == "app_90001"
    assert creds["user"] == "app_90001"
    assert len(creds["password"]) == 32

    conn = _connect_as(creds, database=creds["name"])
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE t (id INT PRIMARY KEY)")
            cur.execute("INSERT INTO t VALUES (1)")
            cur.execute("SELECT COUNT(*) FROM t")
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_second_app_user_cannot_see_first_apps_database(admin):
    creds1 = admin.create_db_user(90001)
    creds2 = admin.create_db_user(90002)

    conn = _connect_as(creds2)
    try:
        with conn.cursor() as cur:
            with pytest.raises(pymysql.err.OperationalError):
                cur.execute(f"USE `{creds1['name']}`")
    finally:
        conn.close()


def test_db_size_bytes_reflects_table_data(admin):
    creds = admin.create_db_user(90001)
    conn = _connect_as(creds, database=creds["name"])
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE t (id INT PRIMARY KEY, payload TEXT)")
            cur.execute("INSERT INTO t VALUES (1, REPEAT('x', 1000))")
    finally:
        conn.close()

    assert admin.db_size_bytes(90001) > 0


def test_db_size_bytes_is_zero_for_unknown_app(admin):
    assert admin.db_size_bytes(90001) == 0


def test_read_only_blocks_insert_but_allows_select_and_delete(admin):
    creds = admin.create_db_user(90001)
    conn = _connect_as(creds, database=creds["name"])
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE t (id INT PRIMARY KEY)")
            cur.execute("INSERT INTO t VALUES (1)")
    finally:
        conn.close()

    admin.set_read_only(90001, True)

    conn = _connect_as(creds, database=creds["name"])
    try:
        with conn.cursor() as cur:
            with pytest.raises(pymysql.err.OperationalError):
                cur.execute("INSERT INTO t VALUES (2)")
            cur.execute("SELECT COUNT(*) FROM t")
            assert cur.fetchone()[0] == 1
            cur.execute("DELETE FROM t WHERE id = 1")
            cur.execute("SELECT COUNT(*) FROM t")
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()

    admin.set_read_only(90001, False)
    conn = _connect_as(creds, database=creds["name"])
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO t VALUES (3)")
            cur.execute("SELECT COUNT(*) FROM t")
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_drop_removes_database_and_user(admin):
    creds = admin.create_db_user(90001)
    admin.drop(90001)

    with pytest.raises(pymysql.err.OperationalError):
        _connect_as(creds)

    conn = _admin_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE schema_name = %s",
                (creds["name"],),
            )
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_rotate_password_invalidates_old_and_keeps_grants(admin):
    old = admin.create_db_user(90001)
    new = admin.rotate_password(90001)
    assert new["password"] != old["password"] and new["user"] == old["user"]
    assert new["url"].startswith("mysql://app_90001:")

    with pytest.raises(pymysql.err.OperationalError):
        _connect_as(old)
    conn = _connect_as(new, database=new["name"])
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE t (id INT PRIMARY KEY)")
            cur.execute("INSERT INTO t VALUES (1)")
    finally:
        conn.close()
