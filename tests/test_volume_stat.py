"""Measured volume usage: a tiny MODE=stat Job per volume app (app/volume_stat.py), exposed as quota.volume."""
from datetime import datetime, timedelta

import pytest

from app import backups, config, models, volume_stat
from tests.test_backups import admin, on, runner, user  # noqa: F401

GI = 1024**3


@pytest.fixture()
def vol(on, monkeypatch, user, db_session):
    monkeypatch.setattr(config, "VOLUMES_ENABLED", True)
    monkeypatch.setattr(config, "BACKUP_KEY_SCOPED", True)
    monkeypatch.setattr(config, "BACKUP_DEFAULT_ENABLED", False)  # no scheduled volume backup competing for the volume
    r = user.post("/apps", json={"name": "v", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": [],
                                 "volume": {"size_gi": 1, "mount_path": "/data"}})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    a = db_session.get(models.App, aid)
    a.status, a.volume_phase = "running", "Bound"
    db_session.commit()
    return aid


def quota(user, aid):
    return user.get(f"/apps/{aid}").json()["quota"]


def test_null_until_measured_and_without_volume(user, vol, db_session):
    assert quota(user, vol)["volume"] is None
    plain = user.post("/apps", json={"name": "p", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []})
    assert plain.status_code == 201
    assert quota(user, plain.json()["id"])["volume"] is None


def test_stat_job_measures_and_is_cheap(user, vol, db_session, runner):
    now = datetime.utcnow()
    backups.tick(db_session, runner, now)
    spec = runner.started[-1]
    assert spec.name == f"vs-{vol}" and spec.env == {"MODE": "stat", "KIND": "volume"}
    assert spec.namespace == f"app-{vol}" and spec.deadline_seconds == config.VOLUME_STAT_DEADLINE_SECONDS
    assert spec.volume["mem_limit_mi"] <= 32 and spec.volume["cpu_limit_m"] <= 50
    assert quota(user, vol)["volume"] is None  # still running
    runner.finish(spec.name, {"used_bytes": 300 * 1024**2})
    backups.tick(db_session, runner, now + timedelta(seconds=15))
    assert quota(user, vol)["volume"] == {"size_bytes": 300 * 1024**2, "limit_bytes": GI, "over_quota": False}
    assert spec.name in runner.cleaned
    assert db_session.get(models.App, vol).volume_stat_job is None
    # not a backup run: nothing in the list, nothing queued, no cooldown/cap input
    assert db_session.query(models.BackupRun).count() == 0
    assert user.get(f"/apps/{vol}/backups").json()["runs"] == []


def test_throttled_to_the_interval_and_over_quota(user, vol, db_session, runner):
    now = datetime.utcnow()
    backups.tick(db_session, runner, now)
    runner.finish(f"vs-{vol}", {"used_bytes": GI})
    backups.tick(db_session, runner, now + timedelta(seconds=15))
    assert quota(user, vol)["volume"]["over_quota"] is True
    n = len(runner.started)
    backups.tick(db_session, runner, now + timedelta(minutes=14))
    assert len(runner.started) == n
    backups.tick(db_session, runner, now + timedelta(minutes=16))
    assert len(runner.started) == n + 1


def test_failed_stat_keeps_old_value_and_retries_later(user, vol, db_session, runner):
    now = datetime.utcnow()
    a = db_session.get(models.App, vol)
    a.volume_used_bytes, a.volume_used_at = 5, now
    db_session.commit()
    backups.tick(db_session, runner, now)
    runner.fail(f"vs-{vol}", "could not read the volume usage")
    backups.tick(db_session, runner, now + timedelta(seconds=15))
    assert quota(user, vol)["volume"]["size_bytes"] == 5
    assert db_session.get(models.App, vol).volume_stat_job is None


def test_hung_stat_job_is_stopped(db_session, vol, runner):
    now = datetime.utcnow()
    backups.tick(db_session, runner, now)
    backups.tick(db_session, runner, now + timedelta(seconds=config.VOLUME_STAT_DEADLINE_SECONDS + 90))
    assert db_session.get(models.App, vol).volume_stat_job is None and f"vs-{vol}" in runner.cleaned


def test_skipped_when_not_eligible(db_session, vol, runner, monkeypatch):
    a = db_session.get(models.App, vol)
    now = datetime.utcnow()
    a.volume_phase = "Pending"
    db_session.commit()
    backups.tick(db_session, runner, now)
    a.volume_phase, a.status = "Bound", "suspended"
    db_session.commit()
    backups.tick(db_session, runner, now)
    a.status = "running"
    db_session.commit()
    monkeypatch.setattr(config, "BACKUP_KEY_SCOPED", False)  # volume jobs are gated like volume backups
    backups.tick(db_session, runner, now)
    assert runner.started == []


def test_volume_backup_result_records_usage_and_active_names_protect_stat_job(db_session, vol, runner):
    now = datetime.utcnow()
    backups.tick(db_session, runner, now)
    assert f"vs-{vol}" in backups.active_jobs(db_session)  # secret sweeps must not delete a live stat Job's Secret
    assert vol in backups.tenant_app_ids(db_session, now)
    run = models.BackupRun(app_id=vol, kind="volume", trigger="manual", status="running", created_at=now, job_name="bk-99")
    db_session.add(run)
    db_session.commit()
    backups._finish(db_session, run, None, ok=True, result={"snapshot_id": "ab12cd34", "bytes_added": 1, "used_bytes": 777}, now=now)
    db_session.expire_all()
    assert db_session.get(models.App, vol).volume_used_bytes == 777


def test_record_ignores_junk():
    a = models.App()
    now = datetime.utcnow()
    for bad in (None, "5", -1, True, 1.5):
        assert volume_stat.record(a, bad, now) is False
    assert volume_stat.record(a, 0, now) is True and a.volume_used_bytes == 0


def test_stat_job_manifest_short_deadline_readonly_claim_no_serviceaccount():
    from app.providers.backup import BackupJobSpec, job_manifests
    from app.volume_stat import _tiny

    base = {"claim": "data", "uid": 10001, "fs_group": 10001, "app_pod_label": "app-1", "cpu_limit_m": 100, "mem_limit_mi": 384,
            "eph_limit_mi": 250, "cpu_request_m": 50, "mem_request_mi": 128, "eph_request_mi": 64}
    secret, job = job_manifests(BackupJobSpec(name="vs-1", run_id=0, app_id=1, kind="volume", env={"MODE": "stat"},
                                              namespace="app-1", volume=_tiny(base), deadline_seconds=120))
    pod = job["spec"]["template"]["spec"]
    assert job["spec"]["activeDeadlineSeconds"] == 120 and "serviceAccountName" not in pod
    assert pod["volumes"][-1]["persistentVolumeClaim"]["readOnly"] is True
    assert pod["containers"][0]["resources"]["limits"] == {"cpu": "50m", "memory": "32Mi", "ephemeral-storage": "16Mi"}
