"""Admin Health extras: backup storage/failures, platform logs. Real login flow, fake kube."""
from datetime import datetime, timedelta, timezone

import pytest

from app import models
from app.dependencies import get_kube
from app.main import app
from app.providers.fake import FakeKubeProvider
from tests.conftest import register_and_login

APP = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
NOW = lambda: datetime.now(timezone.utc).replace(tzinfo=None)  # noqa: E731


@pytest.fixture()
def kube():
    k = FakeKubeProvider()
    k.platform_pod_data[("picopaas-system", "api-0")] = {"containers": ["api"], "lines": {"api": [f"line {i}" for i in range(50)]}}
    k.platform_pod_data[("traefik", "t-0")] = {"containers": ["traefik"], "lines": {"traefik": ["hello"]}}
    k.platform_pod_data[("app-1", "web-1")] = {"containers": ["app"], "lines": {"app": ["tenant secret"]}}
    k.platform_pod_data[("kube-system", "dns-0")] = {"containers": ["coredns"], "lines": {"coredns": ["x"]}}
    app.dependency_overrides[get_kube] = lambda: k
    return k


def _admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


def _seed(db, admin_client):
    aid = admin_client.post("/apps", json=APP).json()["id"]
    return aid


def test_backups_requires_admin(make_client, kube):
    anon = make_client()
    assert anon.get("/admin/health/backups").status_code in (401, 403)
    assert anon.get("/admin/health/logs/pods").status_code in (401, 403)


def test_backups_storage_failures_stuck(make_client, kube, db_session):
    admin = _admin(make_client)
    aid = _seed(db_session, admin)
    db = db_session
    db.add(models.AppBackupConfig(app_id=aid, db_repo_bytes=1000, vol_repo_bytes=500, bucket_repo_bytes=None))
    old = NOW() - timedelta(hours=5)
    db.add(models.BackupRun(app_id=aid, kind="db", trigger="scheduled", status="failed", created_at=NOW() - timedelta(hours=1),
                            finished_at=NOW() - timedelta(hours=1), error="restic: repository locked"))
    db.add(models.BackupRun(app_id=aid, kind="volume", trigger="manual", status="failed",
                            created_at=NOW() - timedelta(days=20), error="too old"))
    db.add(models.BackupRun(app_id=aid, kind="bucket", trigger="manual", status="running", created_at=old, started_at=old))
    db.add(models.BackupRun(app_id=aid, kind="bucket", trigger="manual", status="running", created_at=NOW(), started_at=NOW()))
    db.commit()
    r = admin.get("/admin/health/backups")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["storage"]["used_bytes"] == 1500 and j["storage"]["top_apps"][0]["app_id"] == aid
    assert j["storage"]["cap_bytes"] > 0 and j["storage"]["note"]
    assert [f["error"] for f in j["failures"]] == ["restic: repository locked"]
    f = j["failures"][0]
    assert f["owner_email"] == "admin@example.com" and f["app_name"] == "demo" and f["kind"] == "db" and f["still_failing"] is True
    assert len(j["stuck"]) == 1 and j["stuck"][0]["kind"] == "bucket" and j["stuck"][0]["age_seconds"] > 3600
    assert len(admin.get("/admin/health/backups?days=30").json()["failures"]) == 2
    assert admin.get("/admin/health/backups?days=0").status_code == 422


def test_failure_recovered_not_still_failing(make_client, kube, db_session):
    admin = _admin(make_client)
    aid = _seed(db_session, admin)
    db_session.add(models.BackupRun(app_id=aid, kind="db", trigger="scheduled", status="failed", created_at=NOW(), error="boom"))
    db_session.flush()
    db_session.add(models.BackupRun(app_id=aid, kind="db", trigger="manual", status="succeeded", created_at=NOW()))
    db_session.commit()
    assert admin.get("/admin/health/backups").json()["failures"][0]["still_failing"] is False


def test_non_admin_403(make_client, kube):
    admin = _admin(make_client)
    c = make_client()
    register_and_login(c, "plain@example.com")
    for path in ("/admin/health/backups", "/admin/health/logs/pods",
                 "/admin/health/logs?namespace=traefik&pod=t-0&container=traefik",
                 "/admin/health/logs/download?namespace=traefik&pod=t-0&container=traefik"):
        assert c.get(path).status_code == 403, path


def test_pods_lists_platform_only(make_client, kube):
    admin = _admin(make_client)
    j = admin.get("/admin/health/logs/pods").json()
    assert j["namespaces"] == ["picopaas-system", "traefik"]
    assert {p["name"] for p in j["pods"]} == {"api-0", "t-0"}


