"""Multi-pod logs (per-pod sections, ?pod=) and metrics/health aggregate capacity."""
import pytest

from app import config, health, models
from app.dependencies import get_kube
from app.main import app as fastapi_app
from app.providers.fake import FakeKubeProvider
from tests.conftest import register_and_login

APP = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


@pytest.fixture()
def kube():
    health.reset_cache()
    k = FakeKubeProvider()
    fastapi_app.dependency_overrides[get_kube] = lambda: k
    yield k
    health.reset_cache()


@pytest.fixture()
def owner(make_client, kube):
    c = make_client()
    register_and_login(c, "owner@example.com")
    return c


def _pod(ns, name, cpu, mem):
    return {"name": name, "namespace": ns, "node": "n1", "phase": "Running", "ready": True, "restarts": 0,
            "cpu_used_m": cpu, "mem_used_mi": mem}


def test_logs_all_pods_grouped_and_per_pod_tail(owner, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    kube.pod_lines[f"app-{aid}"] = {"web-b": ["b1", "b2", "b3"], "web-a": ["a1", "a2", "a3"]}
    d = owner.get(f"/apps/{aid}/logs?tail=2").json()
    assert d["pods"] == ["web-a", "web-b"] and d["pod"] is None
    assert [(s["pod"], s["lines"]) for s in d["sections"]] == [("web-a", ["a2", "a3"]), ("web-b", ["b2", "b3"])]
    assert d["lines"] == ["=== pod web-a ===", "a2", "a3", "=== pod web-b ===", "b2", "b3"]


def test_logs_pick_one_pod_and_unknown_is_404(owner, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    kube.pod_lines[f"app-{aid}"] = {"web-a": ["a1"], "web-b": ["b1"]}
    d = owner.get(f"/apps/{aid}/logs?pod=web-b").json()
    assert d["pod"] == "web-b" and d["lines"] == ["b1"] and d["pods"] == ["web-a", "web-b"]
    assert owner.get(f"/apps/{aid}/logs?pod=nope").status_code == 404


def test_logs_no_pod_note_and_authz(owner, make_client, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    d = owner.get(f"/apps/{aid}/logs").json()
    assert d["note"] == "No running pod yet." and d["pods"] == [] and d["sections"] == []
    other = make_client()
    register_and_login(other, "other@example.com")
    assert other.get(f"/apps/{aid}/logs?pod=x").status_code == 404


def test_metrics_aggregate_capacity_and_per_pod(owner, kube, db_session):
    aid = owner.post("/apps", json=APP).json()["id"]
    db_session.get(models.App, aid).replicas = 2
    db_session.commit()
    ns = f"app-{aid}"
    kube.snapshot = {"nodes": [], "pods": [_pod(ns, "web-a", 200, 100), _pod(ns, "web-b", 207, 136)],
                     "metrics_available": True, "errors": []}
    health.reset_cache()
    assert health.sample_app_metrics(db_session, kube) == 1
    d = owner.get(f"/apps/{aid}/metrics-history").json()
    pt = d["points"][0]
    assert (pt["cpu_m"], pt["mem_mi"], pt["pods"]) == (407, 236, 2)
    assert pt["per_pod"] == {"web-a": {"cpu_m": 200, "mem_mi": 100}, "web-b": {"cpu_m": 207, "mem_mi": 136}}
    assert d["replicas"] == 2 and d["pods"] == 2
    assert d["cpu_capacity_m"] == 2 * config.DEFAULT_CPU_LIMIT_M == 2 * d["cpu_limit_m"]
    assert d["mem_capacity_mi"] == 2 * config.DEFAULT_MEM_LIMIT_MI


def test_metrics_old_samples_without_pod_info_do_not_crash(owner, db_session):
    aid = owner.post("/apps", json=APP).json()["id"]
    from datetime import datetime
    db_session.add(models.AppMetricSample(app_id=aid, ts=datetime(2026, 1, 1), cpu_m=5, mem_mi=6))
    db_session.add(models.AppMetricSample(app_id=aid, ts=datetime(2026, 1, 2), cpu_m=5, mem_mi=6, per_pod="{bad"))
    db_session.commit()
    d = owner.get(f"/apps/{aid}/metrics-history").json()
    assert [p["pods"] for p in d["points"]] == [1, 1] and d["points"][0]["per_pod"] == {}
    assert d["replicas"] == 1 and d["cpu_capacity_m"] == d["cpu_limit_m"]


def test_health_apps_carry_aggregate_capacity(owner, kube, db_session, make_client):
    aid = owner.post("/apps", json=APP).json()["id"]
    db_session.get(models.App, aid).replicas = 3
    db_session.commit()
    ns = f"app-{aid}"
    kube.snapshot = {"nodes": [], "pods": [_pod(ns, "web-b", 1, 2), _pod(ns, "web-a", 3, 4)],
                     "metrics_available": True, "errors": []}
    health.reset_cache()
    out = health.build(db_session, kube, [])
    a = next(x for x in out["apps"] if x["id"] == aid)
    assert a["replicas"] == 3 and a["cpu_used_m"] == 4 and a["mem_used_mi"] == 6
    assert a["cpu_capacity_m"] == 3 * config.DEFAULT_CPU_LIMIT_M
    assert a["mem_capacity_mi"] == 3 * config.DEFAULT_MEM_LIMIT_MI
    assert a["cpu_limit_m"] == config.DEFAULT_CPU_LIMIT_M
    assert [p["name"] for p in a["pod_usage"]] == ["web-a", "web-b"]
