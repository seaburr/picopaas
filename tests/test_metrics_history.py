"""Per-app CPU/memory history: leader sampling job, retention, endpoint authz."""
import pytest

from app import config, health, models
from app.dependencies import get_kube
from app.main import app as fastapi_app
from app.providers.fake import FakeKubeProvider
from tests.conftest import register_and_login

APP = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


def _pod(ns, cpu=60, mem=150):
    return {"name": "p", "namespace": ns, "node": "n1", "phase": "Running", "ready": True, "restarts": 0,
            "cpu_used_m": cpu, "mem_used_mi": mem}


@pytest.fixture()
def kube():
    health.reset_cache()
    k = FakeKubeProvider()
    fastapi_app.dependency_overrides[get_kube] = lambda: k
    yield k
    health.reset_cache()


def _snap(kube, pods, available=True):
    kube.snapshot = {"nodes": [], "pods": pods, "metrics_available": available, "errors": []}
    health.reset_cache()


def _rows(db, aid):
    return db.query(models.AppMetricSample).filter_by(app_id=aid).order_by(models.AppMetricSample.id).all()


def test_sample_inserts_and_endpoint_authz(make_client, db_session, kube):
    c = make_client()
    register_and_login(c, "owner@example.com")
    aid = c.post("/apps", json=APP).json()["id"]
    _snap(kube, [_pod(f"app-{aid}")])
    assert health.sample_app_metrics(db_session, kube) == 1
    r = c.get(f"/apps/{aid}/metrics-history")
    assert r.status_code == 200
    d = r.json()
    assert [(p["cpu_m"], p["mem_mi"]) for p in d["points"]] == [(60, 150)]
    assert d["cpu_limit_m"] == config.DEFAULT_CPU_LIMIT_M and d["mem_limit_mi"] == config.DEFAULT_MEM_LIMIT_MI
    other = make_client()
    register_and_login(other, "other@example.com")
    assert other.get(f"/apps/{aid}/metrics-history").status_code == 404
    assert make_client().get(f"/apps/{aid}/metrics-history").status_code == 401


def test_retention_prunes_oldest(make_client, db_session, kube, monkeypatch):
    monkeypatch.setattr(config, "METRICS_RETENTION_POINTS", 3)
    c = make_client()
    register_and_login(c, "owner@example.com")
    aid = c.post("/apps", json=APP).json()["id"]
    for cpu in (1, 2, 3, 4, 5):
        _snap(kube, [_pod(f"app-{aid}", cpu=cpu)])
        health.sample_app_metrics(db_session, kube)
    assert [r.cpu_m for r in _rows(db_session, aid)] == [3, 4, 5]
    pts = c.get(f"/apps/{aid}/metrics-history").json()["points"]
    assert [p["cpu_m"] for p in pts] == [3, 4, 5]


def test_skips_suspended_no_metrics_and_unavailable(make_client, db_session, kube):
    c = make_client()
    register_and_login(c, "owner@example.com")
    a = c.post("/apps", json=APP).json()["id"]
    b = c.post("/apps", json={**APP, "name": "two"}).json()["id"]
    d = c.post("/apps", json={**APP, "name": "three"}).json()["id"]
    db_session.get(models.App, b).status = "suspended"
    db_session.commit()
    _snap(kube, [_pod(f"app-{a}"), _pod(f"app-{b}"), _pod(f"app-{d}", cpu=None, mem=None)])
    assert health.sample_app_metrics(db_session, kube) == 1
    assert len(_rows(db_session, a)) == 1 and not _rows(db_session, b) and not _rows(db_session, d)
    _snap(kube, [_pod(f"app-{a}")], available=False)
    assert health.sample_app_metrics(db_session, kube) == 0
    assert len(_rows(db_session, a)) == 1


def test_empty_history(make_client, kube):
    c = make_client()
    register_and_login(c, "owner@example.com")
    aid = c.post("/apps", json=APP).json()["id"]
    assert c.get(f"/apps/{aid}/metrics-history").json()["points"] == []
