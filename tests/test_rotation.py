"""POST /apps/{id}/rotate-credentials with fake providers and the real login flow."""
from __future__ import annotations

from app import crypto, models
from app.main import app as fastapi_app
from app.providers.fake import FakeMySQLAdmin, FakeObjectStore
from app.routers.apps import get_mysql_admin_factory, get_object_store
from tests.conftest import register_and_login


def _create_app(client):
    r = client.post("/apps", json={"name": "demo", "image": "example/demo:latest", "port": 8080,
                                   "want_db": True, "want_storage": True})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _provision(db_session, app_id):
    server = models.DbServer(name="s", host="h", port=3306, admin_user="r",
                             admin_password_encrypted=crypto.encrypt("x"))
    db_session.add(server)
    db_session.commit()
    db_session.add(models.AppDatabase(app_id=app_id, db_server_id=server.id, db_name="d", db_user="u",
                                      password_encrypted=crypto.encrypt("oldpw")))
    db_session.add(models.AppStorage(app_id=app_id, bucket="b", endpoint="e", region="r",
                                     access_key="oldkey", secret_key_encrypted=crypto.encrypt("oldsecret")))
    a = db_session.get(models.App, app_id)
    a.status = "running"
    db_session.commit()


def test_rotate_db_and_storage(make_client, db_session):
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda server: admin)
    fastapi_app.dependency_overrides[get_object_store] = lambda: store
    c = make_client()
    register_and_login(c, "o@example.com")
    app_id = _create_app(c)
    _provision(db_session, app_id)

    r = c.post(f"/apps/{app_id}/rotate-credentials", json={"target": "db"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    row = db_session.get(models.App, app_id).database
    assert crypto.decrypt(row.password_encrypted) == admin.creds[app_id]["password"] != "oldpw"

    db_session.get(models.App, app_id).status = "running"
    db_session.commit()
    r = c.post(f"/apps/{app_id}/rotate-credentials", json={"target": "storage"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    srow = db_session.get(models.App, app_id).storage
    assert srow.access_key == store.creds[app_id]["access_key"] != "oldkey"
    assert crypto.decrypt(srow.secret_key_encrypted) == store.creds[app_id]["secret_key"]


def test_rotate_404_other_user_409_unprovisioned_422_bad_target(make_client, db_session):
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda server: FakeMySQLAdmin())
    fastapi_app.dependency_overrides[get_object_store] = lambda: FakeObjectStore()
    owner, other = make_client(), make_client()
    register_and_login(owner, "o@example.com")
    register_and_login(other, "x@example.com")
    app_id = _create_app(owner)
    assert owner.post(f"/apps/{app_id}/rotate-credentials", json={"target": "db"}).status_code == 409
    assert owner.post(f"/apps/{app_id}/rotate-credentials", json={"target": "storage"}).status_code == 409
    assert owner.post(f"/apps/{app_id}/rotate-credentials", json={"target": "nope"}).status_code == 422
    assert other.post(f"/apps/{app_id}/rotate-credentials", json={"target": "db"}).status_code == 404


def _setup(make_client, db_session, admin, store):
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda server: admin)
    fastapi_app.dependency_overrides[get_object_store] = lambda: store
    c = make_client()
    register_and_login(c, "o@example.com")
    app_id = _create_app(c)
    _provision(db_session, app_id)
    # Make the fakes agree with the stored (old) creds.
    admin.creds[app_id] = {"host": "h", "port": 3306, "name": "d", "user": "u", "password": "oldpw", "url": ""}
    store.creds[app_id] = {"bucket": "b", "access_key": "oldkey", "secret_key": "oldsecret"}
    store.valid_keys[app_id] = {"oldkey"}
    return c, app_id


def _stored_db_pw(db_session, app_id):
    db_session.expire_all()
    return crypto.decrypt(db_session.get(models.App, app_id).database.password_encrypted)


def _stored_key(db_session, app_id):
    db_session.expire_all()
    return db_session.get(models.App, app_id).storage.access_key


def test_db_rotation_alter_failure_restores_old_password(make_client, db_session):
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    c, app_id = _setup(make_client, db_session, admin, store)
    admin.fail_set_password = True
    assert c.post(f"/apps/{app_id}/rotate-credentials", json={"target": "db"}).status_code == 502
    assert _stored_db_pw(db_session, app_id) == "oldpw" == admin.creds[app_id]["password"]


def test_db_rotation_commit_failure_leaves_mysql_untouched(make_client, db_session, monkeypatch):
    from sqlalchemy.orm import Session

    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    c, app_id = _setup(make_client, db_session, admin, store)
    monkeypatch.setattr(Session, "commit", lambda self: (_ for _ in ()).throw(RuntimeError("db down")))
    assert c.post(f"/apps/{app_id}/rotate-credentials", json={"target": "db"}).status_code == 502
    monkeypatch.undo()
    assert admin.creds[app_id]["password"] == "oldpw" == _stored_db_pw(db_session, app_id)


def test_db_rotation_success_stored_matches_mysql(make_client, db_session):
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    c, app_id = _setup(make_client, db_session, admin, store)
    assert c.post(f"/apps/{app_id}/rotate-credentials", json={"target": "db"}).status_code == 200
    assert _stored_db_pw(db_session, app_id) == admin.creds[app_id]["password"] != "oldpw"


def test_storage_rotation_create_failure_keeps_old_key(make_client, db_session):
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    c, app_id = _setup(make_client, db_session, admin, store)
    store.fail.add("create")
    assert c.post(f"/apps/{app_id}/rotate-credentials", json={"target": "storage"}).status_code == 502
    assert _stored_key(db_session, app_id) == "oldkey" and store.valid_keys[app_id] == {"oldkey"}


def test_storage_rotation_commit_failure_discards_new_key(make_client, db_session, monkeypatch):
    from sqlalchemy.orm import Session

    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    c, app_id = _setup(make_client, db_session, admin, store)
    monkeypatch.setattr(Session, "commit", lambda self: (_ for _ in ()).throw(RuntimeError("db down")))
    assert c.post(f"/apps/{app_id}/rotate-credentials", json={"target": "storage"}).status_code == 502
    monkeypatch.undo()
    assert _stored_key(db_session, app_id) == "oldkey" and store.valid_keys[app_id] == {"oldkey"}


def test_storage_rotation_commit_and_discard_both_fail_old_key_still_valid(make_client, db_session, monkeypatch):
    from sqlalchemy.orm import Session

    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    c, app_id = _setup(make_client, db_session, admin, store)
    store.fail.add("delete_key")
    monkeypatch.setattr(Session, "commit", lambda self: (_ for _ in ()).throw(RuntimeError("db down")))
    assert c.post(f"/apps/{app_id}/rotate-credentials", json={"target": "storage"}).status_code == 502
    monkeypatch.undo()
    assert _stored_key(db_session, app_id) in store.valid_keys[app_id]  # stored key works


def test_storage_rotation_revoke_failure_still_succeeds_with_working_stored_key(make_client, db_session):
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    c, app_id = _setup(make_client, db_session, admin, store)
    store.fail.add("revoke")
    assert c.post(f"/apps/{app_id}/rotate-credentials", json={"target": "storage"}).status_code == 200
    stored = _stored_key(db_session, app_id)
    assert stored != "oldkey" and stored in store.valid_keys[app_id]
    store.fail.clear()  # a later revoke (next rotation / quota poll) cleans the leftover
    store.revoke_other_keys(app_id, stored)
    assert store.valid_keys[app_id] == {stored}
