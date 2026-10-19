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
