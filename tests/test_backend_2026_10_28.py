"""Metrics endpoint, platform DB self-backup, MANIFEST image, admin ceiling follow, alert dismissal, uptime."""
from datetime import datetime, timedelta, timezone

import pytest

from app import backups, config, models, platform_backup, platform_settings
from app.dependencies import get_kube
from app.main import app as fastapi_app
from app.providers.base import PodInfo
from app.providers.fake import FakeBackupRunner, FakeKubeProvider
from tests.conftest import register_and_login
from tests.test_backups import _server, on  # noqa: F401

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def owner(make_client, admin, db_session):
    c = make_client()
    register_and_login(c, "owner@example.com")
    db_session.query(models.User).filter_by(email="owner@example.com").one().app_limit = 5
    db_session.commit()
    return c


@pytest.fixture()
def kube():
    k = FakeKubeProvider()
    fastapi_app.dependency_overrides[get_kube] = lambda: k
    yield k
    fastapi_app.dependency_overrides.pop(get_kube, None)


# --- 1. metrics ---

def test_metrics_access_and_content(admin, owner, make_client, db_session, monkeypatch):
    _server(db_session, "m1")
    _server(db_session, "p1", engine="pg")
    aid = owner.post("/apps", json=APP).json()["id"]
    owner.get(f"/apps/{aid}")
    assert make_client().get("/metrics").status_code == 404
    assert owner.get("/metrics").status_code == 404
    r = admin.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    t = r.text
    assert "picopaas_users 2.0" in t and "picopaas_apps 1.0" in t
    assert 'picopaas_database_servers{type="mysql"} 1.0' in t and 'picopaas_database_servers{type="psql"} 1.0' in t
    for name in ("picopaas_volumes", "picopaas_blob_storage_buckets", "picopaas_databases", "picopaas_cluster_nodes",
                 "picopaas_cluster_nodes_max"):
        assert name in t
    assert 'path="/apps/{app_id}"' in t and f'path="/apps/{aid}"' not in t  # templates, not raw paths
    assert "picopaas_http_request_duration_seconds_bucket" in t
    monkeypatch.setattr(config, "METRICS_TOKEN", "not-the-metrics-token")
    anon = make_client()
    assert anon.get("/metrics", headers={"Authorization": "Bearer not-the-metrics-token"}).status_code == 200
    assert anon.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 404


# --- 2. platform DB self-backup ---

@pytest.fixture()
def pdb(on, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "mysql+pymysql://platform:not-the-db-pass@db.internal:25060/picopaas")


def test_platform_backup_inactive_on_sqlite(on, db_session):
    assert not platform_backup.active()
    runner = FakeBackupRunner()
    platform_backup.tick(db_session, runner, datetime(2026, 10, 28, 1))
    assert runner.started == []


def test_platform_backup_cycle_and_retention_env(pdb, db_session):
    runner, t0 = FakeBackupRunner(), datetime(2026, 10, 28, 1, 0)
    backups.tick(db_session, runner, t0)
    (spec,) = runner.started
    assert spec.name.startswith("pbk-") and spec.app_id == 0
    e = spec.env
    assert e["MODE"] == "backup" and e["KIND"] == "db" and e["RESTIC_REPOSITORY"].endswith("/platform/db")
    assert (e["DB_HOST"], e["DB_PORT"], e["DB_NAME"], e["DB_USER"]) == ("db.internal", "25060", "picopaas", "platform")
    assert e["KEEP_DAILY"] == "7" and e["KEEP_WEEKLY"] == "4" and e["KEEP_MONTHLY"] == "3"
    pw = e["RESTIC_PASSWORD"]
    assert db_session.get(models.PlatformSetting, platform_backup.PASSWORD_KEY).value != pw  # encrypted at rest
    assert spec.name in backups.active_jobs(db_session)  # the secret sweep must not delete it
    runner.finish(spec.name, {"snapshot_id": "aa11", "bytes_added": 10, "repo_bytes": 99})
    backups.tick(db_session, runner, t0 + timedelta(minutes=1))
    s = platform_backup.summary(db_session, now=t0 + timedelta(minutes=2))
    assert s["runs"][0]["status"] == "succeeded" and s["repo_bytes"] == 99 and s["last_success_at"] and not s["stale"]
    backups.tick(db_session, runner, t0 + timedelta(hours=2))
    assert len(runner.started) == 1  # not due yet
    backups.tick(db_session, runner, t0 + timedelta(hours=25))
    assert len(runner.started) == 2 and runner.started[1].env["RESTIC_PASSWORD"] == pw  # same repo password
    runner.fail(runner.started[1].name, "database dump failed")
    backups.tick(db_session, runner, t0 + timedelta(hours=25, minutes=1))
    assert platform_backup.summary(db_session)["runs"][0]["error"] == "database dump failed"
    backups.tick(db_session, runner, t0 + timedelta(hours=26, minutes=5))  # failure retries after an hour
    assert len(runner.started) == 3


