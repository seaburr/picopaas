"""Admin download of a platform database backup (F1): admin browser session + password re-entry, export Job via the fake
runner, exports kept out of the backup bookkeeping. Real register/login flow, no auth mocks."""
from datetime import datetime, timedelta

import pytest

from app import backups, config, models, platform_backup, platform_settings
from app.main import app as fastapi_app
from app.providers.fake import FakeBackupRunner
from app.routers.backups import get_backup_runner
from tests.conftest import DEFAULT_PASSWORD, register_and_login, utcnow_naive
from tests.test_backups import on  # noqa: F401


@pytest.fixture()
def pdb(on, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "DATABASE_URL", "mysql+pymysql://platform:not-the-db-pass@db.internal:25060/picopaas")


@pytest.fixture()
def runner():
    r = FakeBackupRunner()
    fastapi_app.dependency_overrides[get_backup_runner] = lambda: r
    yield r
    fastapi_app.dependency_overrides.pop(get_backup_runner, None)


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def other(make_client, admin):
    c = make_client()
    register_and_login(c, "other@example.com")
    return c


def _backup(db, snap="aa11bb22", status="succeeded", finished=None, trigger="scheduled"):
    r = models.PlatformBackupRun(trigger=trigger, status=status, created_at=utcnow_naive(), finished_at=finished or utcnow_naive(),
                                 snapshot_id=snap if status == "succeeded" else None, repo_bytes=500)
    db.add(r)
    db.commit()
    return r


def _post(c, run_id, password=DEFAULT_PASSWORD):
    return c.post(f"/admin/platform-backups/{run_id}/download", json={"password": password})


def _audit(db, action):
    return db.query(models.AuditLog).filter_by(action=action).all()


def _run_export(db, runner):
    backups.tick(db, runner, utcnow_naive())
    runner.finish(runner.started[-1].name, {"exported": True, "bytes": 4242})
    backups.tick(db, runner, utcnow_naive())


def test_anonymous_and_non_admin_refused(pdb, admin, other, make_client, db_session, runner):
    b = _backup(db_session)
    anon = make_client()
    assert _post(anon, b.id).status_code == 401
    assert anon.get("/admin/platform-backups/exports/1").status_code == 401
    assert _post(other, b.id).status_code == 403
    assert other.get("/admin/platform-backups/exports/1").status_code == 403
    assert db_session.query(models.PlatformBackupRun).filter_by(trigger="export").count() == 0


def test_api_key_refused_even_full_scope(pdb, admin, make_client, db_session, runner):
    b = _backup(db_session)
    key = admin.post("/api/keys", json={"scope": "full"}).json()["key"]
    k = make_client()
    k.headers["Authorization"] = f"Bearer {key}"
    assert _post(k, b.id).status_code == 403
    assert k.get("/admin/platform-backups/exports/1").status_code == 403
    assert db_session.query(models.PlatformBackupRun).filter_by(trigger="export").count() == 0


def test_wrong_password_refused_and_audited(pdb, admin, db_session, runner):
    b = _backup(db_session)
    r = _post(admin, b.id, "not-the-password")
    assert r.status_code == 403
    assert db_session.query(models.PlatformBackupRun).filter_by(trigger="export").count() == 0
    (a,) = _audit(db_session, "platform_backup.download_refused")
    assert a.actor_email == "admin@example.com" and "wrong_password" in a.detail and "not-the-password" not in a.detail
    assert admin.post(f"/admin/platform-backups/{b.id}/download", json={}).status_code == 422  # password is required
    assert admin.post(f"/admin/platform-backups/{b.id}/download", json={"password": ""}).status_code == 422


def test_wrong_password_is_throttled_like_login(pdb, admin, db_session, runner, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "LOGIN_EMAIL_MAX_FAILS", 2)
    b = _backup(db_session)
    assert [_post(admin, b.id, "not-the-password").status_code for _ in range(2)] == [403, 403]
    r = _post(admin, b.id)  # even the right one while locked
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert db_session.query(models.PlatformBackupRun).filter_by(trigger="export").count() == 0


