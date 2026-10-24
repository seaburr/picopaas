"""Backups phase 1a: flag/config, scheduling, job secret lifecycle, retention args, failure events, API authz,
final backup on delete + purge, migration. The suite never runs restic or touches the network (FakeBackupRunner)."""
import os
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from app import backups, config, crypto, models
from app.providers.backup import BackupJobSpec, job_manifests, parse_pod_output
from app.providers.fake import FakeBackupRunner, FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import reconcile_all
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
NOW = datetime(2026, 10, 24, 12, 0, 0)


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(config, "BACKUPS_ENABLED", True)
    monkeypatch.setattr(config, "BACKUP_BUCKET", "picopaas-backups")
    monkeypatch.setattr(config, "BACKUP_REGION", "sfo3")
    monkeypatch.setattr(config, "BACKUP_ACCESS_KEY_ID", "backup-key-id")
    monkeypatch.setattr(config, "BACKUP_SECRET_ACCESS_KEY", "not-the-backup-secret")
    monkeypatch.setattr(config, "BACKUP_DEFAULT_ENABLED", True)  # the original (on-by-default) behaviour; see "opt-in" tests


@pytest.fixture()
def runner():
    return FakeBackupRunner()


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def user(make_client, admin, db_session):
    c = make_client()
    register_and_login(c, "user@example.com")
    db_session.query(models.User).filter(models.User.email == "user@example.com").one().app_limit = 5
    db_session.commit()
    return c


@pytest.fixture()
def other(make_client, admin):
    c = make_client()
    register_and_login(c, "other@example.com")
    return c


def _server(db, name="primary", engine="mysql", ca=None):
    s = db.query(models.DbServer).filter(models.DbServer.name == name).first()
    if s is None:
        s = models.DbServer(name=name, engine=engine, host="db.internal", port=25060, admin_user="admin",
                            admin_password_encrypted=crypto.encrypt("not-the-password"), ca_cert=ca)
        db.add(s)
        db.commit()
    return s


def _app_with_db(client, db, name="web", server=None):
    r = client.post("/apps", json={**APP, "name": name, "want_db": True})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    srv = server or _server(db)
    db.add(models.AppDatabase(app_id=aid, db_server_id=srv.id, db_name=f"app_{aid}", db_user=f"app_{aid}",
                              password_encrypted=crypto.encrypt("not-the-db-password")))
    db.commit()
    return aid


# --- flag off = inert ---

def test_defaults_off_and_misconfigured_is_not_usable(monkeypatch):
    assert config.BACKUPS_ENABLED is False
    monkeypatch.setattr(config, "BACKUPS_ENABLED", True)
    assert backups.enabled() and not backups.usable()  # no bucket/key: reports misconfigured, never falls back to SPACES_*


def test_flag_off_is_inert(user, admin, db_session, runner):
    aid = _app_with_db(user, db_session)
    backups.tick(db_session, runner, NOW)
    assert runner.started == [] and db_session.query(models.AppBackupConfig).count() == 0
    r = user.get(f"/apps/{aid}/backups")
    assert r.status_code == 200 and r.json()["feature_enabled"] is False and r.json()["runs"] == []
    assert user.post(f"/apps/{aid}/backups").status_code == 422
    assert user.patch(f"/apps/{aid}/backups", json={"enabled": False}).status_code == 422
    assert admin.get("/admin/backups").json()["enabled"] is False
    assert admin.get("/admin/health").json()["backups"]["enabled"] is False


def test_misconfigured_post_503_and_tick_noop(monkeypatch, user, db_session, runner):
    monkeypatch.setattr(config, "BACKUPS_ENABLED", True)
    aid = _app_with_db(user, db_session)
    assert user.post(f"/apps/{aid}/backups").status_code == 503
    backups.tick(db_session, runner, NOW)
    assert runner.started == []
    assert user.get(f"/apps/{aid}/backups").json()["configured"] is False


# --- scheduling / secret lifecycle ---

