"""Image history, manual rollback, and deploy health-gate auto-rollback."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import image_check, image_history, k8s_manifests, models
from app.dependencies import get_registry
from app.main import app as fastapi_app
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore, FakeRegistry
from app.reconciler import reconcile_app
from tests.conftest import register_and_login
from tests.test_reconciler import _make_app, session  # noqa: F401  (session fixture)

TAG = "ghcr.io/example/demo:latest"
D = [f"sha256:{c * 64}" for c in "123456789"]


def _rec(session, app, kube, **kw):
    reconcile_app(session, app, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={"mysql_cidr": "10.1.0.0/16"}, **kw)


def _deploy(session, app, kube, digest, forced=None):
    """Roll the app onto `digest` the way the watcher does (pending), then reconcile."""
    app.image_digest = digest
    app.status = "pending"
    session.commit()
    ns = k8s_manifests.namespace_name(app.id)
    if forced:
        kube.forced_status[ns] = forced
    else:
        kube.forced_status.pop(ns, None)
    _rec(session, app, kube)


def _hist(app):
    return [h.digest for h in app.image_history]


def test_history_records_dedupes_and_prunes(session):
    app = _make_app(session)
    kube = FakeKubeProvider()
    for d in D[:7]:
        _deploy(session, app, kube, d)
    session.expire_all()
    assert _hist(app) == [D[6], D[5], D[4], D[3], D[2]]
    # same digest running again (env-only change) is not a new entry
    app.status = "pending"
    session.commit()
    _rec(session, app, kube)
    session.expire_all()
    assert _hist(app) == [D[6], D[5], D[4], D[3], D[2]]
    assert not image_history.record_running(session, app)


def test_no_history_for_failed_rollout(session):
    app = _make_app(session)
    kube = FakeKubeProvider()
    _deploy(session, app, kube, D[0], forced="error: crash_loop")
    assert _hist(app) == []


def test_auto_rollback_on_failed_image_rollout(session):
    app = _make_app(session)
    kube = FakeKubeProvider()
    _deploy(session, app, kube, D[0])
    _deploy(session, app, kube, D[1], forced="error: crash_loop")
    session.expire_all()
    assert app.image_digest == D[0]
    assert app.status == "pending"
    assert app.status_detail == image_history.ROLLBACK_MESSAGE
    assert app.rejected_digest == D[1]
    ev = session.query(models.AppEvent).filter_by(app_id=app.id, kind="rollback").one()
    assert "rolled back" in ev.message
    assert session.query(models.AuditLog).filter_by(action="app.auto_rollback").count() == 1
    # the redeploy of the good digest succeeds; the message survives the deploy, then is cleared
    kube.forced_status.clear()
    _rec(session, app, kube)
    assert app.status == "running" and app.status_detail is None
    assert app.image_update_error == image_history.ROLLBACK_MESSAGE
    assert _hist(app) == [D[0]]


def test_auto_rollback_on_deploy_timeout(session):
    app = _make_app(session)
    kube = FakeKubeProvider()
    _deploy(session, app, kube, D[0])
    app.image_digest = D[1]
    app.status = "deploying"
    app.deploying_since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    session.commit()
    kube.forced_status[k8s_manifests.namespace_name(app.id)] = "deploying"
    _rec(session, app, kube)
    assert app.image_digest == D[0] and app.status == "pending"


def test_no_auto_rollback_without_prior_digest(session):
    app = _make_app(session)
    kube = FakeKubeProvider()
    _deploy(session, app, kube, D[0], forced="error: crash_loop")
    assert app.status == "error" and app.image_digest == D[0]


def test_no_auto_rollback_for_env_only_change(session):
    app = _make_app(session)
    kube = FakeKubeProvider()
    _deploy(session, app, kube, D[0])
    _deploy(session, app, kube, D[1])
    # same (known-good) digest, env change breaks the app
    app.status = "pending"
    session.commit()
    kube.forced_status[k8s_manifests.namespace_name(app.id)] = "error: crash_loop"
    _rec(session, app, kube)
    assert app.status == "error" and app.image_digest == D[1]


def test_watcher_does_not_redeploy_rejected_digest(session):
    app = _make_app(session)
    app.image = TAG
    kube = FakeKubeProvider()
    _deploy(session, app, kube, D[0])
    _deploy(session, app, kube, D[1], forced="error: crash_loop")
    reg = FakeRegistry(default_size_bytes=1024, digests={TAG: D[1]})
    assert image_check.refresh_app_image(session, app, reg) == "unchanged"
    assert app.image_digest == D[0] and app.status == "pending"
    # a genuinely newer digest is picked up again
    reg2 = FakeRegistry(default_size_bytes=1024, digests={TAG: D[2]})
    assert image_check.refresh_app_image(session, app, reg2) == "updated"
    assert app.image_digest == D[2] and app.rejected_digest is None


# --- API ---

@pytest.fixture()
def reg():
    r = FakeRegistry(default_size_bytes=1024, digests={TAG: D[0]})
    fastapi_app.dependency_overrides[get_registry] = lambda: r
    return r


def _seed(client, db_session):
    app_id = client.post("/apps", json={"name": "demo", "image": TAG, "port": 8080}).json()["id"]
    row = db_session.get(models.App, app_id)
    kube = FakeKubeProvider()
    for d in (D[0], D[1]):
        _deploy(db_session, row, kube, d)
    return app_id


def test_history_and_rollback_endpoints(client, db_session, reg):
    register_and_login(client, "rb@example.com")
    app_id = _seed(client, db_session)
    h = client.get(f"/apps/{app_id}/image-history").json()
    assert [(x["digest"], x["current"]) for x in h] == [(D[1], True), (D[0], False)]
    assert client.post(f"/apps/{app_id}/rollback", json={"digest": D[5]}).status_code == 400
    r = client.post(f"/apps/{app_id}/rollback", json={"digest": D[0]})
    assert r.status_code == 200 and r.json()["image_digest"] == D[0] and r.json()["status"] == "pending"
    db_session.expire_all()
    assert db_session.get(models.App, app_id).rejected_digest == D[1]
    assert db_session.query(models.AuditLog).filter_by(action="app.rollback").count() == 1


def test_rollback_authz_and_suspended(client, db_session, reg):
    register_and_login(client, "rb@example.com")
    app_id = _seed(client, db_session)
    row = db_session.get(models.App, app_id)
    row.status = "suspended"
    db_session.commit()
    assert client.post(f"/apps/{app_id}/rollback", json={"digest": D[0]}).status_code == 409
    client.post("/auth/logout")
    register_and_login(client, "other@example.com")
    assert client.get(f"/apps/{app_id}/image-history").status_code == 404
    assert client.post(f"/apps/{app_id}/rollback", json={"digest": D[0]}).status_code == 404