def test_platform_backup_admin_visibility_and_manual_run(pdb, admin, owner, db_session):
    h = admin.get("/admin/health/backups").json()
    assert h["platform_db"]["active"] is True and h["platform_db"]["runs"] == []
    assert owner.post("/admin/health/backups/platform-db/run").status_code == 403
    r = admin.post("/admin/health/backups/platform-db/run")
    assert r.status_code == 202
    assert admin.post("/admin/health/backups/platform-db/run").status_code == 409  # one at a time
    assert admin.get("/admin/health/backups").json()["platform_db"]["runs"][0]["trigger"] == "manual"
    assert db_session.query(models.AuditLog).filter_by(action="platform_backup.create").count() == 1


# --- 3. MANIFEST image ---

def test_split_image():
    assert backups.split_image("ghcr.io/x/y:1.2") == ("ghcr.io/x/y", "1.2")
    assert backups.split_image("localhost:5000/y") == ("localhost:5000/y", "latest")
    assert backups.split_image("x/y:2@sha256:abc") == ("x/y", "2")
    assert backups.split_image(None) == ("", "")


def test_package_env_carries_image(on, owner, db_session):
    aid = owner.post("/apps", json=APP).json()["id"]
    app = db_session.get(models.App, aid)
    app.image_digest = "sha256:" + "a" * 64
    backups.ensure_config(db_session, app)
    st, runs = backups.enqueue_set(db_session, app, ["db"], "manual")
    runs[0].status, runs[0].snapshot_id = "succeeded", "d0d0d0aa"
    app.image = "ghcr.io/example/demo:2.0"  # changed after the backup
    db_session.commit()
    assert st.image == "ghcr.io/example/demo:1.0"
    run = models.BackupRun(app_id=aid, kind="package", trigger="export", status="queued", set_id=st.id,
                           export_key=f"exports/{aid}/1.tar.gz")
    db_session.add(run)
    db_session.commit()
    env = backups._build_package_env(db_session, run)
    assert env["IMAGE_URL"] == "ghcr.io/example/demo" and env["IMAGE_TAG"] == "1.0" and env["IMAGE_DIGEST"].startswith("sha256:")


# --- 4. admin ceiling follows ---

def test_raising_user_ceiling_raises_admin_pair(db_session):
    def g(k):
        return platform_settings.get(db_session, k)
    assert g("max_apps_per_admin") == 10
    assert platform_settings.update(db_session, {"max_apps_per_user": 20}, "a@example.com") == []
    assert g("max_apps_per_admin") == 20 and g("admin_default_app_limit") == 20
    assert platform_settings.update(db_session, {"max_apps_per_user": 15}, "a@example.com") == []  # lowering leaves admin alone
    assert g("max_apps_per_admin") == 20
    assert platform_settings.update(db_session, {"max_apps_per_user": 30, "max_apps_per_admin": 40}, "a@example.com") == []
    assert g("max_apps_per_admin") == 40 and g("admin_default_app_limit") == 30


def test_raising_ceiling_via_api(admin):
    assert admin.put("/admin/settings", json={"settings": {"max_apps_per_user": 12}}).status_code == 200
    vals = {s["key"]: s["value"] for grp in admin.get("/admin/settings").json()["groups"] for s in grp["settings"]}
    assert vals["max_apps_per_admin"] == 12 and vals["admin_default_app_limit"] == 12


