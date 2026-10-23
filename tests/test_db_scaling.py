"""DB scaling: query counts must not grow with the number of apps; connection-limit apply is not repeated per sweep."""
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import event

from app import crypto, models, resources
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import reconcile_all
from tests.conftest import register_and_login
from tests.test_reconciler import _make_app, _make_server, session  # noqa: F401


@contextmanager
def count_statements(bind):
    stmts: list[str] = []

    def _before(conn, cursor, statement, params, context, executemany):
        stmts.append(statement)

    event.listen(bind, "before_cursor_execute", _before)
    try:
        yield stmts
    finally:
        event.remove(bind, "before_cursor_execute", _before)


def _kw(mysql=None, kube=None):
    mysql = mysql or FakeMySQLAdmin()
    return dict(kube=kube or FakeKubeProvider(), mysql_admin_factory=lambda s: mysql,
                object_store=FakeObjectStore(), settings={})


def _running_apps(session, n):
    _make_server(session)
    apps = [_make_app(session, want_db=True, want_storage=True, env=[("K", "v", False), ("S", "v", True)])
            for _ in range(n)]
    return apps


def _tick_statements(session, n):
    _running_apps(session, n)
    kw = _kw()
    reconcile_all(session, **kw)  # provisions
    reconcile_all(session, **kw)  # settles (rendered hash recorded, limit applied)
    session.expire_all()
    with count_statements(session.get_bind()) as stmts:
        reconcile_all(session, **kw)
    return len(stmts)


def test_reconciler_query_count_does_not_grow_with_apps(session):
    few = _tick_statements(session, 2)
    # Fresh DB for the larger run.
    session.rollback()
    for t in reversed(models.Base.metadata.sorted_tables):
        session.execute(t.delete())
    session.commit()
    many = _tick_statements(session, 8)
    assert many <= few + 2, (few, many)


def test_list_apps_query_count_does_not_grow(client, db_session):
    uid = register_and_login(client, "owner@example.com").json()["id"]

    def add(n):
        for i in range(n):
            a = models.App(owner_id=uid, name=f"a{i}", hostname=f"h-{uuid.uuid4().hex[:8]}", image="x/y:1", port=80,
                           want_db=True, want_storage=True)
            a.env_vars.append(models.AppEnvVar(key="K", value_encrypted=crypto.encrypt("v")))
            a.events.append(models.AppEvent(at=datetime.now(timezone.utc).replace(tzinfo=None), kind="oom",
                                            message="m", dedupe_key=uuid.uuid4().hex))
            db_session.add(a)
        db_session.commit()

    def measure():
        db_session.expire_all()
        with count_statements(db_session.get_bind()) as stmts:
            r = client.get("/apps")
        assert r.status_code == 200
        return len(r.json()), len(stmts)

    add(1)
    n1, q1 = measure()
    add(3)
    n4, q4 = measure()
    assert (n1, n4) == (1, 4)
    assert q4 <= q1 + 1, (q1, q4)


def test_sweep_applies_connection_limit_once(session, monkeypatch):
    from app import config
    _make_server(session)
    mysql = FakeMySQLAdmin()
    calls = []
    orig = mysql.set_user_connection_limit
    mysql.set_user_connection_limit = lambda app_id, limit=None: (calls.append((app_id, limit)), orig(app_id, limit))[1]
    app = _make_app(session, want_db=True)
    kw = _kw(mysql)
    for _ in range(4):
        reconcile_all(session, **kw)
    assert app.status == "running"
    assert len(calls) == 1 and app.database.db_conn_limit_applied == resources.effective(app)["db_max_connections"]
    # Effective limit changes (admin override / config): applied again, once.
    app.db_max_connections = 7
    session.commit()
    reconcile_all(session, **kw)
    reconcile_all(session, **kw)
    assert calls[-1] == (app.id, 7) and len(calls) == 2
    # Failure leaves the marker unchanged so it is retried.
    app.db_max_connections = 9
    session.commit()
    mysql.limit_error = "boom"
    reconcile_all(session, **kw)
    assert app.database.db_conn_limit_applied == 7
    mysql.limit_error = None
    reconcile_all(session, **kw)
    assert app.database.db_conn_limit_applied == 9


def test_admin_override_applies_and_records(client, db_session):
    from app.main import app as fastapi_app
    from app.routers.apps import get_mysql_admin_factory, get_object_store
    mysql = FakeMySQLAdmin()
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda s: mysql)
    fastapi_app.dependency_overrides[get_object_store] = lambda: FakeObjectStore()
    register_and_login(client, "admin@example.com")  # first user = platform admin
    _make_server(db_session)
    r = client.post("/apps", json={"name": "d", "image": "x/y:1", "port": 8080, "want_db": True})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    reconcile_all(db_session, **_kw(mysql))
    mysql.conn_limits.clear()
    r = client.patch(f"/admin/apps/{aid}/resources", json={"db_max_connections": 12})
    assert r.status_code == 200, r.text
    assert mysql.conn_limits == {aid: 12}
    db_session.expire_all()
    assert db_session.get(models.App, aid).database.db_conn_limit_applied == 12
    # Rotating credentials clears the marker so the sweep re-asserts the cap.
    r = client.post(f"/apps/{aid}/rotate-credentials", json={"target": "db"})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(models.App, aid).database.db_conn_limit_applied is None
    mysql.conn_limits.clear()
    reconcile_all(db_session, **_kw(mysql))
    assert mysql.conn_limits == {aid: 12}


def test_pool_settings_applied_only_for_mysql(monkeypatch):
    import importlib
    import app.db as dbmod
    assert dbmod._pool_kwargs == {}  # SQLite in tests
    monkeypatch.setattr("app.config.DATABASE_URL", "mysql+pymysql://u:not-the-password@h/db")
    monkeypatch.setattr("app.db_tls.engine_url_and_args", lambda url: (url, {}))
    monkeypatch.setattr("app.config.DB_POOL_SIZE", 3)
    try:
        importlib.reload(dbmod)
        assert dbmod._pool_kwargs["pool_size"] == 3 and dbmod._pool_kwargs["pool_pre_ping"] is True
        assert dbmod.engine.pool.size() == 3
    finally:
        monkeypatch.undo()
        importlib.reload(dbmod)
