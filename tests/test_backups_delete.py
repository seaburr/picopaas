"""Owner delete of one backup: DELETE /apps/{id}/backups/{run_id} (restic forget <snapshot> --prune in a MODE=forget Job)."""
import os
import shutil
import stat
import subprocess
from datetime import datetime, timedelta

import pytest

from app import backups, config, models
from tests.test_backups import (NOW, _finish_export, _ghost_like, _succeeded, admin, dl, on, other, runner, user)  # noqa: F401


def rows(db, action):
    db.expire_all()
    return db.query(models.AuditLog).filter_by(action=action).order_by(models.AuditLog.id).all()


def url(aid, run):
    return f"/apps/{aid}/backups/{run}"


def listing(client, aid):
    return {r["id"]: r for r in client.get(f"/apps/{aid}/backups").json()["runs"]}


def run_forget(db, runner):
    backups.tick(db, runner, datetime.utcnow())
    return [s for s in runner.started if s.env["MODE"] == "forget"][-1]


def test_delete_202_shape_job_and_state(dl, user, db_session, runner):
    aid, run_db, run_bk = dl
    r = user.delete(url(aid, run_db))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["run_id"] == run_db and body["status"] == "deleting"
    assert listing(user, aid)[run_db]["status"] == "deleting" and listing(user, aid)[run_bk]["status"] == "succeeded"
    drun = db_session.get(models.BackupRun, body["delete_run_id"])
    assert (drun.trigger, drun.status, drun.kind, drun.source_run_id) == ("delete", "queued", "db", run_db)
    spec = run_forget(db_session, runner)
    assert spec.name == f"bk-{drun.id}" and spec.scratch_mib == 0
    env = spec.env
    assert env["MODE"] == "forget" and env["KIND"] == "db" and env["SNAPSHOT"] == "d0d0d0d0"
    assert env["RESTIC_REPOSITORY"].endswith(f"/apps/{aid}/db") and "DB_PASSWORD" not in env and "SRC_SECRET_KEY" not in env
    assert db_session.get(models.BackupRun, drun.id).status == "running"
    assert listing(user, aid)[run_db]["status"] == "deleting"  # still while the Job runs


def test_success_hides_run_keeps_row_and_updates_repo_bytes(dl, user, db_session, runner):
    aid, run_db, run_bk = dl
    cfg = db_session.get(models.AppBackupConfig, aid)
    cfg.db_repo_bytes = 9000
    db_session.commit()
    user.delete(url(aid, run_db))
    spec = run_forget(db_session, runner)
    runner.finish(spec.name, {"forgotten": True, "repo_bytes": 1234})
    backups.tick(db_session, runner, datetime.utcnow())
    assert run_db not in listing(user, aid) and run_bk in listing(user, aid)
    assert db_session.get(models.BackupRun, run_db).status == "deleted"  # row kept: restore/export source_run_id still resolves
    assert db_session.get(models.AppBackupConfig, aid).db_repo_bytes == 1234
    assert user.get(f"/apps/{aid}/backups").json()["kinds"][0]["repo_bytes"] == 1234
    assert spec.name in runner.cleaned
    # the deleted run is gone for good: 404 on delete, refused for download
    assert user.delete(url(aid, run_db)).status_code == 404
    assert user.post(f"/apps/{aid}/backups/{run_db}/download").status_code == 422
    (a,) = rows(db_session, "backup.deleted")
    assert a.actor_email == "system" and a.actor_id is None and a.detail == f"run={run_db} kind=db snapshot=d0d0d0d0 delete_run={spec.run_id}"


def test_success_without_repo_bytes_keeps_old_value(dl, user, db_session, runner):
    aid, run_db, _ = dl
    db_session.get(models.AppBackupConfig, aid).db_repo_bytes = 777
    db_session.commit()
    user.delete(url(aid, run_db))
    runner.finish(run_forget(db_session, runner).name, {"forgotten": True})
    backups.tick(db_session, runner, datetime.utcnow())
    assert db_session.get(models.AppBackupConfig, aid).db_repo_bytes == 777


