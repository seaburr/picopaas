"""Reconciler edge paths: soft-deleted apps are never re-applied, kube failures (sweep vs first apply), suspended apps
keep their state on failure, drift-sweep resilience, deploy timeout."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import crypto, models
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import DEPLOY_TIMEOUT, SUSPENDED_APPLIED, reconcile_all, reconcile_app


@pytest.fixture()
def session(db_session):
    return db_session


def _mk(session, env=None, **kw):
    owner = session.query(models.User).first()
    if owner is None:
        owner = models.User(email="o@example.com", password_hash="x")
        session.add(owner)
        session.commit()
    fields = dict(owner_id=owner.id, name="demo", hostname=f"demo-{session.query(models.App).count()}",
                  image="example/demo:latest", port=8080)
    fields.update(kw)
    app = models.App(**fields)
    for k, v in (env or {}).items():
        app.env_vars.append(models.AppEnvVar(key=k, value_encrypted=crypto.encrypt(v), is_secret=False))
    session.add(app)
    session.commit()
    return app


def _run(session, app, kube=None, **kw):
    kube = kube or FakeKubeProvider()
    reconcile_app(session, app, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={}, **kw)
    return kube


class BoomApply(FakeKubeProvider):
    def apply(self, manifests):
        raise RuntimeError("apiserver unavailable")


def test_soft_deleted_app_is_never_reapplied(session):
    app = _mk(session, platform_subdomain="blog", deleted_at=datetime.now(timezone.utc))
    kube = _run(session, app)
    _run(session, app, kube, sweep=True, force=True)
    assert kube.applied == {} and app.status == "pending"  # teardown owns it; a re-apply would resurrect the Ingress


def test_first_apply_failure_marks_error_and_is_retryable(session):
    app = _mk(session)
    _run(session, app, BoomApply())
    assert app.status == "error" and app.error_retryable and app.error_count == 1
    assert "apiserver" not in (app.status_detail or "")  # operator detail stays in logs only


def test_sweep_apply_failure_is_silent_and_keeps_running_state(session):
    app = _mk(session, status="running")
    _run(session, app, BoomApply(), sweep=True)
    assert app.status == "running" and app.status_detail is None and (app.error_count or 0) == 0


def test_failure_on_suspended_app_never_loses_suspension(session):
    app = _mk(session, status="suspended")
    _run(session, app, BoomApply())
    assert app.status == "suspended"


def test_suspended_app_applied_once_then_swept_quietly(session):
    app = _mk(session, status="suspended", platform_subdomain="blog")
    kube = _run(session, app)
    assert app.status == "suspended" and app.status_detail == SUSPENDED_APPLIED
    assert not [m for m in kube.applied[f"app-{app.id}"] if m["kind"] == "Ingress"]
    n = len(kube.applied[f"app-{app.id}"])
    reconcile_all(session, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={})
    assert len(kube.applied[f"app-{app.id}"]) == n  # marker stops re-applying every tick


def test_invalid_env_ref_fails_first_apply_but_sweep_skips(session):
    # A ${{ ref }} that can't resolve (e.g. storage ref on an app with no storage) is a user error.
    app = _mk(session, env={"X": "${{ storage.bucket }}"})
    _run(session, app)
    assert app.status == "error" and app.error_retryable is False
    app2 = _mk(session, env={"X": "${{ storage.bucket }}"}, status="running")
    kube = _run(session, app2, sweep=True)
    assert app2.status == "running" and kube.applied == {}


def test_used_limits_failure_does_not_block_apply(session):
    class NoLimits(FakeKubeProvider):
        def get_used_limits(self, namespace):
            raise RuntimeError("metrics down")

    app = _mk(session)
    _run(session, app, NoLimits())
    assert app.status == "running"


def test_drift_sweep_survives_namespace_listing_failure(session):
    class NoList(FakeKubeProvider):
        def list_app_namespaces(self):
            raise RuntimeError("list failed")

    app = _mk(session, status="running")
    kube = NoList()
    reconcile_all(session, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={})
    assert app.status == "running"


def test_drift_sweep_reapplies_when_namespace_vanished(session):
    app = _mk(session, status="running")
    kube = _run(session, app, sweep=False)
    assert f"app-{app.id}" in kube.applied
    kube.applied.clear()  # cluster rebuilt
    reconcile_all(session, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={})
    assert f"app-{app.id}" in kube.applied


def test_deploy_timeout_gives_up_with_a_reason(session):
    app = _mk(session)
    kube = FakeKubeProvider()
    kube.forced_status[f"app-{app.id}"] = "deploying"
    start = datetime(2026, 1, 1)
    _run(session, app, kube, now=start)
    assert app.status == "deploying"
    kube.hints[f"app-{app.id}"] = "not_ready"
    app.health_check_enabled = True
    _run(session, app, kube, now=start + DEPLOY_TIMEOUT + timedelta(minutes=1))
    assert app.status == "error" and app.error_retryable


@pytest.mark.parametrize("hint", ["waiting", "weird-value"])
def test_deploy_timeout_unknown_hint_falls_back(session, hint):
    app = _mk(session)
    kube = FakeKubeProvider()
    kube.forced_status[f"app-{app.id}"] = "deploying"
    start = datetime(2026, 1, 1)
    _run(session, app, kube, now=start)
    kube.hints[f"app-{app.id}"] = hint
    _run(session, app, kube, now=start + DEPLOY_TIMEOUT + timedelta(minutes=1))
    assert app.status == "error"