def test_logs_view_and_download(make_client, kube, db_session):
    admin = _admin(make_client)
    q = "namespace=picopaas-system&pod=api-0&container=api"
    j = admin.get(f"/admin/health/logs?{q}&tail=5&since_seconds=60").json()
    assert j["lines"] == [f"line {i}" for i in range(45, 50)]
    assert kube.platform_log_calls[-1][3:] == (5, 60, False)
    r = admin.get(f"/admin/health/logs/download?{q}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "attachment" in r.headers["content-disposition"] and r.text.count("\n") == 50
    log = db_session.query(models.AuditLog).filter_by(action="platform_logs.download").one()
    assert log.target_id == "picopaas-system/api-0" and "line 1" not in (log.detail or "")


@pytest.mark.parametrize("ns", ["app-1", "kube-system", "default"])
def test_tenant_and_system_namespaces_refused(make_client, kube, ns):
    admin = _admin(make_client)
    for ep in ("logs", "logs/download"):
        r = admin.get(f"/admin/health/{ep}?namespace={ns}&pod=web-1&container=app")
        assert r.status_code == 403, (ep, ns)
    assert not [c for c in kube.platform_log_calls if c[0] == ns]


@pytest.mark.parametrize("qs", [
    "namespace=traefik%2F..&pod=t-0&container=traefik", "namespace=traefik&pod=t-0%0Aa&container=traefik",
    "namespace=Traefik&pod=t-0&container=traefik", "namespace=traefik&pod=t-0&container=a,b",
    "namespace=traefik&pod=t-0&container=traefik&tail=0", "namespace=traefik&pod=t-0&container=traefik&tail=99999",
    "namespace=traefik&pod=t-0&container=traefik&since_seconds=-1", "namespace=traefik&pod=&container=traefik"])
def test_validation_422(make_client, kube, qs):
    admin = _admin(make_client)
    assert admin.get(f"/admin/health/logs?{qs}").status_code == 422
    assert admin.get(f"/admin/health/logs/download?{qs.replace('tail=99999', 'tail=10001')}").status_code == 422


def test_unknown_pod_404(make_client, kube):
    admin = _admin(make_client)
    assert admin.get("/admin/health/logs?namespace=traefik&pod=nope&container=traefik").status_code == 404
    assert admin.get("/admin/health/logs?namespace=traefik&pod=t-0&container=other").status_code == 404


def test_deleted_apps_hidden_from_failures_and_purged_rows(make_client, kube, db_session):
    admin = _admin(make_client)
    db = db_session
    ids = {}
    for n in ("live", "gone", "purged", "empty"):
        ids[n] = admin.post("/apps", json={**APP, "name": n}).json()["id"]
    until = NOW() + timedelta(days=5)
    for n in ("gone", "purged", "empty"):
        db.get(models.App, ids[n]).deleted_at = NOW()
    db.add(models.AppBackupConfig(app_id=ids["live"], db_repo_bytes=10))
    db.add(models.AppBackupConfig(app_id=ids["gone"], db_repo_bytes=2000, deleted_retention_until=until))
    db.add(models.AppBackupConfig(app_id=ids["purged"], db_repo_bytes=0, purged_at=NOW()))
    db.add(models.AppBackupConfig(app_id=ids["empty"]))
    for n in ("live", "gone"):
        db.add(models.BackupRun(app_id=ids[n], kind="db", trigger="scheduled", status="failed", created_at=NOW(), error="boom"))
    db.commit()
    j = admin.get("/admin/health/backups").json()
    assert [f["app_id"] for f in j["failures"]] == [ids["live"]]
    top = {a["name"]: a for a in j["storage"]["top_apps"]}
    assert set(top) == {"live", "gone"}
    assert top["live"]["purge_after"] is None
    assert top["gone"]["purge_after"] == until.isoformat()


def test_orphaned_deleted_repos_get_a_retention_date(db_session):
    from app import backups
    u = models.User(email="o@example.com", password_hash="x")
    db_session.add(u)
    db_session.flush()
    a = models.App(owner_id=u.id, name="orph", hostname="orph.test", image="x", port=80, deleted_at=NOW() - timedelta(days=3))
    db_session.add(a)
    db_session.flush()
    db_session.add(models.AppBackupConfig(app_id=a.id, db_repo_bytes=500))
    empty = models.App(owner_id=u.id, name="orph2", hostname="orph2.test", image="x", port=80, deleted_at=NOW())
    db_session.add(empty)
    db_session.flush()
    db_session.add(models.AppBackupConfig(app_id=empty.id))
    db_session.commit()
    backups._retain_orphans(db_session, NOW())
    cfg = db_session.get(models.AppBackupConfig, a.id)
    assert cfg.deleted_retention_until == a.deleted_at + timedelta(days=backups.config.BACKUP_DELETED_RETENTION_DAYS)
    assert db_session.get(models.AppBackupConfig, empty.id).deleted_retention_until is None
