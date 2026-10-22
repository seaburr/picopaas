"""Managed PostgreSQL alongside MySQL: engine-aware placement, provisioning, env refs, quota, API/spec field,
bootstrap, and the real PostgresAdmin's SQL (psycopg.connect faked; never a live server)."""
from __future__ import annotations

import pytest
from psycopg import sql

from app import config, crypto, models, scheduler
from app.db_placement import NoDbCapacity, choose_server
from app.env_refs import db_url
from app.providers.factory import db_admin_for
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore, FakePostgresAdmin
from app.quota import DB_QUOTA_BYTES, poll_quotas
from app.reconciler import reconcile_app
from tests.conftest import register_and_login
from tests.test_quota import _setup


def _server(db, name, engine, **kw):
    s = models.DbServer(name=name, engine=engine, host=f"{engine}.example.internal", port=25060 if engine == "pg" else 3306,
                        admin_user="admin", admin_password_encrypted=crypto.encrypt("not-the-password"), **kw)
    db.add(s)
    db.commit()
    return s


def _app(db, engine="mysql", env=None):
    u = models.User(email=f"u{engine}@example.com", password_hash="x")
    db.add(u)
    db.commit()
    a = models.App(owner_id=u.id, name="demo", hostname="demo-1", image="i", port=8080, want_db=True, db_engine=engine)
    for k, v in env or []:
        a.env_vars.append(models.AppEnvVar(key=k, value_encrypted=crypto.encrypt(v), is_secret=True))
    db.add(a)
    db.commit()
    return a


# --- placement -----------------------------------------------------------------------------------------------

def test_placement_filters_by_engine(db_session):
    my, pg = _server(db_session, "m", "mysql"), _server(db_session, "p", "pg")
    assert choose_server(db_session).id == my.id
    assert choose_server(db_session, "mysql").id == my.id
    assert choose_server(db_session, "pg").id == pg.id


def test_placement_no_pg_server_raises(db_session):
    _server(db_session, "m", "mysql")
    with pytest.raises(NoDbCapacity):
        choose_server(db_session, "pg")


# --- env refs ------------------------------------------------------------------------------------------------

def test_db_url_by_engine():
    assert db_url("mysql", "u", "pw", "h", 3306, "d") == "mysql://u:pw@h:3306/d"
    assert db_url("pg", "u", "pw", "h", 25060, "d") == "postgres://u:pw@h:25060/d"
    assert db_url("pg", "u", "pw", "h", 25060, "d", tls=True) == "postgres://u:pw@h:25060/d?sslmode=require"


# --- provisioning via the reconciler ------------------------------------------------------------------------

def _factory(pg, my):
    return lambda server: pg if server.engine == "pg" else my


def test_reconciler_provisions_on_pg_and_renders_postgres_url(db_session):
    _server(db_session, "m", "mysql")
    _server(db_session, "p", "pg", ca_cert="PEM")
    app = _app(db_session, "pg", env=[("DATABASE_URL", "${{ db.url }}"), ("PGHOST", "${{ db.host }}"), ("PGPORT", "${{ db.port }}")])
    kube, pg, my = FakeKubeProvider(), FakePostgresAdmin(), FakeMySQLAdmin()
    reconcile_app(db_session, app, kube=kube, mysql_admin_factory=_factory(pg, my), object_store=FakeObjectStore(),
                  settings={"mysql_cidr": "10.1.0.0/16"})
    assert app.status == "running"
    assert app.database.server.engine == "pg" and app.id in pg.creds and app.id not in my.creds
    secret = next(m for m in kube.applied[f"app-{app.id}"] if m["kind"] == "Secret")["stringData"]
    assert secret["DATABASE_URL"].startswith("postgres://app_") and secret["DATABASE_URL"].endswith("?sslmode=require")
    assert crypto.decrypt(app.database.password_encrypted) in secret["DATABASE_URL"]
    assert secret["PGHOST"] == "pg.example.internal" and secret["PGPORT"] == "25060"


