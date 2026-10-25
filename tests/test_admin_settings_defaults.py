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


def test_admin_app_limit_pair(admin, user, db_session):
    s = {x["key"]: x for g in admin.get("/admin/settings").json()["groups"] for x in g["settings"]}
    assert s["admin_default_app_limit"]["value"] == 5 and s["max_apps_per_admin"]["value"] == 10
    assert s["admin_default_app_limit"]["ceiling_key"] == "max_apps_per_admin"
    assert s["max_apps_per_admin"]["default_key"] == "admin_default_app_limit"
    assert s["admin_default_app_limit"]["row"] == s["max_apps_per_admin"]["row"] == "apps_admin"
    assert (s["admin_default_app_limit"]["column"], s["max_apps_per_admin"]["column"]) == ("default", "ceiling")
    assert _put(admin, admin_default_app_limit=11).status_code == 422
    assert _put(admin, max_apps_per_admin=4).status_code == 422  # default 5 > ceiling 4
    assert _put(admin, admin_default_app_limit=2, max_apps_per_admin=4).status_code == 200
    me = admin.get("/auth/me").json()
    assert me["app_limit_effective"] == 2 and me["max_apps"] == 4
    # the user pair is untouched and still governs regular users
    u = user.get("/auth/me").json()
    assert u["app_limit_effective"] == 1 and u["max_apps"] == 5


def test_admin_user_edit_uses_ceiling_for_admins(admin, make_client, db_session):
    c2 = make_client()
    register_and_login(c2, "second@example.com")
    uid = db_session.query(models.User).filter_by(email="second@example.com").one().id
    db_session.query(models.User).filter_by(id=uid).one().role = "platform_admin"
    db_session.commit()
    assert admin.patch(f"/admin/users/{uid}", json={"app_limit": 10}).status_code == 200
    assert admin.patch(f"/admin/users/{uid}", json={"app_limit": 11}).status_code == 422
    assert admin.get("/admin/users").status_code == 200


def test_settings_rows_one_per_tunable(admin):
    groups = admin.get("/admin/settings").json()["groups"]
    rows = {}
    for g in groups:
        for x in g["settings"]:
            assert x["row"] and x["row_label"] and x["column"] in ("default", "ceiling")
            cols = rows.setdefault((g["name"], x["row"]), [])
            assert x["column"] not in cols
            cols.append(x["column"])
    bk = {x["key"]: x for g in groups if g["name"] == "Backups" for x in g["settings"]}
    assert bk["backup_max_bytes_per_app"]["ceiling_key"] == "backup_override_max_bytes_per_app"
    assert bk["backup_max_held_per_app"]["ceiling_key"] is None and bk["backup_max_held_per_app"]["column"] == "default"


def test_backup_override_ceiling(admin):
    gib = 1024**3
    assert _put(admin, backup_override_max_bytes_per_app=1024 * gib).status_code == 422  # above the platform total
    assert _put(admin, backup_max_bytes_per_app=3 * gib, backup_override_max_bytes_per_app=2 * gib).status_code == 422
    assert _put(admin, backup_max_bytes_per_app=gib, backup_override_max_bytes_per_app=2 * gib).status_code == 200
    from app import backups
    from app.db import get_db  # noqa: F401