def test_failure_returns_to_succeeded_with_event_and_audit(dl, user, db_session, runner):
    aid, run_db, _ = dl
    user.delete(url(aid, run_db))
    spec = run_forget(db_session, runner)
    runner.fail(spec.name, "deleting the backup failed")
    backups.tick(db_session, runner, datetime.utcnow())
    assert listing(user, aid)[run_db]["status"] == "succeeded"
    d = db_session.get(models.BackupRun, spec.run_id)
    assert d.status == "failed" and d.error == "deleting the backup failed"
    ev = db_session.query(models.AppEvent).filter_by(app_id=aid, kind="backup_failed").one()
    assert "Deleting your database backup failed: deleting the backup failed" in ev.message
    (a,) = rows(db_session, "backup.delete_failed")
    assert a.actor_email == "system" and a.detail.startswith(f"run={run_db} kind=db snapshot=d0d0d0d0") and "deleting the backup failed" in a.detail
    assert rows(db_session, "backup.deleted") == []
    # not the kind's summary: unchanged by a failed delete
    assert user.delete(url(aid, run_db)).status_code == 202  # can try again


def test_start_failure_also_restores_source(dl, user, db_session, runner):
    aid, run_db, _ = dl
    user.delete(url(aid, run_db))
    runner.fail_start = True
    backups.tick(db_session, runner, datetime.utcnow())
    assert listing(user, aid)[run_db]["status"] == "succeeded"
    assert len(rows(db_session, "backup.delete_failed")) == 1


def test_last_success_summary_recomputed_from_remaining_runs(on, user, db_session, runner):
    aid = _ghost_like(user, db_session, "summ", with_db=True)
    old = _succeeded(db_session, aid, "db", "11111111", bytes_added=10)
    new = _succeeded(db_session, aid, "db", "22222222", bytes_added=20)
    r_old, r_new = db_session.get(models.BackupRun, old), db_session.get(models.BackupRun, new)
    r_old.finished_at, r_new.finished_at = NOW - timedelta(days=1), NOW
    cfg = db_session.get(models.AppBackupConfig, aid)
    cfg.db_last_backup_at = cfg.last_backup_at = cfg.last_success_at = NOW
    cfg.db_last_status = cfg.last_status = "succeeded"
    cfg.db_last_bytes = 20
    cfg.next_backup_at = NOW + timedelta(days=30)
    db_session.commit()
    from app.main import app as fastapi_app
    from app.routers.backups import get_backup_runner
    fastapi_app.dependency_overrides[get_backup_runner] = lambda: runner
    assert user.delete(url(aid, new)).status_code == 202
    runner.finish(run_forget(db_session, runner).name, {"forgotten": True})
    backups.tick(db_session, runner, datetime.utcnow())
    db_session.expire_all()
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.db_last_backup_at == NOW - timedelta(days=1) and cfg.db_last_bytes == 10 and cfg.db_last_status == "succeeded"
    assert cfg.last_success_at == NOW - timedelta(days=1) and cfg.last_backup_at == NOW - timedelta(days=1)
    # deleting the only remaining one clears the summary
    assert user.delete(url(aid, old)).status_code == 202
    runner.finish(run_forget(db_session, runner).name, {"forgotten": True})
    backups.tick(db_session, runner, datetime.utcnow())
    db_session.expire_all()
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.db_last_backup_at is None and cfg.db_last_status is None and cfg.last_success_at is None and cfg.last_status is None
    out = user.get(f"/apps/{aid}/backups").json()
    assert out["runs"] == [] or all(r["trigger"] == "restore" for r in out["runs"])


def test_older_run_delete_leaves_summary(dl, user, db_session, runner):
    aid, run_db, _ = dl
    older = _succeeded(db_session, aid, "db", "0ldd0ld0")
    db_session.get(models.BackupRun, older).finished_at = NOW - timedelta(days=2)
    cfg = db_session.get(models.AppBackupConfig, aid)
    cfg.db_last_backup_at, cfg.db_last_status = NOW, "succeeded"
    db_session.commit()
    user.delete(url(aid, older))
    runner.finish(run_forget(db_session, runner).name, {"forgotten": True})
    backups.tick(db_session, runner, datetime.utcnow())
    db_session.expire_all()
    assert db_session.get(models.AppBackupConfig, aid).db_last_backup_at == NOW


# --- refusals ---

