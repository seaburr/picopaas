"""Coverage pass: failure branches and pure helpers that the feature tests skip."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import crypto, db_host_metrics as hm, image_check, models, quota
from app.providers import mysql as mysql_mod
from app.providers import postgres as pg_mod
from app.providers.base import ImageInfo, RegistryError
from app.providers.fake import FakeMySQLAdmin, FakeObjectStore, FakeRegistry
from cli import picopaas
from tests.test_quota import _setup

# --- cli helpers ---


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def test_cli_retry_text():
    assert picopaas._retry_text(None) is None
    assert picopaas._retry_text("soonish") == "soonish"
    assert picopaas._retry_text(_iso(timedelta(seconds=-5))) == "any moment now"
    assert picopaas._retry_text(_iso(timedelta(seconds=30))) == "in under a minute"
    assert picopaas._retry_text(_iso(timedelta(minutes=5, seconds=-1))).startswith("in about ")
    naive = (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(tzinfo=None).isoformat()
    assert picopaas._retry_text(naive).startswith("in about ")


def test_cli_ago_text():
    assert picopaas._ago_text("nope") == "nope"
    assert picopaas._ago_text(_iso(timedelta(seconds=-5))) == "just now"
    assert picopaas._ago_text(_iso(timedelta(minutes=-7))) == "7 min ago"
    assert picopaas._ago_text(_iso(timedelta(hours=-3)).replace("+00:00", "Z")) == "3 h ago"
    naive = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None).isoformat()
    assert picopaas._ago_text(naive) == "2 h ago"


def test_cli_validation_line_and_detail():
    assert picopaas._validation_line("plain") == "plain"
    line = picopaas._validation_line({"loc": ["body", "port"], "msg": "Value error, bad port"})
    assert line == "port: bad port"
    assert picopaas._validation_line({"loc": ["body"], "msg": "oops"}) == "oops"
    resp = httpx.Response(422, json={"detail": [{"loc": ["body", "name"], "msg": "too short"}, "x"]})
    assert picopaas._detail(resp) == "name: too short; x"
    assert picopaas._detail(httpx.Response(500, text="boom")) == "boom"
    assert picopaas._detail(httpx.Response(502)) == "Bad Gateway"


# --- quota failure branches ---


def test_check_app_quota_swallows_failures(db_session):
    _, (app,) = _setup(db_session)

    class Boom(FakeMySQLAdmin):
        def db_size_bytes(self, app_id):
            raise RuntimeError("db down")

    class BoomStore(FakeObjectStore):
        def bucket_size_bytes(self, app_id):
            raise RuntimeError("spaces down")

    quota.check_app_quota(db_session, app, mysql_admin_factory=lambda s: Boom(), object_store=BoomStore())
    # the session is still usable afterwards
    assert db_session.get(models.App, app.id) is not None


def test_check_app_quota_without_object_store_only_checks_db(db_session):
    _, (app,) = _setup(db_session)
    admin = FakeMySQLAdmin()
    admin.sizes[app.id] = quota.DB_QUOTA_BYTES + 1
    quota.check_app_quota(db_session, app, mysql_admin_factory=lambda s: admin, object_store=None)
    assert app.database.over_quota


def test_poll_db_unreadable_password_still_enforces(db_session):
    _, (app,) = _setup(db_session)
    app.database.password_encrypted = "not-a-fernet-token"
    db_session.commit()
    admin = FakeMySQLAdmin()
    admin.sizes[app.id] = quota.DB_QUOTA_BYTES + 1
    assert quota._poll_db(db_session, app.database, admin) is True
    assert admin.read_only[app.id] is True


def test_bounce_app_skips_suspended_pending_and_none():
    quota._bounce_app(None)
    for status in ("suspended", "pending"):
        a = models.App(status=status, restart_nonce=None)
        quota._bounce_app(a)
        assert a.restart_nonce is None
    a = models.App(status="running", restart_nonce=None)
    quota._bounce_app(a)
    assert a.status == "pending" and a.restart_nonce


# --- image_check ---


def test_evaluate_per_app_cap_override():
    from app.config import MAX_IMAGE_SIZE_BYTES

    info = ImageInfo(MAX_IMAGE_SIZE_BYTES + 1024 * 1024, "10001", "sha256:" + "a" * 64)
    with pytest.raises(image_check.ImageRejected):
        image_check.evaluate(info)
    assert image_check.evaluate(info, max_bytes=MAX_IMAGE_SIZE_BYTES * 2) is None
    with pytest.raises(image_check.ImageRejected, match="limit is 1 MB"):
        image_check.evaluate(ImageInfo(3 * 1024 * 1024, "10001", "d"), max_bytes=1024 * 1024)
    with pytest.raises(image_check.ImageRejected):
        image_check.evaluate(ImageInfo(1, "root", "d"))
    assert image_check.evaluate(ImageInfo(1, "node", "d")) == image_check.NAMED_USER_RUN_AS_UID


def test_image_ref_helpers():
    assert image_check.image_tag("a/b@sha256:" + "1" * 64) == ""
    assert image_check.image_tag("a/b") == "latest"
    assert image_check.image_tag("host:5000/b") == "latest"
    assert image_check.image_tag("a/b:2") == "2"
    assert image_check.image_repo("host:5000/b:2") == "host:5000/b"
    assert image_check.digest_of_ref("a/b") is None
    assert image_check.short_digest(None) == ""


def _tagged_app(db_session, digest=None):
    user = models.User(email="ic@example.com", password_hash="x")
    db_session.add(user)
    db_session.commit()
    app = models.App(owner_id=user.id, name="ic", hostname="ic", image="ghcr.io/e/d:latest", port=80,
                     status="running", image_digest=digest, auto_update=True)
    db_session.add(app)
    db_session.commit()
    return app


def test_refresh_pinned_and_registry_errors(db_session):
    app = _tagged_app(db_session)
    reg = FakeRegistry(default_size_bytes=10)
    app.image = "ghcr.io/e/d@sha256:" + "1" * 64
    assert image_check.refresh_app_image(db_session, app, reg) == "pinned"
    app.image = "ghcr.io/e/d:latest"
    reg.failing.add(app.image)
    assert image_check.refresh_app_image(db_session, app, reg) == "error"

    # resolve works, inspect fails
    class InspectFails(FakeRegistry):
        def inspect(self, ref):
            raise RegistryError("inspect down")

    assert image_check.refresh_app_image(db_session, app, InspectFails(default_size_bytes=10)) == "error"


def test_watch_all_survives_unexpected_exception(db_session):
    _tagged_app(db_session)

    class Explodes(FakeRegistry):
        def resolve_digest(self, ref):
            raise ValueError("unexpected")

    assert image_check.watch_all(db_session, Explodes(default_size_bytes=1)) == {}


# --- db host metrics ---


def test_parse_metrics_skips_nan_and_bad_values():
    out = hm.parse_metrics("# c\n\ncpu_usage_idle{cpu=\"cpu-total\"} NaN\nmem_total 1e999x\nmem_total 5\n")
    assert out == {"mem_total": [({}, 5.0)]}


def test_compute_cpu_active_fallback():
    got = hm.compute('cpu_usage_active{cpu="cpu-total"} 42.5\ncpu_usage_active{cpu="cpu0"} 1\n')
    assert got["cpu_pct"] == 42.5


def _t(creds_status=200, creds_body=None, metrics=None):
    def h(req: httpx.Request):
        if req.url.host == "api.digitalocean.com":
            body = {"credentials": {"basic_auth_username": "u", "basic_auth_password": "not-the-password"}}
            return httpx.Response(creds_status, json=creds_body if creds_body is not None else body)
        return metrics(req)
    return httpx.MockTransport(h)


def test_fetch_credential_failures():
    hm.reset_credentials()
    with pytest.raises(hm.HostMetricsError, match="credentials request failed"):
        hm.fetch_host_metrics("db.example", token="t", transport=_t(creds_status=403))
    hm.reset_credentials()
    with pytest.raises(hm.HostMetricsError, match="credentials missing"):
        hm.fetch_host_metrics("db.example", token="t", transport=_t(creds_body={"credentials": {}}))


def test_fetch_401_resets_credentials_and_unreachable_tries_both_hosts():
    hm.reset_credentials()
    with pytest.raises(hm.HostMetricsError, match="rejected credentials"):
        hm.fetch_host_metrics("db.example", token="t", transport=_t(metrics=lambda r: httpx.Response(401)))
    assert hm._cred["val"] is None
    hosts = []

    def refuse(req):
        hosts.append(req.url.host)
        raise httpx.ConnectError("no route")

    with pytest.raises(hm.HostMetricsError, match="unreachable"):
        hm.fetch_host_metrics("private-db.example", token="t", transport=_t(metrics=refuse))
    assert hosts == ["private-db.example", "db.example"]
    hm.reset_credentials()


# --- provider pure helpers ---


def test_mysql_helpers():
    assert mysql_mod._db_name(7) == mysql_mod._db_user(7) == "app_7"
    assert mysql_mod._validate_identifier("app_7") == "app_7"
    with pytest.raises(mysql_mod.MySQLAdminError):
        mysql_mod._validate_identifier("app_7; DROP")
    pw = mysql_mod._generate_password()
    assert len(pw) == mysql_mod._PASSWORD_LENGTH and pw != mysql_mod._generate_password()


def test_postgres_helpers():
    assert pg_mod._ident(12) == "app_12"
    with pytest.raises(Exception):
        pg_mod._ident("1; DROP")  # int() rejects it before it reaches SQL
    p1, p2 = pg_mod._ca_path("PEM-A"), pg_mod._ca_path("PEM-A")
    assert p1 == p2 and open(p1).read() == "PEM-A"
    assert pg_mod._ca_path("PEM-B") != p1


# --- admin limit overrides: failure paths ---

from app.main import app as fastapi_app  # noqa: E402
from app.providers.fake import FakePostgresAdmin  # noqa: E402
from app.routers.apps import get_mysql_admin_factory, get_object_store  # noqa: E402
from app.spec import SpecError, parse_spec  # noqa: E402
from tests.conftest import register_and_login  # noqa: E402
from tests.test_admin_limits import APP, _patch, _with_resources  # noqa: E402


def test_conn_limit_apply_failure_does_not_fail_the_patch(make_client, db_session):
    class Flaky(FakeMySQLAdmin):
        def set_user_connection_limit(self, app_id, limit=None):
            raise RuntimeError("server unreachable")

    store = FakeObjectStore()
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda server: Flaky())
    fastapi_app.dependency_overrides[get_object_store] = lambda: store
    c = make_client()
    register_and_login(c, "flaky@example.com")
    aid = c.post("/apps", json=APP).json()["id"]
    _with_resources(db_session, aid)
    r = _patch(c, aid, db_max_connections=7)
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.App, aid).db_max_connections == 7  # saved; the reconciler sweep re-applies it


def test_quota_check_after_raise_survives_unreadable_storage(make_client, db_session):
    class BoomStore(FakeObjectStore):
        def bucket_size_bytes(self, app_id):
            raise RuntimeError("spaces down")

    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda server: FakeMySQLAdmin())
    fastapi_app.dependency_overrides[get_object_store] = lambda: BoomStore()
    c = make_client()
    register_and_login(c, "boom@example.com")
    aid = c.post("/apps", json=APP).json()["id"]
    _with_resources(db_session, aid)
    assert _patch(c, aid, storage_quota_mb=500).status_code == 200


# --- quota: commit failure drops the unpersisted key; stale key revoke failure is logged ---


def test_storage_commit_failure_discards_new_key(db_session, monkeypatch):
    _, (app,) = _setup(db_session)
    store = FakeObjectStore()
    store.create_bucket_and_key(app.id)
    store.sizes[app.id] = quota.STORAGE_QUOTA_BYTES + 1
    deleted = []
    monkeypatch.setattr(store, "delete_key", lambda aid, key: deleted.append(key))
    monkeypatch.setattr(db_session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("db gone")))
    with pytest.raises(RuntimeError):
        quota._poll_storage(db_session, app.storage, store)
    assert len(deleted) == 1
    # and if even the cleanup fails, the original error still propagates
    monkeypatch.setattr(store, "delete_key", lambda aid, key: (_ for _ in ()).throw(RuntimeError("x")))
    store.sizes[app.id] = quota.STORAGE_QUOTA_BYTES + 1
    app.storage.over_quota = False
    with pytest.raises(RuntimeError, match="db gone"):
        quota._poll_storage(db_session, app.storage, store)


def test_revoke_stale_keys_rules(db_session, monkeypatch):
    _, (app,) = _setup(db_session)
    store = FakeObjectStore()
    calls = []
    monkeypatch.setattr(store, "revoke_other_keys", lambda aid, keep: calls.append(keep))
    app.status = "pending"
    quota.revoke_stale_keys(app.storage, store)
    assert calls == []  # waits for the rollout
    app.status = "running"
    quota.revoke_stale_keys(app.storage, store)
    assert calls == ["old"]
    monkeypatch.setattr(store, "revoke_other_keys", lambda aid, keep: (_ for _ in ()).throw(RuntimeError("x")))
    quota.revoke_stale_keys(app.storage, store)  # logged, not raised


# --- spec: healthCheck.timeoutSeconds ---


def _spec(timeout) -> str:
    return (f"apiVersion: picopaas/v1\nname: demo\nimage: ghcr.io/e/d:1\nport: 8080\n"
            f"healthCheck:\n  enabled: true\n  path: /up\n  timeoutSeconds: {timeout}\n")


def test_spec_health_timeout_bounds_and_type():
    assert parse_spec(_spec(30)).healthCheck.timeoutSeconds == 30
    for bad in (0, 91):
        with pytest.raises(SpecError, match="out of range"):
            parse_spec(_spec(bad))
    with pytest.raises(SpecError, match="wrong type"):
        parse_spec(_spec("'soon'"))
