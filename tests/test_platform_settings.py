"""Admin > Settings: registry, GET/PUT /admin/settings, and the effective value reaching enforcement paths."""
import pytest

from app import config, models, platform_settings
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


def _by_key(c):
    r = c.get("/admin/settings")
    assert r.status_code == 200, r.text
    return {s["key"]: s for g in r.json()["groups"] for s in g["settings"]}


def _put(c, **kv):
    return c.put("/admin/settings", json={"settings": kv})


def test_non_admin_and_anonymous(admin, user, make_client):
    assert user.get("/admin/settings").status_code == 403
    assert _put(user, max_replicas=5).status_code == 403
    assert make_client().get("/admin/settings").status_code == 401


def test_get_shape_defaults_and_backup_keys(admin):
    r = admin.get("/admin/settings").json()
    assert [g["name"] for g in r["groups"]] == ["Limit ceilings", "Defaults and quotas", "Backups"]
    s = _by_key(admin)
    assert s["max_replicas"]["value"] == config.MAX_REPLICAS and s["max_replicas"]["overridden"] is False
    assert s["max_replicas"]["default"] == config.MAX_REPLICAS and s["max_replicas"]["min"] == 1
    assert s["backup_max_held_per_app"]["value"] == 14
    assert s["backup_manual_per_hour"]["value"] == 1 and s["backup_export_max_per_hour"]["value"] == 3
    assert s["backups_enabled_default"]["type"] == "bool"
    for k in ("backup_max_bytes_total", "backup_max_bytes_per_app"):
        assert k in s


def test_validation_errors(admin):
    for body in ({"max_replicas": 0}, {"max_replicas": 9999}, {"max_replicas": "3"}, {"max_replicas": True},
                 {"nope": 1}, {"backups_enabled_default": 1}):
        r = _put(admin, **body)
        assert r.status_code == 422, body
        assert isinstance(r.json()["detail"], str)
    assert admin.put("/admin/settings", json={"settings": {}}).status_code == 422
    assert admin.put("/admin/settings", json={"settings": {"max_replicas": 2}, "x": 1}).status_code == 422
    assert _by_key(admin)["max_replicas"]["overridden"] is False


def test_invariants(admin):
    r = _put(admin, volume_max_gi=50)  # above the admin volume ceiling (10)
    assert r.status_code == 422 and "override_volume_max_gi" in r.json()["detail"]
    assert _put(admin, override_volume_max_gi=50, volume_max_gi=50).status_code == 200
    r = _put(admin, override_volume_max_gi=5)  # would drop below the user ceiling
    assert r.status_code == 422
    r = _put(admin, default_app_limit=99)
    assert r.status_code == 422
    r = _put(admin, backup_max_bytes_per_app=config.BACKUP_MAX_BYTES_TOTAL + 1)
    assert r.status_code == 422


def test_set_and_reset_audited(admin, db_session):
    r = _put(admin, max_replicas=7, backups_enabled_default=True)
    assert r.status_code == 200
    s = _by_key(admin)
    assert s["max_replicas"]["value"] == 7 and s["max_replicas"]["overridden"] is True
    assert s["max_replicas"]["updated_by"] == "admin@example.com"
    assert s["backups_enabled_default"]["value"] is True
    assert platform_settings.get(db_session, "max_replicas") == 7
    r = _put(admin, max_replicas=None)
    assert r.status_code == 200
    s = _by_key(admin)
    assert s["max_replicas"]["value"] == config.MAX_REPLICAS and s["max_replicas"]["overridden"] is False
    actions = [a.action for a in db_session.query(models.AuditLog).all()]
    assert actions.count("settings.update") == 3


def test_replicas_ceiling_uses_effective_value(admin, user):
    aid = user.post("/apps", json=APP).json()["id"]
    over = config.MAX_REPLICAS + 1
    assert admin.put(f"/admin/apps/{aid}/replicas", json={"replicas": over}).status_code == 422
    assert _put(admin, max_replicas=over).status_code == 200
    assert admin.put(f"/admin/apps/{aid}/replicas", json={"replicas": over}).status_code == 200
    assert _put(admin, max_replicas=1).status_code == 200
    assert admin.put(f"/admin/apps/{aid}/replicas", json={"replicas": 2}).status_code == 422


def test_resource_ceiling_uses_effective_value(admin, user):
    aid = user.post("/apps", json=APP).json()["id"]
    url = f"/admin/apps/{aid}/resources"
    assert admin.patch(url, json={"mem_limit_mi": 4096}).status_code == 422  # default ceiling 2048
    assert _put(admin, mem_limit_max_mi=4096).status_code == 200
    assert admin.patch(url, json={"mem_limit_mi": 4096}).status_code == 200
    assert _put(admin, override_egress_max_mbit=10).status_code == 200
    r = admin.patch(url, json={"egress_mbit": 20})
    assert r.status_code == 422 and "egress_mbit" in r.json()["detail"]
    assert user.get("/auth/me").json()["limits"]["override"]["mem_max_mi"] == 4096


def test_app_limit_settings(admin, user):
    me = admin.get("/auth/me").json()
    assert me["max_apps"] == config.MAX_APPS_PER_USER
    assert _put(admin, max_apps_per_user=2).status_code == 200
    assert admin.get("/auth/me").json()["max_apps"] == 2
    uid = user.get("/auth/me").json()["id"]
    assert admin.patch(f"/admin/users/{uid}", json={"app_limit": 3}).status_code == 422
    assert admin.patch(f"/admin/users/{uid}", json={"app_limit": 2}).status_code == 200


def test_default_app_limit_for_new_accounts(admin, make_client):
    assert _put(admin, default_app_limit=2).status_code == 200
    c = make_client()
    r = register_and_login(c, "new@example.com")
    assert r.json()["app_limit"] == 2


def test_volume_user_ceiling_effective(admin, user, monkeypatch):
    monkeypatch.setattr(config, "VOLUMES_ENABLED", True)
    vol = {**APP, "volume": {"size_gi": 3}}
    assert user.post("/apps", json=vol).status_code == 422  # default user ceiling 1
    assert _put(admin, volume_max_gi=3).status_code == 200
    assert user.post("/apps", json=vol).status_code == 201
    assert user.get("/auth/me").json()["limits"]["volume_max_gi"] == 3


def test_cache_expiry_sees_other_replica_write(admin, db_session, monkeypatch):
    assert platform_settings.get(db_session, "max_replicas") == config.MAX_REPLICAS
    db_session.add(models.PlatformSetting(key="max_replicas", value="9"))
    db_session.commit()
    assert platform_settings.get(db_session, "max_replicas") == config.MAX_REPLICAS  # cached
    monkeypatch.setattr(platform_settings, "CACHE_TTL_SECONDS", 0)
    assert platform_settings.get(db_session, "max_replicas") == 9
