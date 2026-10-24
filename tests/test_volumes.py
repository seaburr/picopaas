"""Persistent volumes (REQ-1..4): flag, API/spec validation, manifests, reconciler, quota, capacity, admin grow-only."""
import pytest

from app import config, k8s_manifests as k8s, models, volumes
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import reconcile_all
from app.spec import SpecError, parse_spec
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
VOL = {"size_gi": 1, "mount_path": "/data"}


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def user(make_client, admin, db_session):
    c = make_client()
    register_and_login(c, "user@example.com")
    db_session.query(models.User).filter(models.User.email == "user@example.com").one().app_limit = 5
    db_session.commit()
    return c


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(config, "VOLUMES_ENABLED", True)


@pytest.fixture()
def kube():
    return FakeKubeProvider()


def _sweep(db, kube):
    reconcile_all(db, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={})
    db.expire_all()


def _kinds(kube, aid, kind):
    return [m for m in kube.applied.get(f"app-{aid}", []) if m["kind"] == kind]


def _mk(user, **extra):
    r = user.post("/apps", json={**APP, **extra})
    assert r.status_code == 201, r.text
    return r.json()


# --- disabled by default ---

def test_disabled_by_default():
    assert config.VOLUMES_ENABLED is False


def test_disabled_rejects_create_update_and_spec(admin, user):
    r = user.post("/apps", json={**APP, "volume": VOL})
    assert r.status_code == 422 and r.json()["detail"] == "Persistent volumes aren't enabled on this platform."
    aid = _mk(user)["id"]
    assert user.patch(f"/apps/{aid}", json={"volume": VOL}).status_code == 422
    spec = "apiVersion: picopaas/v1\nname: s\nimage: ghcr.io/example/demo:1\nport: 8080\nresources: { volume: { sizeGi: 1 } }\n"
    r = user.post("/apps/spec", json={"spec": spec})
    assert r.status_code == 422 and "aren't enabled" in r.json()["detail"]
    assert user.get(f"/apps/{aid}").json()["volume"] is None


def test_disabled_reconciler_never_renders_pvc_even_if_row_has_volume(admin, user, db_session, kube):
    aid = _mk(user)["id"]
    a = db_session.get(models.App, aid)
    a.volume_size_gi, a.volume_mount_path = 1, "/data"
    db_session.commit()
    _sweep(db_session, kube)
    assert _kinds(kube, aid, "PersistentVolumeClaim") == []
    dep = _kinds(kube, aid, "Deployment")[0]
    assert "volumes" not in dep["spec"]["template"]["spec"] and dep["spec"]["strategy"]["type"] == "RollingUpdate"
    assert "persistentvolumeclaims" not in _kinds(kube, aid, "ResourceQuota")[0]["spec"]["hard"]


def test_admin_grow_disabled_422(admin, user):
    aid = _mk(user)["id"]
    r = admin.patch(f"/admin/apps/{aid}/resources", json={"volume_size_gi": 2})
    assert r.status_code == 422 and "aren't enabled" in r.json()["detail"]


def test_me_features_and_limits(on, user):
    me = user.get("/auth/me").json()
    assert me["features"]["volumes"] is True
    assert me["limits"]["volume_max_gi"] == 1 and me["limits"]["volume_default_mount_path"] == "/data"


def test_me_features_off(user):
    assert user.get("/auth/me").json()["features"]["volumes"] is False


# --- API ---

def test_create_with_defaults_and_out_shape(on, user):
    a = _mk(user, volume={})
    assert a["volume"] == {"size_gi": 1, "mount_path": "/data", "phase": None}
    assert user.get(f"/apps/{a['id']}").json()["volume"]["mount_path"] == "/data"


@pytest.mark.parametrize("path", ["data", "/", "/a/../b", "/proc", "/proc/x", "/sys", "/dev/shm", "/etc", "/etc/x",
                                  "/usr/lib", "/bin", "/lib", "/tmp", "/tmp/x", "/a b", "/" + "a" * 300, "/x:y"])
def test_bad_mount_paths_422(on, user, path):
    assert user.post("/apps", json={**APP, "volume": {"mount_path": path}}).status_code == 422


