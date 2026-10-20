"""Per-app MySQL MAX_USER_CONNECTIONS: CREATE USER, re-apply on sweep, config override."""
import importlib

from app import config
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import reconcile_all
from tests.test_quota import _Cur, _client
from tests.test_reconciler import _make_app, _make_server, session  # noqa: F401


def test_create_user_uses_config_value(monkeypatch):
    monkeypatch.setattr(config, "MAX_DB_USER_CONNECTIONS", 17)
    cur = _Cur()
    try:
        _client(cur).create_db_user(5)
    except Exception:  # _creds needs real server attrs; CREATE USER is already recorded
        pass
    create = [q for q in cur.sql if q.startswith("CREATE USER")]
    assert create and create[0].endswith("WITH MAX_USER_CONNECTIONS 17")


def test_set_user_connection_limit_issues_alter(monkeypatch):
    monkeypatch.setattr(config, "MAX_DB_USER_CONNECTIONS", 12)
    cur = _Cur()
    _client(cur).set_user_connection_limit(93)
    assert cur.sql == ["ALTER USER 'app_93'@'%' WITH MAX_USER_CONNECTIONS 12"]


def test_default_and_env_override(monkeypatch):
    assert config.MAX_DB_USER_CONNECTIONS == 10
    monkeypatch.setenv("MAX_DB_USER_CONNECTIONS", "20")
    try:
        importlib.reload(config)
        assert config.MAX_DB_USER_CONNECTIONS == 20
    finally:
        monkeypatch.delenv("MAX_DB_USER_CONNECTIONS")
        importlib.reload(config)


def _sweep(session, mysql):
    reconcile_all(session, kube=FakeKubeProvider(), mysql_admin_factory=lambda s: mysql,
                  object_store=FakeObjectStore(), settings={})


def test_sweep_reapplies_limit_to_existing_db_app(session, monkeypatch):
    _make_server(session)
    mysql = FakeMySQLAdmin()
    app = _make_app(session, want_db=True)
    _sweep(session, mysql)  # provisions, running
    assert app.status == "running"
    mysql.conn_limits.clear()
    monkeypatch.setattr(config, "MAX_DB_USER_CONNECTIONS", 15)
    _sweep(session, mysql)
    assert mysql.conn_limits == {app.id: 15}


def test_limit_failure_does_not_break_sweep(session):
    _make_server(session)
    mysql = FakeMySQLAdmin()
    app = _make_app(session, want_db=True)
    _sweep(session, mysql)
    mysql.limit_error = "boom"
    _sweep(session, mysql)
    assert app.status == "running"
