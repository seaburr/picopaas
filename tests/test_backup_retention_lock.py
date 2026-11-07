"""A backup whose own `forget --prune` fails (a restic lock held by another Job, or left by a killed one) queues a MODE=prune
Job, which clears stale locks first. MODE=backup never unlocks, so without the follow-up every later run fails the same way."""
from datetime import datetime, timedelta


from app import backups, models, platform_backup
from app.providers.backup import BackupJobSpec, job_manifests
from tests.test_backend_2026_10_28 import pdb  # noqa: F401
from tests.test_backups import NOW, _app_with_db, admin, on, runner, user  # noqa: F401

T0 = datetime(2026, 11, 7, 1, 0)
LOCKED = "retention cleanup failed"


def _fail_first_platform_backup(db, runner):
    backups.tick(db, runner, T0)
    runner.fail(runner.started[0].name, LOCKED)
    backups.tick(db, runner, T0 + timedelta(minutes=1))


def test_platform_retention_failure_queues_a_prune_job_with_the_policy(pdb, db_session, runner):  # noqa: F811
    _fail_first_platform_backup(db_session, runner)
    backups.tick(db_session, runner, T0 + timedelta(minutes=2))
    assert [s.env["MODE"] for s in runner.started] == ["backup", "prune"]
    e = runner.started[1].env
    assert e["KIND"] == "db" and e["RESTIC_REPOSITORY"].endswith("/platform/db")
    assert (e["KEEP_DAILY"], e["KEEP_WEEKLY"], e["KEEP_MONTHLY"]) == ("7", "4", "3") and "DB_PASSWORD" not in e
    backup_run = db_session.query(models.PlatformBackupRun).filter_by(trigger="scheduled").one()
    assert backup_run.status == "failed" and "clean-up run was queued" in backup_run.error and LOCKED in backup_run.error


def test_prune_run_is_not_a_backup_and_success_is_audited(pdb, db_session, runner):  # noqa: F811
    _fail_first_platform_backup(db_session, runner)
    backups.tick(db_session, runner, T0 + timedelta(minutes=2))
    runner.finish(runner.started[1].name, {"pruned": True, "repo_bytes": 42})
    backups.tick(db_session, runner, T0 + timedelta(minutes=3))
    prune = db_session.query(models.PlatformBackupRun).filter_by(trigger="prune").one()
    assert prune.status == "succeeded" and prune.repo_bytes == 42
    assert db_session.query(models.AuditLog).filter_by(action="platform_backup.pruned").count() == 1
    runs = platform_backup.summary(db_session, now=T0 + timedelta(minutes=4))["runs"]
    assert [r["status"] for r in runs] == ["failed"]  # the prune row is not listed as a backup
    backups.tick(db_session, runner, T0 + timedelta(hours=1, minutes=5))  # failure retries after an hour
    assert [s.env["MODE"] for s in runner.started] == ["backup", "prune", "backup"]


def test_failed_prune_is_audited_and_does_not_loop(pdb, db_session, runner):  # noqa: F811
    _fail_first_platform_backup(db_session, runner)
    backups.tick(db_session, runner, T0 + timedelta(minutes=2))
    runner.fail(runner.started[1].name, LOCKED)
    backups.tick(db_session, runner, T0 + timedelta(minutes=3))
    assert db_session.query(models.AuditLog).filter_by(action="platform_backup.prune_failed").count() == 1
    assert db_session.query(models.PlatformBackupRun).filter_by(trigger="prune").count() == 1


def test_other_platform_failures_queue_no_prune(pdb, db_session, runner):  # noqa: F811
    backups.tick(db_session, runner, T0)
    runner.fail(runner.started[0].name, "database dump failed")
    backups.tick(db_session, runner, T0 + timedelta(minutes=1))
    assert db_session.query(models.PlatformBackupRun).filter_by(trigger="prune").count() == 0


def test_app_db_retention_failure_queues_a_prune_run(on, user, db_session, runner):  # noqa: F811
    aid = _app_with_db(user, db_session)
    backups.tick(db_session, runner, NOW)
    runner.fail(runner.started[0].name, LOCKED)
    backups.tick(db_session, runner, NOW + timedelta(minutes=2))
    backups.tick(db_session, runner, NOW + timedelta(minutes=3))
    prune = db_session.query(models.BackupRun).filter_by(app_id=aid, trigger="prune").one()
    assert prune.kind == "db" and prune.status == "running"
    assert runner.started[-1].env["MODE"] == "prune"


