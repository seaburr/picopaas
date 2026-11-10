"""Small branches the suite never reached, picked because they guard something: error text that must not leak a
password, an audit failure that must not break the request, Kaisen redeploy rules, and cookie parsing of app answers."""
import socket
import ssl
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from app import audit
from app.kaisen import deploy
from app.kaisen.httptool import CookieJar
from app.providers import dbcheck

PW = "not-the-password"


@pytest.mark.parametrize("exc,want", [
    (ssl.SSLError("handshake"), "TLS handshake failed"),
    (Exception("certificate verify failed"), "TLS handshake failed"),
    (Exception("(1045, \"Access denied for user 'a'@'h'\")"), "authentication failed"),
    (TimeoutError("timed out"), "connection timed out"),
    (Exception("(2003, \"Can't connect ... (111 Connection refused)\")"), "connection refused"),
    (ConnectionRefusedError("Connection refused"), "connection refused"),
    (socket.gaierror(8, "nodename nor servname provided"), "host name could not be resolved"),
    (RuntimeError("something odd"), "connection failed (RuntimeError)"),
])
def test_db_check_errors_are_classified(exc, want):
    assert dbcheck._classify(exc).startswith(want)


@pytest.mark.parametrize("engine", ["mysql", "pg"])
def test_db_check_never_echoes_the_password_or_driver_text(monkeypatch, engine):
    def boom(*a, **k):
        raise RuntimeError(f"driver said: user=admin password={PW} dsn=internal-host")

    monkeypatch.setattr(dbcheck.RealDbServerChecker, "_mysql", staticmethod(boom))
    monkeypatch.setattr(dbcheck.RealDbServerChecker, "_pg", staticmethod(boom))
    with pytest.raises(dbcheck.DbServerCheckError) as e:
        dbcheck.RealDbServerChecker().check(engine=engine, host="db.example", port=5432, user="admin", password=PW,
                                            ca_cert=None)
    msg = str(e.value)
    assert PW not in msg and "driver said" not in msg and "internal-host" not in msg
    assert msg.startswith("Couldn't connect to db.example:5432 as admin:")
    assert e.value.__cause__ is None  # `from None`: the driver exception is not chained into logs either


def test_db_check_passes_its_own_error_through(monkeypatch):
    def refuse(*a, **k):
        raise dbcheck.DbServerCheckError("already classified")

    monkeypatch.setattr(dbcheck.RealDbServerChecker, "_mysql", staticmethod(refuse))
    with pytest.raises(dbcheck.DbServerCheckError, match="already classified"):
        dbcheck.RealDbServerChecker().check(engine="mysql", host="h", port=3306, user="u", password=PW, ca_cert=None)


def test_audit_write_failure_is_swallowed_and_rolled_back():
    class Db:
        rolled_back = False

        def add(self, row):
            pass

        def commit(self):
            raise RuntimeError("database is gone")

        def rollback(self):
            self.rolled_back = True
            raise RuntimeError("still gone")  # even a failing rollback must not escape

    db = Db()
    audit.record(db, None, "app.delete", "app", 1, actor_email="a@example.com")  # must not raise
    assert db.rolled_back


def _live(**kw):
    base = dict(want_db=False, db_engine=None, want_storage=False, volume_size_gi=None, volume_mount_path=None,
                env_vars=[])
    return SimpleNamespace(**{**base, **kw})


def test_kaisen_redeploy_cannot_drop_a_bucket_or_a_volume():
    with pytest.raises(deploy.DeployError, match="bucket"):
        deploy.check_existing(_live(want_storage=True), deploy.Wanted())
    with pytest.raises(deploy.DeployError, match="volume"):
        deploy.check_existing(_live(volume_size_gi=1, volume_mount_path="/data"), deploy.Wanted())
    deploy.check_existing(_live(want_storage=True), deploy.Wanted(bucket=True))  # keeping it is fine
    deploy.check_existing(None, deploy.Wanted(bucket=True))  # first deploy


@pytest.mark.parametrize("live,want", [
    (_live(), deploy.Wanted(bucket=True)),
    (_live(), deploy.Wanted(database="mysql")),
    (_live(), deploy.Wanted(volume_path="/data")),
    (_live(volume_size_gi=1, volume_mount_path="/data"), deploy.Wanted(volume_path="/srv")),
])
def test_kaisen_resource_change_makes_an_unchanged_bundle_stale(live, want):
    assert deploy.stale(live, want, {}) is True


def test_kaisen_nothing_changed_is_not_stale():
    assert deploy.stale(None, deploy.Wanted(bucket=True), {}) is False
    assert deploy.stale(_live(), deploy.Wanted(), {}) is False


def test_cookie_jar_ignores_bad_attributes_and_foreign_domains():
    jar, host = CookieJar(), "app.example"
    past = format_datetime(datetime.now(timezone.utc) - timedelta(days=1))
    assert jar.store(host, "/", ["a=1; Max-Age=soon", "b=2; Expires=not a date", "c=3; Domain=evil.example"]) == ["a", "b"]
    assert "c=" not in (jar.header_for(host, "/") or "")  # a cookie for another domain is never kept
    assert jar.store(host, "/", [f"a=gone; Expires={past}", "b=gone; Max-Age=0"]) == ["a", "b"]  # both deleted
    assert jar.header_for(host, "/") is None
    assert jar.store(host, "/", [None, "no-equals", "bad name=1"]) == []  # junk is skipped, not raised
