"""Audit rows for creating, deleting and downloading backups (actor, action, no secrets or URLs in the detail)."""
from datetime import datetime, timedelta

from app import backups, config, models
from tests.test_backups import (NOW, _finish_export, _ghost_like, _app_with_db, admin, dl, on, other, runner, user)  # noqa: F401


def rows(db, action):
    db.expire_all()
    return db.query(models.AuditLog).filter_by(action=action).order_by(models.AuditLog.id).all()


def test_manual_create_has_trigger(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    assert user.post(f"/apps/{aid}/backups").status_code == 202
    (r,) = rows(db_session, "backup.create")
    assert r.actor_email == "user@example.com" and r.detail.startswith("trigger=manual kinds=")


def test_scheduled_create_one_row_per_enqueue_and_prune(on, user, db_session, runner):
    aid = _ghost_like(user, db_session)
    backups.tick(db_session, runner, NOW)
    (r,) = rows(db_session, "backup.create")  # db + bucket queued together: one row
    assert r.actor_id is None and r.actor_email == "system" and r.target_id == str(aid)
    assert r.detail.startswith("trigger=scheduled kinds=") and "db" in r.detail and "bucket" in r.detail
    for s in list(runner.started):
        runner.finish(s.name)
    backups.tick(db_session, runner, NOW)
    pruned = rows(db_session, "backup.pruned")
    assert len(pruned) == 2 and all(p.actor_id is None and p.actor_email == "system" for p in pruned)
    assert {p.detail.split()[1] for p in pruned} == {"kind=db", "kind=bucket"}
    assert pruned[0].detail.endswith("keep=daily:1,weekly:1,monthly:1")
    # a failed run prunes nothing
    runner2_before = len(pruned)
    backups.tick(db_session, runner, NOW + timedelta(days=2))
    for s in runner.started[2:]:
        runner.fail(s.name)
    backups.tick(db_session, runner, NOW + timedelta(days=2))
    assert len(rows(db_session, "backup.pruned")) == runner2_before


def test_final_and_purge_audited(on, user, db_session, runner):
    aid = _ghost_like(user, db_session, with_db=False)
    app = db_session.get(models.App, aid)
    backups.ensure_config(db_session, app, NOW)
    assert backups.final_gate(db_session, app, NOW) is False
    (r,) = rows(db_session, "backup.create")
    assert r.actor_email == "system" and r.detail.startswith("trigger=final kinds=")
    assert backups.final_gate(db_session, app, NOW + timedelta(minutes=1)) is False
    assert len(rows(db_session, "backup.create")) == 1  # not re-enqueued
    # purge: success then failure
    cfg = db_session.get(models.AppBackupConfig, aid)
    cfg.bucket_repo_bytes, cfg.deleted_retention_until = 400, NOW - timedelta(days=1)
    app.deleted_at = NOW - timedelta(days=15)
    for run in db_session.query(models.BackupRun).filter_by(app_id=aid).all():
        run.status = "failed"
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    purge = [s for s in runner.started if s.env["MODE"] == "purge"][0]
    runner.fail(purge.name, "purge failed")
    backups.tick(db_session, runner, NOW)
    (p,) = rows(db_session, "backup.purged")
    assert p.actor_id is None and p.actor_email == "system" and "kind=bucket" in p.detail and "result=failed" in p.detail
    backups.tick(db_session, runner, NOW + timedelta(days=2))
    purge2 = [s for s in runner.started if s.env["MODE"] == "purge"][-1]
    runner.finish(purge2.name, {"purged": True})
    backups.tick(db_session, runner, NOW + timedelta(days=2))
    ps = rows(db_session, "backup.purged")
    assert len(ps) == 2 and "result=succeeded" in ps[1].detail
    for x in ps:
        assert "http" not in x.detail and "password" not in x.detail.lower()


def test_export_sweep_audited_per_deleted_export(dl, user, db_session, runner):
    from app.backups import _last_export_sweep
    aid, run_db, run_bk = dl
    ex1 = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    _finish_export(db_session, runner)
    ex2 = user.post(f"/apps/{aid}/backups/{run_bk}/download").json()
    _finish_export(db_session, runner)
    k1 = db_session.get(models.BackupRun, ex1["export_id"]).export_key
    assert k1
    runner.swept_keys = [k1, "exports/unknown-object.tar.gz"]  # an unmapped key writes nothing
    _last_export_sweep.clear()
    backups.tick(db_session, runner, datetime.utcnow() + timedelta(hours=1))
    (r,) = rows(db_session, "backup.export_deleted")
    assert r.actor_id is None and r.actor_email == "system" and r.target_id == str(aid)
    assert r.detail == f"export={ex1['export_id']} run={run_db} reason=expired"
    assert k1 not in r.detail
    assert ex2["export_id"] != ex1["export_id"]


def test_download_link_audited_each_time_only_with_url(dl, user, other, db_session, runner):
    aid, run_db, _ = dl
    ex = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    eid = ex["export_id"]
    assert user.get(f"/apps/{aid}/backups/exports/{eid}").json()["url"] is None  # queued: no link, no row
    assert rows(db_session, "backup.download_link") == []
    _finish_export(db_session, runner)
    a = user.get(f"/apps/{aid}/backups/exports/{eid}").json()
    assert a["url"]
    assert other.get(f"/apps/{aid}/backups/exports/{eid}").status_code == 404
    user.get(f"/apps/{aid}/backups/exports/{eid}")
    user.get(f"/apps/{aid}/backups/exports")  # the list never carries a link
    rs = rows(db_session, "backup.download_link")
    assert len(rs) == 2
    for r in rs:
        assert r.actor_email == "user@example.com" and r.target_id == str(aid)
        assert r.detail == f"export={eid} run={run_db} kind=db expires_in=600"
        assert "http" not in r.detail and "sig" not in r.detail and a["url"] not in r.detail
    key = db_session.get(models.BackupRun, eid).export_key
    assert key not in rs[0].detail