def test_happy_path_download_and_poll(pdb, admin, db_session, runner):
    b = _backup(db_session, finished=datetime(2026, 11, 3, 1, 0))
    r = _post(admin, b.id)
    assert r.status_code == 202, r.text
    ex = r.json()
    assert ex["status"] == "queued" and ex["url"] is None and ex["run_id"] == b.id
    row = db_session.get(models.PlatformBackupRun, ex["export_id"])
    assert row.trigger == "export" and row.snapshot_id == "aa11bb22"
    (d,) = _audit(db_session, "platform_backup.download")
    assert d.actor_email == "admin@example.com" and f"run={b.id}" in d.detail
    assert admin.get(f"/admin/platform-backups/exports/{ex['export_id']}").json()["status"] == "queued"
    runner.started.clear()
    backups.tick(db_session, runner, utcnow_naive())
    (spec,) = runner.started
    # The Job's root filesystem is read-only, so the dump needs /work. Missing it failed live with "not a MySQL dump".
    from app.providers.backup import job_manifests
    pod = job_manifests(spec)[1]["spec"]["template"]["spec"]
    assert {"name": "work", "mountPath": "/work"} in pod["containers"][0]["volumeMounts"]
    assert any(v["name"] == "work" for v in pod["volumes"])
    e = spec.env
    assert e["MODE"] == "export" and e["KIND"] == "db" and e["SNAPSHOT"] == "aa11bb22" and e["EXPORT_BUCKET"] == config.BACKUP_BUCKET
    assert e["EXPORT_KEY"] == f"exports/platform/{row.id}.sql.gz" and e["RESTIC_REPOSITORY"].endswith("/platform/db")
    assert e["RESTIC_PASSWORD"] and "DB_PASSWORD" not in e and spec.app_id == 0
    assert admin.get(f"/admin/platform-backups/exports/{ex['export_id']}").json()["status"] == "running"
    runner.finish(spec.name, {"exported": True, "bytes": 4242})
    backups.tick(db_session, runner, utcnow_naive())
    got = admin.get(f"/admin/platform-backups/exports/{ex['export_id']}").json()
    assert got["status"] == "ready" and got["url"].startswith("https://") and got["size_bytes"] == 4242
    assert got["filename"] == "picopaas-platform-db-2026-11-03.sql.gz" and got["available_until"]
    assert runner.presigned == [(f"exports/platform/{row.id}.sql.gz", config.BACKUP_EXPORT_URL_SECONDS, got["filename"])]
    (lk,) = _audit(db_session, "platform_backup.download_link")
    assert f"export={row.id}" in lk.detail and "https" not in lk.detail and "sig=" not in lk.detail
    assert _audit(db_session, "platform_backup.export_succeeded")
    assert spec.name in runner.cleaned


def test_post_is_idempotent_while_live(pdb, admin, db_session, runner):
    b = _backup(db_session)
    first, again = _post(admin, b.id), _post(admin, b.id)
    assert first.json()["export_id"] == again.json()["export_id"]
    assert len(_audit(db_session, "platform_backup.download")) == 1


def test_failed_export_reported_and_retry_allowed(pdb, admin, db_session, runner):
    b = _backup(db_session)
    ex = _post(admin, b.id).json()
    backups.tick(db_session, runner, utcnow_naive())
    runner.fail(runner.started[-1].name, "reading the snapshot from the repository failed")
    backups.tick(db_session, runner, utcnow_naive())
    got = admin.get(f"/admin/platform-backups/exports/{ex['export_id']}").json()
    assert got["status"] == "failed" and got["url"] is None and "repository" in got["error"]
    assert _audit(db_session, "platform_backup.export_failed")
    s = platform_backup.summary(db_session)
    assert s["failed_download"]["id"] == ex["export_id"] and s["runs"][0]["export"] is None
    assert _post(admin, b.id).status_code == 202  # a failed one doesn't block or count


def test_expired_export_has_no_link(pdb, admin, db_session, runner):
    b = _backup(db_session)
    ex = _post(admin, b.id).json()
    _run_export(db_session, runner)
    row = db_session.get(models.PlatformBackupRun, ex["export_id"])
    row.finished_at = utcnow_naive() - timedelta(seconds=config.BACKUP_EXPORT_TTL_SECONDS + 5)
    db_session.commit()
    got = admin.get(f"/admin/platform-backups/exports/{ex['export_id']}").json()
    assert got["status"] == "expired" and got["url"] is None and got["filename"] is None
    assert runner.presigned == [] and not _audit(db_session, "platform_backup.download_link")


def test_presign_failure_is_503(pdb, admin, db_session, runner):
    b = _backup(db_session)
    ex = _post(admin, b.id).json()
    _run_export(db_session, runner)
    runner.fail_presign = True
    assert admin.get(f"/admin/platform-backups/exports/{ex['export_id']}").status_code == 503
    assert not _audit(db_session, "platform_backup.download_link")


def test_bad_run_ids(pdb, admin, db_session, runner):
    assert _post(admin, 9999).status_code == 404
    failed = _backup(db_session, status="failed")
    r = _post(admin, failed.id)
    assert r.status_code == 422 and "nothing to download" in r.json()["detail"]
    ex = _post(admin, _backup(db_session).id).json()
    assert _post(admin, ex["export_id"]).status_code == 404  # an export row is not a backup
    queued = models.PlatformBackupRun(trigger="manual", status="queued", created_at=utcnow_naive())
    db_session.add(queued)
    db_session.commit()
    assert _post(admin, queued.id).status_code == 422
    assert admin.get("/admin/platform-backups/exports/9999").status_code == 404
    assert admin.get(f"/admin/platform-backups/exports/{failed.id}").status_code == 404  # a backup row is not an export
    assert len(_audit(db_session, "platform_backup.download_refused")) == 2