def test_reconciler_mysql_default_unchanged(db_session):
    _server(db_session, "p", "pg")
    _server(db_session, "m", "mysql")
    app = _app(db_session, "mysql", env=[("DATABASE_URL", "${{ db.url }}")])
    kube, pg, my = FakeKubeProvider(), FakePostgresAdmin(), FakeMySQLAdmin()
    reconcile_app(db_session, app, kube=kube, mysql_admin_factory=_factory(pg, my), object_store=FakeObjectStore(),
                  settings={"mysql_cidr": "10.1.0.0/16"})
    assert app.database.server.engine == "mysql" and app.id in my.creds
    secret = next(m for m in kube.applied[f"app-{app.id}"] if m["kind"] == "Secret")["stringData"]
    assert secret["DATABASE_URL"].startswith("mysql://")


def test_reconciler_pg_without_pg_server_is_capacity_error(db_session):
    _server(db_session, "m", "mysql")
    app = _app(db_session, "pg")
    reconcile_app(db_session, app, kube=FakeKubeProvider(), mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={})
    assert app.status == "error" and app.database is None


# --- quota ---------------------------------------------------------------------------------------------------

def _pg_quota_setup(db_session):
    server, (app,) = _setup(db_session)
    server.engine = "pg"
    db_session.commit()
    return app


def test_quota_calls_pg_admin_two_stage(db_session):
    app = _pg_quota_setup(db_session)
    admin, store = FakePostgresAdmin(), FakeObjectStore()
    run = lambda: poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=store, storage=False)
    admin.sizes[app.id] = DB_QUOTA_BYTES + 1
    run()
    assert app.database.over_quota and admin.hard_calls[-1] == (app.id, True, False)
    admin.sizes[app.id] = int(DB_QUOTA_BYTES * config.PG_HARD_CAP_RATIO) + 1
    run()
    assert admin.hard_calls[-1] == (app.id, True, True) and admin.hard[app.id]
    admin.sizes[app.id] = 1
    run()
    assert not app.database.over_quota and admin.hard_calls[-1] == (app.id, False, False) and not admin.hard[app.id]


def test_quota_pg_zero_listed_sessions_does_not_roll_app(db_session):
    app = _pg_quota_setup(db_session)

    class Quiet(FakePostgresAdmin):
        def set_read_only(self, *a, **k):
            super().set_read_only(*a, **k)
            return {"listed": 0, "killed": 0, "failed": 0, "list_error": False, "path": "pg"}

    admin = Quiet()
    admin.sizes[app.id] = DB_QUOTA_BYTES + 1
    poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=FakeObjectStore(), storage=False)
    assert app.status == "running"  # the pg session list is authoritative


def test_quota_mysql_path_gets_no_hard_kwarg(db_session):
    _, (app,) = _setup(db_session)
    admin = FakeMySQLAdmin()  # its set_read_only has no `hard` parameter: would raise TypeError
    admin.sizes[app.id] = DB_QUOTA_BYTES + 1
    poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=FakeObjectStore(), storage=False)
    assert admin.calls == [(app.id, True)]


# --- API / spec ---------------------------------------------------------------------------------------------

def test_api_db_engine_field(client):
    register_and_login(client, "pg@example.com")
    base = {"name": "a", "image": "ghcr.io/example/demo:1.0", "port": 8080, "want_db": True}
    r = client.post("/apps", json=base)
    assert r.status_code == 201 and r.json()["db_engine"] == "mysql"
    r = client.post("/apps", json={**base, "name": "b", "db_engine": "pg"})
    assert r.status_code == 201 and r.json()["db_engine"] == "pg"
    assert client.post("/apps", json={**base, "name": "c", "db_engine": "oracle"}).status_code == 422


SPEC = """apiVersion: picopaas/v1
name: pgapp
image: ghcr.io/example/demo:1.0
port: 8080
resources:
  db: true
  db_engine: pg
"""


