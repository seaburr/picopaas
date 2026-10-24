"""Per-app serialisation of backup/export enqueueing (row lock) and the owner's "come back later" download listing."""
from datetime import datetime, timedelta

from sqlalchemy.dialects import mysql

from app import backups, config, models
from tests.test_backups import (_finish_export, _ghost_like, admin, dl, on, other, runner, user)  # noqa: F401


def test_lock_app_selects_config_row_for_update(monkeypatch, user, db_session, on):
    aid = _ghost_like(user, db_session)
    seen = []
    orig = db_session.execute

    def spy(stmt, *a, **kw):
        seen.append(str(stmt.compile(dialect=mysql.dialect())) if hasattr(stmt, "compile") else "")
        return orig(stmt, *a, **kw)

    monkeypatch.setattr(db_session, "execute", spy)
    backups.lock_app(db_session, db_session.get(models.App, aid))
    assert any("FOR UPDATE" in s and "app_backup_config" in s for s in seen), seen


def test_manual_backup_lock_taken_before_checks(monkeypatch, user, db_session, on):
    """Simulate the other request committing an active run while we waited for the lock: the checks must see it."""
    aid = _ghost_like(user, db_session)
    real = backups.lock_app

    def lock_then_rival_commits(session, app):
        cfg = real(session, app)
        session.add(models.BackupRun(app_id=app.id, kind="db", trigger="manual", status="queued", created_at=datetime.utcnow()))
        session.commit()
        return cfg

    monkeypatch.setattr(backups, "lock_app", lock_then_rival_commits)
    assert user.post(f"/apps/{aid}/backups").status_code == 409
    assert db_session.query(models.BackupRun).filter_by(app_id=aid, trigger="manual").count() == 1


def test_export_lock_taken_before_checks(dl, monkeypatch, user, db_session):
    aid, run_db, run_bk = dl
    real = backups.lock_app

    def lock_then_rival_commits(session, app):
        cfg = real(session, app)
        session.add(models.BackupRun(app_id=app.id, kind="bucket", trigger="export", status="queued",
                                     created_at=datetime.utcnow(), source_run_id=run_bk))
        session.commit()
        return cfg

    monkeypatch.setattr(backups, "lock_app", lock_then_rival_commits)
    assert user.post(f"/apps/{aid}/backups/{run_db}/download").status_code == 409  # different snapshot: one active export
    assert db_session.query(models.BackupRun).filter_by(app_id=aid, trigger="export").count() == 1


def test_double_click_never_duplicates(dl, user, db_session):
    aid, run_db, _ = dl
    a = user.post(f"/apps/{aid}/backups/{run_db}/download")
    b = user.post(f"/apps/{aid}/backups/{run_db}/download")
    assert a.json()["export_id"] == b.json()["export_id"]
    assert user.post(f"/apps/{aid}/backups").status_code == 202
    assert user.post(f"/apps/{aid}/backups").status_code in (409, 429)
    assert db_session.query(models.BackupRun).filter_by(app_id=aid, trigger="export").count() == 1


def test_list_exports_and_run_export_field(dl, user, other, admin, db_session, runner):
    aid, run_db, run_bk = dl
    assert user.get(f"/apps/{aid}/backups/exports").json() == []
    ex = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    runs = {r["id"]: r for r in user.get(f"/apps/{aid}/backups").json()["runs"]}
    assert runs[run_db]["export"] == {"id": ex["export_id"], "status": "queued", "available_until": None}
    assert runs[run_bk]["export"] is None
    _finish_export(db_session, runner)
    lst = user.get(f"/apps/{aid}/backups/exports").json()
    assert [e["export_id"] for e in lst] == [ex["export_id"]] and lst[0]["status"] == "ready" and lst[0]["url"] is None
    assert lst[0]["run_id"] == run_db and lst[0]["available_until"]
    runs = {r["id"]: r for r in user.get(f"/apps/{aid}/backups").json()["runs"]}
    assert runs[run_db]["export"]["status"] == "ready" and runs[run_db]["export"]["available_until"]
    assert admin.get(f"/apps/{aid}/backups/exports").status_code == 200
    assert other.get(f"/apps/{aid}/backups/exports").status_code == 404
    assert other.get("/apps/99999/backups/exports").status_code == 404


def test_list_exports_newest_first_hides_expired_and_old_failed(dl, user, db_session, runner):
    aid, run_db, run_bk = dl
    now = datetime.utcnow()

    def row(status, finished_ago, created_ago, src):
        r = models.BackupRun(app_id=aid, kind="db", trigger="export", status=status, source_run_id=src,
                             created_at=now - timedelta(seconds=created_ago),
                             finished_at=None if finished_ago is None else now - timedelta(seconds=finished_ago),
                             export_key="exports/x/1.sql.gz")
        db_session.add(r)
        db_session.commit()
        return r.id

    expired = row("succeeded", config.BACKUP_EXPORT_TTL_SECONDS + 60, config.BACKUP_EXPORT_TTL_SECONDS + 120, run_db)
    old_failed = row("failed", 4000, 4100, run_db)
    new_failed = row("failed", 100, 200, run_db)
    ready = row("succeeded", 50, 80, run_db)
    ids = [e["export_id"] for e in user.get(f"/apps/{aid}/backups/exports").json()]
    assert ids == [ready, new_failed] and expired not in ids and old_failed not in ids
    run = {r["id"]: r for r in user.get(f"/apps/{aid}/backups").json()["runs"]}[run_db]
    assert run["export"]["id"] == ready  # failed ones never count as the run's export


def test_scheduled_run_download_and_fresh_url_after_ten_minutes(dl, user, db_session, runner):
    aid, run_db, _ = dl
    assert db_session.get(models.BackupRun, run_db).trigger == "scheduled"
    ex = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    _finish_export(db_session, runner)
    first = user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()
    assert first["status"] == "ready" and first["url"] and first["url_expires_in"] == 600
    n = len(runner.presigned)
    row = db_session.get(models.BackupRun, ex["export_id"])
    row.finished_at = datetime.utcnow() - timedelta(minutes=11)  # past the old link's life, inside the file's
    db_session.commit()
    again = user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()
    assert again["status"] == "ready" and again["url"] and len(runner.presigned) == n + 1
    assert again["available_until"] == first["available_until"] or again["available_until"] < first["available_until"]
    assert user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").status_code == 200
