"""A platform FERNET_KEY change must never break listings, rotation, PATCH, reconcile sweeps or deletes."""
from __future__ import annotations

from datetime import datetime, timezone

from app import crypto, models
from app.main import app as fastapi_app
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import UNREADABLE_DETAIL, reconcile_all
from app.routers.apps import get_mysql_admin_factory, get_object_store
from tests.conftest import register_and_login

BAD = "gAAAAAnot-a-valid-fernet-token"


def _setup(make_client, db_session, with_resources=False):
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda s: FakeMySQLAdmin())
    fastapi_app.dependency_overrides[get_object_store] = lambda: FakeObjectStore()
    c = make_client()
    register_and_login(c, "o@example.com")
    r = c.post("/apps", json={
        "name": "demo", "image": "example/demo:latest", "port": 8080,
        "want_db": with_resources, "want_storage": with_resources,
        "env": [{"key": "A", "value": "plain"}, {"key": "S", "value": "sec", "is_secret": True}],
    })
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]
    for v in db_session.get(models.App, app_id).env_vars:
        v.value_encrypted = BAD
    if with_resources:
        server = models.DbServer(name="s", host="h", port=3306, admin_user="r",
                                 admin_password_encrypted=crypto.encrypt("x"))
        db_session.add(server)
        db_session.commit()
        db_session.add(models.AppDatabase(app_id=app_id, db_server_id=server.id, db_name="d", db_user="u",
                                          password_encrypted=BAD))
        db_session.add(models.AppStorage(app_id=app_id, bucket="b", endpoint="e", region="r",
                                         access_key="k", secret_key_encrypted=BAD))
        db_session.get(models.App, app_id).status = "running"
    db_session.commit()
    return c, app_id


def test_listing_and_get_flag_undecryptable(make_client, db_session):
    c, app_id = _setup(make_client, db_session)
    for r in (c.get("/apps"), c.get(f"/apps/{app_id}")):
        assert r.status_code == 200, r.text
        body = r.json()
        env = (body[0] if isinstance(body, list) else body)["env"]
        by = {e["key"]: e for e in env}
        assert by["A"]["value"] == "" and by["A"]["undecryptable"] is True
        assert by["S"]["undecryptable"] is True


def test_patch_reenter_values_fixes_it(make_client, db_session):
    c, app_id = _setup(make_client, db_session)
    r = c.patch(f"/apps/{app_id}", json={"env": [{"key": "A", "value": "new"},
                                                 {"key": "S", "value": "new2", "is_secret": True}]})
    assert r.status_code == 200, r.text
    assert not any(e["undecryptable"] for e in r.json()["env"])
    assert r.json()["status"] == "pending"


def test_spec_deploy_and_admin_listing_survive(make_client, db_session):
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    c, app_id = _setup(make_client, db_session)
    assert admin.get("/admin/apps").status_code == 200


def test_rotate_works_without_decrypting_old(make_client, db_session):
    c, app_id = _setup(make_client, db_session, with_resources=True)
    assert c.get("/apps").status_code == 200
    for target in ("db", "storage"):
        db_session.get(models.App, app_id).status = "running"
        db_session.commit()
        r = c.post(f"/apps/{app_id}/rotate-credentials", json={"target": target})
        assert r.status_code == 200, r.text
    a = db_session.get(models.App, app_id)
    db_session.refresh(a)
    assert crypto.try_decrypt(a.database.password_encrypted) is not None
    assert crypto.try_decrypt(a.storage.secret_key_encrypted) is not None


def _providers():
    mysql, store = FakeMySQLAdmin(), FakeObjectStore()
    return dict(kube=FakeKubeProvider(), mysql_admin_factory=lambda s: mysql, object_store=store, settings={})


def test_reconciler_marks_error_and_continues(make_client, db_session):
    c, bad_id = _setup(make_client, db_session)
    c.post("/apps", json={"name": "good", "image": "example/demo:latest", "port": 8080})
    good = db_session.query(models.App).filter(models.App.name == "good").one()
    bad = db_session.get(models.App, bad_id)
    bad.status = "pending"
    db_session.commit()
    reconcile_all(db_session, **_providers())
    db_session.refresh(bad)
    db_session.refresh(good)
    assert bad.status == "error" and bad.status_detail == UNREADABLE_DETAIL
    assert good.status == "running"


