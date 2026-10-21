"""/apps/{id}/database/usage and /storage/*: missing resource, provider failure, cross-user, over-quota."""
import pytest

from app import config, crypto, models
from app.main import app as fastapi_app
from app.providers.base import MySQLAdminError, ObjectStoreError
from app.providers.fake import FakeMySQLAdmin, FakeObjectStore
from app.routers.apps import get_mysql_admin_factory, get_object_store
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


class Ctx:
    def __init__(self):
        self.mysql, self.store = FakeMySQLAdmin(), FakeObjectStore()


@pytest.fixture()
def ctx():
    c = Ctx()
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda s: c.mysql)
    fastapi_app.dependency_overrides[get_object_store] = lambda: c.store
    yield c
    fastapi_app.dependency_overrides.pop(get_mysql_admin_factory, None)
    fastapi_app.dependency_overrides.pop(get_object_store, None)


@pytest.fixture()
def owner(make_client):
    c = make_client()
    register_and_login(c, "owner@example.com")
    return c


def _provision(db_session, aid):
    server = models.DbServer(name="p", host="h", port=3306, admin_user="r", max_databases=5,
                             admin_password_encrypted=crypto.encrypt("x"))
    db_session.add(server)
    db_session.commit()
    db_session.add(models.AppDatabase(app_id=aid, db_server_id=server.id, db_name="d", db_user="u",
                                      password_encrypted=crypto.encrypt("p")))
    db_session.add(models.AppStorage(app_id=aid, bucket="b", endpoint="e", region="r", access_key="k",
                                     secret_key_encrypted=crypto.encrypt("s")))
    db_session.commit()


def test_no_db_or_bucket_is_404(owner, ctx):
    aid = owner.post("/apps", json=APP).json()["id"]
    assert owner.get(f"/apps/{aid}/database/usage").status_code == 404
    assert owner.get(f"/apps/{aid}/storage/usage").status_code == 404
    assert owner.post(f"/apps/{aid}/storage/delete", json={"all": True}).status_code == 404


def test_usage_reports_and_flags_over_quota(owner, ctx, db_session):
    aid = owner.post("/apps", json=APP).json()["id"]
    _provision(db_session, aid)
    ctx.mysql.sizes[aid] = config.DB_QUOTA_BYTES + 1
    r = owner.get(f"/apps/{aid}/database/usage").json()
    assert r["over_quota"] is True and r["limit_bytes"] == config.DB_QUOTA_BYTES
    ctx.mysql.sizes[aid] = config.DB_QUOTA_BYTES
    assert owner.get(f"/apps/{aid}/database/usage").json()["over_quota"] is False  # boundary: exactly at the cap is OK
    ctx.store.sizes[aid] = config.STORAGE_QUOTA_BYTES + 1
    assert owner.get(f"/apps/{aid}/storage/usage").json()["over_quota"] is True


def test_provider_errors_are_502_with_generic_text(owner, ctx, db_session):
    aid = owner.post("/apps", json=APP).json()["id"]
    _provision(db_session, aid)

    def mysql_boom(app_id):
        raise MySQLAdminError("host=10.0.0.5 password=hunter2")

    def store_boom(*a, **k):
        raise ObjectStoreError("secret=do-secret")

    ctx.mysql.db_size_bytes = mysql_boom
    ctx.store.usage = store_boom
    ctx.store.delete_objects = store_boom
    for method, path, body in (("get", "database/usage", None), ("get", "storage/usage", None),
                               ("post", "storage/delete", {"all": True})):
        r = getattr(owner, method)(f"/apps/{aid}/{path}", **({"json": body} if body else {}))
        assert r.status_code == 502 and "hunter2" not in r.text and "do-secret" not in r.text and "10.0.0.5" not in r.text


def test_other_users_cannot_read_or_delete(owner, ctx, db_session, make_client):
    aid = owner.post("/apps", json=APP).json()["id"]
    _provision(db_session, aid)
    ctx.store.put_object(aid, "a/1", 10)
    stranger = make_client()
    register_and_login(stranger, "stranger@example.com")
    assert stranger.get(f"/apps/{aid}/database/usage").status_code == 404
    assert stranger.get(f"/apps/{aid}/storage/usage").status_code == 404
    assert stranger.post(f"/apps/{aid}/storage/delete", json={"all": True}).status_code == 404
    assert ctx.store.objects[aid] == {"a/1": 10}  # untouched


@pytest.mark.parametrize("body", [{}, {"all": False}, {"prefix": "a/", "all": True}, {"prefix": ""}, {"all": True, "x": 1}])
def test_storage_delete_requires_exactly_one_selector(owner, ctx, db_session, body):
    aid = owner.post("/apps", json=APP).json()["id"]
    _provision(db_session, aid)
    ctx.store.put_object(aid, "a/1", 10)
    assert owner.post(f"/apps/{aid}/storage/delete", json=body).status_code == 422
    assert ctx.store.objects[aid] == {"a/1": 10}


def test_storage_delete_prefix_only_removes_that_prefix_and_is_audited(owner, ctx, db_session):
    aid = owner.post("/apps", json=APP).json()["id"]
    _provision(db_session, aid)
    ctx.store.put_object(aid, "keep/1", 10)
    ctx.store.put_object(aid, "tmp/1", 5)
    r = owner.post(f"/apps/{aid}/storage/delete", json={"prefix": "tmp/"})
    assert r.status_code == 200 and r.json()["deleted"] == 1 and r.json()["size_bytes"] == 10
    assert ctx.store.objects[aid] == {"keep/1": 10}
    entry = db_session.query(models.AuditLog).filter_by(action="app.storage.delete").one()
    assert "prefix=tmp/" in entry.detail and "deleted=1" in entry.detail


def test_storage_delete_survives_state_flip_failure(owner, ctx, db_session, monkeypatch):
    from app import quota

    aid = owner.post("/apps", json=APP).json()["id"]
    _provision(db_session, aid)
    ctx.store.put_object(aid, "x", 10)
    monkeypatch.setattr(quota, "apply_storage_state", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("spaces flaky")))
    r = owner.post(f"/apps/{aid}/storage/delete", json={"all": True})
    assert r.status_code == 200 and r.json()["over_quota"] is False
