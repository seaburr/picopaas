"""Docs claim: credential rotation repairs an app whose stored secrets are unreadable (USER_GUIDE section 9)."""
from __future__ import annotations

from datetime import datetime, timedelta

from cryptography.fernet import Fernet

from app import crypto, models
from app.main import app as fastapi_app
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import reconcile_all
from app.routers.apps import get_mysql_admin_factory, get_object_store
from tests.conftest import register_and_login

OTHER_KEY_TOKEN = Fernet(Fernet.generate_key()).encrypt(b"old-value").decode()


def _setup(make_client, db_session):
    mysql, store, kube = FakeMySQLAdmin(), FakeObjectStore(), FakeKubeProvider()
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda s: mysql)
    fastapi_app.dependency_overrides[get_object_store] = lambda: store
    db_session.add(models.DbServer(name="s", host="h", port=3306, admin_user="r",
                                   admin_password_encrypted=crypto.encrypt("x")))
    db_session.commit()
    c = make_client()
    register_and_login(c, "o@example.com")
    r = c.post("/apps", json={"name": "demo", "image": "example/demo:latest", "port": 8080,
                              "want_db": True, "want_storage": True,
                              "env": [{"key": "S", "value": "sec", "is_secret": True}]})
    assert r.status_code == 201, r.text
    return c, r.json()["id"], dict(kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store, settings={})


def _readable(db_session, app_id):
    a = db_session.get(models.App, app_id)
    db_session.refresh(a)
    return (crypto.try_decrypt(a.database.password_encrypted) is not None
            and crypto.try_decrypt(a.storage.secret_key_encrypted) is not None)


def _running(c, db_session, prov, app_id):
    for i in range(3):
        reconcile_all(db_session, **prov)
    assert c.get(f"/apps/{app_id}").json()["status"] == "running"


def _corrupt_resources(db_session, app_id):
    a = db_session.get(models.App, app_id)
    a.database.password_encrypted = OTHER_KEY_TOKEN
    a.storage.secret_key_encrypted = OTHER_KEY_TOKEN
    a.status = "pending"
    db_session.commit()


def test_rotation_repairs_unreadable_db_and_storage_secrets(make_client, db_session):
    c, app_id, prov = _setup(make_client, db_session)
    _running(c, db_session, prov, app_id)
    _corrupt_resources(db_session, app_id)

    reconcile_all(db_session, **prov)
    body = c.get(f"/apps/{app_id}").json()
    assert body["status"] == "error" and body["status_reason"] == "unreadable_secrets"
    a = db_session.get(models.App, app_id)
    db_session.refresh(a)
    assert a.error_retryable is True

    for target in ("db", "storage"):
        r = c.post(f"/apps/{app_id}/rotate-credentials", json={"target": target})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"
    assert _readable(db_session, app_id)

    reconcile_all(db_session, **prov)
    reconcile_all(db_session, **prov)
    assert c.get(f"/apps/{app_id}").json()["status"] == "running"


def test_rotation_does_not_repair_unreadable_env_var(make_client, db_session):
    """Documents behaviour: an undecryptable env var keeps the app failed; the user must re-enter it."""
    c, app_id, prov = _setup(make_client, db_session)
    _running(c, db_session, prov, app_id)
    _corrupt_resources(db_session, app_id)
    for v in db_session.get(models.App, app_id).env_vars:
        v.value_encrypted = OTHER_KEY_TOKEN
    db_session.commit()

    for target in ("db", "storage"):
        assert c.post(f"/apps/{app_id}/rotate-credentials", json={"target": target}).status_code == 200
    reconcile_all(db_session, **prov)
    body = c.get(f"/apps/{app_id}").json()
    assert body["status"] == "error" and body["status_reason"] == "unreadable_secrets"
    assert [e["undecryptable"] for e in body["env"]] == [True]

    # Re-entering the value fixes it.
    r = c.patch(f"/apps/{app_id}", json={"env": [{"key": "S", "value": "again", "is_secret": True}]})
    assert r.status_code == 200, r.text
    reconcile_all(db_session, **prov)
    reconcile_all(db_session, **prov)
    assert c.get(f"/apps/{app_id}").json()["status"] == "running"
