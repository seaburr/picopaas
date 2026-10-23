"""Stuck-rollout recovery (quota sizing, FailedCreate detection, deploy timeout), status reasons, logs, restart."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config, crypto, k8s_manifests, models
from app.db import Base
from app.dependencies import get_kube
from app.main import app as fastapi_app
from app.providers.base import PodLogs
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.providers.kube import KubeClientProvider
from app.reconciler import DEPLOY_TIMEOUT, STATUS_MESSAGES, reconcile_all, reconcile_app
from tests import test_kube_provider as tk
from tests.conftest import register_and_login
from tests.test_reconciler import _make_app, session  # noqa: F401  (session fixture)

T0 = datetime(2026, 1, 1, 12, 0, 0)


def _rec(session, kube, now=None, app=None):
    kw = dict(kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=FakeObjectStore(), settings={})
    if app is not None:
        reconcile_app(session, app, now=now, **kw)
    else:
        reconcile_all(session, now=now, **kw)


def _quota(kube, ns):
    return [m for m in kube.applied[ns] if m["kind"] == "ResourceQuota"][-1]["spec"]["hard"]


# --- (a) quota sizing ---------------------------------------------------------

def test_quota_manifest_sizes():
    steady = k8s_manifests.resource_quota_manifest(1)["spec"]["hard"]
    assert steady["limits.cpu"] == f"{2 * config.DEFAULT_CPU_LIMIT_M}m"
    live = {"cpu_limit_m": 250, "mem_limit_mi": 512}
    wide = k8s_manifests.resource_quota_manifest(1, used_limits=live)["spec"]["hard"]
    assert wide["limits.cpu"] == f"{config.DEFAULT_CPU_LIMIT_M + 250}m"
    assert wide["limits.memory"] == f"{config.DEFAULT_MEM_LIMIT_MI + 512}Mi"
    assert wide["requests.cpu"] == wide["limits.cpu"]
    # used lower than new never shrinks below 2x new
    low = k8s_manifests.resource_quota_manifest(1, used_limits={"cpu_limit_m": 10, "mem_limit_mi": 10})["spec"]["hard"]
    assert low == steady


def test_lowered_caps_widen_quota_then_shrink_without_flapping(session):
    app = _make_app(session)
    kube = FakeKubeProvider()
    ns = k8s_manifests.namespace_name(app.id)
    kube.used_limits[ns] = {"cpu_limit_m": 250, "mem_limit_mi": 512}  # old pod, pre-lowering
    _rec(session, kube, app=app)
    hard = _quota(kube, ns)
    assert hard["limits.cpu"] == f"{config.DEFAULT_CPU_LIMIT_M + 250}m"  # old 250m + new default fits
    assert hard["limits.memory"] == f"{config.DEFAULT_MEM_LIMIT_MI + 512}Mi"
    wide_hash = app.rendered_hash

    # still rolling: sweeping again is a no-op (no flapping)
    applied = len(kube.applied[ns])
    app.status = "running"
    session.commit()
    _rec(session, kube)
    assert len(kube.applied[ns]) == applied and app.rendered_hash == wide_hash

    # rollout finished: live == new -> next sweep shrinks the quota to 2x, then stays put
    kube.used_limits[ns] = {"cpu_limit_m": config.DEFAULT_CPU_LIMIT_M, "mem_limit_mi": config.DEFAULT_MEM_LIMIT_MI}  # old pod gone
    _rec(session, kube)
    assert _quota(kube, ns)["limits.cpu"] == f"{2 * config.DEFAULT_CPU_LIMIT_M}m"
    assert app.rendered_hash != wide_hash
    count = len(kube.applied[ns])
    _rec(session, kube)
    assert len(kube.applied[ns]) == count


# --- (b) failure detection ----------------------------------------------------

def _dep(conditions):
    return {"metadata": {"generation": 3}, "spec": {"replicas": 1},
            "status": {"readyReplicas": 1, "updatedReplicas": 0, "observedGeneration": 3, "conditions": conditions}}


def test_replica_failure_quota_condition():
    quota = {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate",
             "message": 'pods "x" is forbidden: exceeded quota: app-quota, requested: limits.cpu=100m'}
    assert KubeClientProvider(tk._dyn(deployments=[_dep([quota])])).get_status("n") == "error: quota"
    other = {**quota, "message": "pods is forbidden: something else"}
    assert KubeClientProvider(tk._dyn(deployments=[_dep([other])])).get_status("n") == "error: replica_failure"
    cleared = {**quota, "status": "False"}
    assert KubeClientProvider(tk._dyn(deployments=[_dep([cleared])])).get_status("n") == "deploying"


def test_oom_detected():
    pod = {"status": {"containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}},
                                             "lastState": {"terminated": {"reason": "OOMKilled"}}}]}}
    assert KubeClientProvider(tk._dyn(pods=[pod])).get_status("n") == "error: oom"


def _pod(cpu, mem, phase="Running"):
    return {"status": {"phase": phase},
            "spec": {"containers": [{"resources": {"limits": {"cpu": cpu, "memory": mem}}}]}}


def test_used_limits_sum_existing_pods():
    prov = KubeClientProvider(tk._dyn(pods=[_pod("250m", "512Mi"), _pod("100m", "384Mi"), _pod("900m", "1Gi", "Succeeded")]))
    assert prov.get_used_limits("n") == {"cpu_limit_m": 350, "mem_limit_mi": 896, "ephemeral_limit_mi": 0, "pods": 2}  # Succeeded pod excluded
    assert KubeClientProvider(tk._dyn()).get_used_limits("n") is None


def test_regression_template_already_new_old_pod_bigger_quota_fits_old_plus_new(session):
    """Prod app 48: Deployment template already holds the NEW limits, the old pod still runs 250m/512Mi."""
    app = _make_app(session)
    kube = FakeKubeProvider()
    ns = k8s_manifests.namespace_name(app.id)
    kube.used_limits[ns] = {"cpu_limit_m": 250, "mem_limit_mi": 512}  # only the pod matters, not the template
    kube.forced_status[ns] = "error: quota"
    _rec(session, kube, now=T0, app=app)
    hard = _quota(kube, ns)
    assert hard["limits.cpu"] == f"{250 + config.DEFAULT_CPU_LIMIT_M}m"
    assert hard["limits.memory"] == f"{512 + config.DEFAULT_MEM_LIMIT_MI}Mi"
    # stuck in error/quota: the retry sweep picks it up automatically and it recovers
    assert app.status == "error" and app.status_reason == "quota" and app.error_retryable is True
    kube.forced_status.pop(ns)
    kube.used_limits[ns] = {"cpu_limit_m": 250, "mem_limit_mi": 512}
    _rec(session, kube, now=T0 + timedelta(hours=1))
    assert app.status == "running" and app.status_reason is None
    # old pod gone: quota shrinks back to 2x new on the next sweep
    kube.used_limits[ns] = {"cpu_limit_m": config.DEFAULT_CPU_LIMIT_M, "mem_limit_mi": config.DEFAULT_MEM_LIMIT_MI}
    _rec(session, kube, now=T0 + timedelta(hours=2))
    assert _quota(kube, ns)["limits.cpu"] == f"{2 * config.DEFAULT_CPU_LIMIT_M}m"


@pytest.mark.parametrize("code,reason,needle", [
    ("quota", "quota", "quota"),
    ("image_pull", "image_pull", "Can't pull the image"),
    ("crash_loop", "crash_loop", "listens on port 8080"),
    ("oom", "oom", f"limit {config.DEFAULT_MEM_LIMIT_MI}Mi"),
    ("root_image", "root_image", "USER 10001"),
])
def test_cluster_error_becomes_human_failure(session, code, reason, needle):
    app = _make_app(session)
    kube = FakeKubeProvider()
    kube.forced_status[k8s_manifests.namespace_name(app.id)] = f"error: {code}"
    _rec(session, kube, now=T0, app=app)
    assert app.status == "error" and app.status_reason == reason
    assert needle in app.status_detail and app.error_retryable is True


# --- (c) deploy timeout -------------------------------------------------------

def test_deploy_timeout_fails_retryably_with_what_we_saw(session):
    app = _make_app(session)
    kube = FakeKubeProvider()
    ns = k8s_manifests.namespace_name(app.id)
    kube.forced_status[ns] = "deploying"
    kube.hints[ns] = "not_ready"
    _rec(session, kube, now=T0, app=app)
    assert app.status == "deploying" and app.deploying_since == T0
    _rec(session, kube, now=T0 + DEPLOY_TIMEOUT - timedelta(seconds=1), app=app)
    assert app.status == "deploying"
    _rec(session, kube, now=T0 + DEPLOY_TIMEOUT + timedelta(seconds=1), app=app)
    assert app.status == "error" and app.status_reason == "timeout" and app.error_retryable is True
    assert "port 8080 isn't answering" in app.status_detail
    assert str(config.DEPLOY_TIMEOUT_MINUTES) in app.status_detail

    # retried after backoff: a fresh deploy window starts
    kube.forced_status[ns] = "running"
    _rec(session, kube, now=T0 + DEPLOY_TIMEOUT + timedelta(minutes=2))
    assert app.status == "running" and app.deploying_since is None


def test_deploy_timeout_variants(session):
    for hint, needle in (("waiting", "still waiting to start"), ("none", "no pod was started")):
        app = _make_app(session)
        kube = FakeKubeProvider()
        ns = k8s_manifests.namespace_name(app.id)
        kube.forced_status[ns] = "deploying"
        kube.hints[ns] = hint
        _rec(session, kube, now=T0, app=app)
        _rec(session, kube, now=T0 + DEPLOY_TIMEOUT + timedelta(minutes=1), app=app)
        assert app.status_reason == "timeout" and needle in app.status_detail


# --- key rotation follow-ups --------------------------------------------------

def test_unreadable_secrets_is_retryable_and_self_heals(session):
    app = _make_app(session, env=[("A", "x", False)])
    good = app.env_vars[0].value_encrypted
    app.env_vars[0].value_encrypted = "gAAAAAnot-a-valid-fernet-token"
    session.commit()
    kube = FakeKubeProvider()
    _rec(session, kube, now=T0)
    assert app.status == "error" and app.status_reason == "unreadable_secrets"
    assert app.error_retryable is True and app.status_detail == STATUS_MESSAGES["unreadable_secrets"]
    app.env_vars[0].value_encrypted = good  # rollout finished: the leader can read the rows again
    session.commit()
    _rec(session, kube, now=T0 + timedelta(minutes=2))
    assert app.status == "running"


def test_rotate_all_logs_summary_at_warning(session, caplog):
    caplog.set_level("WARNING", logger="app.crypto")
    crypto.rotate_all(session)
    assert any(r.levelname == "WARNING" and "fernet rotation: rotated=" in r.getMessage() for r in caplog.records)


# --- real provider logs -------------------------------------------------------

def test_pod_logs_previous_container_and_all_pods():
    pods = [
        {"metadata": {"name": "old", "creationTimestamp": "2026-01-01T00:00:00Z"}, "status": {}},
        {"metadata": {"name": "new", "creationTimestamp": "2026-01-02T00:00:00Z"},
         "status": {"containerStatuses": [{"restartCount": 2}]}},
    ]
    core = MagicMock()
    core.read_namespaced_pod_log.side_effect = lambda **kw: "boom\nbye" if kw["previous"] else "hello\nworld"
    out = KubeClientProvider(tk._dyn(pods=pods), core_api=core).pod_logs("app-7", 200, pod="new")
    assert out.pod == "new" and out.pods == ["new", "old"]
    assert out.lines[0].startswith("--- previous container") and "boom" in out.lines
    assert out.lines[-2:] == ["hello", "world"]
    both = KubeClientProvider(tk._dyn(pods=pods), core_api=core).pod_logs("app-7", 200)
    assert both.pod is None and [s.pod for s in both.sections] == ["new", "old"]
    assert both.lines[0] == "=== pod new ===" and "=== pod old ===" in both.lines
    with pytest.raises(LookupError):
        KubeClientProvider(tk._dyn(pods=pods), core_api=core).pod_logs("app-7", 200, pod="nope")
    assert KubeClientProvider(tk._dyn(), core_api=core).pod_logs("app-7", 5).note == "No running pod yet."
    with pytest.raises(ValueError):
        KubeClientProvider(tk._dyn(pods=pods), core_api=core).pod_logs("picopaas-system", 5)


# --- endpoints ----------------------------------------------------------------

@pytest.fixture()
def kube(make_client):
    k = FakeKubeProvider()
    fastapi_app.dependency_overrides[get_kube] = lambda: k
    yield k
    fastapi_app.dependency_overrides.pop(get_kube, None)


def _create(c):
    r = c.post("/apps", json={"name": "demo", "image": "example/demo:1", "port": 8080})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_logs_endpoint_authz_tail_cap_and_note(make_client, kube):
    c = make_client()
    register_and_login(c, "o@example.com")
    app_id = _create(c)
    r = c.get(f"/apps/{app_id}/logs")
    assert r.status_code == 200 and r.json() == {"lines": [], "pod": None, "note": "No running pod yet.", "pods": [], "sections": [], "pod_info": []}
    assert kube.log_calls[-1] == (f"app-{app_id}", 200)
    kube.logs[f"app-{app_id}"] = PodLogs(pod="app-1-abc", lines=["a", "b"])
    assert c.get(f"/apps/{app_id}/logs?tail=99999").json()["lines"] == ["a", "b"]
    assert kube.log_calls[-1][1] == 1000
    assert c.get(f"/apps/{app_id}/logs?tail=0").status_code == 422

    other = make_client()
    register_and_login(other, "x@example.com")
    assert other.get(f"/apps/{app_id}/logs").status_code == 404
    assert make_client().get(f"/apps/{app_id}/logs").status_code == 401


def test_logs_admin_can_read(make_client, kube):
    admin = make_client()
    register_and_login(admin, "admin@example.com")  # first user is the administrator
    user = make_client()
    register_and_login(user, "u@example.com")
    app_id = _create(user)
    assert admin.get(f"/apps/{app_id}/logs").status_code == 200


def test_restart_rolls_pod_audits_and_blocks_suspended(make_client, db_session, kube):
    c = make_client()
    register_and_login(c, "o@example.com")
    app_id = _create(c)
    row = db_session.get(models.App, app_id)
    row.status = "running"
    db_session.commit()
    ns = k8s_manifests.namespace_name(app_id)

    reconcile_all(db_session, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={})
    reconcile_all(db_session, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={})

    r = c.post(f"/apps/{app_id}/restart")
    assert r.status_code == 200 and r.json()["status"] == "pending"
    db_session.expire_all()
    assert db_session.get(models.App, app_id).restart_nonce
    reconcile_all(db_session, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={})
    dep = [m for m in kube.applied[ns] if m["kind"] == "Deployment"][-1]
    assert dep["spec"]["template"]["metadata"]["annotations"]["picopaas.io/restart-nonce"]
    db_session.expire_all()
    assert db_session.get(models.App, app_id).status == "running"
    # the drift sweep agrees with the new template: nothing more to apply
    n = len(kube.applied[ns])
    reconcile_all(db_session, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={})
    assert len(kube.applied[ns]) == n
    assert db_session.query(models.AuditLog).filter_by(action="app.restart").count() == 1

    row = db_session.get(models.App, app_id)
    row.status = "suspended"
    db_session.commit()
    assert c.post(f"/apps/{app_id}/restart").status_code == 409


def test_status_reason_only_shown_for_errors(make_client, db_session):
    c = make_client()
    register_and_login(c, "o@example.com")
    app_id = _create(c)
    row = db_session.get(models.App, app_id)
    row.status, row.status_detail, row.status_reason = "error", "x", "oom"
    db_session.commit()
    assert c.get(f"/apps/{app_id}").json()["status_reason"] == "oom"
    row.status = "pending"
    db_session.commit()
    assert c.get(f"/apps/{app_id}").json()["status_reason"] is None


def test_pods_quota_widens_when_old_pod_still_terminating(session):
    """Storage key-swap roll (status pending) while a Terminating pod + the current pod still count: the surge pod needs
    existing+1 slots in every dimension, including the pod count (a fixed 2 rejected it live)."""
    assert k8s_manifests.resource_quota_manifest(1)["spec"]["hard"]["pods"] == "2"
    app = _make_app(session, want_storage=True)
    kube = FakeKubeProvider()
    ns = k8s_manifests.namespace_name(app.id)
    d, m, e = config.DEFAULT_CPU_LIMIT_M, config.DEFAULT_MEM_LIMIT_MI, config.EPHEMERAL_LIMIT_MI
    _rec(session, kube, app=app)
    assert _quota(kube, ns)["pods"] == "2"  # steady state
    app.status = "running"
    session.commit()

    # steady-state quota, one running pod + one still-terminating pod from the previous roll
    kube.used_limits[ns] = {"cpu_limit_m": 2 * d, "mem_limit_mi": 2 * m, "ephemeral_limit_mi": 2 * e, "pods": 2}
    app.status = "pending"  # what quota._bounce_app does
    app.restart_nonce = "storage-roll"
    session.commit()
    _rec(session, kube, app=app)
    hard = _quota(kube, ns)
    assert int(hard["pods"]) >= 2 + 1
    for dim, per in (("cpu", d), ("memory", m), ("ephemeral-storage", e)):
        unit = "m" if dim == "cpu" else "Mi"
        assert int(hard[f"limits.{dim}"].removesuffix(unit)) >= 3 * per
        assert int(hard[f"requests.{dim}"].removesuffix(unit)) >= 3 * per