def test_app_dump_failure_queues_no_prune(on, user, db_session, runner):  # noqa: F811
    aid = _app_with_db(user, db_session)
    backups.tick(db_session, runner, NOW)
    runner.fail(runner.started[0].name, "database dump failed")
    backups.tick(db_session, runner, NOW + timedelta(minutes=2))
    assert db_session.query(models.BackupRun).filter_by(app_id=aid, trigger="prune").count() == 0


def test_every_platform_job_has_the_writable_path_the_script_needs(pdb, db_session, runner):  # noqa: F811
    """The image sets HOME=/tmp (restic's cache) and TMP_DIR=/tmp (dump status, restic JSON); the root filesystem is read-only,
    so /tmp must be a writable emptyDir for backup and prune Jobs alike (only export also needs /work)."""
    _fail_first_platform_backup(db_session, runner)
    backups.tick(db_session, runner, T0 + timedelta(minutes=2))
    assert len(runner.started) == 2
    for spec in runner.started:
        _, job = job_manifests(spec)
        c = job["spec"]["template"]["spec"]["containers"][0]
        assert c["securityContext"]["readOnlyRootFilesystem"] is True
        assert {m["mountPath"] for m in c["volumeMounts"]} >= {"/tmp"}
    assert isinstance(runner.started[0], BackupJobSpec)


def test_in_flight_prune_blocks_a_new_backup_but_not_the_schedule_clock(pdb, db_session, runner):  # noqa: F811
    """A prune needs the repo alone: enqueue() refuses while one is active, and the prune row never counts as the last backup."""
    _fail_first_platform_backup(db_session, runner)
    backups.tick(db_session, runner, T0 + timedelta(minutes=2))  # prune now running
    assert platform_backup.enqueue(db_session, "manual", T0 + timedelta(minutes=3)) is None
    runner.finish(runner.started[1].name, {"repo_bytes": 1})
    backups.tick(db_session, runner, T0 + timedelta(minutes=4))
    assert platform_backup.enqueue(db_session, "manual", T0 + timedelta(minutes=5)) is not None
    # the failed backup, not the (newer) succeeded prune row, drives the retry-after-failure wait
    assert platform_backup._due(db_session, T0 + timedelta(minutes=30)) is False


def test_prune_that_cannot_start_fails_without_queueing_another(pdb, db_session, runner):  # noqa: F811
    backups.tick(db_session, runner, T0)
    runner.fail(runner.started[0].name, LOCKED)
    runner.fail_start = True  # the prune is started in the same tick that records the failure
    backups.tick(db_session, runner, T0 + timedelta(minutes=1))
    prune = db_session.query(models.PlatformBackupRun).filter_by(trigger="prune").one()
    assert prune.status == "failed" and "could not be started" in prune.error
    assert db_session.query(models.AuditLog).filter_by(action="platform_backup.prune_failed").count() == 1


def test_old_finished_prune_rows_are_swept_after_a_day(pdb, db_session, runner):  # noqa: F811
    _fail_first_platform_backup(db_session, runner)
    backups.tick(db_session, runner, T0 + timedelta(minutes=2))
    runner.finish(runner.started[1].name, {"repo_bytes": 1})
    backups.tick(db_session, runner, T0 + timedelta(minutes=3))
    assert db_session.query(models.PlatformBackupRun).filter_by(trigger="prune").count() == 1
    later = T0 + timedelta(days=2)
    backups.tick(db_session, runner, later)  # a new scheduled backup starts
    runner.finish(runner.started[-1].name, {"snapshot_id": "aa11", "bytes_added": 1})
    backups.tick(db_session, runner, later + timedelta(minutes=1))
    assert db_session.query(models.PlatformBackupRun).filter_by(trigger="prune").count() == 0


def test_marker_past_the_error_limit_still_queues_a_prune(pdb, db_session, runner):  # noqa: F811
    backups.tick(db_session, runner, T0)
    runner.fail(runner.started[0].name, "x" * 400 + LOCKED)
    backups.tick(db_session, runner, T0 + timedelta(minutes=1))
    assert db_session.query(models.PlatformBackupRun).filter_by(trigger="prune").count() == 1
