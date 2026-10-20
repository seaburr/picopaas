"""Quota enforcement hardening: session kills, key-revoke ordering, recovery endpoints, AppOut.quota."""
from __future__ import annotations

import logging

from app import crypto, models
from app.main import app as fastapi_app
from app.providers.fake import FakeMySQLAdmin, FakeObjectStore
from app.quota import DB_QUOTA_BYTES, STORAGE_QUOTA_BYTES, poll_quotas
from app.routers.apps import get_mysql_admin_factory, get_object_store
from tests.conftest import register_and_login
from tests.test_quota import _setup

MB = 1024 * 1024


def _poll(db_session, admin, store, **kw):
    poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=store, **kw)


def test_db_over_cap_is_re_enforced_every_poll(db_session, caplog):
    caplog.set_level(logging.WARNING)
    _, (app,) = _setup(db_session)
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    admin.sizes[app.id] = DB_QUOTA_BYTES + 1
    _poll(db_session, admin, store)
    _poll(db_session, admin, store)
    _poll(db_session, admin, store)
    assert admin.calls == [(app.id, True)] * 3  # still over: sessions killed again on each poll
    assert "db write access REVOKED" in caplog.text and "killed 1, failed 0" in caplog.text


def test_unkillable_sessions_roll_the_app_once(db_session, caplog):
    caplog.set_level(logging.WARNING)
    _, (app,) = _setup(db_session)
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    admin.kill_result = {"listed": 2, "killed": 0, "failed": 2, "list_error": False}
    admin.sizes[app.id] = DB_QUOTA_BYTES + 1
    _poll(db_session, admin, store)
    assert app.status == "pending" and app.restart_nonce
    assert "could not confirm open db sessions were killed" in caplog.text and "failed 2" in caplog.text
    nonce = app.restart_nonce
    app.status = "running"
    db_session.commit()
    _poll(db_session, admin, store)  # not a flip: no second roll
    assert app.restart_nonce == nonce and app.status == "running"


def test_clean_kill_does_not_roll_the_app(db_session):
    _, (app,) = _setup(db_session)
    admin = FakeMySQLAdmin()
    admin.sizes[app.id] = DB_QUOTA_BYTES + 1
    _poll(db_session, admin, FakeObjectStore())
    assert app.status == "running"


def test_db_and_storage_polls_are_separable(db_session):
    _, (app,) = _setup(db_session)
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    store.create_bucket_and_key(app.id)
    admin.sizes[app.id] = DB_QUOTA_BYTES + 1
    store.sizes[app.id] = STORAGE_QUOTA_BYTES + 1
    _poll(db_session, admin, store, storage=False)
    assert app.database.over_quota and not app.storage.over_quota
    _poll(db_session, admin, store, databases=False)
    assert app.storage.over_quota


def test_old_key_survives_until_rollout_finishes(db_session):
    _, (app,) = _setup(db_session)
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    orig = store.create_bucket_and_key(app.id)
    app.storage.access_key = orig["access_key"]
    app.storage.secret_key_encrypted = crypto.encrypt(orig["secret_key"])
    db_session.commit()
    store.sizes[app.id] = STORAGE_QUOTA_BYTES + 1
    _poll(db_session, admin, store)
    new = app.storage.access_key
    assert app.status == "pending" and store.valid_keys[app.id] == {orig["access_key"], new}
    _poll(db_session, admin, store)  # still rolling out
    assert orig["access_key"] in store.valid_keys[app.id]
    app.status = "running"
    db_session.commit()
    _poll(db_session, admin, store)
    assert store.valid_keys[app.id] == {new}


# --- API ---