def test_spec_db_engine_and_immutability(client):
    register_and_login(client, "spec@example.com")
    r = client.post("/apps/spec", json={"spec": SPEC, "secrets": {}})
    assert r.status_code in (200, 201), r.text
    assert r.json()["db_engine"] == "pg"
    r2 = client.post("/apps/spec", json={"spec": SPEC.replace("db_engine: pg", "db_engine: mysql"), "secrets": {}})
    assert r2.status_code == 422 and "engine" in r2.json()["detail"]


def test_spec_default_engine_is_mysql():
    from app.spec import parse_spec
    s = parse_spec("apiVersion: picopaas/v1\nname: x\nimage: i\nport: 8080\nresources:\n  db: true\n")
    assert s.resources.db_engine == "mysql"


# --- bootstrap + factory ---------------------------------------------------------------------------------------

def test_bootstrap_pg_server(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    ca = tmp_path / "ca.pem"
    ca.write_text("PEM")
    _server(db_session, "primary", "mysql")
    for k, v in dict(BOOTSTRAP_PG_HOST="h", BOOTSTRAP_PG_USER="u", BOOTSTRAP_PG_PASSWORD="not-the-password",
                     BOOTSTRAP_PG_CA=str(ca)).items():
        monkeypatch.setenv(k, v)
    scheduler.bootstrap_pg_server()
    scheduler.bootstrap_pg_server()
    row = db_session.query(models.DbServer).filter_by(name="primary-pg").one()
    assert row.engine == "pg" and row.port == 25060 and row.ca_cert == "PEM\n"
    monkeypatch.setenv("BOOTSTRAP_PG_PORT", "25061")
    scheduler.bootstrap_pg_server()
    assert db_session.query(models.DbServer).filter_by(name="primary-pg").one().port == 25061


def test_bootstrap_pg_needs_all_vars(db_session, monkeypatch):
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db_session)
    monkeypatch.setenv("BOOTSTRAP_PG_HOST", "h")
    monkeypatch.delenv("BOOTSTRAP_PG_USER", raising=False)
    scheduler.bootstrap_pg_server()
    assert db_session.query(models.DbServer).count() == 0


def test_factory_dispatch(db_session):
    from app.providers.mysql import MySQLAdminClient
    from app.providers.postgres import PostgresAdmin
    assert isinstance(db_admin_for(_server(db_session, "p", "pg")), PostgresAdmin)
    assert isinstance(db_admin_for(_server(db_session, "m", "mysql")), MySQLAdminClient)


# --- real PostgresAdmin SQL (fake psycopg connection) -----------------------------------------------------------

class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Conn:
    log: list[tuple[str, str]] = []

    def __init__(self, dbname, existing=False):
        self.dbname, self.existing = dbname, existing

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        text = query.as_string(None) if isinstance(query, sql.Composable) else query
        _Conn.log.append((self.dbname, text, params))
        if text.startswith("SELECT 1 FROM"):
            return _Cur([(1,)] if self.existing else [])
        if "pg_database_size(%s)" in text:
            return _Cur([(1234,)])
        if "pg_terminate_backend" in text:
            return _Cur([(True,), (False,)])
        return _Cur([])


@pytest.fixture()
def pg(db_session, monkeypatch):
    from app.providers import postgres
    _Conn.log = []
    state = {"existing": False}
    monkeypatch.setattr(postgres.psycopg, "connect", lambda **kw: _Conn(kw["dbname"], state["existing"]))
    monkeypatch.setattr(sql.Composable, "as_string", lambda self, ctx=None: _render(self), raising=False)
    admin = postgres.PostgresAdmin(_server(db_session, "p", "pg"))
    admin.state = state
    return admin


def _render(c):
    from psycopg.sql import SQL, Composed, Identifier, Literal
    if isinstance(c, Composed):
        return "".join(_render(x) for x in c)
    if isinstance(c, SQL):
        return c._obj
    if isinstance(c, Identifier):
        return '"' + c._obj[0] + '"'
    if isinstance(c, Literal):
        return repr(c._obj)
    return str(c)