def test_unavailable_when_platform_backup_inactive(on, admin, db_session, runner):  # SQLite: platform_backup.active() is False
    b = _backup(db_session)
    r = _post(admin, b.id)
    assert r.status_code == 503
    assert _audit(db_session, "platform_backup.download_refused")


def test_one_at_a_time_and_hourly_limit(pdb, admin, db_session, runner):
    platform_settings.update(db_session, {"backup_export_max_per_hour": 2}, "admin@example.com")
    limit = int(platform_settings.get(db_session, "backup_export_max_per_hour"))
    runs = [_backup(db_session, snap=f"s{i:07d}") for i in range(limit + 1)]
    for r in runs[:limit]:
        assert _post(admin, r.id).status_code == 202
        _run_export(db_session, runner)
    over = _post(admin, runs[limit].id)
    assert over.status_code == 429 and int(over.headers["retry-after"]) > 0
    assert len(_audit(db_session, "platform_backup.download_refused")) == 1
    assert db_session.query(models.PlatformBackupRun).filter_by(trigger="export").count() == limit


def test_second_snapshot_while_one_is_active_is_409(pdb, admin, db_session, runner):
    a, b = _backup(db_session, snap="aaaaaaaa"), _backup(db_session, snap="bbbbbbbb")
    assert _post(admin, a.id).status_code == 202
    assert _post(admin, b.id).status_code == 409


def test_exports_are_not_backups(pdb, admin, db_session, runner):
    """Summary, last success, scheduling, the in-flight check and pruning ignore export rows."""
    b = _backup(db_session, snap="aa11bb22", finished=utcnow_naive() - timedelta(hours=3))
    ex = _post(admin, b.id).json()
    _run_export(db_session, runner)
    s = admin.get("/admin/health/backups").json()["platform_db"]
    assert [r["id"] for r in s["runs"]] == [b.id]  # the export is not listed as a backup
    assert s["runs"][0]["downloadable"] is True and s["runs"][0]["export"]["id"] == ex["export_id"]
    assert s["runs"][0]["export"]["status"] == "ready" and s["download_per_hour"] >= 1
    assert s["last_success_at"] == b.finished_at.isoformat() and s["repo_bytes"] == 500
    assert platform_backup._due(db_session, utcnow_naive()) is False  # the backup is 3h old, interval 24h; the fresh export is ignored
    b.created_at = utcnow_naive() - timedelta(hours=config.PLATFORM_BACKUP_INTERVAL_HOURS + 1)
    db_session.commit()
    assert platform_backup._due(db_session, utcnow_naive()) is True  # overdue despite a newer export row


def test_export_in_flight_does_not_block_backup_and_stays_when_pruning(pdb, admin, db_session, runner, monkeypatch):
    monkeypatch.setattr(platform_backup, "KEPT_RUNS", 2)
    b = _backup(db_session)
    ex = _post(admin, b.id).json()
    backups.tick(db_session, runner, utcnow_naive())  # export Job running
    assert platform_backup.enqueue(db_session, "manual") is not None  # not blocked
    for i in range(4):
        _backup(db_session, snap=f"p{i:07d}")
    # finishing a backup runs the pruning: only 2 backups are kept, the live export row survives
    done = _backup(db_session, snap="dd55ee66")
    platform_backup._finish(db_session, done, None, ok=True, result={"snapshot_id": "dd55ee66"}, now=utcnow_naive())
    assert db_session.query(models.PlatformBackupRun).filter(models.PlatformBackupRun.trigger != "export").count() == 2
    assert db_session.get(models.PlatformBackupRun, ex["export_id"]).status == "running"
    # old finished export rows go after a day
    row = db_session.get(models.PlatformBackupRun, ex["export_id"])
    row.status, row.created_at = "succeeded", utcnow_naive() - timedelta(days=2)
    db_session.commit()
    platform_backup._finish(db_session, _backup(db_session, snap="ff77aa88"), None, ok=True, result={}, now=utcnow_naive())
    assert db_session.get(models.PlatformBackupRun, ex["export_id"]) is None


def test_job_secret_not_swept_while_export_runs(pdb, admin, db_session, runner):
    _post(admin, _backup(db_session).id)
    backups.tick(db_session, runner, utcnow_naive())
    assert runner.started[-1].name in backups.active_jobs(db_session)


def test_export_sweep_covers_platform_prefix(pdb, admin, db_session, runner):
    ex = _post(admin, _backup(db_session).id).json()
    assert platform_backup.export_key(db_session.get(models.PlatformBackupRun, ex["export_id"])).startswith(config.BACKUP_EXPORT_PREFIX)
    assert config.backup_export_prefix_ok()