def _api_app(make_client, db_session, email="o@example.com"):
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda server: admin)
    fastapi_app.dependency_overrides[get_object_store] = lambda: store
    c = make_client()
    register_and_login(c, email)
    r = c.post("/apps", json={"name": "demo", "image": "example/demo:1", "port": 8080,
                              "want_db": True, "want_storage": True})
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]
    server = models.DbServer(name="s", host="h", port=3306, admin_user="r",
                             admin_password_encrypted=crypto.encrypt("x"))
    db_session.add(server)
    db_session.commit()
    creds = store.create_bucket_and_key(app_id)
    db_session.add(models.AppDatabase(app_id=app_id, db_server_id=server.id, db_name=f"app_{app_id}",
                                      db_user=f"app_{app_id}", password_encrypted=crypto.encrypt("p")))
    db_session.add(models.AppStorage(app_id=app_id, bucket=creds["bucket"], endpoint="e", region="r",
                                     access_key=creds["access_key"],
                                     secret_key_encrypted=crypto.encrypt(creds["secret_key"])))
    db_session.commit()
    return c, app_id, admin, store


def test_app_out_quota_from_stored_rows(make_client, db_session):
    c, app_id, admin, store = _api_app(make_client, db_session)
    row = db_session.get(models.App, app_id).database
    row.size_bytes, row.over_quota = DB_QUOTA_BYTES + 5, True
    db_session.commit()
    q = c.get(f"/apps/{app_id}").json()["quota"]
    assert q["db"] == {"size_bytes": DB_QUOTA_BYTES + 5, "limit_bytes": DB_QUOTA_BYTES, "over_quota": True}
    assert q["storage"] == {"size_bytes": 0, "limit_bytes": STORAGE_QUOTA_BYTES, "over_quota": False}
    assert c.get("/apps").json()[0]["quota"]["db"]["over_quota"] is True


def test_app_out_quota_null_without_resources(client):
    register_and_login(client, "o@example.com")
    r = client.post("/apps", json={"name": "demo", "image": "example/demo:1", "port": 8080})
    assert r.json()["quota"] == {"db": None, "storage": None}


def test_usage_endpoints(make_client, db_session):
    c, app_id, admin, store = _api_app(make_client, db_session)
    admin.sizes[app_id] = 3 * MB
    assert c.get(f"/apps/{app_id}/database/usage").json() == {
        "size_bytes": 3 * MB, "limit_bytes": DB_QUOTA_BYTES, "over_quota": False}
    store.put_object(app_id, "img/a.png", 2 * MB)
    store.put_object(app_id, "img/b.png", 1 * MB)
    store.put_object(app_id, "root.txt", 10)
    u = c.get(f"/apps/{app_id}/storage/usage").json()
    assert u["size_bytes"] == 3 * MB + 10 and u["objects"] == 3 and not u["over_quota"]
    assert u["top_prefixes"][0] == {"prefix": "img/", "size_bytes": 3 * MB, "objects": 2}


def test_usage_hidden_from_other_users(make_client, db_session):
    c, app_id, admin, store = _api_app(make_client, db_session)
    other = make_client()
    register_and_login(other, "x@example.com")
    for path in ("database/usage", "storage/usage"):
        assert other.get(f"/apps/{app_id}/{path}").status_code == 404
    assert other.post(f"/apps/{app_id}/storage/delete", json={"all": True}).status_code == 404


def test_storage_delete_validates_body(make_client, db_session):
    c, app_id, *_ = _api_app(make_client, db_session)
    url = f"/apps/{app_id}/storage/delete"
    for body in ({}, {"all": False}, {"prefix": "a/", "all": True}, {"prefix": ""}, {"all": True, "x": 1}):
        assert c.post(url, json=body).status_code == 422, body


def test_storage_delete_by_prefix_then_all_restores_rw_key(make_client, db_session):
    c, app_id, admin, store = _api_app(make_client, db_session)
    store.put_object(app_id, "big/blob", STORAGE_QUOTA_BYTES)
    store.put_object(app_id, "keep/x", 5 * MB)
    _poll(db_session, admin, store)
    srow = db_session.get(models.App, app_id).storage
    assert srow.over_quota and store.read_only[app_id] is True
    ro_key = srow.access_key

    r = c.post(f"/apps/{app_id}/storage/delete", json={"prefix": "big/"})
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 1, "size_bytes": 5 * MB, "truncated": False, "over_quota": False}
    db_session.expire_all()
    srow = db_session.get(models.App, app_id).storage
    # under the cap: read-write key restored immediately (new key persisted, app rolls), not at the next poll
    assert not srow.over_quota and store.read_only[app_id] is False and srow.access_key != ro_key
    assert db_session.get(models.App, app_id).status == "pending"

    r = c.post(f"/apps/{app_id}/storage/delete", json={"all": True})
    assert r.json()["deleted"] == 1 and r.json()["size_bytes"] == 0
    actions = [a.action for a in db_session.query(models.AuditLog).all()]
    assert actions.count("app.storage.delete") == 2