# --- 5. alert dismissal ---

def _event(db, aid, key, age_min=5):
    ev = models.AppEvent(app_id=aid, at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=age_min),
                         kind="crashed", message="Restarted: your app exited with code 137. Check the logs.", dedupe_key=key)
    db.add(ev)
    db.commit()
    return ev


def test_dismiss_event_alert_until_new_occurrence(owner, admin, make_client, db_session, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    ev = _event(db_session, aid, "a", 10)
    assert owner.get(f"/apps/{aid}").json()["last_event"]["id"] == ev.id
    assert make_client().post(f"/apps/{aid}/alerts/event/dismiss").status_code == 401
    assert admin.post(f"/apps/{aid}/alerts/event/dismiss").status_code == 404  # owner only
    assert owner.post(f"/apps/{aid}/alerts/bogus/dismiss").status_code == 422
    r = owner.post(f"/apps/{aid}/alerts/event/dismiss")
    assert r.status_code == 200 and r.json()["last_event"] is None
    assert owner.get(f"/apps/{aid}").json()["last_event"] is None
    assert owner.get(f"/apps/{aid}/events").json()[0]["dismissed_at"] is not None
    _event(db_session, aid, "b", 1)  # a NEW occurrence shows again
    assert owner.get(f"/apps/{aid}").json()["last_event"]["id"] != ev.id


def test_dismiss_and_undismiss_event_by_id(owner, kube, db_session):
    aid = owner.post("/apps", json=APP).json()["id"]
    ev = _event(db_session, aid, "a")
    assert owner.post(f"/apps/{aid}/events/{ev.id}/dismiss").json()["dismissed_at"]
    assert owner.get(f"/apps/{aid}").json()["last_event"] is None
    assert owner.delete(f"/apps/{aid}/events/{ev.id}/dismiss").json()["dismissed_at"] is None
    assert owner.get(f"/apps/{aid}").json()["last_event"] is not None
    assert owner.post(f"/apps/{aid}/events/999999/dismiss").status_code == 404


def test_dismiss_image_update_error(owner, kube, db_session):
    aid = owner.post("/apps", json=APP).json()["id"]
    app = db_session.get(models.App, aid)
    app.image_update_error = "New image runs as root; still running the previous version."
    db_session.commit()
    assert owner.get(f"/apps/{aid}").json()["image_update_error"]
    assert owner.post(f"/apps/{aid}/alerts/image_update/dismiss").json()["image_update_error"] is None
    assert owner.get(f"/apps/{aid}").json()["image_update_error"] is None
    db_session.expire_all()
    app = db_session.get(models.App, aid)
    app.image_update_error = "A different problem."
    app.image_update_error_dismissed = False  # what image_check/image_history do for a new message
    db_session.commit()
    assert owner.get(f"/apps/{aid}").json()["image_update_error"] == "A different problem."


# --- 6. uptime ---

def test_uptime_in_app_and_logs(owner, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    assert owner.get(f"/apps/{aid}").json()["uptime_seconds"] is None  # no pods
    ns = f"app-{aid}"
    now = datetime.now(timezone.utc)
    kube.pod_lines[ns] = {"web-a": ["a"]}
    kube.pod_meta[ns] = {"web-a": PodInfo(name="web-a", created_at=now - timedelta(hours=2), started_at=now - timedelta(hours=1),
                                          phase="Running", ready=True, rollout="x")}
    up = owner.get(f"/apps/{aid}").json()["uptime_seconds"]
    assert 3590 <= up <= 3700
    logs = owner.get(f"/apps/{aid}/logs").json()
    assert 3590 <= logs["uptime_seconds"] <= 3700 and logs["pod_info"][0]["uptime_seconds"] == logs["uptime_seconds"]


def test_metrics_method_label_is_bounded():
    from types import SimpleNamespace
    from app import metrics
    assert metrics.method_label(SimpleNamespace(method="GET")) == "GET"
    assert metrics.method_label(SimpleNamespace(method="XYZZY-1234")) == "OTHER"
