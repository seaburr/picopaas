"""Admin-controlled replica count: endpoint, reconciler render, replicas-aware quota, same-namespace policy."""
import pytest

from app import config, k8s_manifests as k8s, models
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import reconcile_all
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


def _put(c, aid, n):
    return c.put(f"/admin/apps/{aid}/replicas", json={"replicas": n})


def _sweep(db, kube):
    reconcile_all(db, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={})
    db.expire_all()


def _dep(kube, aid):
    return [m for m in kube.applied[f"app-{aid}"] if m["kind"] == "Deployment"][-1]


def test_non_admin_forbidden(admin, user):
    aid = user.post("/apps", json=APP).json()["id"]
    assert _put(user, aid, 2).status_code == 403
    assert user.get(f"/apps/{aid}").json()["replicas"] == 1


@pytest.mark.parametrize("n", [0, -1, config.MAX_REPLICAS + 1, "two", None, 1.5])
def test_bounds_422(admin, user, n):
    aid = user.post("/apps", json=APP).json()["id"]
    assert _put(admin, aid, n).status_code == 422
    assert user.get(f"/apps/{aid}").json()["replicas"] == 1


def test_extra_field_422_and_404(admin, user):
    aid = user.post("/apps", json=APP).json()["id"]
    assert admin.put(f"/admin/apps/{aid}/replicas", json={"replicas": 2, "x": 1}).status_code == 422
    assert _put(admin, 99999, 2).status_code == 404


def test_happy_path_persists_requeues_audits(admin, user, db_session):
    aid = user.post("/apps", json=APP).json()["id"]
    row = db_session.get(models.App, aid)
    row.status = "running"
    db_session.commit()
    r = _put(admin, aid, config.MAX_REPLICAS)
    assert r.status_code == 200 and r.json()["replicas"] == config.MAX_REPLICAS
    db_session.expire_all()
    row = db_session.get(models.App, aid)
    assert row.replicas == config.MAX_REPLICAS and row.status == "pending"
    actions = [a["action"] for a in admin.get("/admin/audit").json()]
    assert "app.replicas" in actions


def test_reconciler_renders_replicas_and_zero_when_suspended(admin, user, db_session):
    kube = FakeKubeProvider()
    aid = user.post("/apps", json=APP).json()["id"]
    _put(admin, aid, 2)
    _sweep(db_session, kube)
    assert _dep(kube, aid)["spec"]["replicas"] == 2
    admin.post(f"/admin/apps/{aid}/suspend")
    _put(admin, aid, 3)
    assert user.get(f"/apps/{aid}").json()["status"] == "suspended"  # not unsuspended
    _sweep(db_session, kube)
    assert _dep(kube, aid)["spec"]["replicas"] == 0
    admin.post(f"/admin/apps/{aid}/unsuspend")
    _sweep(db_session, kube)
    assert _dep(kube, aid)["spec"]["replicas"] == 3


def test_rolling_update_surges_by_one():
    m = k8s.render_app_manifests(1, "h", "img", 80, {}, None, replicas=3)
    assert m["deployment"]["spec"]["strategy"]["rollingUpdate"] == {"maxSurge": 1, "maxUnavailable": 0}


def _hard(replicas, **kw):
    return k8s.resource_quota_manifest(1, replicas=replicas, **kw)["spec"]["hard"]


def test_quota_scales_with_replicas():
    one, two = _hard(1), _hard(2)
    per_cpu, per_mem = config.DEFAULT_CPU_LIMIT_M, config.DEFAULT_MEM_LIMIT_MI
    assert one["pods"] == "2" and two["pods"] == "3"
    assert two["limits.cpu"] == f"{3 * per_cpu}m" and two["requests.cpu"] == f"{3 * per_cpu}m"
    assert two["limits.memory"] == f"{3 * per_mem}Mi"
    assert two["limits.ephemeral-storage"] == f"{3 * config.EPHEMERAL_LIMIT_MI}Mi"
    assert one["limits.cpu"] == f"{2 * per_cpu}m"  # single replica unchanged


def test_quota_two_replicas_admits_two_steady_plus_surge_and_keeps_used_max_and_solver():
    per = config.DEFAULT_CPU_LIMIT_M
    used = {"cpu_limit_m": 2 * per, "mem_limit_mi": 2 * config.DEFAULT_MEM_LIMIT_MI,
            "ephemeral_limit_mi": 2 * config.EPHEMERAL_LIMIT_MI, "pods": 2}
    h = _hard(2, used_limits=used)
    assert int(h["pods"]) >= 3 and int(h["limits.cpu"][:-1]) >= 3 * per
    # used + new beats the floor -> used-based max still applies
    big = {"cpu_limit_m": 10 * per, "mem_limit_mi": 1, "ephemeral_limit_mi": 1, "pods": 6}
    h = _hard(2, used_limits=big, custom_domain=True)
    assert h["limits.cpu"] == f"{11 * per}m" and h["pods"] == "8" and h["services"] == "2"
    assert _hard(2, custom_domain=True)["pods"] == "4"


def _policy_names(policies):
    return {p["metadata"]["name"] for p in policies}


def test_same_namespace_policy_only_when_replicas_gt_1():
    single = k8s.network_policies(7, 8080, None)
    assert "allow-same-namespace" not in _policy_names(single)
    multi = k8s.network_policies(7, 8080, None, replicas=2)
    pol = next(p for p in multi if p["metadata"]["name"] == "allow-same-namespace")
    assert pol["metadata"]["namespace"] == k8s.namespace_name(7)
    spec = pol["spec"]
    assert spec["podSelector"] == {} and set(spec["policyTypes"]) == {"Ingress", "Egress"}
    # Only a bare podSelector peer: no namespaceSelector / ipBlock, so strictly this namespace.
    assert spec["ingress"] == [{"from": [{"podSelector": {}}]}]
    assert spec["egress"] == [{"to": [{"podSelector": {}}]}]
    assert "namespaceSelector" not in str(spec) and "ipBlock" not in str(spec)
    # others unchanged
    assert _policy_names(multi) - {"allow-same-namespace"} == _policy_names(single)


def test_render_threads_replicas_into_quota_and_policies():
    m = k8s.render_app_manifests(1, "h", "img", 80, {}, None, replicas=2)
    assert m["resource_quota"]["spec"]["hard"]["pods"] == "3"
    assert "allow-same-namespace" in _policy_names(m["network_policies"])