def test_mount_path_normalised(on, user):
    assert _mk(user, volume={"mount_path": "/var/www//html/"})["volume"]["mount_path"] == "/var/www/html"


def test_user_size_cap_and_bounds(on, user):
    assert user.post("/apps", json={**APP, "volume": {"size_gi": 2}}).status_code == 422
    assert user.post("/apps", json={**APP, "volume": {"size_gi": 0}}).status_code == 422
    assert user.post("/apps", json={**APP, "volume": {"size_gi": 1, "x": 1}}).status_code == 422


def test_raised_user_ceiling(on, user, monkeypatch):
    monkeypatch.setattr(config, "VOLUME_MAX_GI", 3)
    a = _mk(user, volume={"size_gi": 3})
    assert a["volume"]["size_gi"] == 3


def test_enable_on_existing_app_and_requeue(on, user, db_session):
    aid = _mk(user)["id"]
    db_session.get(models.App, aid).status = "running"
    db_session.commit()
    r = user.patch(f"/apps/{aid}", json={"volume": VOL})
    assert r.status_code == 200 and r.json()["volume"]["size_gi"] == 1 and r.json()["status"] == "pending"


def test_change_mount_path_requeues_same_values_do_not(on, user, db_session):
    aid = _mk(user, volume=VOL)["id"]
    db_session.get(models.App, aid).status = "running"
    db_session.commit()
    assert user.patch(f"/apps/{aid}", json={"volume": VOL}).json()["status"] == "running"
    r = user.patch(f"/apps/{aid}", json={"volume": {"mount_path": "/var/lib/x"}})
    assert r.json()["volume"]["mount_path"] == "/var/lib/x" and r.json()["status"] == "pending"


def test_detach_is_rejected(on, user):
    aid = _mk(user, volume=VOL)["id"]
    r = user.patch(f"/apps/{aid}", json={"volume": None})
    assert r.status_code == 422 and "can't be detached" in r.json()["detail"]
    assert user.get(f"/apps/{aid}").json()["volume"] is not None


# --- replicas ---

def test_admin_replicas_gt1_with_volume_409(on, admin, user):
    aid = _mk(user, volume=VOL)["id"]
    r = admin.put(f"/admin/apps/{aid}/replicas", json={"replicas": 2})
    assert r.status_code == 409 and "one pod" in r.json()["detail"]
    assert admin.put(f"/admin/apps/{aid}/replicas", json={"replicas": 1}).status_code == 200


def test_enabling_volume_on_multi_replica_app_409(on, admin, user):
    aid = _mk(user)["id"]
    assert admin.put(f"/admin/apps/{aid}/replicas", json={"replicas": 2}).status_code == 200
    r = user.patch(f"/apps/{aid}", json={"volume": VOL})
    assert r.status_code == 409 and "replicas to 1" in r.json()["detail"]


# --- admin grow-only ---

def test_admin_grow_only(on, admin, user, db_session):
    aid = _mk(user, volume=VOL)["id"]
    db_session.get(models.App, aid).status = "running"
    db_session.commit()
    r = admin.patch(f"/admin/apps/{aid}/resources", json={"volume_size_gi": 5})
    assert r.status_code == 200, r.text
    assert r.json()["volume"]["size_gi"] == 5 and r.json()["restarts_app"] is False
    assert user.get(f"/apps/{aid}").json()["status"] == "pending"  # re-applied so the PVC grows
    assert admin.patch(f"/admin/apps/{aid}/resources", json={"volume_size_gi": 3}).status_code == 422
    assert admin.patch(f"/admin/apps/{aid}/resources", json={"volume_size_gi": None}).status_code == 422
    assert admin.patch(f"/admin/apps/{aid}/resources", json={"volume_size_gi": config.OVERRIDE_VOLUME_MAX_GI + 1}).status_code == 422
    assert user.get(f"/apps/{aid}").json()["volume"]["size_gi"] == 5
    # a smaller request keeps the granted size (no shrink, no error), and re-sending the granted size is fine
    r = user.patch(f"/apps/{aid}", json={"volume": {"size_gi": 1}})
    assert r.status_code == 200 and r.json()["volume"]["size_gi"] == 5
    assert user.patch(f"/apps/{aid}", json={"volume": {"size_gi": 5}}).status_code == 200
    # users can't grow past the ceiling themselves
    assert user.patch(f"/apps/{aid}", json={"volume": {"size_gi": 6}}).status_code == 422


