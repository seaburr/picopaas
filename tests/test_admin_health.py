"""GET /admin/health: shape, auth, caching, metrics-unavailable; quantity parsing."""
import pytest

from app import health
from app.dependencies import get_kube
from app.main import app
from app.providers.fake import FakeKubeProvider
from app.providers.kube import parse_cpu_m, parse_mem_mi
from tests.conftest import register_and_login

APP = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


def _node(**kw):
    n = {"name": "n1", "ready": True, "age_s": 100, "cpu_capacity_m": 2000, "cpu_allocatable_m": 1900,
         "cpu_requested_m": 300, "cpu_used_m": 120, "mem_capacity_mi": 4096, "mem_allocatable_mi": 3500,
         "mem_requested_mi": 600, "mem_used_mi": 900, "pods": 3, "pods_capacity": 110}
    n.update(kw)
    return n


def _pod(name, ns, **kw):
    p = {"name": name, "namespace": ns, "node": "n1", "phase": "Running", "ready": True, "restarts": 0,
         "cpu_used_m": 10, "mem_used_mi": 50}
    p.update(kw)
    return p


@pytest.fixture()
def kube():
    health.reset_cache()
    k = FakeKubeProvider()
    app.dependency_overrides[get_kube] = lambda: k
    yield k
    health.reset_cache()


def _admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


def test_shape(make_client, kube):
    admin = _admin(make_client)
    aid = admin.post("/apps", json=APP).json()["id"]
    kube.snapshot = {
        "nodes": [_node()],
        "pods": [_pod("web-1", f"app-{aid}", restarts=2), _pod("api-0", "picopaas-system"),
                 _pod("t-0", "traefik", ready=False, cpu_used_m=None, mem_used_mi=None),
                 _pod("other", "kube-system")],
        "metrics_available": True, "errors": [],
    }
    r = admin.get("/admin/health")
    assert r.status_code == 200
    d = r.json()
    assert d["metrics_available"] is True and d["errors"] == []
    assert d["cluster"]["node_count"] == 1 and d["cluster"]["pods_total"] == 4
    assert "max_nodes" in d["cluster"]
    assert d["nodes"][0]["cpu_requested_m"] == 300
    a = d["apps"][0]
    assert a["owner_email"] == "admin@example.com" and a["namespace"] == f"app-{aid}"
    assert (a["pods_ready"], a["pods_total"], a["restarts"]) == (1, 1, 2)
    assert (a["cpu_limit_m"], a["mem_limit_mi"]) == (250, 512)
    assert [p["name"] for p in d["platform"]] == ["api-0", "t-0"]
    assert d["platform"][1]["ready"] is False and d["platform"][1]["cpu_used_m"] is None
    assert isinstance(d["db_servers"], list) and "generated_at" in d


def test_non_admin_forbidden_and_anonymous(make_client, kube):
    _admin(make_client)
    user = make_client()
    register_and_login(user, "user@example.com")
    assert user.get("/admin/health").status_code == 403
    assert make_client().get("/admin/health").status_code in (401, 403)


def test_cached_15s(make_client, kube, monkeypatch):
    admin = _admin(make_client)
    admin.get("/admin/health")
    admin.get("/admin/health")
    assert kube.snapshot_calls == 1
    t = health.time.monotonic() + 16
    monkeypatch.setattr(health.time, "monotonic", lambda: t)
    admin.get("/admin/health")
    assert kube.snapshot_calls == 2


def test_metrics_unavailable(make_client, kube):
    admin = _admin(make_client)
    kube.snapshot = {
        "nodes": [_node(cpu_used_m=None, mem_used_mi=None)],
        "pods": [_pod("api-0", "picopaas-system", cpu_used_m=None, mem_used_mi=None)],
        "metrics_available": False, "errors": ["metrics.k8s.io unavailable (ApiException)"],
    }
    d = admin.get("/admin/health").json()
    assert d["metrics_available"] is False
    assert d["nodes"][0]["cpu_used_m"] is None and d["platform"][0]["mem_used_mi"] is None
    assert d["errors"]


def test_snapshot_failure_is_secret_free(make_client, kube):
    admin = _admin(make_client)

    def boom():
        raise RuntimeError("token=abc123")

    kube.cluster_snapshot = boom
    d = admin.get("/admin/health").json()
    assert d["metrics_available"] is False and "abc123" not in str(d["errors"])


@pytest.mark.parametrize("q,m", [("250m", 250), ("1", 1000), ("2", 2000), ("1000000n", 1), ("500000000n", 500)])
def test_parse_cpu(q, m):
    assert parse_cpu_m(q) == m


@pytest.mark.parametrize("q,mi", [("512Mi", 512), ("1Gi", 1024), ("123456Ki", 120), ("536870912", 512)])
def test_parse_mem(q, mi):
    assert parse_mem_mi(q) == mi