def test_reconciler_sweep_marks_running_app_with_bad_db_creds(make_client, db_session):
    c, app_id = _setup(make_client, db_session, with_resources=True)
    for v in db_session.get(models.App, app_id).env_vars:
        v.value_encrypted = crypto.encrypt("ok")
    db_session.commit()
    reconcile_all(db_session, **_providers())
    a = db_session.get(models.App, app_id)
    db_session.refresh(a)
    assert a.status == "error" and a.status_detail == UNREADABLE_DETAIL


def test_admin_delete_tears_down_without_decrypting(make_client, db_session):
    c, app_id = _setup(make_client, db_session, with_resources=True)
    a = db_session.get(models.App, app_id)
    a.deleted_at = datetime.now(timezone.utc)
    db_session.commit()
    reconcile_all(db_session, **_providers())
    db_session.refresh(a)
    assert a.status == "deleted"


# --- storage private-IP egress policy ---

import socket

from app import k8s_manifests, reconciler


def _gai(ips):
    return lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


def _storage_policy(kube):
    return [m for m in kube.applied_flat if m["metadata"]["name"] == "allow-egress-storage"] if hasattr(kube, "applied_flat") else None


def test_manifest_storage_policy_is_32_on_443():
    pols = k8s_manifests.network_policies(1, 8080, None, None, ["10.11.0.254"])
    p = [x for x in pols if x["metadata"]["name"] == "allow-egress-storage"][0]
    assert p["spec"]["egress"] == [{"to": [{"ipBlock": {"cidr": "10.11.0.254/32"}}],
                                    "ports": [{"protocol": "TCP", "port": 443}]}]
    assert not [x for x in k8s_manifests.network_policies(1, 8080, None) if x["metadata"]["name"] == "allow-egress-storage"]


def test_private_storage_ips(monkeypatch):
    reconciler._storage_ip_cache.clear()
    monkeypatch.setattr(socket, "getaddrinfo", _gai(["10.11.0.254", "134.199.128.128"]))
    assert reconciler._private_storage_ips("https://atl1.digitaloceanspaces.com") == ["10.11.0.254"]
    reconciler._storage_ip_cache.clear()
    monkeypatch.setattr(socket, "getaddrinfo", _gai(["134.199.128.128"]))
    assert reconciler._private_storage_ips("https://atl1.digitaloceanspaces.com") == []


def test_dns_failure_no_policy_no_crash(monkeypatch):
    reconciler._storage_ip_cache.clear()

    def boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert reconciler._private_storage_ips("https://atl1.digitaloceanspaces.com") == []


def test_reconcile_threads_storage_ips(make_client, db_session, monkeypatch):
    reconciler._storage_ip_cache.clear()
    monkeypatch.setattr(socket, "getaddrinfo", _gai(["10.11.0.254"]))
    seen = []
    real = k8s_manifests.render_app_manifests
    monkeypatch.setattr(k8s_manifests, "render_app_manifests", lambda **kw: (seen.append(kw["storage_ips"]), real(**kw))[1])
    c, app_id = _setup(make_client, db_session, with_resources=True)
    for v in db_session.get(models.App, app_id).env_vars:
        v.value_encrypted = crypto.encrypt("ok")
    st = db_session.get(models.App, app_id).storage
    st.secret_key_encrypted = crypto.encrypt("x")
    st.endpoint = "https://atl1.digitaloceanspaces.com"
    db_session.get(models.App, app_id).database.password_encrypted = crypto.encrypt("y")
    db_session.get(models.App, app_id).status = "pending"
    db_session.commit()
    reconcile_all(db_session, **_providers())
    assert seen and all(s == ["10.11.0.254"] for s in seen)
    # app without storage gets none
    c.post("/apps", json={"name": "nost", "image": "example/demo:latest", "port": 8080})
    reconcile_all(db_session, **_providers())
    assert seen[-1] == []