def test_admin_grow_needs_existing_volume_and_admin(on, admin, user):
    aid = _mk(user)["id"]
    r = admin.patch(f"/admin/apps/{aid}/resources", json={"volume_size_gi": 2})
    assert r.status_code == 422 and "no persistent volume" in r.json()["detail"]
    vid = _mk(user, name="b", volume=VOL)["id"]
    assert user.patch(f"/admin/apps/{vid}/resources", json={"volume_size_gi": 2}).status_code == 403


# --- capacity ---

def test_capacity_limit_409_and_health_count(on, admin, user, monkeypatch, db_session):
    monkeypatch.setattr(config, "VOLUMES_MAX_TOTAL", 2)
    monkeypatch.setattr(config, "MAX_APPS_PER_USER", 5)
    user_row = db_session.query(models.User).filter(models.User.email == "user@example.com").one()
    user_row.app_limit = 5
    db_session.commit()
    _mk(user, name="a", volume=VOL)
    b = _mk(user, name="b", volume=VOL)
    r = user.post("/apps", json={**APP, "name": "c", "volume": VOL})
    assert r.status_code == 409 and "capacity" in r.json()["detail"]
    plain = _mk(user, name="d")
    assert user.patch(f"/apps/{plain['id']}", json={"volume": VOL}).status_code == 409
    assert admin.get("/admin/health").json()["workloads"]["with_volume"] == 2
    # a deleted app frees its slot
    assert user.delete(f"/apps/{b['id']}").status_code == 204
    assert user.post("/apps", json={**APP, "name": "e", "volume": VOL}).status_code == 201


def test_capacity_count_is_locked_for_update():
    from sqlalchemy.dialects import mysql
    q = volumes._volume_apps_query(_FakeDb()).with_for_update()
    assert "FOR UPDATE" in str(q.statement.compile(dialect=mysql.dialect()))


class _FakeDb:
    def query(self, *a):
        from sqlalchemy.orm import Query
        return Query(a)


def test_require_capacity_uses_locked_count(on, user, db_session, monkeypatch):
    monkeypatch.setattr(config, "VOLUMES_MAX_TOTAL", 1)
    seen = []
    orig = volumes._locked_total_in_use
    monkeypatch.setattr(volumes, "_locked_total_in_use", lambda db: seen.append(1) or orig(db))
    _mk(user, name="a", volume=VOL)
    assert user.post("/apps", json={**APP, "name": "b", "volume": VOL}).status_code == 409
    assert len(seen) == 2


def test_spec_reapply_after_admin_grow_keeps_size(on, admin, user):
    r = user.post("/apps/spec", json={"spec": SPEC})
    aid = r.json()["id"]
    assert admin.patch(f"/admin/apps/{aid}/resources", json={"volume_size_gi": 5}).status_code == 200
    r2 = user.post("/apps/spec", json={"spec": SPEC})  # sizeGi: 1 < granted 5
    assert r2.status_code == 200 and r2.json()["volume"]["size_gi"] == 5
    no_size = SPEC.replace("sizeGi: 1, ", "")  # default size
    r3 = user.post("/apps/spec", json={"spec": no_size})
    assert r3.status_code == 200 and r3.json()["volume"]["size_gi"] == 5
    # a user can't grow above their cap beyond the grant
    r4 = user.post("/apps/spec", json={"spec": SPEC.replace("sizeGi: 1", "sizeGi: 6")})
    assert r4.status_code == 422
    assert user.get(f"/apps/{aid}").json()["volume"]["size_gi"] == 5


# --- spec / CLI ---

SPEC = ("apiVersion: picopaas/v1\nname: s\nimage: ghcr.io/example/demo:1\nport: 8080\n"
        "resources: { volume: { sizeGi: 1, mountPath: /var/www/html } }\n")


