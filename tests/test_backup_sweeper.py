"""Backup orphan sweeper (app/backup_sweeper.py): runs stuck after a restart / lost Job, orphaned k8s objects, blobs, rows."""
import json
from datetime import timedelta

import pytest

from app import backup_sweeper, backups, config, models
from app.providers.backup import BackupJobSpec
from tests.test_backups import NOW, admin, on, other, runner, user  # noqa: F401
from tests.test_backups_limits import aid, api, mkset, rows  # noqa: F401

LATE = NOW + timedelta(hours=2)  # well past a Job deadline (30 min) after the runs below started at NOW


def running_run(db, aid, kind="db", trigger="manual", runner=None, at=NOW, with_job=True):
    st = models.BackupSet(app_id=aid, trigger=trigger, created_at=at)
    db.add(st)
    db.flush()
    r = models.BackupRun(app_id=aid, kind=kind, trigger=trigger, status="running", created_at=at, started_at=at, set_id=st.id)
    db.add(r)
    db.flush()
    r.job_name = f"bk-{r.id}"
    db.commit()
    if runner is not None and with_job:
        runner.start(BackupJobSpec(name=r.job_name, run_id=r.id, app_id=aid, kind=kind, env={"X": "not-a-secret"}))
    return r.id


def sweep(db, runner, now=LATE, **kw):
    db.expire_all()
    return backup_sweeper.sweep(db, runner, now, **kw)


def run(db, rid):
    db.expire_all()
    return db.get(models.BackupRun, rid)


def test_api_restart_job_lost_marks_failed_with_event(db_session, aid, runner):
    rid = running_run(db_session, aid, runner=runner, with_job=False)
    res = sweep(db_session, runner)
    r = run(db_session, rid)
    assert r.status == "failed" and r.error == backups.INTERRUPTED_MSG
    assert res["cleaned"]["runs_failed"] == 1
    ev = db_session.query(models.AppEvent).filter_by(app_id=aid, kind="backup_failed").one()
    assert "interrupted" in ev.message
    assert rows(db_session, "backup.orphan_cleaned")


def test_job_failed_while_api_down(db_session, aid, runner):
    rid = running_run(db_session, aid, runner=runner)
    runner.fail(f"bk-{rid}", "database dump failed")
    sweep(db_session, runner)
    r = run(db_session, rid)
    assert r.status == "failed" and r.error == "database dump failed"
    assert f"bk-{rid}" not in runner.jobs  # cleaned up


def test_job_succeeded_while_api_down(db_session, aid, runner):
    rid = running_run(db_session, aid, runner=runner)
    runner.finish(f"bk-{rid}")
    res = sweep(db_session, runner)
    r = run(db_session, rid)
    assert r.status == "succeeded" and r.snapshot_id == "ab12cd34"
    assert res["cleaned"]["runs_finished"] == 1


def test_too_young_run_is_not_touched(db_session, aid, runner):
    rid = running_run(db_session, aid, runner=runner, with_job=False)
    res = sweep(db_session, runner, now=NOW + timedelta(minutes=10))
    assert run(db_session, rid).status == "running"
    assert "runs_failed" not in res["cleaned"]


def test_hung_job_past_deadline_is_stopped(db_session, aid, runner):
    rid = running_run(db_session, aid, runner=runner)  # Job still "running"
    sweep(db_session, runner, now=NOW + timedelta(seconds=config.BACKUP_JOB_DEADLINE_SECONDS + 60))
    assert run(db_session, rid).status == "running"  # inside deadline + grace
    sweep(db_session, runner, now=LATE)
    assert run(db_session, rid).status == "failed"


def test_old_queued_run_fails(db_session, aid, runner):
    st = models.BackupSet(app_id=aid, trigger="manual", created_at=NOW)
    db_session.add(st)
    db_session.flush()
    r = models.BackupRun(app_id=aid, kind="db", trigger="manual", status="queued", created_at=NOW, set_id=st.id)
    db_session.add(r)
    db_session.commit()
    sweep(db_session, runner, now=NOW + timedelta(hours=1))
    assert run(db_session, r.id).status == "queued"
    sweep(db_session, runner, now=NOW + timedelta(seconds=config.BACKUP_QUEUED_MAX_SECONDS + 5))
    assert run(db_session, r.id).status == "failed"


def test_deleting_without_delete_run_reverts(db_session, aid, runner):
    sid = mkset(db_session, aid, 500)
    part = db_session.query(models.BackupRun).filter_by(set_id=sid).one()
    part.status = "deleting"
    st = db_session.get(models.BackupSet, sid)
    st.delete_status = "deleting"
    db_session.commit()
    res = sweep(db_session, runner, now=NOW + timedelta(hours=3))
    assert run(db_session, part.id).status == "succeeded"
    db_session.expire_all()
    assert db_session.get(models.BackupSet, sid).delete_status is None
    assert res["cleaned"]["deletes_reverted"] == 1
    assert db_session.query(models.AppEvent).filter_by(app_id=aid, kind="backup_failed").count() == 1


def test_deleting_with_live_delete_run_left_alone(db_session, aid, runner):
    sid = mkset(db_session, aid, 500)
    part = db_session.query(models.BackupRun).filter_by(set_id=sid).one()
    part.status = "deleting"
    db_session.add(models.BackupRun(app_id=aid, kind="db", trigger="delete", status="queued", created_at=NOW,
                                    source_run_id=part.id, set_id=sid))
    db_session.commit()
    sweep(db_session, runner, now=NOW + timedelta(hours=3))
    assert run(db_session, part.id).status == "deleting"


