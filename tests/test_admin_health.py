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


def test_platform_counts(make_client, kube, db_session):
    from datetime import datetime, timedelta
    from app import models
    admin = _admin(make_client)
    d = admin.get("/admin/health").json()
    assert d["users"] == {"total": 0, "active": 0, "pending": 0, "suspended": 0}  # uberadmin excluded
    base = datetime(2026, 1, 1)
    for i in range(22):
        db_session.add(models.User(email=f"p{i}@x.io", password_hash="x", role="user", status="pending",
                                   created_at=base + timedelta(minutes=30 - i)))
    for st in ("active", "active", "suspended", "deleted"):
        db_session.add(models.User(email=f"{st}{len(db_session.new)}@x.io", password_hash="x", role="user", status=st))
    db_session.commit()
    d = admin.get("/admin/health").json()  # not stale despite cache
    assert d["users"] == {"total": 25, "active": 2, "pending": 22, "suspended": 1}
    pend = d["pending_users"]
    assert len(pend) == 20 and pend[0]["email"] == "p21@x.io" and pend[1]["email"] == "p20@x.io"
    assert set(pend[0]) == {"id", "email", "created_at"}
    owner = db_session.query(models.User).filter_by(email="admin@example.com").one().id
    mk = lambda n, **kw: db_session.add(models.App(owner_id=owner, name=n, hostname=f"{n}.x.io", image="i", port=1, **kw))
    mk("a", status="running", want_db=True)
    mk("b", status="running", want_storage=True)
    mk("c", status="error", want_db=True, want_storage=True)
    mk("d", status="suspended")
    mk("e", status="running", want_db=True, deleted_at=datetime(2026, 1, 1))
    db_session.commit()
    w = admin.get("/admin/health").json()["workloads"]
    assert w == {"apps_total": 4, "with_db": 2, "with_storage": 2,
                 "by_status": {"running": 2, "deploying": 0, "pending": 0, "error": 1, "suspended": 1}}