def test_spec_parse_and_errors():
    assert parse_spec(SPEC).resources.volume.mountPath == "/var/www/html"
    assert parse_spec(SPEC.replace(", mountPath: /var/www/html", "")).resources.volume.mountPath == "/data"
    for bad in ("mountPath: /etc", "mountPath: /", "sizeGi: 0", "sizeGi: 99", "sizeGi: 1Gi", "foo: 1"):
        with pytest.raises(SpecError):
            parse_spec(SPEC.replace("sizeGi: 1, mountPath: /var/www/html", bad))


def test_spec_deploy_round_trip_and_no_removal(on, user):
    r = user.post("/apps/spec", json={"spec": SPEC})
    assert r.status_code == 200 and r.json()["volume"] == {"size_gi": 1, "mount_path": "/var/www/html", "phase": None}
    from cli.picopaas import spec_from_app
    exported = spec_from_app(r.json())
    assert "volume: { sizeGi: 1, mountPath: \"/var/www/html\" }" in exported
    assert parse_spec(exported).resources.volume.mountPath == "/var/www/html"
    r2 = user.post("/apps/spec", json={"spec": exported})
    assert r2.status_code == 200 and r2.json()["created"] is False
    no_vol = "apiVersion: picopaas/v1\nname: s\nimage: ghcr.io/example/demo:1\nport: 8080\n"
    r3 = user.post("/apps/spec", json={"spec": no_vol})
    assert r3.status_code == 422 and "can't remove a persistent volume" in r3.json()["detail"]


# --- manifests ---

def _render(**kw):
    base = dict(app_id=9, hostname="h", image="img:1", port=8080, env={}, mysql_cidr=None)
    base.update(kw)
    return k8s.render_app_manifests(**base)


def test_no_volume_manifests_unchanged():
    r = _render(replicas=2)
    assert r["pvc"] is None
    dep = r["deployment"]["spec"]
    assert dep["strategy"]["type"] == "RollingUpdate" and dep["replicas"] == 2
    pod = dep["template"]["spec"]
    assert "volumes" not in pod and "fsGroup" not in pod["securityContext"]
    assert "volumeMounts" not in pod["containers"][0]
    hard = r["resource_quota"]["spec"]["hard"]
    assert "persistentvolumeclaims" not in hard and "requests.storage" not in hard
    assert all(m["kind"] != "PersistentVolumeClaim" for m in k8s.flatten_manifests(r))


