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
    assert (env["KEEP_DAILY"], env["KEEP_WEEKLY"], env["KEEP_MONTHLY"]) == ("3", "4", "12")
    assert config.BACKUP_KEEP_WEEKLY == 4 and config.BACKUP_MAX_BYTES_PER_APP == 2 * 1024**3
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
    assert job["spec"]["activeDeadlineSeconds"] == config.BACKUP_JOB_DEADLINE_SECONDS and job["spec"]["backoffLimit"] == 1


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