def test_first_tick_creates_config_and_starts_db_job(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    backups.tick(db_session, runner, NOW)
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.enabled is True and cfg.schedule == "daily"
    assert cfg.next_backup_at > NOW and cfg.next_backup_at.hour == config.BACKUP_SCHEDULE_HOUR
    assert len(runner.started) == 1
    spec = runner.started[0]
    env = spec.env
    assert env["ENGINE"] == "mysql" and env["DB_NAME"] == f"app_{aid}" and env["DB_PASSWORD"] == "not-the-db-password"
    assert env["RESTIC_REPOSITORY"] == f"s3:https://sfo3.digitaloceanspaces.com/picopaas-backups/backups/apps/{aid}/db"
    assert env["MODE"] == "backup" and env["AWS_ACCESS_KEY_ID"] == "backup-key-id"
    # repo password: random, stored encrypted, round-trips
    assert cfg.repo_password_enc and cfg.repo_password_enc != env["RESTIC_PASSWORD"]
    assert crypto.decrypt(cfg.repo_password_enc) == env["RESTIC_PASSWORD"] and len(env["RESTIC_PASSWORD"]) >= 32
    run = db_session.query(models.BackupRun).one()
    assert run.status == "running" and run.trigger == "scheduled" and run.job_name == spec.name
    # not due again until next_backup_at; a second tick must not start another job while one runs
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    assert len(runner.started) == 1


def test_success_records_result_and_deletes_secret(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    backups.tick(db_session, runner, NOW)
    name = runner.started[0].name
    assert name in runner.secrets
    runner.finish(name, {"snapshot_id": "deadbeef", "bytes_added": 1234, "repo_bytes": 9999})
    backups.tick(db_session, runner, NOW + timedelta(minutes=2))
    run = db_session.query(models.BackupRun).one()
    assert (run.status, run.snapshot_id, run.bytes_added, run.repo_bytes) == ("succeeded", "deadbeef", 1234, 9999)
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.last_status == "succeeded" and cfg.db_last_status == "succeeded" and cfg.db_repo_bytes == 9999
    assert cfg.last_success_at is not None and cfg.db_last_bytes == 1234
    assert name not in runner.secrets and name not in runner.jobs and name in runner.cleaned
    assert db_session.query(models.AppEvent).count() == 0
    assert db_session.query(models.AuditLog).filter_by(action="backup.succeeded").count() == 1


def test_leftover_secret_is_swept(on, user, db_session, runner):
    _app_with_db(user, db_session)
    runner.secrets["bk-9999"] = {"RESTIC_PASSWORD": "x"}  # orphan from a crashed tick
    backups.tick(db_session, runner, NOW)
    assert "bk-9999" not in runner.secrets
    assert runner.started[0].name in runner.secrets  # the live run's Secret is kept


def test_failure_records_event_status_and_redacts(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    backups.tick(db_session, runner, NOW)
    runner.fail(runner.started[0].name, "dump failed at s3:https://x.example/b/p password=hunter2")
    backups.tick(db_session, runner, NOW + timedelta(minutes=2))
    run = db_session.query(models.BackupRun).one()
    assert run.status == "failed" and "hunter2" not in run.error and "https://" not in run.error
    ev = db_session.query(models.AppEvent).one()
    assert ev.kind == "backup_failed" and ev.app_id == aid and "database" in ev.message
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.last_status == "failed" and backups.app_state(cfg, NOW + timedelta(minutes=3)) == "failing"
    assert db_session.query(models.AuditLog).filter_by(action="backup.failed").count() == 1


def test_start_failure_marks_failed_without_leaking(on, user, db_session, runner):
    _app_with_db(user, db_session)
    runner.fail_start = True
    backups.tick(db_session, runner, NOW)
    run = db_session.query(models.BackupRun).one()
    assert run.status == "failed" and "could not be started" in run.error and runner.secrets == {}


def test_vanished_job_fails(on, user, db_session, runner):
    _app_with_db(user, db_session)
    backups.tick(db_session, runner, NOW)
    runner.jobs.clear()
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    assert db_session.query(models.BackupRun).one().status == "failed"


def test_concurrency_limit_and_one_dump_per_db_server(on, monkeypatch, make_client, user, db_session, runner):
    monkeypatch.setattr(config, "BACKUP_MAX_CONCURRENT", 2)
    a = _app_with_db(user, db_session, "a")
    b = _app_with_db(user, db_session, "b")  # same server as a: must wait for it
    pg = _server(db_session, "pg", "pg")
    c = _app_with_db(user, db_session, "c", server=pg)
    d = _app_with_db(user, db_session, "d", server=pg)
    backups.tick(db_session, runner, NOW)
    started = sorted(s.app_id for s in runner.started)
    assert len(started) == 2 and started == [a, c]  # b and d queued behind their servers
    assert {r.status for r in db_session.query(models.BackupRun).filter(models.BackupRun.app_id.in_([b, d]))} == {"queued"}
    for s in list(runner.started):
        runner.finish(s.name)
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    assert sorted(s.app_id for s in runner.started) == [a, b, c, d]


def test_postgres_and_ca_in_env(on, user, db_session, runner):
    pg = _server(db_session, "pg", "pg", ca="-----BEGIN CERTIFICATE-----\nnot-a-cert\n-----END CERTIFICATE-----\n")
    _app_with_db(user, db_session, server=pg)
    backups.tick(db_session, runner, NOW)
    env = runner.started[0].env
    assert env["ENGINE"] == "pg" and "not-a-cert" in env["DB_CA"] and env["DB_PORT"] == "25060"


def test_retention_args_from_config(on, monkeypatch, user, db_session, runner):
    monkeypatch.setattr(config, "BACKUP_KEEP_DAILY", 3)
    monkeypatch.setattr(config, "BACKUP_KEEP_MONTHLY", 12)
    _app_with_db(user, db_session)
    backups.tick(db_session, runner, NOW)
    env = runner.started[0].env
    assert (env["KEEP_DAILY"], env["KEEP_WEEKLY"], env["KEEP_MONTHLY"]) == ("3", "1", "12")
    assert config.BACKUP_KEEP_WEEKLY == 1 and config.BACKUP_MAX_BYTES_PER_APP == 2 * 1024**3
    assert config.BACKUP_MAX_BYTES_TOTAL == 100 * 1024**3 and config.BACKUP_PREFIX == "backups/"


def test_per_app_cap_skips_with_event(on, monkeypatch, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    cfg = backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    cfg.db_repo_bytes = config.BACKUP_MAX_BYTES_PER_APP
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    assert runner.started == []
    run = db_session.query(models.BackupRun).one()
    assert run.status == "failed" and "cap" in run.error
    assert db_session.query(models.AppEvent).one().kind == "backup_failed"


def test_platform_cap(on, monkeypatch, user, db_session, runner):
    monkeypatch.setattr(config, "BACKUP_MAX_BYTES_TOTAL", 100)
    aid = _app_with_db(user, db_session)
    other_id = _app_with_db(user, db_session, "o")
    backups.ensure_config(db_session, db_session.get(models.App, other_id), NOW).db_repo_bytes = 500
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    assert [s.app_id for s in runner.started] == [other_id] or runner.started == []  # capped for apps below
    failed = db_session.query(models.BackupRun).filter_by(app_id=aid).one()
    assert failed.status == "failed" and "full" in failed.error


def test_disabled_or_off_schedule_not_scheduled(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    backups.tick(db_session, runner, NOW)
    runner.finish(runner.started[0].name)
    backups.tick(db_session, runner, NOW)
    r = user.patch(f"/apps/{aid}/backups", json={"schedule": "off"})
    assert r.status_code == 200 and r.json()["state"] == "disabled"
    backups.tick(db_session, runner, NOW + timedelta(days=3))
    assert len(runner.started) == 1


def test_next_backup_compute():
    t = datetime(2026, 10, 24, 12, 0)
    d = backups.compute_next(1, t, "daily")
    assert d == datetime(2026, 10, 25, config.BACKUP_SCHEDULE_HOUR, 7)
    assert backups.compute_next(1, t, "weekly") == d + timedelta(days=6)
    assert backups.compute_next(1, t, "off") is None


def test_stale_state(on, user, db_session):
    aid = _app_with_db(user, db_session)
    cfg = backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    cfg.last_success_at, cfg.last_status = NOW - timedelta(hours=40), "succeeded"
    assert backups.app_state(cfg, NOW) == "stale"
    cfg.last_success_at = NOW - timedelta(hours=3)
    assert backups.app_state(cfg, NOW) == "ok"


# --- job manifests ---

def test_job_manifests_are_hardened_and_secret_holds_creds():
    spec = BackupJobSpec(name="bk-5", run_id=5, app_id=2, kind="db", env={"RESTIC_PASSWORD": "not-the-password"})
    secret, job = job_manifests(spec)
    assert secret["metadata"]["namespace"] == "picopaas-backup" and secret["stringData"]["RESTIC_PASSWORD"] == "not-the-password"
    assert "not-the-password" not in repr(spec) and "not-the-password" not in str(job)
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert pod["securityContext"]["runAsNonRoot"] is True and pod["automountServiceAccountToken"] is False
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and c["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert c["resources"]["limits"] == {"cpu": "200m", "memory": "320Mi", "ephemeral-storage": "200Mi"}
    assert c["envFrom"] == [{"secretRef": {"name": "bk-5"}}] and "env" not in c
    assert job["spec"]["activeDeadlineSeconds"] == config.BACKUP_JOB_DEADLINE_SECONDS and job["spec"]["backoffLimit"] == 0


def test_parse_pod_output():
    res, err = parse_pod_output(["noise", 'PICOPAAS_RESULT {"snapshot_id":"ab","bytes_added":3,"repo_bytes":9}'])
    assert res == {"snapshot_id": "ab", "bytes_added": 3, "repo_bytes": 9} and err is None
    assert parse_pod_output(["PICOPAAS_ERROR database dump failed"]) == (None, "database dump failed")
    assert parse_pod_output(["PICOPAAS_RESULT {not json"]) == (None, None)


# --- API ---

def test_api_list_post_and_rate_limit(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    r = user.get(f"/apps/{aid}/backups")
    assert r.status_code == 200 and r.json()["feature_enabled"] and r.json()["enabled"] and r.json()["cap_bytes"] == 2 * 1024**3
    r = user.post(f"/apps/{aid}/backups")
    assert r.status_code == 202 and r.json()[0]["status"] == "queued" and r.json()[0]["trigger"] == "manual"
    assert user.post(f"/apps/{aid}/backups").status_code == 409  # one already queued
    db_session.get(models.AppBackupConfig, aid).next_backup_at = datetime.utcnow() + timedelta(days=1)  # not due
    db_session.commit()
    backups.tick(db_session, runner, datetime.utcnow())  # starts the manual run
    assert len(runner.started) == 1
    runner.finish(runner.started[0].name)
    backups.tick(db_session, runner, datetime.utcnow())
    r = user.post(f"/apps/{aid}/backups")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    out = user.get(f"/apps/{aid}/backups").json()
    assert out["last_status"] == "succeeded" and out["kinds"][0]["kind"] == "db" and out["runs"][0]["snapshot_id"] == "ab12cd34"
    assert db_session.query(models.AuditLog).filter_by(action="backup.create").count() == 1


def test_api_no_db_app_and_volume_kind(on, user, db_session):
    r = user.post("/apps", json=APP)
    aid = r.json()["id"]
    assert user.post(f"/apps/{aid}/backups").status_code == 422
    assert user.post(f"/apps/{aid}/backups", json={"kinds": ["volume"]}).status_code == 422
    assert user.get(f"/apps/{aid}/backups").json()["enabled"] is False
    assert user.patch(f"/apps/{aid}/backups", json={"enabled": True}).status_code == 422


def test_api_authz(on, user, other, admin, db_session):
    aid = _app_with_db(user, db_session)
    assert other.get(f"/apps/{aid}/backups").status_code == 404
    assert other.post(f"/apps/{aid}/backups").status_code == 404
    assert other.patch(f"/apps/{aid}/backups", json={"enabled": False}).status_code == 404
    assert admin.get(f"/apps/{aid}/backups").status_code == 200  # admin sees any app
    assert user.get("/admin/backups").status_code == 403
    assert admin.get("/admin/backups").status_code == 200
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app
    assert TestClient(fastapi_app).get(f"/apps/{aid}/backups").status_code == 401


def test_read_scope_key_lists_but_cannot_create(on, user, make_client, db_session):
    aid = _app_with_db(user, db_session)
    key = user.post("/api/keys", json={"name": "ro", "scope": "read"})
    assert key.status_code in (200, 201), key.text
    raw = key.json().get("key") or key.json().get("raw_key")
    c = make_client()
    h = {"Authorization": f"Bearer {raw}"}
    assert c.get(f"/apps/{aid}/backups", headers=h).status_code == 200
    assert c.post(f"/apps/{aid}/backups", headers=h).status_code == 403
    assert c.patch(f"/apps/{aid}/backups", json={"enabled": False}, headers=h).status_code == 403


def test_patch_config_and_audit(on, user, db_session):
    aid = _app_with_db(user, db_session)
    r = user.patch(f"/apps/{aid}/backups", json={"schedule": "weekly"})
    assert r.status_code == 200 and r.json()["schedule"] == "weekly"
    r = user.patch(f"/apps/{aid}/backups", json={"enabled": False})
    assert r.json()["enabled"] is False and r.json()["state"] == "disabled"
    assert user.patch(f"/apps/{aid}/backups", json={"schedule": "hourly"}).status_code == 422
    assert db_session.query(models.AuditLog).filter_by(action="backup.config_changed").count() == 2


def test_admin_overview_and_health(on, admin, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    backups.tick(db_session, runner, NOW)
    runner.finish(runner.started[0].name, {"snapshot_id": "aa", "bytes_added": 1, "repo_bytes": 4096})
    backups.tick(db_session, runner, NOW)
    ov = admin.get("/admin/backups").json()
    assert ov["enabled"] and ov["configured"] and ov["total_repo_bytes"] == 4096
    assert ov["apps"][0]["app_id"] == aid and ov["apps"][0]["owner_email"] == "user@example.com"
    h = admin.get("/admin/health").json()["backups"]
    assert h["counts"]["failing"] == 0 and h["total_repo_bytes"] == 4096


# --- delete: final backup, retention, purge ---

def _sweep(db, kube, runner_on=True):
    reconcile_all(db, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=FakeObjectStore(),
                  settings={}, backups_active=runner_on)
    db.expire_all()


def test_delete_waits_for_final_backup_then_retains_and_purges(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    app = db_session.get(models.App, aid)
    backups.ensure_config(db_session, app, NOW)
    kube = FakeKubeProvider()
    assert user.delete(f"/apps/{aid}").status_code == 204
    _sweep(db_session, kube)
    app = db_session.get(models.App, aid)
    assert app.status != "deleted" and app.database is not None  # held: final backup queued
    final = db_session.query(models.BackupRun).filter_by(trigger="final").one()
    assert final.status == "queued"
    now = datetime.utcnow()
    backups.tick(db_session, runner, now)
    assert runner.started[0].env["TAG"] == "final"
    _sweep(db_session, kube)
    assert db_session.get(models.App, aid).status != "deleted"  # still running
    runner.finish(runner.started[0].name, {"snapshot_id": "ff", "bytes_added": 5, "repo_bytes": 777})
    backups.tick(db_session, runner, now)
    _sweep(db_session, kube)
    app = db_session.get(models.App, aid)
    assert app.status == "deleted"
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.deleted_retention_until is not None
    assert abs((cfg.deleted_retention_until - now).days - config.BACKUP_DELETED_RETENTION_DAYS) <= 1
    # before expiry: no purge; after: a purge job runs with MODE=purge and no DB creds, then purged_at is set
    backups.tick(db_session, runner, now + timedelta(days=13))
    assert len(runner.started) == 1
    later = now + timedelta(days=15)
    backups.tick(db_session, runner, later)
    purge = runner.started[1]
    assert purge.env["MODE"] == "purge" and "DB_PASSWORD" not in purge.env
    runner.finish(purge.name, {"purged": True})
    backups.tick(db_session, runner, later)
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.purged_at is not None and cfg.db_repo_bytes == 0
    assert db_session.query(models.AppEvent).count() == 0
    assert runner.secrets == {}


def test_delete_proceeds_when_final_backup_fails(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    user.delete(f"/apps/{aid}")
    kube = FakeKubeProvider()
    _sweep(db_session, kube)
    now = datetime.utcnow()
    backups.tick(db_session, runner, now)
    runner.fail(runner.started[0].name)
    backups.tick(db_session, runner, now)
    _sweep(db_session, kube)
    assert db_session.get(models.App, aid).status == "deleted"
    assert db_session.get(models.AppBackupConfig, aid).deleted_retention_until is None  # nothing was ever saved


def test_delete_not_blocked_forever(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    user.delete(f"/apps/{aid}")
    kube = FakeKubeProvider()
    _sweep(db_session, kube)  # queues the final run; nobody ever runs it
    run = db_session.query(models.BackupRun).filter_by(trigger="final").one()
    run.created_at = datetime.utcnow() - timedelta(hours=3)
    db_session.commit()
    _sweep(db_session, kube)
    assert db_session.get(models.App, aid).status == "deleted"
    assert db_session.query(models.BackupRun).filter_by(trigger="final").one().status == "failed"


def test_delete_without_backups_active_is_unchanged(user, db_session):
    aid = _app_with_db(user, db_session)
    user.delete(f"/apps/{aid}")
    _sweep(db_session, FakeKubeProvider(), runner_on=False)
    assert db_session.get(models.App, aid).status == "deleted"
    assert db_session.query(models.BackupRun).count() == 0


# --- misc ---

def test_repo_password_rotates_with_fernet_rotation(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    backups.tick(db_session, runner, NOW)
    cfg = db_session.get(models.AppBackupConfig, aid)
    before = crypto.decrypt(cfg.repo_password_enc)
    crypto.rotate_all(db_session)
    assert crypto.decrypt(db_session.get(models.AppBackupConfig, aid).repo_password_enc) == before


def test_rotate_all_skips_apps_without_password(on, user, db_session):
    aid = _app_with_db(user, db_session)
    backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)  # repo_password_enc still NULL
    crypto.rotate_all(db_session)


def test_migration_single_head_and_roundtrip(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path}/m.db", "ENVIRONMENT": "dev"}

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True,
                              capture_output=True, text=True).stdout

    assert len(alembic("heads").strip().splitlines()) == 1
    alembic("upgrade", "head")
    alembic("downgrade", "b6d3f1a8c2e4")
    alembic("upgrade", "head")


# --- security review additions (2026-10-23) ---

def test_delete_with_backups_turned_off_still_retains_old_repos(on, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    cfg = backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    cfg.enabled, cfg.db_repo_bytes, cfg.last_success_at = False, 1234, NOW
    db_session.commit()
    user.delete(f"/apps/{aid}")
    _sweep(db_session, FakeKubeProvider())
    assert db_session.get(models.App, aid).status == "deleted"  # no final backup requested
    assert db_session.query(models.BackupRun).filter_by(trigger="final").count() == 0
    assert db_session.get(models.AppBackupConfig, aid).deleted_retention_until is not None  # but the purge is scheduled


def test_script_removes_truncated_snapshot_when_dump_fails(tmp_path):
    """Run the real script with stub restic/mariadb-dump/jq: a dump that dies mid-stream must fail the run AND forget
    the snapshot restic sealed from the partial stream."""
    import shutil
    import stat

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "restic.log"

    def stub(name, body):
        p = bindir / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    stub("restic", f'echo "$@" >> {log}\n'
         'case "$1 $2" in *"backup --limit"*|*) :;; esac\n'
         'for a in "$@"; do [ "$a" = backup ] && { cat >/dev/null; echo \'{"message_type":"summary","snapshot_id":"abcdef1234567890","data_added":5}\'; exit 0; }; done\n'
         'exit 0\n')
    stub("mariadb-dump", 'if [ "$1" = "--help" ]; then echo; exit 0; fi\necho partial; exit 2\n')
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq not installed")
    (bindir / "jq").symlink_to(jq)
    script = os.path.join(os.path.dirname(__file__), "..", "images", "backup", "picopaas-backup.sh")
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "RESTIC_REPOSITORY": "s3:x/y", "RESTIC_PASSWORD": "not-the-password",
           "ENGINE": "mysql", "DB_HOST": "h", "DB_NAME": "d", "DB_USER": "u", "DB_PASSWORD": "not-the-db-password", "TAG": "manual"}
    # the script writes to /tmp; that is fine on a dev box
    r = subprocess.run(["sh", script], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 1 and "PICOPAAS_ERROR database dump failed" in r.stdout
    assert "not-the-db-password" not in r.stdout + r.stderr and "s3:x/y" not in r.stdout + r.stderr
    assert "forget abcdef1234567890" in log.read_text()


# --- blob storage ("bucket") kind ---

def _add_storage(db, aid, over_quota=False, size=1000):
    db.add(models.AppStorage(app_id=aid, bucket=f"bkt-{aid}", endpoint="https://atl1.digitaloceanspaces.com", region="atl1",
                             access_key=f"AK{aid}", secret_key_encrypted=crypto.encrypt(f"not-the-bucket-secret-{aid}"),
                             size_bytes=size, over_quota=over_quota))
    db.commit()


def _ghost_like(client, db, name="devlog", with_db=True, **kw):
    """An app like the devlog: database + blob storage."""
    r = client.post("/apps", json={**APP, "name": name, "want_db": with_db, "want_storage": True})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    if with_db:
        db.add(models.AppDatabase(app_id=aid, db_server_id=_server(db).id, db_name=f"app_{aid}", db_user=f"app_{aid}",
                                  password_encrypted=crypto.encrypt("not-the-db-password")))
        db.commit()
    _add_storage(db, aid, **kw)
    return aid


def test_bucket_kind_scheduled_with_db_and_secret_contents(on, user, db_session, runner):
    aid = _ghost_like(user, db_session)
    backups.tick(db_session, runner, NOW)
    by_kind = {s.kind: s for s in runner.started}
    assert set(by_kind) == {"db", "bucket"}
    env = by_kind["bucket"].env
    assert env["MODE"] == "backup" and env["KIND"] == "bucket"
    assert env["RESTIC_REPOSITORY"].endswith(f"/apps/{aid}/bucket") and env["TAG"] == "scheduled"
    assert env["SRC_BUCKET"] == f"bkt-{aid}" and env["SRC_ACCESS_KEY"] == f"AK{aid}"
    assert env["SRC_SECRET_KEY"] == f"not-the-bucket-secret-{aid}" and env["SRC_ENDPOINT"].startswith("https://")
    assert "DB_PASSWORD" not in env  # the bucket Job never gets the database credentials
    assert "SRC_SECRET_KEY" not in by_kind["db"].env
    # same repo password for both kinds, backup-bucket key stays separate from the app's key
    assert env["RESTIC_PASSWORD"] == by_kind["db"].env["RESTIC_PASSWORD"]
    assert env["AWS_ACCESS_KEY_ID"] == "backup-key-id" and env["AWS_ACCESS_KEY_ID"] != env["SRC_ACCESS_KEY"]
    assert by_kind["bucket"].scratch_mib > 0 and by_kind["db"].scratch_mib == 0
    assert db_session.query(models.BackupRun).filter_by(kind="bucket").count() == 1


def test_bucket_status_is_separate_from_db_status(on, user, db_session, runner):
    aid = _ghost_like(user, db_session)
    backups.tick(db_session, runner, NOW)
    by_kind = {s.kind: s.name for s in runner.started}
    runner.finish(by_kind["db"], {"snapshot_id": "d1", "bytes_added": 10, "repo_bytes": 100})
    runner.fail(by_kind["bucket"], "copying the bucket's objects failed")
    backups.tick(db_session, runner, NOW + timedelta(minutes=2))
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert (cfg.db_last_status, cfg.bucket_last_status) == ("succeeded", "failed")
    assert cfg.bucket_last_error == "copying the bucket's objects failed" and cfg.db_repo_bytes == 100
    assert backups.app_state(cfg, NOW + timedelta(minutes=3)) == "failing"  # one kind failing is enough
    ev = db_session.query(models.AppEvent).one()
    assert "blob storage" in ev.message
    kinds = {k["kind"]: k for k in user.get(f"/apps/{aid}/backups").json()["kinds"]}
    assert kinds["db"]["last_status"] == "succeeded" and kinds["bucket"]["last_status"] == "failed"
    # next success clears it
    runner.outcomes.clear()
    db_session.get(models.AppBackupConfig, aid).next_backup_at = NOW
    db_session.commit()
    backups.tick(db_session, runner, NOW + timedelta(days=1))
    for s in runner.started[2:]:
        runner.finish(s.name, {"snapshot_id": "x", "bytes_added": 1, "repo_bytes": 50})
    backups.tick(db_session, runner, NOW + timedelta(days=1, minutes=2))
    assert backups.app_state(db_session.get(models.AppBackupConfig, aid), NOW + timedelta(days=1, minutes=3)) == "ok"


def test_storage_only_app_is_backed_up(on, user, db_session, runner):
    aid = _ghost_like(user, db_session, "imgs", with_db=False)
    backups.tick(db_session, runner, NOW)
    assert [s.kind for s in runner.started] == ["bucket"]
    assert user.get(f"/apps/{aid}/backups").json()["enabled"] is True


def test_bucket_larger_than_scratch_is_refused(on, monkeypatch, user, db_session, runner):
    monkeypatch.setattr(config, "BACKUP_BUCKET_MAX_BYTES", 5000)
    _ghost_like(user, db_session, with_db=False, size=6000)
    backups.tick(db_session, runner, NOW)
    run = db_session.query(models.BackupRun).one()
    assert runner.started == [] and run.status == "failed" and "larger" in run.error


def test_bucket_bytes_count_toward_per_app_cap(on, user, db_session, runner):
    aid = _ghost_like(user, db_session)
    cfg = backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    cfg.bucket_repo_bytes = config.BACKUP_MAX_BYTES_PER_APP
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    assert runner.started == []
    assert {r.status for r in db_session.query(models.BackupRun)} == {"failed"}


def test_manual_bucket_backup_and_validation(on, user, db_session, runner):
    plain = _app_with_db(user, db_session, "plain")
    r = user.post(f"/apps/{plain}/backups", json={"kinds": ["bucket"]})
    assert r.status_code == 422 and "blob storage" in r.json()["detail"]
    aid = _ghost_like(user, db_session)
    r = user.post(f"/apps/{aid}/backups", json={"kinds": ["bucket"]})
    assert r.status_code == 202 and [x["kind"] for x in r.json()] == ["bucket"]


def test_bucket_job_manifest_has_scratch_and_no_creds_in_args():
    spec = BackupJobSpec(name="bk-6", run_id=6, app_id=2, kind="bucket", scratch_mib=1088,
                         env={"SRC_SECRET_KEY": "not-the-bucket-secret", "RESTIC_PASSWORD": "not-the-password"})
    secret, job = job_manifests(spec)
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert {"name": "work", "mountPath": "/work"} in c["volumeMounts"]
    assert {"name": "work", "emptyDir": {"sizeLimit": "1088Mi"}} in pod["volumes"]
    assert c["resources"]["limits"]["ephemeral-storage"] == "1288Mi" and c["securityContext"]["readOnlyRootFilesystem"] is True
    assert "not-the-bucket-secret" not in str(job) and secret["stringData"]["SRC_SECRET_KEY"] == "not-the-bucket-secret"
    assert "env" not in c and "args" not in c and "command" not in c


def test_delete_takes_final_backup_of_every_kind(on, user, db_session, runner):
    aid = _ghost_like(user, db_session)
    backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    kube = FakeKubeProvider()
    assert user.delete(f"/apps/{aid}").status_code == 204
    _sweep(db_session, kube)
    finals = db_session.query(models.BackupRun).filter_by(trigger="final").all()
    assert sorted(r.kind for r in finals) == ["bucket", "db"]
    now = datetime.utcnow()
    backups.tick(db_session, runner, now)
    assert sorted(s.kind for s in runner.started) == ["bucket", "db"]
    runner.finish(runner.started[0].name, {"snapshot_id": "f1", "bytes_added": 1, "repo_bytes": 9})
    backups.tick(db_session, runner, now)
    _sweep(db_session, kube)
    assert db_session.get(models.App, aid).status != "deleted"  # the other kind is still running
    runner.finish(runner.started[1].name, {"snapshot_id": "f2", "bytes_added": 1, "repo_bytes": 9})
    backups.tick(db_session, runner, now)
    _sweep(db_session, kube)
    assert db_session.get(models.App, aid).status == "deleted"
    assert db_session.get(models.AppBackupConfig, aid).deleted_retention_until is not None


def test_purge_includes_bucket_repo(on, user, db_session, runner):
    aid = _ghost_like(user, db_session)
    cfg = backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    cfg.bucket_repo_bytes, cfg.deleted_retention_until = 400, NOW - timedelta(days=1)
    db_session.get(models.App, aid).deleted_at = NOW - timedelta(days=15)
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    purge = [s for s in runner.started if s.env["MODE"] == "purge"]
    assert [s.kind for s in purge] == ["bucket"] and "SRC_SECRET_KEY" not in purge[0].env


# --- admin restore ---

def _succeeded(db, aid, kind="db", snap="aa11bb22", **kw):
    backups.ensure_config(db, db.get(models.App, aid), NOW)
    cfg = db.get(models.AppBackupConfig, aid)
    if cfg.repo_password_enc is None:
        cfg.repo_password_enc = crypto.encrypt("not-the-repo-password")
    run = models.BackupRun(app_id=aid, kind=kind, trigger="scheduled", status="succeeded", snapshot_id=snap,
                           created_at=NOW, finished_at=NOW, **kw)
    db.add(run)
    db.commit()
    return run.id


def _restore(client, run_id, target, **body):
    return client.post(f"/admin/backups/{run_id}/restore", json={"target_app_id": target, **body})


@pytest.fixture()
def pair(on, user, admin, db_session):
    """Source (devlog: db + bucket) and a separate target app of the same shape, each with a succeeded run."""
    src = _ghost_like(user, db_session, "devlog")
    dst = _ghost_like(user, db_session, "devlog-copy")
    ids = _succeeded(db_session, src, "db", "d0d0d0d0"), _succeeded(db_session, src, "bucket", "b0b0b0b0")
    _succeeded(db_session, dst, "db", "e0e0e0e0")
    for a in (src, dst):  # keep the nightly scheduler out of these tests
        db_session.get(models.AppBackupConfig, a).next_backup_at = NOW + timedelta(days=30)
    db_session.commit()
    return src, dst, *ids


def test_restore_requires_platform_admin(pair, user, other, admin, db_session, make_client):
    src, dst, rdb, _ = pair
    assert _restore(user, rdb, dst).status_code == 403  # even the app's owner
    assert _restore(other, rdb, dst).status_code == 403
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app
    assert TestClient(fastapi_app).post(f"/admin/backups/{rdb}/restore", json={"target_app_id": dst}).status_code == 401
    assert db_session.query(models.BackupRun).filter_by(trigger="restore").count() == 0


def test_restore_refused_for_read_scope_admin_key(pair, admin, make_client):
    src, dst, rdb, _ = pair
    key = admin.post("/api/keys", json={"name": "ro", "scope": "read"})
    raw = key.json().get("key") or key.json().get("raw_key")
    c = make_client()
    r = c.post(f"/admin/backups/{rdb}/restore", json={"target_app_id": dst}, headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 403


def test_restore_refuses_the_source_app_by_default(pair, admin, db_session):
    src, dst, rdb, rbk = pair
    for rid in (rdb, rbk):
        r = _restore(admin, rid, src)
        assert r.status_code == 409 and "overwrite" in r.json()["detail"]
    r = _restore(admin, rdb, src, overwrite_source=True)  # app is running: still refused
    assert r.status_code == 409 and "Suspend" in r.json()["detail"]
    assert db_session.query(models.BackupRun).filter_by(trigger="restore").count() == 0


def test_restore_over_suspended_source_with_flag(pair, admin, db_session, runner):
    src, dst, rdb, _ = pair
    assert admin.post(f"/admin/apps/{src}/suspend").status_code == 200
    r = _restore(admin, rdb, src, overwrite_source=True)
    assert r.status_code == 202 and r.json()["trigger"] == "restore" and r.json()["source_run_id"] == rdb
    a = db_session.query(models.AuditLog).filter_by(action="backup.restore").one()
    assert a.actor_email == "admin@example.com" and "overwrite_source" in a.detail and str(rdb) in a.detail
    backups.tick(db_session, runner, NOW)
    assert runner.started[-1].env["MODE"] == "restore"
    assert _restore(admin, rdb, src, overwrite_source=True).status_code == 409


def test_restore_overwrite_rechecked_when_job_starts(pair, admin, db_session, runner):
    src, dst, rdb, _ = pair
    admin.post(f"/admin/apps/{src}/suspend")
    assert _restore(admin, rdb, src, overwrite_source=True).status_code == 202
    db_session.get(models.App, src).status = "running"
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    assert [s for s in runner.started if s.env["MODE"] == "restore"] == []
    run = db_session.query(models.BackupRun).filter_by(trigger="restore").one()
    assert run.status == "failed" and "suspended" in run.error


def test_restore_db_job_uses_target_role_and_source_repo(pair, admin, user, db_session, runner):
    src, dst, rdb, _ = pair
    db_session.get(models.AppDatabase, db_session.query(models.AppDatabase).filter_by(app_id=dst).one().id).password_encrypted = \
        crypto.encrypt("not-the-target-db-password")
    db_session.commit()
    r = _restore(admin, rdb, dst)
    assert r.status_code == 202 and r.json()["status"] == "queued"
    run_id = r.json()["id"]
    backups.tick(db_session, runner, NOW)
    spec = [s for s in runner.started if s.run_id == run_id][0]
    env = spec.env
    assert env["MODE"] == "restore" and env["KIND"] == "db" and env["SNAPSHOT"] == "d0d0d0d0"
    assert env["RESTIC_REPOSITORY"].endswith(f"/apps/{src}/db")  # the SOURCE's repo ...
    assert crypto.decrypt(db_session.get(models.AppBackupConfig, src).repo_password_enc) == env["RESTIC_PASSWORD"]
    assert env["DB_NAME"] == f"app_{dst}" and env["DB_USER"] == f"app_{dst}"  # ... the TARGET's database role
    assert env["DB_PASSWORD"] == "not-the-target-db-password" and "SRC_SECRET_KEY" not in env
    assert spec.name in runner.secrets
    runner.finish(spec.name, {"restored": True})
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    run = db_session.get(models.BackupRun, run_id)
    assert run.status == "succeeded" and run.snapshot_id is None
    assert spec.name not in runner.secrets and spec.name not in runner.jobs
    assert db_session.query(models.AuditLog).filter_by(action="backup.restore_succeeded").count() == 1
    cfg = db_session.get(models.AppBackupConfig, dst)  # the target's own backup summary is untouched
    assert cfg is None or cfg.db_last_status is None
    assert db_session.query(models.AppEvent).count() == 0


def test_restore_bucket_job_writes_target_bucket(pair, admin, db_session, runner):
    src, dst, _, rbk = pair
    assert _restore(admin, rbk, dst).status_code == 202
    backups.tick(db_session, runner, NOW)
    env = [s for s in runner.started if s.env["MODE"] == "restore"][0].env
    assert env["KIND"] == "bucket" and env["SNAPSHOT"] == "b0b0b0b0" and env["RESTIC_REPOSITORY"].endswith(f"/apps/{src}/bucket")
    assert env["SRC_BUCKET"] == f"bkt-{dst}" and env["SRC_SECRET_KEY"] == f"not-the-bucket-secret-{dst}"
    assert f"bkt-{src}" not in str(env) and "DB_PASSWORD" not in env


def test_restore_failure_is_audited_and_cleaned(pair, admin, db_session, runner):
    src, dst, rdb, _ = pair
    run_id = _restore(admin, rdb, dst).json()["id"]
    backups.tick(db_session, runner, NOW)
    runner.fail(f"bk-{run_id}", "loading the dump into the target database failed")
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    run = db_session.get(models.BackupRun, run_id)
    assert run.status == "failed" and "loading the dump" in run.error and runner.secrets == {}
    assert db_session.query(models.AuditLog).filter_by(action="backup.restore_failed").count() == 1
    assert db_session.query(models.AppEvent).count() == 0  # admin-initiated: no event in the owner's feed


def test_restore_validation(pair, admin, user, db_session, runner):
    src, dst, rdb, rbk = pair
    assert _restore(admin, 99999, dst).status_code == 404
    assert _restore(admin, rdb, 99999).status_code == 404
    assert admin.post(f"/admin/backups/{rdb}/restore", json={}).status_code == 422
    assert admin.post(f"/admin/backups/{rdb}/restore", json={"target_app_id": dst, "x": 1}).status_code == 422
    failed = models.BackupRun(app_id=src, kind="db", trigger="manual", status="failed", created_at=NOW)
    vol = models.BackupRun(app_id=src, kind="volume", trigger="manual", status="succeeded", snapshot_id="v", created_at=NOW)
    db_session.add_all([failed, vol])
    db_session.commit()
    assert _restore(admin, failed.id, dst).status_code == 422
    assert _restore(admin, vol.id, dst).status_code == 422
    nodb = user.post("/apps", json={**APP, "name": "nodb"}).json()["id"]
    r = _restore(admin, rdb, nodb)
    assert r.status_code == 422 and "no database" in r.json()["detail"]
    r = _restore(admin, rbk, nodb)
    assert r.status_code == 422 and "no blob storage" in r.json()["detail"]
    over = _ghost_like(user, db_session, "overq", with_db=False, over_quota=True)
    assert _restore(admin, rbk, over).status_code == 422
    assert _restore(admin, rdb, dst).status_code == 202
    assert _restore(admin, rbk, dst).status_code == 409  # one backup/restore at a time per app


def test_restore_from_deleted_app_and_purged_repo(pair, admin, user, db_session):
    src, dst, rdb, _ = pair
    cfg = db_session.get(models.AppBackupConfig, src)
    db_session.get(models.App, src).deleted_at = NOW  # the source is gone (repo retained): still restorable elsewhere
    db_session.commit()
    assert _restore(admin, rdb, dst).status_code == 202
    cfg.purged_at = NOW
    db_session.commit()
    db_session.query(models.BackupRun).filter_by(trigger="restore").delete()
    db_session.commit()
    assert _restore(admin, rdb, dst).status_code == 410


def test_restore_flag_off_and_misconfigured(user, admin, db_session):
    assert _restore(admin, 1, 1).status_code == 422


# --- script level (stub restic / s5cmd / jq): bucket backup + restore ---

def _stub_env(tmp_path, bucket_files):
    import shutil
    import stat
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq not installed")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fixture = tmp_path / "remote"
    for rel, data in bucket_files.items():
        f = fixture / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(data)
    calls = tmp_path / "calls.log"

    def stub(name, body):
        p = bindir / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    (bindir / "jq").symlink_to(jq)
    # restic: `cat config` ok; backup prints a summary; restore SNAP:/path --target T drops a file tree under T
    stub("restic", f'echo "restic $@" >> {calls}\n'
         'CMD=""; for a in "$@"; do case "$a" in backup|stats|restore|forget|cat) CMD=$a; break;; esac; done\n'
         'case "$CMD" in\n'
         '  backup) echo \'{"message_type":"summary","snapshot_id":"cafe0123456789","data_added":42}\'; exit 0;;\n'
         '  stats) echo \'{"total_size":99}\'; exit 0;;\n'
         '  restore) T=""; while [ $# -gt 0 ]; do [ "$1" = --target ] && T="$2"; shift; done\n'
         f'           mkdir -p "$T/sub"; echo one > "$T/a.txt"; echo two > "$T/sub/b.txt"; exit 0;;\n'
         'esac\nexit 0\n')
    # s5cmd over a local directory standing in for the bucket (the script must pass creds only via env)
    stub("s5cmd", f'echo "s5cmd $@ KEY=$AWS_ACCESS_KEY_ID SECRET=$AWS_SECRET_ACCESS_KEY" >> {calls}\n'
         f'R={fixture}\n'
         'JSON=""; while [ "$1" != ls ] && [ "$1" != cp ] && [ "$1" != rm ]; do case "$1" in --json) JSON=1; shift;; --endpoint-url|--numworkers) shift 2;; *) shift;; esac; done\n'
         'CMD=$1; shift\n'
         'case "$CMD" in\n'
         '  ls) if [ -z "$(find $R -type f 2>/dev/null)" ]; then echo "ERROR no object found" >&2; exit 1; fi\n'
         '      (cd $R && find . -type f | sed "s|^\\./||" | while read k; do echo "{\\"key\\":\\"s3://b/$k\\",\\"size\\":3}"; done)\n'
         '      echo "{\\"key\\":\\"s3://b/folder/\\",\\"size\\":0}"; exit 0;;\n'
         '  cp) case "$1" in\n'
         '        s3://*) mkdir -p "$2"; (cd $R && find . -type f | while read k; do mkdir -p "$2/$(dirname $k)"; cp "$k" "$2/$k"; done);;\n'
         '        *) rm -rf $R; mkdir -p $R; SRC=${1%/\\*}; (cd $SRC && find . -type f | while read k; do mkdir -p "$R/$(dirname $k)"; cp "$k" "$R/$k"; done);;\n'
         '      esac; exit 0;;\n'
         '  rm) if [ -z "$(find $R -type f 2>/dev/null)" ]; then echo "ERROR no object found" >&2; exit 1; fi; rm -rf $R; mkdir -p $R; exit 0;;\n'
         'esac\n')
    work = tmp_path / "work"
    work.mkdir()
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "RESTIC_REPOSITORY": "s3:x/y", "RESTIC_PASSWORD": "not-the-password",
           "AWS_ACCESS_KEY_ID": "backup-key-id", "AWS_SECRET_ACCESS_KEY": "not-the-backup-secret",
           "KIND": "bucket", "SRC_ENDPOINT": "https://s3.example", "SRC_BUCKET": "b", "SRC_REGION": "atl1",
           "SRC_ACCESS_KEY": "app-key-id", "SRC_SECRET_KEY": "not-the-app-secret", "TAG": "scheduled",
           "WORK_DIR": str(work), "TMP_DIR": str(scratch)}
    return env, calls, fixture, work


def _run_script(env, tmp_path):
    script = os.path.join(os.path.dirname(__file__), "..", "images", "backup", "picopaas-backup.sh")
    return subprocess.run(["sh", script], env=env, capture_output=True, text=True, cwd=tmp_path)


def test_script_bucket_backup_copies_then_restics_with_creds_only_in_env(tmp_path):
    env, calls, fixture, work = _stub_env(tmp_path, {"content/images/p.png": "png", "top.txt": "top"})
    r = _run_script({**env, "MODE": "backup"}, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[-1] == 'PICOPAAS_RESULT {"snapshot_id":"cafe0123","bytes_added":42,"repo_bytes":99}'
    log = calls.read_text()
    assert f"restic --limit-upload 0 --cleanup-cache backup {work}/bucket --json --tag scheduled --host app" in log
    assert "forget --keep-daily" in log and "--group-by host,paths --prune" in log
    # the app's key only ever appears as s5cmd's environment, never in an argument; the repo key never reaches s5cmd
    for line in log.splitlines():
        if line.startswith("s5cmd"):
            args, _, envpart = line.partition(" KEY=")
            assert "app-key-id" not in args and "not-the-app-secret" not in args and "backup-key-id" not in line
            assert envpart.startswith("app-key-id SECRET=not-the-app-secret")
        else:
            assert "app-key-id" not in line and "not-the-app-secret" not in line
    assert "app-key-id" not in r.stdout + r.stderr and "not-the-app-secret" not in r.stdout + r.stderr
    assert not (work / "bucket").exists()  # scratch removed


def test_script_bucket_backup_empty_bucket(tmp_path):
    env, calls, fixture, work = _stub_env(tmp_path, {})
    r = _run_script({**env, "MODE": "backup"}, tmp_path)
    assert r.returncode == 0 and "PICOPAAS_RESULT" in r.stdout  # an empty bucket is a valid (empty) snapshot


def test_script_bucket_backup_incomplete_copy_fails_before_restic(tmp_path):
    """A copy that silently drops an object must fail the run rather than seal a partial snapshot."""
    env, calls, fixture, work = _stub_env(tmp_path, {"a.txt": "a", "b.txt": "b"})
    stub_path = os.path.join(env["PATH"].split(":")[0], "s5cmd")
    body = open(stub_path).read().replace('cp "$k" "$2/$k"; done);;', 'cp "$k" "$2/$k"; break; done);;', 1)
    assert "break" in body
    open(stub_path, "w").write(body)
    r = _run_script({**env, "MODE": "backup"}, tmp_path)
    assert r.returncode == 1 and "PICOPAAS_ERROR the copy of the bucket is incomplete" in r.stdout
    assert " backup " not in calls.read_text()


def test_script_bucket_restore_replaces_target(tmp_path):
    env, calls, fixture, work = _stub_env(tmp_path, {"stale/old.txt": "stale"})
    r = _run_script({**env, "MODE": "restore", "SNAPSHOT": "cafe0123"}, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[-1] == 'PICOPAAS_RESULT {"restored":true,"objects":2}'
    assert (fixture / "a.txt").read_text().strip() == "one" and (fixture / "sub/b.txt").read_text().strip() == "two"
    assert not (fixture / "stale/old.txt").exists()  # replaced, not merged
    log = calls.read_text()
    assert f"restore cafe0123:{work}/bucket --target {work}/restore" in log
    assert "rm s3://b/*" in log and "cp " in log and "backup" not in log
    assert "app-key-id" not in r.stdout + r.stderr and "not-the-app-secret" not in r.stdout + r.stderr


def test_script_bucket_restore_missing_snapshot_fails_closed(tmp_path):
    env, calls, fixture, work = _stub_env(tmp_path, {"keep/me.txt": "x"})
    import stat
    p = os.path.join(env["PATH"].split(":")[0], "restic")
    open(p, "w").write('#!/bin/sh\nfor a in "$@"; do [ "$a" = restore ] && exit 1; done\nexit 0\n')
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    r = _run_script({**env, "MODE": "restore", "SNAPSHOT": "nope"}, tmp_path)
    assert r.returncode == 1 and "PICOPAAS_ERROR restoring the snapshot from the repository failed" in r.stdout
    assert (fixture / "keep/me.txt").exists()  # the target bucket was not touched


# --- hung-run guards (first prod backup sat Running with no output) ---

def test_tick_keeps_spaces_egress_policy_current(on, runner, db_session):
    backups.tick(db_session, runner, NOW)
    assert runner.egress_endpoints == ["https://sfo3.digitaloceanspaces.com"]


def test_hung_job_is_failed_after_deadline_and_frees_the_server_slot(on, user, db_session, runner):
    a = _app_with_db(user, db_session, "a")
    b = _app_with_db(user, db_session, "b")  # same DB server: waits for a's dump
    backups.tick(db_session, runner, NOW)
    assert [s.app_id for s in runner.started] == [a]
    later = NOW + timedelta(seconds=config.BACKUP_JOB_DEADLINE_SECONDS + 60)
    backups.tick(db_session, runner, later)  # inside the grace window: Kubernetes may still report DeadlineExceeded
    assert db_session.query(models.BackupRun).filter_by(app_id=a).one().status == "running"
    backups.tick(db_session, runner, NOW + timedelta(seconds=config.BACKUP_JOB_DEADLINE_SECONDS + 130))
    run = db_session.query(models.BackupRun).filter_by(app_id=a).one()
    assert run.status == "failed" and "took too long" in run.error and run.job_name in runner.cleaned
    assert [s.app_id for s in runner.started] == [a, b]  # the slot is free again


def test_kube_runner_applies_egress_policy_once_per_answer(monkeypatch):
    from app.providers import backup as pb

    applied = []

    class Kube:
        def apply(self, manifests):
            applied.extend(manifests)

    monkeypatch.setattr("app.reconciler._private_storage_ips", lambda endpoint: ["10.11.0.9", "10.11.0.8"])
    monkeypatch.setattr(pb, "_egress_applied", [])
    r = pb.KubeBackupRunner(dynamic_client=object(), core_api=object())
    r._kube = Kube()
    r.ensure_spaces_egress("https://atl1.digitaloceanspaces.com")
    r.ensure_spaces_egress("https://atl1.digitaloceanspaces.com")  # unchanged: not re-applied
    assert len(applied) == 1
    pol = applied[0]
    assert pol["metadata"] == {"name": "allow-egress-spaces", "namespace": "picopaas-backup"}
    rule = pol["spec"]["egress"][0]
    assert [t["ipBlock"]["cidr"] for t in rule["to"]] == ["10.11.0.8/32", "10.11.0.9/32"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]


def test_backup_yaml_grants_networkpolicy_rights_and_documents_spaces():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "deploy", "platform", "backup.yaml")).read()
    assert "networkpolicies" in text and "allow-egress-spaces" in text


def test_script_prints_progress_and_checks_password(tmp_path):
    env, calls, fixture, work = _stub_env(tmp_path, {"a.txt": "a"})
    r = _run_script({**env, "MODE": "backup"}, tmp_path)
    lines = r.stdout.splitlines()
    steps = [x for x in lines if x.startswith("PICOPAAS_STEP ")]
    assert steps[0] == "PICOPAAS_STEP start mode=backup kind=bucket"
    assert "PICOPAAS_STEP backup repository ready" in steps and "PICOPAAS_STEP writing the snapshot" in steps
    assert lines[-1].startswith("PICOPAAS_RESULT ")
    assert "app-key-id" not in r.stdout and "not-the-app-secret" not in r.stdout and "not-the-password" not in r.stdout
    r = _run_script({**env, "MODE": "backup", "RESTIC_PASSWORD": ""}, tmp_path)
    assert r.returncode == 1 and "PICOPAAS_ERROR the repository password is missing" in r.stdout


def test_script_db_dump_has_connect_timeouts_and_step_lines():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "images", "backup", "picopaas-backup.sh")).read()
    assert "mariadb-dump --connect-timeout=20" in text and "PGCONNECT_TIMEOUT=20" in text
    assert "timeout -k 10 $QUICK_TIMEOUT restic" in text and "timeout -k 10 $LONG_TIMEOUT restic" in text
    assert "dumping the database into the repository" in text


# --- opt-in: scheduled backups are off by default ---

@pytest.fixture()
def optin(on, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DEFAULT_ENABLED", False)


def test_default_is_opt_in(monkeypatch):
    import importlib
    monkeypatch.delenv("BACKUP_DEFAULT_ENABLED", raising=False)
    assert importlib.reload(config).BACKUP_DEFAULT_ENABLED is False
    monkeypatch.setenv("BACKUP_DEFAULT_ENABLED", "1")
    assert importlib.reload(config).BACKUP_DEFAULT_ENABLED is True
    monkeypatch.delenv("BACKUP_DEFAULT_ENABLED")
    importlib.reload(config)


def test_optin_scheduler_creates_no_rows_and_runs_nothing(optin, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    backups.tick(db_session, runner, NOW)
    assert runner.started == [] and db_session.get(models.AppBackupConfig, aid) is None
    out = user.get(f"/apps/{aid}/backups").json()
    assert out["enabled"] is False and out["state"] == "disabled"


def test_optin_user_can_enable_then_it_runs(optin, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    r = user.patch(f"/apps/{aid}/backups", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    backups.tick(db_session, runner, datetime.utcnow() + timedelta(minutes=1))  # the first backup is due right away
    assert [s.env["MODE"] for s in runner.started] == ["backup"]
    assert db_session.query(models.BackupRun).one().trigger == "scheduled"


def test_optin_manual_backup_works_while_scheduled_disabled(optin, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    assert user.post(f"/apps/{aid}/backups").status_code == 202
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.enabled is False  # the manual run did not opt the app in
    assert user.post(f"/apps/{aid}/backups").status_code == 409  # active-run rule unchanged
    backups.tick(db_session, runner, NOW)
    assert len(runner.started) == 1
    runner.finish(runner.started[0].name)
    backups.tick(db_session, runner, NOW + timedelta(minutes=2))
    assert db_session.query(models.BackupRun).one().status == "succeeded"
    backups.tick(db_session, runner, NOW + timedelta(days=3))  # still no scheduled run
    assert len(runner.started) == 1
    assert user.post(f"/apps/{aid}/backups").status_code in (202, 429)  # hourly limit unchanged (429 within the hour)


def test_existing_rows_keep_their_value(optin, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    cfg = backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    cfg.enabled = True
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    assert len(runner.started) == 1


def test_optin_delete_after_manual_backup_keeps_repos_without_final_backup(optin, user, db_session, runner):
    aid = _app_with_db(user, db_session)
    user.post(f"/apps/{aid}/backups")
    backups.tick(db_session, runner, NOW)
    runner.finish(runner.started[0].name)
    backups.tick(db_session, runner, NOW)
    user.delete(f"/apps/{aid}")
    _sweep(db_session, FakeKubeProvider())
    assert db_session.get(models.App, aid).status == "deleted"
    assert db_session.query(models.BackupRun).filter_by(trigger="final").count() == 0
    assert db_session.get(models.AppBackupConfig, aid).deleted_retention_until is not None


# --- owner downloads (export) ---

@pytest.fixture()
def dl(on, user, admin, db_session, runner):
    from app.main import app as fastapi_app
    from app.routers.backups import get_backup_runner
    fastapi_app.dependency_overrides[get_backup_runner] = lambda: runner
    aid = _ghost_like(user, db_session, "devlog")
    run_db = _succeeded(db_session, aid, "db", "d0d0d0d0")
    run_bk = _succeeded(db_session, aid, "bucket", "b0b0b0b0")
    db_session.get(models.AppBackupConfig, aid).next_backup_at = NOW + timedelta(days=30)
    db_session.commit()
    return aid, run_db, run_bk


def _finish_export(db_session, runner, now=None):
    backups.tick(db_session, runner, now or datetime.utcnow())
    name = [s.name for s in runner.started][-1]
    runner.finish(name, {"exported": True, "bytes": 4242})
    backups.tick(db_session, runner, now or datetime.utcnow())


def test_download_flow_db(dl, user, db_session, runner):
    aid, run_db, _ = dl
    r = user.post(f"/apps/{aid}/backups/{run_db}/download")
    assert r.status_code == 202, r.text
    ex = r.json()
    assert ex["status"] == "queued" and ex["run_id"] == run_db and ex["url"] is None and ex["kind"] == "db"
    assert user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()["status"] == "queued"
    backups.tick(db_session, runner, datetime.utcnow())
    spec = runner.started[-1]
    env = spec.env
    assert env["MODE"] == "export" and env["KIND"] == "db" and env["SNAPSHOT"] == "d0d0d0d0"
    assert env["RESTIC_REPOSITORY"].endswith(f"/backups/apps/{aid}/db")
    assert env["EXPORT_BUCKET"] == "picopaas-backups" and env["EXPORT_KEY"] == f"exports/{aid}/{ex['export_id']}.sql.gz"
    assert "DB_PASSWORD" not in env and "SRC_SECRET_KEY" not in env  # an export never touches the app's own credentials
    assert user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()["status"] == "running"
    runner.finish(spec.name, {"exported": True, "bytes": 4242})
    backups.tick(db_session, runner, datetime.utcnow())
    got = user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()
    assert got["status"] == "ready" and got["size_bytes"] == 4242 and got["url_expires_in"] == 600
    assert got["filename"] == f"devlog-db-backup{run_db}.sql.gz" and got["url"].startswith("https://")
    assert runner.presigned[-1] == (f"exports/{aid}/{ex['export_id']}.sql.gz", 600, got["filename"])
    log = db_session.query(models.AuditLog).filter_by(action="backup.download").all()
    assert len(log) == 1 and "https://" not in log[0].detail and "sig=" not in log[0].detail


def test_download_bucket_and_scratch(dl, user, db_session, runner):
    aid, _, run_bk = dl
    ex = user.post(f"/apps/{aid}/backups/{run_bk}/download").json()
    backups.tick(db_session, runner, datetime.utcnow())
    spec = runner.started[-1]
    assert spec.env["KIND"] == "bucket" and spec.env["EXPORT_KEY"].endswith(".tar.gz")
    assert spec.scratch_mib == 2 * (config.BACKUP_BUCKET_MAX_BYTES // 1024**2) + 64
    secret, job = job_manifests(spec)
    assert job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == config.BACKUP_JOB_DEADLINE_SECONDS


def test_download_postgres_extension(dl, user, db_session, runner):
    aid, run_db, _ = dl
    db_session.get(models.App, aid).database.server.engine = "pg"
    db_session.commit()
    ex = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    assert user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").status_code == 200
    assert db_session.get(models.BackupRun, ex["export_id"]).export_key.endswith(".pgdump")


def test_download_does_not_block_backups_or_show_in_runs(dl, user, db_session, runner):
    aid, run_db, _ = dl
    user.post(f"/apps/{aid}/backups/{run_db}/download")
    backups.tick(db_session, runner, datetime.utcnow())
    assert user.post(f"/apps/{aid}/backups").status_code == 202  # export is running; manual backup still allowed
    runs = user.get(f"/apps/{aid}/backups").json()["runs"]
    assert all(r["trigger"] != "export" for r in runs)
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.last_status is None  # an export never touches the backup summary


def test_download_idempotent_one_active_and_rate_limit(dl, user, db_session, runner):
    aid, run_db, run_bk = dl
    a = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    assert user.post(f"/apps/{aid}/backups/{run_db}/download").json()["export_id"] == a["export_id"]  # same snapshot: reuse
    assert user.post(f"/apps/{aid}/backups/{run_bk}/download").status_code == 409  # one active export per app
    assert db_session.query(models.AuditLog).filter_by(action="backup.download").count() == 1
    now = datetime.utcnow()
    for _ in range(2):  # finish it, then make two more to reach 3 in the hour
        _finish_export(db_session, runner, now)
        row = db_session.query(models.BackupRun).filter_by(trigger="export").order_by(models.BackupRun.id.desc()).first()
        row.finished_at = now - timedelta(hours=2)  # no longer a live export, still counted by created_at
        db_session.commit()
        r = user.post(f"/apps/{aid}/backups/{run_db}/download")
        assert r.status_code == 202
    _finish_export(db_session, runner, now)
    db_session.query(models.BackupRun).filter_by(trigger="export").update({"finished_at": now - timedelta(hours=2)})
    db_session.commit()
    r = user.post(f"/apps/{aid}/backups/{run_db}/download")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    # an hour later it is allowed again
    for e in db_session.query(models.BackupRun).filter_by(trigger="export"):
        e.created_at = now - timedelta(hours=2)
    db_session.commit()
    assert user.post(f"/apps/{aid}/backups/{run_db}/download").status_code == 202


def test_download_expires_and_failure_reports_error(dl, user, db_session, runner):
    aid, run_db, _ = dl
    ex = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    _finish_export(db_session, runner)
    row = db_session.get(models.BackupRun, ex["export_id"])
    row.finished_at = datetime.utcnow() - timedelta(seconds=config.BACKUP_EXPORT_TTL_SECONDS + 5)
    db_session.commit()
    got = user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()
    assert got["status"] == "expired" and got["url"] is None
    row.status, row.error, row.created_at = "failed", "uploading the file failed", datetime.utcnow()
    db_session.commit()
    got = user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()
    assert got["status"] == "failed" and got["error"] == "uploading the file failed"


def test_download_failure_cleans_up_and_audits_without_event(dl, user, db_session, runner):
    aid, run_db, _ = dl
    ex = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    backups.tick(db_session, runner, datetime.utcnow())
    runner.fail(runner.started[-1].name, "uploading the file failed")
    backups.tick(db_session, runner, datetime.utcnow())
    assert runner.secrets == {} and runner.cleaned
    assert user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()["status"] == "failed"
    assert db_session.query(models.AuditLog).filter_by(action="backup.export_failed").count() == 1
    assert db_session.query(models.AppEvent).filter_by(kind="backup_failed").count() == 0


def test_download_authz_and_validation(dl, user, other, admin, db_session, runner):
    aid, run_db, _ = dl
    assert other.post(f"/apps/{aid}/backups/{run_db}/download").status_code == 404
    ex = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    assert other.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").status_code == 404
    assert admin.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").status_code == 200  # admins can fetch
    assert user.post(f"/apps/{aid}/backups/9999/download").status_code == 404
    assert user.get(f"/apps/{aid}/backups/exports/{run_db}").status_code == 404  # not an export row
    # a run of another app is not reachable through this app's path
    other_app = _app_with_db(user, db_session, "other-app")
    foreign = _succeeded(db_session, other_app, "db", "f0f0f0f0")
    assert user.post(f"/apps/{aid}/backups/{foreign}/download").status_code == 404
    failed = models.BackupRun(app_id=aid, kind="db", trigger="manual", status="failed", created_at=NOW)
    db_session.add(failed)
    db_session.commit()
    assert user.post(f"/apps/{aid}/backups/{failed.id}/download").status_code == 422


def test_download_admin_can_start_and_purged_repo_is_gone(dl, user, admin, db_session, runner):
    aid, run_db, _ = dl
    assert admin.post(f"/apps/{aid}/backups/{run_db}/download").status_code == 202
    db_session.query(models.BackupRun).filter_by(trigger="export").delete()
    db_session.get(models.AppBackupConfig, aid).purged_at = NOW
    db_session.commit()
    assert user.post(f"/apps/{aid}/backups/{run_db}/download").status_code == 410


def test_download_flag_off_and_presign_failure(dl, user, db_session, runner, monkeypatch):
    aid, run_db, _ = dl
    ex = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    _finish_export(db_session, runner)
    runner.fail_presign = True
    assert user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").status_code == 503
    monkeypatch.setattr(config, "BACKUPS_ENABLED", False)
    assert user.post(f"/apps/{aid}/backups/{run_db}/download").status_code == 422


def test_tick_sweeps_export_objects_after_ttl(on, runner, db_session):
    from app.backups import _last_export_sweep
    _last_export_sweep.clear()
    backups.tick(db_session, runner, NOW)
    assert runner.export_sweeps == [config.BACKUP_EXPORT_TTL_SECONDS] == [3600]
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    assert len(runner.export_sweeps) == 1  # throttled
    backups.tick(db_session, runner, NOW + timedelta(minutes=6))
    assert len(runner.export_sweeps) == 2


def test_migration_adds_export_key():
    from alembic.script import ScriptDirectory
    from alembic.config import Config
    sd = ScriptDirectory.from_config(Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini")))
    assert sd.get_heads() == ["e5b1c7d9a2f4"] or len(sd.get_heads()) == 1
    assert sd.get_revision("e5b1c7d9a2f4").down_revision == "d4a9e1b7c3f2"


def _export_stubs(tmp_path):
    import stat
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    up = tmp_path / "uploaded"
    up.mkdir()

    def stub(name, body):
        p = bindir / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    stub("restic", f'echo "restic $@" >> {calls}\n'
         'CMD=""; for a in "$@"; do case "$a" in dump|restore|cat) CMD=$a; break;; esac; done\n'
         'case "$CMD" in\n'
         '  dump) case "$*" in *db.sql*) echo "CREATE TABLE t (id int);";; *db.pgdump*) printf PGDMP;; esac; exit 0;;\n'
         '  restore) T=""; while [ $# -gt 0 ]; do [ "$1" = --target ] && T="$2"; shift; done; mkdir -p "$T/sub"; echo one > "$T/a.txt"; echo two > "$T/sub/b.txt"; exit 0;;\n'
         'esac\nexit 0\n')
    stub("s5cmd", f'echo "s5cmd $@ KEY=$AWS_ACCESS_KEY_ID" >> {calls}\n'
         'while [ "$1" != cp ]; do shift; done; shift\n'
         f'cp "$1" {up}/out.bin; exit 0\n')
    work, scratch = tmp_path / "work", tmp_path / "tmp"
    work.mkdir()
    scratch.mkdir()
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "RESTIC_REPOSITORY": "s3:x/y", "RESTIC_PASSWORD": "not-the-password",
           "AWS_ACCESS_KEY_ID": "backup-key-id", "AWS_SECRET_ACCESS_KEY": "not-the-backup-secret", "MODE": "export",
           "SNAPSHOT": "cafe0123", "EXPORT_ENDPOINT": "https://s3.example", "EXPORT_BUCKET": "bk",
           "WORK_DIR": str(work), "TMP_DIR": str(scratch)}
    return env, calls, up


def test_script_export_db_gzips_and_uploads(tmp_path):
    import gzip
    env, calls, up = _export_stubs(tmp_path)
    r = _run_script({**env, "KIND": "db", "EXPORT_KEY": "exports/1/2.sql.gz"}, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    last = r.stdout.strip().splitlines()[-1]
    assert last.startswith('PICOPAAS_RESULT {"exported":true,"bytes":')
    assert gzip.decompress((up / "out.bin").read_bytes()).decode().startswith("CREATE TABLE")
    log = calls.read_text()
    assert "dump cafe0123 db.sql" in log and "cp " in log and "s3://bk/exports/1/2.sql.gz" in log
    assert "init" not in log and "not-the-backup-secret" not in r.stdout + r.stderr  # never creates a repo
    assert "PICOPAAS_STEP uploading the file" in r.stdout


def test_script_export_pg_and_bucket(tmp_path):
    import tarfile
    env, calls, up = _export_stubs(tmp_path)
    r = _run_script({**env, "KIND": "db", "EXPORT_KEY": "exports/1/3.pgdump"}, tmp_path)
    assert r.returncode == 0 and (up / "out.bin").read_bytes() == b"PGDMP"
    r = _run_script({**env, "KIND": "bucket", "EXPORT_KEY": "exports/1/4.tar.gz"}, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    with tarfile.open(up / "out.bin") as t:
        names = sorted(n.lstrip("./") for n in t.getnames())
    assert "a.txt" in names and "sub/b.txt" in names
    assert f"restore cafe0123:{tmp_path}/work/bucket --target {tmp_path}/work/restore" in calls.read_text()
    assert not (tmp_path / "work" / "restore").exists()


def test_script_export_wrong_engine_and_missing_repo_fail_closed(tmp_path):
    env, calls, up = _export_stubs(tmp_path)
    # restic dump of a name that is not in the snapshot prints nothing
    (tmp_path / "bin" / "restic").write_text("#!/bin/sh\nexit 0\n")
    r = _run_script({**env, "KIND": "db", "EXPORT_KEY": "exports/1/2.sql.gz"}, tmp_path)
    assert r.returncode == 1 and "PICOPAAS_ERROR the snapshot is not a MySQL dump" in r.stdout
    assert not (up / "out.bin").exists()
    (tmp_path / "bin" / "restic").write_text("#!/bin/sh\n[ \"$1\" = cat ] && exit 1\nexit 0\n")
    r = _run_script({**env, "KIND": "db", "EXPORT_KEY": "exports/1/2.sql.gz"}, tmp_path)
    assert r.returncode == 1 and "PICOPAAS_ERROR the backup repository could not be opened" in r.stdout
