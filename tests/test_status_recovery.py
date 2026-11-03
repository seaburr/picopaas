"""Status must follow reality: a failure tied to an old revision / crashed pod must not stick once a newer rollout is healthy."""
from __future__ import annotations

from datetime import datetime, timedelta

from app import k8s_manifests
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.providers.kube import KubeClientProvider
from app.reconciler import reconcile_all
from tests import test_kube_provider as tk
from tests.test_reconciler import _make_app, session  # noqa: F401  (session fixture)

T0 = datetime(2026, 1, 1, 12, 0, 0)


def _pod(name, tmpl, created, *, crash=False, ready=False, terminating=False):
    meta = {"name": name, "labels": {"pod-template-hash": tmpl}, "creationTimestamp": created}
    if terminating:
        meta["deletionTimestamp"] = created
    if crash:
        cs = {"ready": False, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}
        return {"metadata": meta, "status": {"phase": "Running", "containerStatuses": [cs]}}
    cs = {"ready": ready, "state": {"running": {}}}
    return {"metadata": meta, "status": {"phase": "Running", "containerStatuses": [cs]}}


SETTLED = {"metadata": {"generation": 3}, "spec": {"replicas": 1},
           "status": {"observedGeneration": 3, "readyReplicas": 1, "replicas": 1, "updatedReplicas": 1}}


def test_old_revision_crash_does_not_mask_healthy_new_pod():
    pods = [_pod("old", "aaa", "2026-01-01T10:00:00Z", crash=True),
            _pod("new", "bbb", "2026-01-01T11:00:00Z", ready=True)]
    assert KubeClientProvider(tk._dyn(pods=pods, deployments=[SETTLED])).get_status("n") == "running"


def test_terminating_crashed_pod_is_ignored():
    pods = [_pod("old", "aaa", "2026-01-01T10:00:00Z", crash=True, terminating=True),
            _pod("new", "bbb", "2026-01-01T11:00:00Z", ready=True)]
    assert KubeClientProvider(tk._dyn(pods=pods, deployments=[SETTLED])).get_status("n") == "running"


def test_failing_newest_revision_still_reports_error():
    pods = [_pod("old", "aaa", "2026-01-01T10:00:00Z", ready=True),
            _pod("new", "bbb", "2026-01-01T11:00:00Z", crash=True)]
    assert KubeClientProvider(tk._dyn(pods=pods, deployments=[SETTLED])).get_status("n") == "error: crash_loop"


def _errored(session, **kw):
    app = _make_app(session)
    app.status, app.status_reason, app.status_detail = "error", "crash_loop", "Your app starts and then exits."
    app.error_count, app.error_retryable, app.last_attempt_at = 4, True, T0  # 60 min backoff
    app.rendered_hash = "x"
    session.commit()
    return app


def _sweep(session, kube, now):
    reconcile_all(session, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={}, now=now)


def test_errored_app_recovers_when_pods_are_healthy_before_backoff(session):
    app = _errored(session)
    kube = FakeKubeProvider()
    kube.statuses[k8s_manifests.namespace_name(app.id)] = "running"
    _sweep(session, kube, T0 + timedelta(minutes=2))  # well inside the 60 min backoff
    session.refresh(app)
    assert app.status == "running"
    assert app.status_reason is None and app.status_detail is None
    assert app.error_count == 0


def test_errored_app_stays_errored_while_pods_still_failing(session):
    app = _errored(session)
    kube = FakeKubeProvider()
    kube.statuses[k8s_manifests.namespace_name(app.id)] = "error: crash_loop"
    _sweep(session, kube, T0 + timedelta(minutes=2))
    session.refresh(app)
    assert app.status == "error" and app.status_reason == "crash_loop"
    assert not kube.applied  # no re-apply before the backoff elapses


def test_failed_apply_error_is_not_cleared_by_old_serving_pods(session):
    """deploy_failed has reason 'unknown': the old revision still serves, so the probe must not clear it."""
    app = _errored(session)
    app.status_reason = "unknown"
    session.commit()
    kube = FakeKubeProvider()
    kube.statuses[k8s_manifests.namespace_name(app.id)] = "running"
    _sweep(session, kube, T0 + timedelta(minutes=2))
    session.refresh(app)
    assert app.status == "error" and app.status_reason == "unknown"
