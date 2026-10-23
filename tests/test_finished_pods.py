"""Finished (Succeeded/Failed/Evicted) pods: hidden from the pod picker + deleted by the reconciler after an hour."""
from __future__ import annotations

import datetime as dt
from datetime import datetime, timedelta, timezone

from app import k8s_manifests, reconciler
from app.providers.base import PodInfo, PodSection, mark_current, select_log_pods
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.providers.kube import KubeClientProvider
from tests import test_kube_provider as tk
from tests.test_app_events import _ev, _events, _running_app, _sweep
from tests.test_reconciler import session  # noqa: F401  (session fixture)

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _info(name, minutes, phase="Running", ready=True, rollout="h1"):
    return PodInfo(name=name, created_at=T0 + dt.timedelta(minutes=minutes), phase=phase, ready=ready, rollout=rollout)


def test_select_drops_finished_and_puts_current_first():
    infos = [_info("old-evicted", 0, "Failed", False, "h0"), _info("done", 1, "Succeeded", False, "h0"),
             _info("pending", 9, "Pending", False), _info("cur", 5), _info("run2", 6, ready=False)]
    out = select_log_pods(mark_current(infos))
    assert [i.name for i in out] == ["cur", "run2", "pending"]


def test_select_keeps_newest_finished_when_nothing_live():
    infos = [_info("a", 0, "Failed", False), _info("b", 3, "Failed", False), _info("c", 1, "Succeeded", False)]
    assert [i.name for i in select_log_pods(mark_current(infos))] == ["b"]
    assert select_log_pods([]) == []


def _pod(name, phase, created, ns="app-7", finished=None):
    st = {"phase": phase}
    if finished:
        st["containerStatuses"] = [{"state": {"terminated": {"finishedAt": finished}}}]
    return {"metadata": {"name": name, "namespace": ns, "creationTimestamp": created, "labels": {"pod-template-hash": "h"}},
            "status": st}


def _iso(delta):
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_provider_pod_logs_lists_live_pods_only(monkeypatch):
    pods = [_pod("evicted", "Failed", _iso(timedelta(hours=3))), _pod("live", "Running", _iso(timedelta(minutes=5)))]
    prov = KubeClientProvider(tk._dyn(pods=pods))
    monkeypatch.setattr(prov, "_pod_section", lambda ns, p, tail: PodSection(pod=p["metadata"]["name"], lines=["x"]))
    out = prov.pod_logs("app-7", 10)
    assert out.pods == ["live"] and [i.name for i in out.pod_info] == ["live"]
    only = KubeClientProvider(tk._dyn(pods=pods[:1]))
    monkeypatch.setattr(only, "_pod_section", lambda ns, p, tail: PodSection(pod=p["metadata"]["name"], lines=["boom"]))
    assert only.pod_logs("app-7", 10).pods == ["evicted"]  # nothing live: crash logs still reachable


def test_provider_deletes_only_old_finished_app_pods():
    old = _iso(timedelta(hours=2))
    new = _iso(timedelta(minutes=10))
    pods = [
        _pod("old-failed", "Failed", old), _pod("old-done", "Succeeded", old, finished=old),
        _pod("fresh-failed", "Failed", old, finished=new),  # created long ago but only just finished
        _pod("running", "Running", old), _pod("kube", "Failed", old, ns="kube-system"),
    ]
    dyn = tk._dyn(pods=pods)
    n = KubeClientProvider(dyn).delete_finished_pods(timedelta(hours=1))
    pod_res = dyn.resources.get(api_version="v1", kind="Pod")
    deleted = sorted(c.kwargs["name"] for c in pod_res.delete.call_args_list)
    assert n == 2 and deleted == ["old-done", "old-failed"]
    assert all(c.kwargs["namespace"] == "app-7" for c in pod_res.delete.call_args_list)


def test_sweep_records_events_before_deleting_and_survives_failures(session):  # noqa: F811
    app = _running_app(session)
    order = []

    class K(FakeKubeProvider):
        def app_pod_events(self):
            order.append("events")
            return super().app_pod_events()

        def delete_finished_pods(self, older_than):
            order.append(("delete", older_than, len(_events(session, app))))
            return 0

    k = K()
    k.restart_events[k8s_manifests.namespace_name(app.id)] = [_ev("u1:evicted", "disk")]
    _sweep(session, k)
    assert order == ["events", ("delete", reconciler.FINISHED_POD_MAX_AGE, 1)]  # event stored before the delete
    assert reconciler.FINISHED_POD_MAX_AGE == timedelta(hours=1)

    class Boom(FakeKubeProvider):
        def delete_finished_pods(self, older_than):
            raise RuntimeError("forbidden")

    _sweep(session, Boom())  # must not raise


def test_no_delete_when_event_recording_failed(session):  # noqa: F811
    _running_app(session)

    class K(FakeKubeProvider):
        def app_pod_events(self):
            raise RuntimeError("api down")

    k = K()
    _sweep(session, k)
    assert k.cleanup_calls == []
