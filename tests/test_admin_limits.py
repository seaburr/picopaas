"""Per-app admin limit overrides, recovery after a raise, and the user health-check timeout."""
import pytest

from app import crypto, models, resources
from app import k8s_manifests as k8s
from app.main import app as fastapi_app
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore, FakePostgresAdmin
from app.reconciler import reconcile_all
from app.routers.apps import get_mysql_admin_factory, get_object_store
from tests.conftest import register_and_login

APP = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
MB = 1024 * 1024


@pytest.fixture()
def env(make_client, db_session):
    mysql, pg, store = FakeMySQLAdmin(), FakePostgresAdmin(), FakeObjectStore()
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (
        lambda server: pg if getattr(server, "engine", "mysql") == "pg" else mysql)
    fastapi_app.dependency_overrides[get_object_store] = lambda: store
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c, mysql, pg, store


def _with_resources(db_session, aid, engine="mysql"):
    a = db_session.get(models.App, aid)
    server = models.DbServer(name=f"s-{engine}", host="h", port=3306, admin_user="r", engine=engine,
                             admin_password_encrypted=crypto.encrypt("x"))
    db_session.add(server)
    db_session.commit()
    a.want_db = a.want_storage = True
    a.db_engine = engine
    a.status = "running"
    db_session.add(models.AppDatabase(app_id=aid, db_server_id=server.id, db_name=f"app_{aid}",
                                      db_user=f"app_{aid}", password_encrypted=crypto.encrypt("p")))
    db_session.add(models.AppStorage(app_id=aid, bucket="b", endpoint="e", region="r", access_key="old",
                                     secret_key_encrypted=crypto.encrypt("old")))
    db_session.commit()
    return a


def _patch(c, aid, **body):
    return c.patch(f"/admin/apps/{aid}/resources", json=body)


def test_new_fields_ranges_and_response(env, db_session):
    c = env[0]
    aid = c.post("/apps", json=APP).json()["id"]
    for body in ({"db_max_connections": 0}, {"db_max_connections": 26}, {"ephemeral_limit_mi": 63},
                 {"ephemeral_limit_mi": 2049}, {"db_quota_mb": 0}, {"db_quota_mb": 1025},
                 {"storage_quota_mb": 51201}, {"image_size_mb": 2049}, {"image_size_mb": 0}):
        assert _patch(c, aid, **body).status_code == 422, body
    r = _patch(c, aid, db_max_connections=25, ephemeral_limit_mi=2048, db_quota_mb=1024,
               storage_quota_mb=51200, image_size_mb=2048)
    assert r.status_code == 200
    d = r.json()
    assert d["resources"]["overridden"] and d["resources"]["db_quota_mb"] == 1024
    assert d["restarts_app"] is True and d["replicas"] == 1 and d["db_engine"] is None
    assert d["resources"]["defaults"]["ephemeral_limit_mi"] == 250
    # quota-only change does not restart; null resets
    r = _patch(c, aid, db_quota_mb=None)
    assert r.json()["restarts_app"] is False and r.json()["resources"]["db_quota_mb"] == 50
    assert c.get("/admin/apps").json()[0]["replicas"] == 1