def test_volume_manifests():
    r = _render(replicas=3, volume={"size_gi": 2, "mount_path": "/var/www/html"})
    pvc = r["pvc"]
    assert pvc["metadata"] == {"name": "app-9-data", "namespace": "app-9", "labels": {"app": "app-9"}}
    assert pvc["spec"] == {"accessModes": ["ReadWriteOnce"], "storageClassName": "do-block-storage",
                           "resources": {"requests": {"storage": "2Gi"}}}
    dep = r["deployment"]["spec"]
    assert dep["strategy"] == {"type": "Recreate"} and dep["replicas"] == 1
    pod = dep["template"]["spec"]
    assert pod["securityContext"]["fsGroup"] == 10001
    assert pod["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["volumes"] == [{"name": "data", "persistentVolumeClaim": {"claimName": "app-9-data"}}]
    assert pod["containers"][0]["volumeMounts"] == [{"name": "data", "mountPath": "/var/www/html"}]
    hard = r["resource_quota"]["spec"]["hard"]
    assert hard["persistentvolumeclaims"] == "1" and hard["requests.storage"] == "2Gi"
    flat = [m["kind"] for m in k8s.flatten_manifests(r)]
    assert flat.index("ResourceQuota") < flat.index("PersistentVolumeClaim") < flat.index("Deployment")


def test_suspended_volume_app_stays_at_zero():
    assert _render(replicas=0, volume={"size_gi": 1, "mount_path": "/data"})["deployment"]["spec"]["replicas"] == 0


def test_storage_class_from_config(monkeypatch):
    monkeypatch.setattr(k8s, "VOLUME_STORAGE_CLASS", "custom-class")
    assert _render(volume={"size_gi": 1, "mount_path": "/d"})["pvc"]["spec"]["storageClassName"] == "custom-class"


# --- reconciler ---

def test_reconciler_renders_pvc_and_tracks_phase(on, user, db_session, kube):
    aid = _mk(user, volume={"size_gi": 1, "mount_path": "/var/www/html"})["id"]
    kube.pvc_phases[f"app-{aid}"] = "Pending"
    _sweep(db_session, kube)
    (pvc,) = _kinds(kube, aid, "PersistentVolumeClaim")
    assert pvc["metadata"]["name"] == f"app-{aid}-data"
    dep = _kinds(kube, aid, "Deployment")[0]
    assert dep["spec"]["strategy"]["type"] == "Recreate"
    assert user.get(f"/apps/{aid}").json()["volume"]["phase"] == "Pending"
    kube.pvc_phases[f"app-{aid}"] = "Bound"  # the next sweeps pick up the bind without re-applying
    _sweep(db_session, kube)
    _sweep(db_session, kube)
    assert user.get(f"/apps/{aid}").json()["volume"]["phase"] == "Bound"


def test_reconciler_forces_one_replica_with_volume(on, admin, user, db_session, kube):
    aid = _mk(user, volume=VOL)["id"]
    db_session.get(models.App, aid).replicas = 3  # e.g. set before the volume existed
    db_session.commit()
    _sweep(db_session, kube)
    assert _kinds(kube, aid, "Deployment")[-1]["spec"]["replicas"] == 1
    assert all(m["metadata"]["name"] != "allow-same-namespace" for m in _kinds(kube, aid, "NetworkPolicy"))


def test_restart_and_resweep_never_delete_pvc(on, user, db_session, kube):
    aid = _mk(user, volume=VOL)["id"]
    _sweep(db_session, kube)
    db_session.get(models.App, aid).status = "pending"
    db_session.commit()
    _sweep(db_session, kube)
    assert kube.deleted == set() and len(_kinds(kube, aid, "PersistentVolumeClaim")) >= 1


def test_grow_rerenders_pvc_and_quota(on, admin, user, db_session, kube):
    aid = _mk(user, volume=VOL)["id"]
    _sweep(db_session, kube)
    admin.patch(f"/admin/apps/{aid}/resources", json={"volume_size_gi": 4})
    _sweep(db_session, kube)
    assert _kinds(kube, aid, "PersistentVolumeClaim")[-1]["spec"]["resources"]["requests"]["storage"] == "4Gi"
    assert _kinds(kube, aid, "ResourceQuota")[-1]["spec"]["hard"]["requests.storage"] == "4Gi"


def test_delete_app_removes_namespace_with_pvc(on, user, db_session, kube):
    aid = _mk(user, volume=VOL)["id"]
    _sweep(db_session, kube)
    user.delete(f"/apps/{aid}")
    _sweep(db_session, kube)
    assert f"app-{aid}" in kube.deleted and f"app-{aid}" not in kube.applied  # PVC goes with the namespace (reclaim Delete)


def test_validate_mount_path_unit():
    assert volumes.validate_mount_path("/data/") == "/data"
    with pytest.raises(ValueError):
        volumes.validate_mount_path("")


@pytest.mark.parametrize("path", [
    "//etc", "///etc", "//var/run/secrets", "//proc/self", "/./etc", "/etc/", "/var//run/secrets", "/var/run/secrets/kubernetes.io",
    "/run/secrets", "/var/lock", "/data/../etc", "/data/​..", "/",
])
def test_mount_path_cannot_shadow_system_dirs(path):
    with pytest.raises(ValueError):
        volumes.validate_mount_path(path)


def test_mount_path_leading_slashes_collapse():
    assert volumes.validate_mount_path("//data") == "/data"


def test_override_ceiling_default_env_and_clamp(monkeypatch):
    import importlib
    try:
        monkeypatch.delenv("OVERRIDE_VOLUME_MAX_GI", raising=False)
        monkeypatch.delenv("VOLUME_MAX_GI", raising=False)
        assert importlib.reload(config).OVERRIDE_VOLUME_MAX_GI == 10
        monkeypatch.setenv("OVERRIDE_VOLUME_MAX_GI", "4")
        assert importlib.reload(config).OVERRIDE_VOLUME_MAX_GI == 4
        monkeypatch.setenv("VOLUME_MAX_GI", "9")
        c = importlib.reload(config)
        assert c.VOLUME_MAX_GI == 4  # clamped to the admin ceiling
    finally:
        monkeypatch.undo()
        importlib.reload(config)