def test_storage_delete_still_over_keeps_read_only(make_client, db_session):
    c, app_id, admin, store = _api_app(make_client, db_session)
    store.put_object(app_id, "a", STORAGE_QUOTA_BYTES + MB)
    store.put_object(app_id, "b/x", 2 * MB)
    _poll(db_session, admin, store)
    r = c.post(f"/apps/{app_id}/storage/delete", json={"prefix": "b/"})
    assert r.json()["over_quota"] is True and store.read_only[app_id] is True


def test_storage_delete_is_capped_and_reports_truncated(make_client, db_session, monkeypatch):
    from app.routers import usage

    monkeypatch.setattr(usage, "MAX_DELETE_PER_CALL", 2)
    c, app_id, admin, store = _api_app(make_client, db_session)
    for i in range(5):
        store.put_object(app_id, f"k{i}", 10)
    r = c.post(f"/apps/{app_id}/storage/delete", json={"all": True}).json()
    assert r["deleted"] == 2 and r["truncated"] is True and r["size_bytes"] == 30


def test_cli_status_shows_usage_and_storage_delete(monkeypatch, capsys):
    import json

    import httpx
    import pytest

    from cli import picopaas

    seen = []
    app = {"id": 7, "name": "demo", "hostname": "h", "status": "running", "image": "i",
           "quota": {"db": {"size_bytes": 2 * MB, "limit_bytes": 50 * MB, "over_quota": False},
                     "storage": {"size_bytes": 101 * MB, "limit_bytes": 100 * MB, "over_quota": True}}}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/apps":
            return httpx.Response(200, json=[app])
        if req.url.path == "/apps/7/storage/delete":
            return httpx.Response(200, json={"deleted": 9, "size_bytes": 0, "truncated": False, "over_quota": False})
        return httpx.Response(404, json={"detail": "x"})

    t = httpx.MockTransport(handler)
    monkeypatch.setenv("PICOPAAS_TOKEN", "pp_sk_test")
    assert picopaas.main(["status", "demo"], transport=t) == 0
    out = capsys.readouterr().out
    assert "2.0 / 50.0 MB" in out and "OVER CAP" in out and "storage-delete demo --all" in out
    assert picopaas.main(["storage-delete", "demo", "--prefix", "tmp/"], transport=t) == 0
    assert json.loads(seen[-1].content) == {"prefix": "tmp/"} and "Deleted 9 objects" in capsys.readouterr().out
    assert picopaas.main(["storage-delete", "demo", "--all"], transport=t) == 0
    assert json.loads(seen[-1].content) == {"all": True}
    with pytest.raises(SystemExit):
        picopaas.build_parser().parse_args(["storage-delete", "demo"])


def test_app_user_kill_path_is_preferred_and_verified(db_session, caplog):
    caplog.set_level(logging.WARNING)
    _, (app,) = _setup(db_session)
    admin = FakeMySQLAdmin()
    admin.app_user_kill_ok = True
    admin.kill_result = {"listed": 0, "killed": 0, "failed": 1, "list_error": False}  # admin path would roll
    admin.sizes[app.id] = DB_QUOTA_BYTES + 1
    _poll(db_session, admin, FakeObjectStore())
    assert admin.passwords_seen[0] == crypto.decrypt(app.database.password_encrypted)
    assert app.status == "running" and "via app-user" in caplog.text


def test_app_user_failure_falls_back_to_pod_roll(db_session):
    _, (app,) = _setup(db_session)
    admin = FakeMySQLAdmin()
    admin.kill_result = {"listed": 2, "killed": 0, "failed": 2, "list_error": False}
    admin.sizes[app.id] = DB_QUOTA_BYTES + 1
    _poll(db_session, admin, FakeObjectStore())
    assert app.status == "pending"