def _stmts(only_db=None):
    return [t for d, t, _ in _Conn.log if only_db is None or d == only_db]


def test_pg_create_db_user_sql(pg):
    creds = pg.create_db_user(7)
    default, appdb = _stmts("defaultdb"), _stmts("app_7")
    assert 'REVOKE CONNECT, TEMP ON DATABASE "defaultdb" FROM PUBLIC' in default
    create_role = next(s for s in default if s.startswith('CREATE ROLE "app_7" LOGIN PASSWORD '))
    assert create_role.endswith("CONNECTION LIMIT 5")
    assert 'CREATE DATABASE "app_7"' in default
    assert 'REVOKE ALL ON DATABASE "app_7" FROM PUBLIC' in default
    assert 'GRANT CONNECT, TEMP ON DATABASE "app_7" TO "app_7"' in default
    assert appdb == ['ALTER SCHEMA public OWNER TO "app_7"']  # only statement run inside the app DB
    assert creds["name"] == creds["user"] == "app_7" and creds["port"] == 25060
    assert creds["url"].startswith("postgres://app_7:") and creds["url"].endswith("/app_7?sslmode=require")


def test_pg_create_is_idempotent(pg):
    pg.state["existing"] = True
    pg.create_db_user(7)
    default = _stmts("defaultdb")
    assert any(s.startswith('ALTER ROLE "app_7" LOGIN PASSWORD') for s in default)
    assert not any(s.startswith("CREATE ROLE") or s.startswith("CREATE DATABASE") for s in default)


def test_pg_drop_set_password_size_and_limit_sql(pg):
    pg.drop(7)
    assert _stmts() == ['DROP DATABASE IF EXISTS "app_7" WITH (FORCE)', 'DROP ROLE IF EXISTS "app_7"']
    assert set(d for d, _, _ in _Conn.log) == {"defaultdb"}
    _Conn.log.clear()
    assert pg.db_size_bytes(7) == 1234
    pg.set_user_connection_limit(7)
    assert _stmts()[-1] == 'ALTER ROLE "app_7" CONNECTION LIMIT 5'
    assert pg.set_password(7, "not-the-password")["url"].startswith("postgres://app_7:not-the-password@")


def test_pg_set_read_only_stages_all_from_defaultdb(pg):
    r = pg.set_read_only(7, True)
    s = _stmts("defaultdb")
    assert 'ALTER DATABASE "app_7" SET default_transaction_read_only = on' in s
    assert not any("REVOKE CONNECT ON DATABASE" in t for t in s)
    assert r == {"listed": 2, "killed": 1, "failed": 1, "list_error": False, "path": "pg"}
    _Conn.log.clear()
    pg.set_read_only(7, True, hard=True)
    assert 'REVOKE CONNECT ON DATABASE "app_7" FROM "app_7"' in _stmts()
    _Conn.log.clear()
    assert pg.set_read_only(7, False) is None
    assert _stmts() == ['GRANT CONNECT ON DATABASE "app_7" TO "app_7"',
                        'ALTER DATABASE "app_7" SET default_transaction_read_only = off']
    assert set(d for d, _, _ in _Conn.log) == {"defaultdb"}


def test_pg_app_egress_policy_uses_pg_cidr_and_server_port():
    from types import SimpleNamespace
    from app import k8s_manifests, reconciler

    settings = SimpleNamespace(mysql_egress_mode="in-cluster", mysql_cidr=None, pg_cidr="10.11.0.0/20")
    cidr = reconciler._mysql_cidr(settings, "pg")  # never in-cluster for pg
    assert cidr == "10.11.0.0/20"
    pol = next(p for p in k8s_manifests.network_policies(1, 8080, cidr, 25060)
               if p["metadata"]["name"] == "allow-egress-mysql")
    rule = pol["spec"]["egress"][0]
    assert rule["to"] == [{"ipBlock": {"cidr": "10.11.0.0/20"}}]
    assert rule["ports"] == [{"protocol": "TCP", "port": 25060}]