def test_404_unknown_and_other_apps_run(dl, user, other, db_session):
    aid, run_db, _ = dl
    assert user.delete(url(aid, 99999)).status_code == 404
    assert other.delete(url(aid, run_db)).status_code == 404  # not the owner
    oid = user.post("/apps", json={"name": "second", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}).json()["id"]
    assert user.delete(url(oid, run_db)).status_code == 404  # another app's run through my app
    assert db_session.query(models.BackupRun).filter_by(trigger="delete").count() == 0


def test_403_read_scope_key(dl, user, make_client, db_session):
    aid, run_db, _ = dl
    key = user.post("/api/keys", json={"name": "ro", "scope": "read"}).json()["key"]
    c = make_client()
    assert c.delete(url(aid, run_db), headers={"Authorization": f"Bearer {key}"}).status_code == 403
    assert db_session.get(models.BackupRun, run_db).status == "succeeded"


def test_write_key_allowed_and_csrf(dl, user, admin, make_client, db_session):
    aid, run_db, run_bk = dl
    assert user.delete(url(aid, run_db), headers={"Origin": "https://evil-app.picopaas.xyz"}).status_code == 403  # CSRF guard
    assert db_session.get(models.BackupRun, run_db).status == "succeeded"
    kr = user.post("/api/keys", json={"name": "rw", "scope": "full"})
    assert kr.status_code in (200, 201), kr.text
    key = kr.json().get("key") or kr.json().get("raw_key")
    assert make_client().delete(url(aid, run_db), headers={"Authorization": f"Bearer {key}"}).status_code == 202


def test_admin_can_delete_users_backup(dl, user, admin, db_session):
    aid, _, run_bk = dl
    r = admin.delete(url(aid, run_bk))
    assert r.status_code == 202, r.text
    (a,) = rows(db_session, "backup.delete")
    assert a.actor_email == "admin@example.com"


@pytest.mark.parametrize("mut", ["failed", "queued", "running", "nosnap", "restore", "export", "purge", "prune"])
def test_422_not_deletable(dl, user, db_session, mut):
    aid, _, _ = dl
    run = models.BackupRun(app_id=aid, kind="db", trigger="scheduled", status="succeeded", snapshot_id="cc11cc11",
                           created_at=NOW, finished_at=NOW)
    if mut in ("failed", "queued", "running"):
        run.status = mut
    elif mut == "nosnap":
        run.snapshot_id = None
    else:
        run.trigger = mut
        run.status = "succeeded"
    db_session.add(run)
    db_session.commit()
    r = user.delete(url(aid, run.id))
    assert r.status_code == 422, r.text
    assert db_session.query(models.BackupRun).filter_by(trigger="delete").count() == 0
    assert db_session.get(models.BackupRun, run.id).status == ("succeeded" if mut not in ("failed", "queued", "running") else mut)


def test_flag_off_and_unconfigured(user, db_session, monkeypatch):
    assert user.delete("/apps/1/backups/1").status_code in (404, 422)
    monkeypatch.setattr(config, "BACKUPS_ENABLED", True)
    from tests.test_backups import _app_with_db
    aid = _app_with_db(user, db_session)
    assert user.delete(url(aid, 1)).status_code == 503


def test_409_when_repo_busy(dl, user, admin, db_session, runner):
    aid, run_db, run_bk = dl
    busy = models.BackupRun(app_id=aid, kind="db", trigger="manual", status="queued", created_at=NOW)
    db_session.add(busy)
    db_session.commit()
    assert user.delete(url(aid, run_db)).status_code == 409
    assert db_session.get(models.BackupRun, run_db).status == "succeeded"
    assert user.delete(url(aid, run_bk)).status_code == 202  # the other kind's repo is idle
    # running export of the same kind
    busy.status = "running"
    db_session.commit()
    busy.trigger = "export"
    db_session.commit()
    assert user.delete(url(aid, run_db)).status_code == 409


def test_409_for_active_delete_restore_and_second_delete(dl, user, admin, db_session, runner):
    aid, run_db, run_bk = dl
    assert user.delete(url(aid, run_db)).status_code == 202
    again = user.delete(url(aid, run_db))
    assert again.status_code == 409 and "already being deleted" in again.json()["detail"]
    older = _succeeded(db_session, aid, "db", "0ldd0ld0")
    assert user.delete(url(aid, older)).status_code == 409  # a delete job is active for the db repo
    # restore from this repo into another app
    tgt = user.post("/apps", json={"name": "tgt", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}).json()["id"]
    db_session.add(models.BackupRun(app_id=tgt, kind="bucket", trigger="restore", status="running", created_at=NOW, source_run_id=run_bk))
    db_session.commit()
    assert user.delete(url(aid, run_bk)).status_code == 409
    assert user.post(f"/apps/{aid}/backups").status_code == 409  # and a delete blocks new backups too


