"""Admin settings: node-size caps, per-app defaults vs ceilings, session-only admin reads."""
import pytest

from app import k8s_manifests as k8s, models, platform_settings, resources
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def user(make_client, admin):
    c = make_client()
    register_and_login(c, "user@example.com")
    return c


def _put(c, **kv):
    return c.put("/admin/settings", json={"settings": kv})


def test_ceilings_capped_at_node_size(admin):
    r = _put(admin, cpu_limit_max_m=4000)
    assert r.status_code == 422 and "one node can schedule" in r.json()["detail"]
    r = _put(admin, mem_limit_max_mi=8192)
    assert r.status_code == 422 and "one node can schedule" in r.json()["detail"]
    assert _put(admin, cpu_limit_max_m=1500, mem_limit_max_mi=2560).status_code == 200


def test_default_above_ceiling_rejected(admin):
    r = _put(admin, default_cpu_limit_m=1001)  # ceiling is 1000
    assert r.status_code == 422 and "cpu_limit_max_m" in r.json()["detail"]
    r = _put(admin, default_ephemeral_mi=3000)  # beyond the schema max of the ceiling
    assert r.status_code == 422
    # lowering a ceiling under the current default is rejected too
    r = _put(admin, cpu_limit_max_m=150)
    assert r.status_code == 422 and "default_cpu_limit_m" in r.json()["detail"]
    # default and ceiling raised together is fine
    assert _put(admin, cpu_limit_max_m=1200, default_cpu_limit_m=1100).status_code == 200


def test_changing_default_cpu_changes_new_manifests(admin, user, db_session):
    aid = user.post("/apps", json=APP).json()["id"]
    row = db_session.get(models.App, aid)
    assert resources.effective(row)["cpu_limit_m"] == 200
    assert _put(admin, default_cpu_limit_m=300, default_mem_limit_mi=512).status_code == 200
    eff = resources.effective(row)
    assert eff["cpu_limit_m"] == 300 and eff["mem_limit_mi"] == 512
    c = k8s.deployment_manifest(aid, "img", 80, resources=eff)["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"]["limits"]["cpu"] == "300m" and c["resources"]["limits"]["memory"] == "512Mi"
    assert user.get("/auth/me").json()["limits"]["cpu_limit_m"] == 300
    # an app with its own override keeps it
    assert admin.patch(f"/admin/apps/{aid}/resources", json={"cpu_limit_m": 250}).status_code == 200
    assert resources.effective(db_session.get(models.App, aid))["cpu_limit_m"] == 250


def test_override_above_ceiling_rejected(admin, user):
    aid = user.post("/apps", json=APP).json()["id"]
    url = f"/admin/apps/{aid}/resources"
    assert admin.patch(url, json={"ephemeral_limit_mi": 2000}).status_code == 200
    assert _put(admin, override_ephemeral_max_mi=1024).status_code == 200
    r = admin.patch(url, json={"ephemeral_limit_mi": 2000})
    assert r.status_code == 422 and "at most 1024" in r.json()["detail"]


def test_admin_api_key_cannot_read_admin_views(admin, make_client):
    uid = next(u["id"] for u in admin.get("/admin/users").json() if u["email"] == "admin@example.com")
    for scope in ("read", "full"):
        raw = admin.post("/api/keys", json={"name": scope, "scope": scope}).json()["key"]
        c = make_client()
        h = {"Authorization": f"Bearer {raw}"}
        for path in ("/admin/settings", "/admin/users", "/admin/audit", "/admin/audit/export"):
            assert c.get(path, headers=h).status_code == 403, (scope, path)
        # key-accessible by design (not an admin read of platform internals): app list, db servers
        assert c.get("/admin/apps", headers=h).status_code == 200
    assert admin.get("/admin/settings").status_code == 200
    assert admin.get("/admin/audit").status_code == 200
    assert admin.get(f"/admin/users/{uid}/api-keys").status_code == 200
