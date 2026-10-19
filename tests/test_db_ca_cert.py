"""db.ca_cert / db.tls refs, ca_cert migration, bootstrap seeding + backfill."""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect

from app import crypto, models, scheduler
from app.env_refs import ALLOWED_REFS
from app.reconciler import _db_refs

PEM = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"


def _app(db_session, ca):
    server = models.DbServer(name="s", host="h", port=3306, admin_user="a",
                             admin_password_encrypted=crypto.encrypt("x"), ca_cert=ca)
    db_session.add(server)
    db_session.flush()
    row = models.AppDatabase(server=server, db_name="d", db_user="u",
                             password_encrypted=crypto.encrypt("pw"))
    return type("A", (), {"database": row})()


@pytest.mark.parametrize("ca,tls", [(PEM, "required"), (None, "off")])
def test_refs(db_session, ca, tls):
    refs = _db_refs(_app(db_session, ca))
    assert refs["db.ca_cert"] == (ca or "")
    assert refs["db.tls"] == tls
    assert refs["db.url"] == "mysql://u:pw@h:3306/d"
    assert {"db.ca_cert", "db.tls"} <= ALLOWED_REFS


def test_migration_round_trip(tmp_path):
    url = f"sqlite:///{tmp_path}/m.db"
    root = os.path.join(os.path.dirname(__file__), "..")
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True,
                       capture_output=True)

    cols = lambda: {c["name"] for c in inspect(create_engine(url)).get_columns("db_servers")}
    alembic("upgrade", "head")
    assert "ca_cert" in cols()
    alembic("downgrade", "e5b2c8d1f3a7")
    assert "ca_cert" not in cols()
    alembic("upgrade", "head")
    assert "ca_cert" in cols()


def _env(monkeypatch, db_session, ca_path):
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    for k, v in dict(BOOTSTRAP_DB_HOST="h", BOOTSTRAP_DB_USER="u", BOOTSTRAP_DB_PASSWORD="pw").items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("MYSQL_SSL_CA", str(ca_path))


def test_bootstrap_seeds_ca(db_session, monkeypatch, tmp_path):
    f = tmp_path / "ca.crt"
    f.write_text(PEM)
    _env(monkeypatch, db_session, f)
    scheduler.bootstrap_db_server()
    assert db_session.query(models.DbServer).one().ca_cert == PEM


def test_bootstrap_backfills_and_missing_file(db_session, monkeypatch, tmp_path):
    _env(monkeypatch, db_session, tmp_path / "nope.crt")
    scheduler.bootstrap_db_server()
    assert db_session.query(models.DbServer).one().ca_cert is None
    f = tmp_path / "ca.crt"
    f.write_text(PEM)
    monkeypatch.setenv("MYSQL_SSL_CA", str(f))
    scheduler.bootstrap_db_server()
    assert db_session.query(models.DbServer).one().ca_cert == PEM


def test_bootstrap_repairs_undecryptable_admin_password(db_session, monkeypatch, tmp_path):
    from app import crypto

    _env(monkeypatch, db_session, tmp_path / "nope.crt")
    scheduler.bootstrap_db_server()
    row = db_session.query(models.DbServer).one()
    row.admin_password_encrypted = "gAAAAA-encrypted-with-a-lost-key"  # simulates a FERNET_KEY change
    db_session.commit()
    scheduler.bootstrap_db_server()
    assert crypto.decrypt(db_session.query(models.DbServer).one().admin_password_encrypted) == "pw"


def test_bootstrap_syncs_rotated_admin_password(db_session, monkeypatch, tmp_path):
    from app import crypto

    _env(monkeypatch, db_session, tmp_path / "nope.crt")
    scheduler.bootstrap_db_server()
    monkeypatch.setenv("BOOTSTRAP_DB_PASSWORD", "rotated")
    scheduler.bootstrap_db_server()
    assert crypto.decrypt(db_session.query(models.DbServer).one().admin_password_encrypted) == "rotated"
