"""Updates reach the pod (status -> pending), and per-app MySQL egress uses the DbServer port."""
from __future__ import annotations

from app import crypto, models
from app.main import app as fastapi_app
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import reconcile_all
from app.routers.apps import get_mysql_admin_factory, get_object_store
from tests import test_reconciler as tr
from tests.conftest import register_and_login


def _create(client, **kw):
    payload = {"name": "demo", "image": "example/demo:1", "port": 8080,
               "env": [{"key": "APP_VERSION", "value": "1"}, {"key": "S", "value": "x", "is_secret": True}]}
    payload.update(kw)
    r = client.post("/apps", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _set_running(db_session, app_id):
    a = db_session.get(models.App, app_id)
    a.status = "running"
    db_session.commit()


def _status(db_session, app_id):
    db_session.expire_all()
    return db_session.get(models.App, app_id).status


def test_patch_changes_set_pending_noop_does_not(client, db_session):
    register_and_login(client, "o@example.com")
    app_id = _create(client)
    for body in [{"image": "example/demo:2"}, {"port": 9090},
                 {"env": [{"key": "APP_VERSION", "value": "2"}, {"key": "S", "value": "x", "is_secret": True}]}]:
        _set_running(db_session, app_id)
        assert client.patch(f"/apps/{app_id}", json=body).status_code == 200
        assert _status(db_session, app_id) == "pending", body
    # no-ops: same values, masked secret round-trip, rename
    _set_running(db_session, app_id)
    same = client.get(f"/apps/{app_id}").json()
    r = client.patch(f"/apps/{app_id}", json={"image": same["image"], "port": same["port"], "env": same["env"],
                                              "name": "renamed"})
    assert r.status_code == 200
    assert _status(db_session, app_id) == "running"


def test_secret_change_sets_pending(client, db_session):
    register_and_login(client, "o@example.com")
    app_id = _create(client)
    _set_running(db_session, app_id)
    client.patch(f"/apps/{app_id}", json={"env": [{"key": "APP_VERSION", "value": "1"},
                                                  {"key": "S", "value": "changed", "is_secret": True}]})
    assert _status(db_session, app_id) == "pending"


def test_spec_reapply_pending_only_when_changed(client, db_session):
    register_and_login(client, "o@example.com")
    spec = "apiVersion: picopaas/v1\nname: x\nimage: example/demo:1\nport: 80\nenv:\n  A: '1'\n"
    assert client.post("/apps/spec", json={"spec": spec}).status_code == 200
    app_id = client.get("/apps").json()[0]["id"]
    _set_running(db_session, app_id)
    assert client.post("/apps/spec", json={"spec": spec}).status_code == 200
    assert _status(db_session, app_id) == "running"
    assert client.post("/apps/spec", json={"spec": spec.replace("'1'", "'2'")}).status_code == 200
    assert _status(db_session, app_id) == "pending"
    _set_running(db_session, app_id)
    assert client.post("/apps/spec", json={"spec": spec.replace("demo:1", "demo:2")}).status_code == 200
    assert _status(db_session, app_id) == "pending"


def test_update_rolls_pod_via_env_checksum(client, db_session):
    register_and_login(client, "o@example.com")
    app_id = _create(client)
    kube = FakeKubeProvider()
    kw = dict(kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=FakeObjectStore(),
              settings={})
    reconcile_all(db_session, **kw)
    ns = f"app-{app_id}"

    def checksum():
        dep = [m for m in kube.applied[ns] if m["kind"] == "Deployment"][-1]
        return dep["spec"]["template"]["metadata"]["annotations"]["picopaas.io/env-checksum"]

    first = checksum()
    assert _status(db_session, app_id) == "running"
    client.patch(f"/apps/{app_id}", json={"env": [{"key": "APP_VERSION", "value": "2"}]})
    reconcile_all(db_session, **kw)
    assert checksum() != first


def test_rotation_reaches_pod(make_client, db_session):
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda server: admin)
    fastapi_app.dependency_overrides[get_object_store] = lambda: store
    c = make_client()
    register_and_login(c, "o@example.com")
    app_id = _create(c, want_db=True, env=[{"key": "DATABASE_URL", "value": "${{ db.url }}"}])
    db_session.add(models.DbServer(name="s", host="h", port=3306, admin_user="r",
                                   admin_password_encrypted=crypto.encrypt("x")))
    db_session.commit()
    kube = FakeKubeProvider()
    kw = dict(kube=kube, mysql_admin_factory=lambda s: admin, object_store=store, settings={})
    reconcile_all(db_session, **kw)
    ns = f"app-{app_id}"

    def secret_url():
        return [m for m in kube.applied[ns] if m["kind"] == "Secret"][-1]["stringData"]["DATABASE_URL"]

    def checksum():
        dep = [m for m in kube.applied[ns] if m["kind"] == "Deployment"][-1]
        return dep["spec"]["template"]["metadata"]["annotations"]["picopaas.io/env-checksum"]

    old_url, old_sum = secret_url(), checksum()
    assert c.post(f"/apps/{app_id}/rotate-credentials", json={"target": "db"}).status_code == 200
    assert _status(db_session, app_id) == "pending"
    reconcile_all(db_session, **kw)
    assert secret_url() != old_url and checksum() != old_sum


def _egress_port(kube, ns):
    pol = next(m for m in kube.applied[ns] if m["kind"] == "NetworkPolicy"
               and m["metadata"]["name"] == "allow-egress-mysql")
    return pol["spec"]["egress"][0]["ports"][0]["port"], pol["spec"]["egress"][0]["to"][0]


def test_mysql_egress_uses_db_server_port(db_session):
    server = tr._make_server(db_session)
    server.port = 25060
    db_session.commit()
    kube = FakeKubeProvider()
    app = tr._make_app(db_session, want_db=True)
    kw = dict(kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=FakeObjectStore())
    from app.reconciler import reconcile_app
    reconcile_app(db_session, app, settings={"mysql_cidr": "10.1.0.0/16"}, **kw)
    assert _egress_port(kube, f"app-{app.id}")[0] == 25060
    # in-cluster path: pod selector peer, DbServer port
    kube2 = FakeKubeProvider()
    app.status = "pending"
    reconcile_app(db_session, app, settings={"mysql_egress_mode": "in-cluster"}, **{**kw, "kube": kube2})
    port, peer = _egress_port(kube2, f"app-{app.id}")
    assert port == 25060 and "podSelector" in peer


def test_mysql_egress_defaults_to_3306_without_db(db_session):
    kube = FakeKubeProvider()
    app = tr._make_app(db_session)
    from app.reconciler import reconcile_app
    reconcile_app(db_session, app, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={"mysql_cidr": "10.1.0.0/16"})
    assert _egress_port(kube, f"app-{app.id}")[0] == 3306
