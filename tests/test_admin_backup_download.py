"""Admin download of any app's backups, including deleted apps' (browser session only). Real login flow, fake runner."""
from datetime import datetime, timedelta

from app import backups, models
from tests.test_backups import NOW, admin, on, other, runner, user  # noqa: F401
from tests.test_backups_sets import _mk_set, api, app_ctx, rows, scoped, vols  # noqa: F401

OK = ("succeeded", None, 100, None)


def _delete(db, aid):
    app = db.get(models.App, aid)
    app.deleted_at = datetime.utcnow()
    db.get(models.AppBackupConfig, aid).deleted_retention_until = datetime.utcnow() + timedelta(days=14)
    db.commit()


def _finish(db, runner):
    backups.tick(db, runner, datetime.utcnow())
    runner.finish(runner.started[-1].name, {"exported": True, "bytes": 4242})
    backups.tick(db, runner, datetime.utcnow())


def test_non_admin_and_api_key_refused(app_ctx, user, admin, other, make_client, db_session):
    aid = app_ctx
    sid, _ = _mk_set(db_session, aid, {"db": OK})
    for c in (user, other):
        assert c.get(f"/admin/apps/{aid}/backup-sets").status_code == 403
        assert c.post(f"/admin/apps/{aid}/backup-sets/{sid}/download").status_code == 403
        assert c.get(f"/admin/apps/{aid}/backup-exports/1").status_code == 403
    key = admin.post("/api/keys", json={"scope": "full"}).json()["key"]
    k = make_client()
    k.headers["Authorization"] = f"Bearer {key}"
    assert k.get(f"/admin/apps/{aid}/backup-sets").status_code == 403
    assert k.post(f"/admin/apps/{aid}/backup-sets/{sid}/download").status_code == 403
    assert k.get(f"/admin/apps/{aid}/backup-exports/1").status_code == 403
    assert db_session.query(models.BackupRun).filter_by(trigger="export").count() == 0


def test_admin_downloads_deleted_apps_backup(app_ctx, user, admin, db_session, runner):
    aid = app_ctx
    sid, _ = _mk_set(db_session, aid, {"db": OK, "bucket": OK})
    _delete(db_session, aid)
    assert user.post(f"/apps/{aid}/backups/sets/{sid}/download").status_code == 404  # the owner can't
    j = admin.get(f"/admin/apps/{aid}/backup-sets").json()
    assert j["deleted"] is True and j["purge_after"] and j["owner_email"] == "user@example.com"
    assert [b["id"] for b in j["backups"]] == [sid]
    r = admin.post(f"/admin/apps/{aid}/backup-sets/{sid}/download")
    assert r.status_code == 202, r.text
    ex = r.json()
    assert ex["set_id"] == sid and ex["status"] == "queued" and ex["url"] is None
    _finish(db_session, runner)  # the scheduler must not fail the export of a retained, deleted app
    got = admin.get(f"/admin/apps/{aid}/backup-exports/{ex['export_id']}").json()
    assert got["status"] == "ready" and got["url"].startswith("https://")
    (dl,) = rows(db_session, "backup.download")
    assert dl.target_id == str(aid) and f"set={sid}" in dl.detail and "deleted_app" in dl.detail
    (lk,) = rows(db_session, "backup.download_link")
    assert lk.actor_email == "admin@example.com" and f"export={ex['export_id']}" in lk.detail and "https" not in lk.detail


def test_admin_download_live_app_rate_limit_and_unknowns(app_ctx, admin, db_session):
    aid = app_ctx
    sid, _ = _mk_set(db_session, aid, {"db": OK})
    assert admin.get("/admin/apps/99999/backup-sets").status_code == 404
    assert admin.post(f"/admin/apps/{aid}/backup-sets/99999/download").status_code == 404
    assert admin.get(f"/admin/apps/{aid}/backup-exports/99999").status_code == 404
    first = admin.post(f"/admin/apps/{aid}/backup-sets/{sid}/download")
    assert first.status_code == 202
    again = admin.post(f"/admin/apps/{aid}/backup-sets/{sid}/download")  # idempotent while live
    assert again.json()["export_id"] == first.json()["export_id"]
    assert len(rows(db_session, "backup.download")) == 1
