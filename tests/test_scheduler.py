from app import crypto, models, scheduler
from app.reconciler import _mysql_cidr


def test_mysql_cidr_modes():
    assert _mysql_cidr({"mysql_egress_mode": "in-cluster"}) is None
    assert _mysql_cidr({"mysql_egress_mode": "cidr", "mysql_cidr": "10.0.0.0/24"}) == "10.0.0.0/24"


def test_bootstrap_db_server(db_session, monkeypatch):
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    for k, v in dict(BOOTSTRAP_DB_HOST="h", BOOTSTRAP_DB_USER="u", BOOTSTRAP_DB_PASSWORD="pw", BOOTSTRAP_DB_PORT="3307").items():
        monkeypatch.setenv(k, v)
    scheduler.bootstrap_db_server()
    scheduler.bootstrap_db_server()  # idempotent
    rows = db_session.query(models.DbServer).all()
    assert len(rows) == 1 and rows[0].name == "primary" and rows[0].port == 3307
    assert crypto.decrypt(rows[0].admin_password_encrypted) == "pw"


def test_leader_noop_on_sqlite():
    assert scheduler.try_acquire_leader() is True