def test_orphan_job_secret_pod_deleted_but_active_and_young_kept(db_session, aid, runner):
    live = running_run(db_session, aid, runner=runner, at=LATE - timedelta(minutes=5))
    runner.start(BackupJobSpec(name="bk-9999", run_id=9999, app_id=aid, kind="db", env={"X": "not-a-secret"}))
    runner.start(BackupJobSpec(name="bk-8888", run_id=8888, app_id=aid, kind="db", env={"X": "not-a-secret"}))
    runner.job_age["bk-8888"] = 60  # just started: its row may not be committed yet
    runner.secrets["bk-7777"] = {"X": "not-a-secret"}  # Secret whose Job is gone
    runner.orphan_pods["bk-6666-abcde"] = 5000
    res = sweep(db_session, runner)
    assert set(runner.jobs) == {f"bk-{live}", "bk-8888"}
    assert "bk-7777" not in runner.secrets and f"bk-{live}" in runner.secrets
    assert not runner.orphan_pods
    assert res["cleaned"]["jobs"] == 1 and res["cleaned"]["secrets"] == 2 and res["cleaned"]["pods"] == 1


def test_tenant_namespace_is_included(db_session, aid, runner):
    running_run(db_session, aid, kind="volume", runner=runner, at=LATE - timedelta(minutes=1))
    sweep(db_session, runner)
    namespaces, min_age, _ = runner.orphan_sweeps[-1]
    assert namespaces == [config.BACKUP_NAMESPACE, f"app-{aid}"] and min_age == config.BACKUP_ORPHAN_MIN_AGE_SECONDS


def test_orphan_export_objects_and_stale_uploads(db_session, aid, runner):
    runner.swept_keys = ["exports/old-1.tar.gz", "exports/old-2.sql.gz"]
    runner.stale_uploads = [10, 90000, 200000]
    res = sweep(db_session, runner)
    assert runner.export_sweeps == [config.BACKUP_EXPORT_TTL_SECONDS]
    assert res["cleaned"]["exports"] == 2 and res["cleaned"]["uploads_aborted"] == 2
    assert runner.stale_uploads == [10]


def test_orphan_repos_reported_not_deleted(db_session, aid, runner):
    runner.repo_ids = {aid, 4242}
    res = sweep(db_session, runner)
    assert res["orphan_repos"] == [4242]
    assert not runner.deleted_exports


def test_empty_set_and_dangling_runs_and_orphan_export_rows(db_session, aid, runner):
    empty = models.BackupSet(app_id=aid, trigger="manual", created_at=NOW)
    young_empty = models.BackupSet(app_id=aid, trigger="manual", created_at=LATE - timedelta(minutes=5))
    db_session.add_all([empty, young_empty])
    db_session.add(models.BackupRun(app_id=aid, kind="db", trigger="manual", status="failed", created_at=NOW, set_id=987654))
    db_session.add(models.BackupRun(app_id=aid, kind="db", trigger="export", status="succeeded", created_at=LATE, finished_at=LATE,
                                    source_run_id=424242, export_key="exports/x.sql.gz"))
    db_session.commit()
    eid, yid = empty.id, young_empty.id
    res = sweep(db_session, runner)
    assert db_session.get(models.BackupSet, eid) is None and db_session.get(models.BackupSet, yid) is not None
    assert res["cleaned"]["sets_removed"] == 1 and res["cleaned"]["runs_relinked"] == 1 and res["cleaned"]["export_rows"] == 1
    assert runner.deleted_exports == ["exports/x.sql.gz"]


def test_idempotent_second_sweep_cleans_nothing(db_session, aid, runner):
    running_run(db_session, aid, runner=runner, with_job=False)
    runner.secrets["bk-7777"] = {"X": "not-a-secret"}
    first = sweep(db_session, runner)
    assert first["cleaned"]
    second = sweep(db_session, runner)
    assert second["cleaned"] == {} and second["errors"] == []
    assert len(rows(db_session, "backup.orphan_cleaned")) == 1


def test_limit_bounds_a_sweep(db_session, aid, runner):
    for i in range(5):
        runner.secrets[f"bk-70{i}"] = {"X": "not-a-secret"}
    res = sweep(db_session, runner, limit=2)
    assert res["cleaned"]["secrets"] == 2 and len(runner.secrets) == 3


def test_a_failing_phase_is_reported_and_others_still_run(db_session, aid, runner):
    runner.fail_abort = True
    runner.fail_repo_list = True
    runner.secrets["bk-7777"] = {"X": "not-a-secret"}
    res = sweep(db_session, runner)
    assert res["cleaned"]["secrets"] == 1
    assert len(res["errors"]) == 2


def test_health_payload_has_sweeper_field(db_session, aid, runner, admin):
    r = admin.get("/admin/health/backups")
    assert r.status_code == 200
    assert r.json()["sweeper"] == {"last_run_at": None, "cleaned": {}, "errors": [], "orphan_repos": []}
    runner.secrets["bk-7777"] = {"X": "not-a-secret"}
    sweep(db_session, runner)
    sw = admin.get("/admin/health/backups").json()["sweeper"]
    assert sw["last_run_at"] == LATE.isoformat() and sw["cleaned"] == {"secrets": 1}


def test_stored_result_fits_the_column(db_session, aid, runner):
    runner.repo_ids = set(range(5000, 5030))
    runner.fail_abort = True
    sweep(db_session, runner)
    row = db_session.get(models.PlatformSetting, backup_sweeper.STATE_KEY)
    assert len(row.value) <= 255 and json.loads(row.value)["last_run_at"]


def test_settings_listing_ignores_the_state_row(db_session, aid, runner, admin):
    sweep(db_session, runner)
    keys = [s["key"] for g in admin.get("/admin/settings").json()["groups"] for s in g["settings"]] \
        if "groups" in admin.get("/admin/settings").json() else None
    assert keys is None or backup_sweeper.STATE_KEY not in keys