def test_restore_and_download_refused_for_run_being_deleted(dl, user, admin, db_session):
    aid, run_db, _ = dl
    tgt = user.post("/apps", json={"name": "tgt", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}).json()["id"]
    user.delete(url(aid, run_db))
    assert user.post(f"/apps/{aid}/backups/{run_db}/download").status_code == 422
    assert admin.post(f"/admin/backups/{run_db}/restore", json={"target_app_id": tgt}).status_code == 422


# --- live exports ---

def test_ready_export_is_deleted_and_marked_expired(dl, user, db_session, runner):
    aid, run_db, run_bk = dl
    ex = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    _finish_export(db_session, runner)
    key = db_session.get(models.BackupRun, ex["export_id"]).export_key
    assert user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()["status"] == "ready"
    assert listing(user, aid)[run_db]["export"]["status"] == "ready"
    assert user.delete(url(aid, run_db)).status_code == 202
    assert runner.deleted_exports == [key]
    assert user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()["status"] == "expired"
    assert user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()["url"] is None
    assert user.get(f"/apps/{aid}/backups/exports").json() == []
    (r,) = rows(db_session, "backup.export_deleted")
    assert r.actor_email == "user@example.com" and r.detail == f"export={ex['export_id']} run={run_db} reason=backup_deleted"
    assert key not in r.detail


def test_export_of_other_run_is_untouched(dl, user, db_session, runner):
    aid, run_db, run_bk = dl
    user.post(f"/apps/{aid}/backups/{run_bk}/download")
    _finish_export(db_session, runner)
    assert user.delete(url(aid, run_db)).status_code == 202  # db repo; the bucket export is another repo
    assert runner.deleted_exports == []


def test_running_export_of_the_run_is_409(dl, user, db_session, runner):
    aid, run_db, _ = dl
    user.post(f"/apps/{aid}/backups/{run_db}/download")
    backups.tick(db_session, runner, datetime.utcnow())  # export now running
    r = user.delete(url(aid, run_db))
    assert r.status_code == 409 and db_session.get(models.BackupRun, run_db).status == "succeeded"


def test_export_object_delete_failure_changes_nothing(dl, user, db_session, runner):
    aid, run_db, _ = dl
    ex = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    _finish_export(db_session, runner)
    runner.fail_delete_exports = True
    r = user.delete(url(aid, run_db))
    assert r.status_code == 503
    db_session.expire_all()
    assert db_session.get(models.BackupRun, run_db).status == "succeeded"
    assert db_session.query(models.BackupRun).filter_by(trigger="delete").count() == 0
    assert user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()["status"] == "ready"
    assert rows(db_session, "backup.delete") == []


def test_expired_export_needs_no_object_delete(dl, user, db_session, runner):
    aid, run_db, _ = dl
    user.post(f"/apps/{aid}/backups/{run_db}/download")
    _finish_export(db_session, runner)
    e = db_session.query(models.BackupRun).filter_by(trigger="export").one()
    e.finished_at = datetime.utcnow() - timedelta(seconds=config.BACKUP_EXPORT_TTL_SECONDS + 5)
    db_session.commit()
    assert user.delete(url(aid, run_db)).status_code == 202
    assert runner.deleted_exports == []


# --- audit ---

def test_delete_audit_by_user_without_secrets(dl, user, db_session, runner):
    aid, run_db, _ = dl
    user.delete(url(aid, run_db))
    (a,) = rows(db_session, "backup.delete")
    assert a.actor_email == "user@example.com" and a.target_id == str(aid)
    assert a.detail == f"run={run_db} kind=db snapshot=d0d0d0d0"
    spec = run_forget(db_session, runner)
    runner.finish(spec.name, {"forgotten": True})
    backups.tick(db_session, runner, datetime.utcnow())
    for x in rows(db_session, "backup.delete") + rows(db_session, "backup.deleted"):
        assert "s3:" not in x.detail and "http" not in x.detail and spec.env["RESTIC_PASSWORD"] not in x.detail


