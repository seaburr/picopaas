"""Per-app resource overrides (platform_admin): validation, reset, manifests, quota scaling, audit."""
import pytest

from app import health, k8s_manifests as k8s, models
from app.dependencies import get_kube
from app.main import app
from app.providers.fake import FakeKubeProvider
from tests.conftest import register_and_login

APP = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
DEFAULTS = {"cpu_request_m": 64, "cpu_limit_m": 100, "mem_request_mi": 128, "mem_limit_mi": 384,
            "overridden": False, "exceeds_platform_caps": False}


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def aid(admin):
    r = admin.post("/apps", json=APP)
    assert r.json()["resources"] == DEFAULTS
    return r.json()["id"]


def test_set_and_response(admin, aid, db_session):
    r = admin.patch(f"/admin/apps/{aid}/resources", json={"cpu_limit_m": 500, "mem_limit_mi": 1024})
    assert r.status_code == 200
    d = r.json()
    assert d["resources"] == {"cpu_request_m": 64, "cpu_limit_m": 500, "mem_request_mi": 128, "mem_limit_mi": 1024,
                              "overridden": True, "exceeds_platform_caps": True}
    assert d["exceeds_platform_caps"] is True
    assert admin.get(f"/apps/{aid}").json()["resources"]["cpu_limit_m"] == 500
    assert admin.get("/admin/apps").json()[0]["resources"]["mem_limit_mi"] == 1024
    db_session.expire_all()
    assert db_session.get(models.App, aid).status == "pending"


def test_within_caps_not_flagged(admin, aid):
    d = admin.patch(f"/admin/apps/{aid}/resources", json={"cpu_limit_m": 90}).json()
    assert d["resources"]["overridden"] and not d["resources"]["exceeds_platform_caps"]
    assert d["exceeds_platform_caps"] is False


def test_reset_to_default(admin, aid):
    admin.patch(f"/admin/apps/{aid}/resources", json={"cpu_limit_m": 500, "mem_request_mi": 256})
    r = admin.patch(f"/admin/apps/{aid}/resources", json={"cpu_limit_m": None})
    assert r.json()["resources"]["cpu_limit_m"] == 100 and r.json()["resources"]["mem_request_mi"] == 256  # omitted = kept
    r = admin.patch(f"/admin/apps/{aid}/resources",
                    json={"cpu_request_m": None, "cpu_limit_m": None, "mem_request_mi": None, "mem_limit_mi": None})
    assert r.json()["resources"] == DEFAULTS


@pytest.mark.parametrize("body", [
    {"cpu_limit_m": 9}, {"cpu_limit_m": 1001}, {"cpu_request_m": 5}, {"mem_limit_mi": 31}, {"mem_limit_mi": 2049},
    {"mem_request_mi": 31}, {"cpu_request_m": 300}, {"cpu_request_m": 200, "cpu_limit_m": 100}, {"cpu_request_m": 101},
    {"mem_request_mi": 600}, {"mem_request_mi": 600, "mem_limit_mi": 512}, {"mem_request_mi": 385}, {"bogus": 1}, {"cpu_limit_m": "x"},
])
def test_validation_rejected(admin, aid, body):
    assert admin.patch(f"/admin/apps/{aid}/resources", json=body).status_code == 422


@pytest.mark.parametrize("body", [
    {"cpu_limit_m": 10, "cpu_request_m": 10}, {"cpu_limit_m": 1000}, {"mem_limit_mi": 2048},
    {"cpu_request_m": 100}, {"mem_request_mi": 384}, {"mem_limit_mi": 32, "mem_request_mi": 32},
])
def test_validation_bounds_accepted(admin, aid, body):
    if body.get("cpu_limit_m") == 10 or body.get("mem_limit_mi") == 32:
        body = {**body}
    r = admin.patch(f"/admin/apps/{aid}/resources", json=body)
    assert r.status_code == 200, r.text


def test_not_found_and_non_admin(admin, aid, make_client):
    assert admin.patch("/admin/apps/9999/resources", json={"cpu_limit_m": 100}).status_code == 404
    user = make_client()
    register_and_login(user, "user@example.com")
    assert user.patch(f"/admin/apps/{aid}/resources", json={"cpu_limit_m": 100}).status_code == 403
    assert make_client().patch(f"/admin/apps/{aid}/resources", json={}).status_code in (401, 403)


def test_audit(admin, aid):
    admin.patch(f"/admin/apps/{aid}/resources", json={"cpu_limit_m": 500})
    e = [x for x in admin.get("/admin/audit").json() if x["action"] == "app.resources"][0]
    assert e["target_id"] == str(aid)
    assert "cpu_limit_m=None" in e["detail"] and "cpu_limit_m=500" in e["detail"] and "->" in e["detail"]


def test_manifests_default_unchanged():
    q = k8s.resource_quota_manifest(1)["spec"]["hard"]
    assert (q["limits.cpu"], q["limits.memory"], q["requests.cpu"], q["requests.memory"]) == ("200m", "768Mi", "200m", "768Mi")
    lr = k8s.limit_range_manifest(1)["spec"]["limits"][0]
    assert lr["max"] == {"cpu": "100m", "memory": "384Mi", "ephemeral-storage": "250Mi"}
    c = k8s.deployment_manifest(1, "img", 80)["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"] == {"requests": {"cpu": "64m", "memory": "128Mi", "ephemeral-storage": "64Mi"}, "limits": {"cpu": "100m", "memory": "384Mi", "ephemeral-storage": "250Mi"}}


def test_manifests_scale_with_overrides():
    eff = {"cpu_request_m": 100, "cpu_limit_m": 600, "mem_request_mi": 256, "mem_limit_mi": 1024}
    m = k8s.render_app_manifests(1, "h", "img", 80, {}, None, resources=eff)
    q = m["resource_quota"]["spec"]["hard"]
    assert (q["limits.cpu"], q["limits.memory"]) == ("1200m", "2048Mi")
    assert m["limit_range"]["spec"]["limits"][0]["max"] == {"cpu": "600m", "memory": "1024Mi", "ephemeral-storage": "250Mi"}
    c = m["deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"]["limits"] == {"cpu": "600m", "memory": "1024Mi", "ephemeral-storage": "250Mi"}
    assert c["resources"]["requests"] == {"cpu": "100m", "memory": "256Mi", "ephemeral-storage": "64Mi"}


def test_reconciler_applies_effective_resources_and_health(admin, aid, db_session):
    from app import reconciler
    from app.providers.fake import FakeMySQLAdmin, FakeObjectStore
    from types import SimpleNamespace
    admin.patch(f"/admin/apps/{aid}/resources", json={"cpu_limit_m": 400, "mem_limit_mi": 768})
    kube = FakeKubeProvider()
    reconciler.reconcile_all(db_session, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                             object_store=FakeObjectStore(),
                             settings=SimpleNamespace(mysql_egress_mode="cidr", mysql_cidr=None))
    ns = f"app-{aid}"
    quota = [m for m in kube.applied[ns] if m["kind"] == "ResourceQuota"][-1]
    assert quota["spec"]["hard"]["limits.cpu"] == "800m" and quota["spec"]["hard"]["limits.memory"] == "1536Mi"
    health.reset_cache()
    app.dependency_overrides[get_kube] = lambda: kube
    a = admin.get("/admin/health").json()["apps"][0]
    assert (a["cpu_limit_m"], a["mem_limit_mi"]) == (400, 768)
    health.reset_cache()
