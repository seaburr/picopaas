"""Coverage pass for backups: the real KubeBackupRunner against a fake dynamic client, export naming, health/overview
states, restore/export env refusals, purge/retention edges. No cluster, no network."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import backups, config, models
from app.providers import backup as pb
from app.providers.backup import BackupJobSpec, BackupRunnerError, KubeBackupRunner
from tests.test_backups import (NOW, _add_storage, _app_with_db, _ghost_like, _restore, _succeeded, admin, on, other,  # noqa: F401
                                pair, runner, user)

kexc = pytest.importorskip("kubernetes.dynamic.exceptions")


_NF = SimpleNamespace(status=404, reason="nf", data="", headers={}, body="")


class _Res:
    def __init__(self, store, kind):
        self.store, self.kind = store, kind

    def create(self, body, namespace):
        if self.store.fail_create == self.kind:
            raise RuntimeError("boom with secret-looking text")
        self.store.objs[(self.kind, body["metadata"]["name"])] = body

    def get(self, name=None, namespace=None, label_selector=None):
        if name is None:
            return {"items": [o for (k, _), o in self.store.objs.items() if k == self.kind]}
        if (self.kind, name) not in self.store.objs:
            raise kexc.NotFoundError(_NF)
        return self.store.objs[(self.kind, name)]

    def delete(self, name, namespace, body=None):
        if self.store.fail_delete == self.kind:
            raise RuntimeError("nope")
        if (self.kind, name) not in self.store.objs:
            raise kexc.NotFoundError(_NF)
        del self.store.objs[(self.kind, name)]
        self.store.deleted.append((self.kind, name, body))


class _Dyn:
    def __init__(self):
        self.objs, self.deleted, self.fail_create, self.fail_delete = {}, [], None, None
        self.resources = SimpleNamespace(get=lambda api_version, kind: _Res(self, kind))


class _Core:
    def __init__(self, log="", pods=1, boom=False):
        self.log, self.pods, self.boom = log, pods, boom

    def list_namespaced_pod(self, ns, label_selector):
        if self.boom:
            raise RuntimeError("api down")
        mk = lambda n, t: SimpleNamespace(metadata=SimpleNamespace(name=n, creation_timestamp=t))  # noqa: E731
        return SimpleNamespace(items=[mk(f"p{i}", i) for i in range(self.pods)])

    def read_namespaced_pod_log(self, name, ns, tail_lines):
        return self.log


def _kr(core=None):
    d = _Dyn()
    return KubeBackupRunner(d, core or _Core()), d


SPEC = BackupJobSpec(name="bk-7", run_id=7, app_id=3, kind="db", env={"RESTIC_PASSWORD": "not-the-password"})


def test_kube_runner_start_creates_secret_then_job_and_hides_errors():
    r, d = _kr()
    r.start(SPEC)
    assert ("Secret", "bk-7") in d.objs and ("Job", "bk-7") in d.objs
    job = d.objs[("Job", "bk-7")]
    assert job["spec"]["backoffLimit"] == 0 and job["spec"]["template"]["metadata"]["labels"][pb.LABEL] == "1"
    r2, d2 = _kr()
    d2.fail_create = "Job"  # the Secret must not outlive a Job that never started
    with pytest.raises(BackupRunnerError) as e:
        r2.start(SPEC)
    assert "RuntimeError" in str(e.value) and "secret-looking" not in str(e.value) and d2.objs == {}
    r3, d3 = _kr()
    d3.fail_create = "Secret"
    with pytest.raises(BackupRunnerError):
        r3.start(SPEC)


def _job(status):
    return {"metadata": {"name": "bk-7"}, "status": status}


def test_kube_runner_status_states():
    log = 'noise\nPICOPAAS_RESULT {"snapshot_id": "ab12"}'
    r, d = _kr(_Core(log))
    assert r.status("bk-7").state == "missing"
    d.objs[("Job", "bk-7")] = _job({})
    assert r.status("bk-7").state == "running"
    d.objs[("Job", "bk-7")] = _job({"succeeded": 1})
    st = r.status("bk-7")
    assert st.state == "succeeded" and st.result == {"snapshot_id": "ab12"}
    fail = _job({"conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]})
    d.objs[("Job", "bk-7")] = fail
    assert r.status("bk-7").error == "the backup job failed"
    r2, d2 = _kr(_Core("PICOPAAS_ERROR repository password is wrong"))
    d2.objs[("Job", "bk-7")] = fail
    assert r2.status("bk-7").error == "repository password is wrong"
    r3, d3 = _kr(_Core(""))
    d3.objs[("Job", "bk-7")] = _job({"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]})
    assert "took too long" in r3.status("bk-7").error


def test_kube_runner_pod_output_tolerates_no_pods_and_api_errors():
    r, d = _kr(_Core(pods=0))
    d.objs[("Job", "bk-7")] = _job({"succeeded": 1})
    st = r.status("bk-7")
    assert st.state == "succeeded" and st.result is None
    r2, d2 = _kr(_Core(boom=True))
    d2.objs[("Job", "bk-7")] = _job({"succeeded": 1})
    assert r2.status("bk-7").result is None
    r3, d3 = _kr(_Core("PICOPAAS_RESULT {}", pods=3))  # newest pod is read
    d3.objs[("Job", "bk-7")] = _job({"succeeded": 1})
    assert r3.status("bk-7").state == "succeeded"


def test_kube_runner_cleanup_idempotent_background_delete_and_error_reporting():
    r, d = _kr()
    r.start(SPEC)
    r.cleanup("bk-7")
    assert d.objs == {} and ("Job", "bk-7", {"propagationPolicy": "Background"}) in d.deleted
    r.cleanup("bk-7")  # already gone: fine
    r.start(SPEC)
    d.fail_delete = "Secret"
    with pytest.raises(BackupRunnerError, match="RuntimeError"):
        r.cleanup("bk-7")
    assert ("Job", "bk-7") not in d.objs  # the Job was still deleted


def test_kube_runner_sweep_removes_only_inactive_secrets():
    r, d = _kr()
    for n in ("bk-1", "bk-2", "bk-3"):
        d.objs[("Secret", n)] = {"metadata": {"name": n}}
    assert r.sweep({"bk-2"}) == 2
    assert set(d.objs) == {("Secret", "bk-2")}


def test_kube_runner_presign_and_sweep_exports(monkeypatch):
    calls = {}

    class S3:
        def generate_presigned_url(self, op, **kw):
            calls["presign"] = (op, kw)
            if kw.get("fail"):
                raise RuntimeError
            return "https://x.invalid/u"

        def get_paginator(self, name):
            old = datetime.now(timezone.utc) - timedelta(hours=3)
            new = datetime.now(timezone.utc)
            return SimpleNamespace(paginate=lambda **kw: iter([{"Contents": [
                {"Key": "exports/1/1.gz", "LastModified": old}, {"Key": "exports/1/2.gz", "LastModified": new}]}, {}]))

        def delete_objects(self, **kw):
            calls["del"] = kw

    r, _ = _kr()
    monkeypatch.setattr(r, "_s3", lambda: S3())
    monkeypatch.setattr(config, "BACKUP_BUCKET", "picopaas-backups")
    assert r.presign_export("exports/1/1.gz", 900, "a.gz") == "https://x.invalid/u"
    op, kw = calls["presign"]
    assert op == "get_object" and kw["ExpiresIn"] == 900 and kw["Params"]["Key"] == "exports/1/1.gz"
    assert 'filename="a.gz"' in kw["Params"]["ResponseContentDisposition"]
    assert r.sweep_exports(3600) == 1
    assert calls["del"]["Delete"]["Objects"] == [{"Key": "exports/1/1.gz"}]
    monkeypatch.setattr(r, "_s3", lambda: (_ for _ in ()).throw(RuntimeError("endpoint leak")))
    with pytest.raises(BackupRunnerError, match="RuntimeError") as e:
        r.presign_export("k", 1, "f")
    assert "leak" not in str(e.value)
    with pytest.raises(BackupRunnerError):
        r.sweep_exports(1)
    monkeypatch.setattr(config, "BACKUP_EXPORT_PREFIX", "backups/")  # overlaps the repos: refuse to delete anything
    with pytest.raises(BackupRunnerError, match="unsafe"):
        r.sweep_exports(1)


def test_kube_runner_s3_client_uses_backup_key_and_endpoint(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_REGION", "sfo3")
    monkeypatch.setattr(config, "BACKUP_ACCESS_KEY_ID", "backup-key-id")
    monkeypatch.setattr(config, "BACKUP_SECRET_ACCESS_KEY", "not-the-backup-secret")
    c = KubeBackupRunner(object(), object())._s3()
    assert c.meta.endpoint_url == config.backup_endpoint()


def test_ensure_spaces_egress_noop_without_private_ips(monkeypatch):
    applied = []
    r, _ = _kr()
    r._kube = SimpleNamespace(apply=applied.extend)
    monkeypatch.setattr("app.reconciler._private_storage_ips", lambda e: [])
    r.ensure_spaces_egress("https://x")
    assert applied == []


# --- export file names / states ---

def test_export_filename_is_sanitised():
    app = SimpleNamespace(name='../"evil";\r\nname/‮.sh')
    src = SimpleNamespace(kind="db", id=9)
    n = backups.export_filename(app, src, "sql.gz")
    assert n.endswith("-db-backup9.sql.gz") and not set(n) & set('"/\\;\r\n\u202e')
    assert backups.export_filename(SimpleNamespace(name="///"), src, "tar.gz").startswith("app-db-backup9")
    assert len(backups.export_filename(SimpleNamespace(name="a" * 300), src, "x")) < 100


def test_export_state_ready_then_expired():
    run = SimpleNamespace(status="succeeded", finished_at=NOW, created_at=NOW)
    assert backups.export_state(run, NOW + timedelta(minutes=59)) == "ready"
    assert backups.export_state(run, NOW + timedelta(hours=1)) == "expired"
    assert backups.export_state(SimpleNamespace(status="running", finished_at=None, created_at=NOW), NOW) == "running"


def test_helpers_int_and_naive():
    assert backups._int("12") == 12 and backups._int(-5) == 0 and backups._int(None) is None and backups._int("x") is None
    aware = datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=2)))
    assert backups._naive(aware) == datetime(2026, 1, 1, 1) and backups._naive(None) is None


# --- state / overview ---

def test_app_state_matrix():
    mk = lambda **kw: SimpleNamespace(**{"enabled": True, "schedule": "daily", "last_status": None, "db_last_status": None,  # noqa: E731
                                          "bucket_last_status": None, "vol_last_status": None, "last_success_at": None,
                                          "created_at": NOW, **kw})
    assert backups.app_state(None) == "disabled" and backups.app_state(mk(enabled=False), NOW) == "disabled"
    assert backups.app_state(mk(schedule="off"), NOW) == "disabled"
    assert backups.app_state(mk(), NOW + timedelta(hours=1)) == "pending"
    assert backups.app_state(mk(), NOW + timedelta(days=3)) == "stale"
    assert backups.app_state(mk(bucket_last_status="failed"), NOW) == "failing"
    assert backups.app_state(mk(last_status="failed"), NOW) == "failing"
    assert backups.app_state(mk(last_success_at=NOW), NOW + timedelta(hours=2)) == "ok"
    assert backups.app_state(mk(last_success_at=NOW), NOW + timedelta(days=3)) == "stale"
    assert backups.app_state(mk(schedule="weekly", last_success_at=NOW), NOW + timedelta(days=3)) == "ok"
    assert backups.repo_bytes(None) == 0


def test_overview_warnings_and_retained(on, user, db_session, monkeypatch):
    aid = _app_with_db(user, db_session)
    cfg = backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    cfg.db_repo_bytes = 900
    db_session.commit()
    monkeypatch.setattr(config, "BACKUP_MAX_BYTES_TOTAL", 1000)
    o = backups.overview(db_session, NOW)
    assert "80%" in o["warning"] and o["total_repo_bytes"] == 900 and o["counts"]["pending"] == 1
    assert backups.health_summary(db_session)["warning"] == o["warning"]
    gone = db_session.get(models.App, aid)
    gone.deleted_at = NOW
    cfg.deleted_retention_until = NOW + timedelta(days=14)
    db_session.commit()
    o = backups.overview(db_session, NOW)
    assert o["apps"] == [] and o["retained"][0]["app_id"] == aid and o["total_repo_bytes"] == 900
    monkeypatch.setattr(config, "BACKUP_BUCKET", "")
    assert "not configured" in backups.overview(db_session, NOW)["warning"]


# --- queue / build refusals ---

def _queue(db, aid, kind, trigger, **kw):
    run = models.BackupRun(app_id=aid, kind=kind, trigger=trigger, status="queued", created_at=NOW, **kw)
    db.add(run)
    db.commit()
    return run


def test_queued_run_for_deleted_app_or_missing_database_fails(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    r1 = _queue(db_session, aid, "db", "manual")
    db_session.get(models.App, aid).deleted_at = NOW
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    assert db_session.get(models.BackupRun, r1.id).error == "the app was deleted" and runner.started == []
    db_session.get(models.App, aid).deleted_at = None
    db_session.query(models.AppDatabase).delete()
    db_session.commit()
    r2 = _queue(db_session, aid, "db", "manual")
    db_session.expire_all()
    backups.tick(db_session, runner, NOW)
    assert db_session.get(models.BackupRun, r2.id).status == "failed" and runner.started == []


def test_purge_with_nothing_to_purge_marks_purged_without_a_job(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    cfg = backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    cfg.deleted_retention_until = NOW - timedelta(days=1)
    db_session.get(models.App, aid).deleted_at = NOW - timedelta(days=15)
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    assert db_session.get(models.AppBackupConfig, aid).purged_at is not None and runner.started == []


def test_failed_purge_retries_tomorrow_and_success_zeroes_bytes(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    app = db_session.get(models.App, aid)
    cfg = backups.ensure_config(db_session, app, NOW)
    cfg.repo_password_enc = cfg.repo_password_enc or backups.crypto.encrypt("not-the-repo-password")
    cfg.db_repo_bytes = 500
    cfg.deleted_retention_until = NOW - timedelta(minutes=1)
    app.deleted_at = NOW - timedelta(days=15)
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    spec = runner.started[-1]
    assert spec.env["MODE"] == "purge" and "DB_HOST" not in spec.env
    runner.fail(spec.name, "purge failed")
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.purged_at is None and cfg.deleted_retention_until == NOW + timedelta(minutes=1) + timedelta(days=1)
    assert db_session.query(models.AppEvent).filter_by(kind="backup_failed").count() == 0  # purge failures are not user events
    backups.tick(db_session, runner, NOW + timedelta(days=1, minutes=5))
    runner.finish(runner.started[-1].name)
    backups.tick(db_session, runner, NOW + timedelta(days=1, minutes=6))
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.purged_at is not None and cfg.db_repo_bytes == 0


def test_retain_is_idempotent_and_skipped_for_never_backed_up(on, user, db_session):
    aid = _app_with_db(user, db_session)
    app = db_session.get(models.App, aid)
    cfg = backups.ensure_config(db_session, app, NOW)
    backups._retain(db_session, app, cfg, NOW)
    assert cfg.deleted_retention_until is None  # nothing backed up: nothing to keep
    cfg.last_success_at = NOW
    backups._retain(db_session, app, cfg, NOW)
    first = cfg.deleted_retention_until
    assert first == NOW + timedelta(days=config.BACKUP_DELETED_RETENTION_DAYS)
    backups._retain(db_session, app, cfg, NOW + timedelta(days=5))
    assert cfg.deleted_retention_until == first
    assert db_session.query(models.AuditLog).filter_by(action="backup.retained").count() == 1


def test_restore_and_export_env_refusals(pair, admin, user, db_session, runner):
    src, dst, run_db, run_bk = pair
    # target without blob storage / database at the time the job starts
    q1 = _queue(db_session, dst, "db", "restore", source_run_id=run_db)
    q2 = _queue(db_session, dst, "bucket", "restore", source_run_id=run_bk)
    db_session.query(models.AppDatabase).filter_by(app_id=dst).delete()
    db_session.query(models.AppStorage).filter_by(app_id=dst).delete()
    db_session.commit()
    db_session.expire_all()
    backups.tick(db_session, runner, NOW)
    for q in (q1, q2):
        assert db_session.get(models.BackupRun, q.id).status == "failed"
    # source purged / snapshot gone / export key missing
    db_session.get(models.AppBackupConfig, src).purged_at = NOW
    q3 = _queue(db_session, src, "db", "export", source_run_id=run_db, export_key="exports/1/1.sql.gz")
    q4 = _queue(db_session, src, "db", "export", source_run_id=None, export_key="exports/1/2.sql.gz")
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    for q in (q3, q4):
        row = db_session.get(models.BackupRun, q.id)
        assert row.status == "failed" and "gone" in row.error


def test_restore_over_source_refused_at_api_and_at_start(pair, admin, db_session, runner):
    src, dst, run_db, _ = pair
    r = _restore(admin, run_db, src)
    assert r.status_code == 409
    assert runner.started == [] and db_session.query(models.BackupRun).filter_by(trigger="restore").count() == 0