def test_long_snapshot_id_truncated_to_8(dl, user, db_session):
    aid, _, _ = dl
    rid = _succeeded(db_session, aid, "db", "abcdef0123456789abcdef")
    user.delete(url(aid, rid))
    assert rows(db_session, "backup.delete")[-1].detail == f"run={rid} kind=db snapshot=abcdef01"


# --- retention still works / the script ---

def test_retention_prune_unaffected_by_deleted_rows(dl, user, db_session, runner):
    aid, run_db, _ = dl
    user.delete(url(aid, run_db))
    runner.finish(run_forget(db_session, runner).name, {"forgotten": True})
    backups.tick(db_session, runner, datetime.utcnow())
    assert user.post(f"/apps/{aid}/backups").status_code == 202  # a new backup is queued as usual
    backups.tick(db_session, runner, datetime.utcnow())
    spec = [s for s in runner.started if s.env["MODE"] == "backup"][-1]
    assert spec.env["KEEP_DAILY"] == str(config.BACKUP_KEEP_DAILY)
    runner.finish(spec.name)
    backups.tick(db_session, runner, datetime.utcnow())
    assert len(rows(db_session, "backup.pruned")) >= 1


def _script(tmp_path, snapshots_out, snapshots_rc=0, snapshots_err=""):
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq not installed")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    (bindir / "jq").symlink_to(jq)
    stub = bindir / "restic"
    stub.write_text(
        "#!/bin/sh\n" f'echo "restic $@" >> {calls}\n'
        'CMD=""; for a in "$@"; do case "$a" in snapshots|stats|forget|cat) CMD=$a; break;; esac; done\n'
        'case "$CMD" in\n'
        f"  snapshots) echo '{snapshots_out}'; echo '{snapshots_err}' >&2; exit {snapshots_rc};;\n"
        "  stats) echo '{\"total_size\":321}'; exit 0;;\n"
        "esac\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "RESTIC_REPOSITORY": "s3:x/y", "RESTIC_PASSWORD": "not-the-password",
           "MODE": "forget", "KIND": "db", "SNAPSHOT": "cafe0123", "TMP_DIR": str(tmp), "WORK_DIR": str(tmp)}
    return env, calls


def run_script(env, cwd):
    path = os.path.join(os.path.dirname(__file__), "..", "images", "backup", "picopaas-backup.sh")
    return subprocess.run(["sh", path], env=env, capture_output=True, text=True, cwd=cwd)


def test_script_forget_existing_snapshot(tmp_path):
    env, calls = _script(tmp_path, '[{"id":"cafe0123aa"}]')
    r = run_script(env, tmp_path)
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1] == 'PICOPAAS_RESULT {"forgotten":true,"repo_bytes":321}'
    assert "restic --limit-upload 0 --cleanup-cache forget cafe0123 --prune" in calls.read_text() or "forget cafe0123 --prune" in calls.read_text()
    assert "deleting the snapshot" in r.stdout


def test_script_forget_missing_snapshot_is_success(tmp_path):
    for out, rc, err in (("[]", 0, ""), ("", 1, "Fatal: no matching ID found for prefix cafe0123")):
        d = tmp_path / f"t{rc}"
        d.mkdir()
        env, calls = _script(d, out, rc, err)
        r = run_script(env, d)
        assert r.returncode == 0 and '"forgotten":true' in r.stdout.strip().splitlines()[-1]
        assert "already gone" in r.stdout and "forget cafe0123" not in calls.read_text()


def test_script_forget_check_failure_and_bad_id(tmp_path):
    env, calls = _script(tmp_path, "", 1, "Fatal: unable to open repository")
    r = run_script(env, tmp_path)
    assert r.returncode == 1 and r.stdout.strip().splitlines()[-1] == "PICOPAAS_ERROR checking the snapshot failed"
    assert "forget cafe0123" not in calls.read_text()
    d = tmp_path / "bad"
    d.mkdir()
    env, calls = _script(d, "[]")
    env["SNAPSHOT"] = "--keep-last"
    r = run_script(env, d)
    assert r.returncode == 1 and "PICOPAAS_ERROR the snapshot id is not valid" in r.stdout and "forget" not in calls.read_text()
