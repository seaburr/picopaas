"""Recent events: restarts the owner would otherwise never hear about (disk eviction, OOM, crash)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import config, k8s_manifests, models, reconciler
from app.dependencies import get_kube
from app.main import app as fastapi_app
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.providers.kube import KubeClientProvider, pod_events
from tests import test_kube_provider as tk
from tests.conftest import register_and_login
from tests.test_reconciler import _make_app, session  # noqa: F401  (session fixture)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ev(key, kind, at=None, code=None):
    return {"key": key, "at": at or _utcnow(), "kind": kind, "exit_code": code}


def _running_app(session):
    app = _make_app(session)
    app.status = "running"
    session.commit()
    return app


def _sweep(session, kube):
    reconciler.reconcile_all(
        session, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=FakeObjectStore(), settings={}
    )


def _events(session, app):
    return session.query(models.AppEvent).filter_by(app_id=app.id).order_by(models.AppEvent.at.desc()).all()


# --- detection (real provider's pod parsing) ---------------------------------

EVICTED_POD = {
    "metadata": {"uid": "u1", "name": "p", "namespace": "app-7"},
    "status": {
        "phase": "Failed", "reason": "Evicted",
        "message": "Pod ephemeral local storage usage exceeds the total limit of containers 250Mi.",
        "conditions": [{"type": "DisruptionTarget", "lastTransitionTime": "2026-10-20T12:00:30Z"}],
    },
}


def _cpod(last, uid="u2", restarts=1):
    return {"metadata": {"uid": uid, "name": "p", "namespace": "app-7"},
            "status": {"phase": "Running", "containerStatuses": [{"restartCount": restarts, "lastState": {"terminated": last}}]}}


def test_pod_events_kinds():
    ev = pod_events(EVICTED_POD)
    assert [(e["kind"], e["key"]) for e in ev] == [("disk", "u1:evicted")]
    assert ev[0]["at"] == datetime(2026, 10, 20, 12, 0, 30)
    other = {**EVICTED_POD, "status": {**EVICTED_POD["status"], "message": "The node was low on resource: memory."}}
    assert pod_events(other)[0]["kind"] == "evicted"
    oom = pod_events(_cpod({"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-10-20T12:00:00Z"}))
    assert [e["kind"] for e in oom] == ["oom"]
    crash = pod_events(_cpod({"reason": "Error", "exitCode": 3, "finishedAt": "2026-10-20T12:00:00Z"}))
    assert [(e["kind"], e["exit_code"]) for e in crash] == [("crashed", 3)]
    assert pod_events(_cpod({"reason": "Completed", "exitCode": 0, "finishedAt": "2026-10-20T12:00:00Z"})) == []
    assert pod_events(_cpod({})) == []
    # a second restart of the same pod is a different occurrence
    again = pod_events(_cpod({"reason": "Error", "exitCode": 3, "finishedAt": "2026-10-20T12:05:00Z"}, restarts=2))
    assert again[0]["key"] != crash[0]["key"]


def test_provider_lists_pods_once_and_only_app_namespaces():
    other = {"metadata": {"uid": "x", "name": "t", "namespace": "traefik"}, "status": EVICTED_POD["status"]}
    dyn = tk._dyn(pods=[EVICTED_POD, other])
    out = KubeClientProvider(dyn).app_pod_events()
    assert list(out) == ["app-7"] and out["app-7"][0]["kind"] == "disk"


# --- recording ---------------------------------------------------------------

def test_same_eviction_over_many_sweeps_is_one_event(session):
    app = _running_app(session)
    kube = FakeKubeProvider()
    kube.pod_events[k8s_manifests.namespace_name(app.id)] = [_ev("u1:evicted", "disk")]
    for _ in range(4):
        _sweep(session, kube)
    rows = _events(session, app)
    assert len(rows) == 1 and rows[0].kind == "disk"
    assert f"{config.EPHEMERAL_LIMIT_MI} MB of local disk" in rows[0].message and "storage bucket" in rows[0].message
    assert kube.pod_event_calls == 4  # one pod list per sweep, not per app


def test_each_kind_message(session):
    app = _running_app(session)
    kube = FakeKubeProvider()
    ns = k8s_manifests.namespace_name(app.id)
    t = _utcnow()
    kube.pod_events[ns] = [
        _ev("a", "disk", t - timedelta(minutes=3)), _ev("b", "evicted", t - timedelta(minutes=2)),
        _ev("c", "oom", t - timedelta(minutes=1)), _ev("d", "crashed", t, code=7),
    ]
    _sweep(session, kube)
    by = {e.kind: e.message for e in _events(session, app)}
    assert set(by) == {"disk", "evicted", "oom", "crashed"}
    assert "ran out of memory (limit 384 MB)" in by["oom"] or f"limit {config.DEFAULT_MEM_LIMIT_MI} MB" in by["oom"]
    assert by["crashed"] == "Restarted: your app exited with code 7. Check the logs."
    assert [e.kind for e in _events(session, app)] == ["crashed", "oom", "evicted", "disk"]  # newest first


def test_only_newest_20_kept_and_not_readded(session):
    app = _running_app(session)
    kube = FakeKubeProvider()
    ns = k8s_manifests.namespace_name(app.id)
    t = _utcnow() - timedelta(hours=1)
    kube.pod_events[ns] = [_ev(f"k{i}", "crashed", t + timedelta(minutes=i), code=1) for i in range(25)]
    _sweep(session, kube)
    rows = _events(session, app)
    assert len(rows) == 20
    assert {r.dedupe_key for r in rows} == {f"k{i}" for i in range(5, 25)}
    _sweep(session, kube)  # pruned ones are not resurrected
    assert len(_events(session, app)) == 20


def test_events_not_recorded_for_non_running_apps(session):
    app = _make_app(session)  # pending -> reconciled to running by the sweep, but only running apps are scanned
    app.status = "error"
    app.error_retryable = False
    session.commit()
    kube = FakeKubeProvider()
    kube.pod_events[k8s_manifests.namespace_name(app.id)] = [_ev("u1:evicted", "disk")]
    _sweep(session, kube)
    assert _events(session, app) == []


def test_provider_failure_does_not_break_sweep(session):
    app = _running_app(session)

    class Boom(FakeKubeProvider):
        def app_pod_events(self):
            raise RuntimeError("api down")

    _sweep(session, Boom())
    assert _events(session, app) == []


# --- API ---------------------------------------------------------------------

@pytest.fixture()
def kube(make_client):
    k = FakeKubeProvider()
    fastapi_app.dependency_overrides[get_kube] = lambda: k
    yield k
    fastapi_app.dependency_overrides.pop(get_kube, None)


def _create(c):
    r = c.post("/apps", json={"name": "demo", "image": "example/demo:1", "port": 8080})
    assert r.status_code == 201, r.text
    return r.json()


def _add(db_session, app_id, kind, age, msg="Restarted: x", key=None):
    db_session.add(models.AppEvent(app_id=app_id, at=_utcnow() - age, kind=kind, message=msg, dedupe_key=key or f"{kind}{age}"))
    db_session.commit()


def test_events_endpoint_authz_and_order(make_client, db_session, kube):
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    owner = make_client()
    register_and_login(owner, "owner@example.com")
    other = make_client()
    register_and_login(other, "other@example.com")
    app_id = _create(owner)["id"]
    _add(db_session, app_id, "oom", timedelta(hours=2), "Restarted: old")
    _add(db_session, app_id, "disk", timedelta(minutes=5), "Restarted: new")

    r = owner.get(f"/apps/{app_id}/events")
    assert r.status_code == 200
    assert [e["message"] for e in r.json()] == ["Restarted: new", "Restarted: old"]
    assert set(r.json()[0]) == {"at", "kind", "message"}
    assert admin.get(f"/apps/{app_id}/events").status_code == 200
    assert other.get(f"/apps/{app_id}/events").status_code == 404
    assert make_client().get(f"/apps/{app_id}/events").status_code in (401, 403)
    assert owner.get("/apps/9999/events").status_code == 404


def test_last_event_only_under_24h(make_client, db_session, kube):
    c = make_client()
    register_and_login(c, "admin@example.com")
    app_id = _create(c)["id"]
    assert c.get(f"/apps/{app_id}").json()["last_event"] is None
    _add(db_session, app_id, "oom", timedelta(hours=25), "Restarted: stale")
    assert c.get(f"/apps/{app_id}").json()["last_event"] is None
    _add(db_session, app_id, "crashed", timedelta(hours=23), "Restarted: recent")
    le = c.get(f"/apps/{app_id}").json()["last_event"]
    assert le["kind"] == "crashed" and le["message"] == "Restarted: recent"
    assert [a["last_event"]["kind"] for a in c.get("/apps").json()] == ["crashed"]
    # the endpoint still lists the stale one
    assert len(c.get(f"/apps/{app_id}/events").json()) == 2