def test_ephemeral_flows_into_manifests_and_message(env, db_session):
    c = env[0]
    aid = c.post("/apps", json=APP).json()["id"]
    _patch(c, aid, ephemeral_limit_mi=500)
    a = db_session.get(models.App, aid)
    assert a.status == "pending"
    e = resources.effective(a)
    rendered = k8s.render_app_manifests(aid, "h", "img", 8080, {}, None, resources=e)
    ctr = rendered["deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert ctr["resources"]["limits"]["ephemeral-storage"] == "500Mi"
    assert rendered["limit_range"]["spec"]["limits"][0]["max"]["ephemeral-storage"] == "500Mi"
    assert rendered["resource_quota"]["spec"]["hard"]["limits.ephemeral-storage"] == "1000Mi"
    from app import reconciler
    assert "500 MB" in reconciler.event_message("disk", a)


def test_pg_connection_limit_default_and_applied(env, db_session):
    c, mysql, pg, _ = env
    aid = c.post("/apps", json=APP).json()["id"]
    _with_resources(db_session, aid, "pg")
    r = _patch(c, aid, db_max_connections=8)
    assert r.json()["resources"]["defaults"]["db_max_connections"] == 5 and r.json()["db_engine"] == "pg"
    assert pg.conn_limits[aid] == 8
    # the reconciler sweep self-heals with the per-app value
    pg.conn_limits.clear()
    reconcile_all(db_session, kube=FakeKubeProvider(), mysql_admin_factory=lambda s: pg,
                  object_store=FakeObjectStore(), settings={})
    assert pg.conn_limits[aid] == 8


def test_health_timeout_probes(env, db_session):
    c = env[0]
    r = c.post("/apps", json={**APP, "health_check_enabled": True, "health_check_timeout_seconds": 30})
    assert r.status_code == 201 or r.status_code == 200, r.text
    assert r.json()["health_check_timeout_seconds"] == 30
    aid = r.json()["id"]
    dep = k8s.deployment_manifest(aid, "img", 8080, health_check_enabled=True, health_check_timeout_seconds=30)
    ctr = dep["spec"]["template"]["spec"]["containers"][0]
    assert ctr["readinessProbe"]["timeoutSeconds"] == 30 and ctr["readinessProbe"]["periodSeconds"] == 30
    assert ctr["livenessProbe"]["timeoutSeconds"] == 30 and ctr["livenessProbe"]["periodSeconds"] == 30
    dep = k8s.deployment_manifest(aid, "img", 8080, health_check_enabled=True)
    ctr = dep["spec"]["template"]["spec"]["containers"][0]
    assert (ctr["readinessProbe"]["timeoutSeconds"], ctr["livenessProbe"]["timeoutSeconds"]) == (3, 5)
    assert c.patch(f"/apps/{aid}", json={"health_check_timeout_seconds": 91}).status_code == 422
    r = c.patch(f"/apps/{aid}", json={"health_check_timeout_seconds": None})
    assert r.status_code == 200 and r.json()["health_check_timeout_seconds"] is None
    r = c.patch(f"/apps/{aid}", json={"health_check_timeout_seconds": 12})
    assert r.json()["health_check_timeout_seconds"] == 12


def test_spec_health_timeout(env):
    c = env[0]
    spec = "apiVersion: picopaas/v1\nname: s1\nimage: ghcr.io/example/demo:1.0\nport: 8080\nhealthCheck:\n  enabled: true\n  timeoutSeconds: 20\n"
    r = c.post("/apps/spec", json={"spec": spec})
    assert r.status_code == 200, r.text
    assert r.json()["health_check_timeout_seconds"] == 20
    bad = spec.replace("20", "100")
    assert c.post("/apps/spec", json={"spec": bad}).status_code == 422


def test_image_cap_uses_app_override():
    from app.image_check import ImageRejected, evaluate
    from app.providers.base import ImageInfo
    info = ImageInfo(size_bytes=600 * MB, user="1000", digest="sha256:a")
    with pytest.raises(ImageRejected):
        evaluate(info)
    assert evaluate(info, 700 * MB) is None
    with pytest.raises(ImageRejected):
        evaluate(info, 500 * MB)


# --- recovery after a limit raise ---------------------------------------------------------------------

def test_mysql_recovers_after_quota_raise(env, db_session):
    c, mysql, _, _ = env
    aid = c.post("/apps", json=APP).json()["id"]
    a = _with_resources(db_session, aid)
    mysql.create_db_user(aid)
    mysql.sizes[aid] = 60 * MB
    from app.quota import poll_quotas
    poll_quotas(db_session, mysql_admin_factory=lambda s: mysql, object_store=FakeObjectStore(), storage=False)
    assert a.database.over_quota and mysql.read_only[aid]
    d = _patch(c, aid, db_quota_mb=100).json()
    db_session.refresh(a)
    assert not a.database.over_quota and mysql.read_only[aid] is False
    assert d["resources"]["db_quota_mb"] == 100
    assert c.get(f"/apps/{aid}").json()["quota"]["db"] == {"size_bytes": 60 * MB, "limit_bytes": 100 * MB,
                                                           "over_quota": False}


@pytest.mark.parametrize("raise_to,expect_hard", [(100, False), (40 * 3, None)])
def test_pg_both_stages_recover(env, db_session, raise_to, expect_hard):
    c, _, pg, _ = env
    aid = c.post("/apps", json=APP).json()["id"]
    a = _with_resources(db_session, aid, "pg")
    pg.create_db_user(aid)
    from app.quota import poll_quotas
    pg.sizes[aid] = 120 * MB  # > 2x the 50 MB cap: stage 2
    poll_quotas(db_session, mysql_admin_factory=lambda s: pg, object_store=FakeObjectStore(), storage=False)
    assert pg.hard[aid] is True and a.database.over_quota
    _patch(c, aid, db_quota_mb=raise_to)
    db_session.refresh(a)
    if expect_hard is False:  # still over the new cap? 120 > 100 yes, but under 2x: back to stage 1
        assert pg.read_only[aid] is True and pg.hard[aid] is False and a.database.over_quota
        assert pg.hard_calls[-1] == (aid, True, False)
        _patch(c, aid, db_quota_mb=200)
        db_session.refresh(a)
    assert not a.database.over_quota and pg.read_only[aid] is False and pg.hard[aid] is False


def test_storage_recovers_after_quota_raise(env, db_session):
    c, _, _, store = env
    aid = c.post("/apps", json=APP).json()["id"]
    a = _with_resources(db_session, aid)
    store.create_bucket_and_key(aid)
    store.sizes[aid] = 150 * MB
    from app.quota import poll_quotas
    poll_quotas(db_session, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=store, databases=False)
    assert a.storage.over_quota and store.read_only[aid]
    _patch(c, aid, storage_quota_mb=200)
    db_session.refresh(a)
    assert not a.storage.over_quota and store.read_only[aid] is False
    assert c.get(f"/apps/{aid}").json()["quota"]["storage"]["limit_bytes"] == 200 * MB


def test_ephemeral_raise_rerenders_deployment(env, db_session):
    c = env[0]
    aid = c.post("/apps", json=APP).json()["id"]
    kube = FakeKubeProvider()
    kw = dict(kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=FakeObjectStore(), settings={})
    reconcile_all(db_session, **kw)
    _patch(c, aid, ephemeral_limit_mi=600)
    db_session.expire_all()
    assert db_session.get(models.App, aid).status == "pending"
    reconcile_all(db_session, **kw)
    dep = [m for batch in kube.applied.values() for m in batch if m.get("kind") == "Deployment"]
    assert dep and dep[-1]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"][
        "ephemeral-storage"] == "600Mi"
