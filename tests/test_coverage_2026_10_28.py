"""Coverage for the 2026-10-28 round: platform DB backup edge paths, project helpers and invite edges."""
from datetime import datetime, timedelta

import pytest

from app import backups, config, models, platform_backup, projects
from app.providers.backup import JobStatus
from app.providers.fake import FakeBackupRunner
from tests.conftest import register_and_login
from tests.test_backend_2026_10_28 import pdb  # noqa: F401
from tests.test_backups import on  # noqa: F401

T0 = datetime(2026, 10, 28, 1, 0)


def _runs(db):
    db.expire_all()
    return db.query(models.PlatformBackupRun).order_by(models.PlatformBackupRun.id).all()


def test_repo_url_prefix_and_password_unreadable(pdb, db_session, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_PREFIX", "/pre/")
    assert platform_backup.repo_url().endswith("/pre/platform/db")
    monkeypatch.setattr(config, "BACKUP_PREFIX", "")
    assert platform_backup.repo_url().endswith("/platform/db") and "//platform" not in platform_backup.repo_url()
    platform_backup._repo_password(db_session)
    db_session.get(models.PlatformSetting, platform_backup.PASSWORD_KEY).value = "garbage"
    db_session.commit()
    with pytest.raises(ValueError):
        platform_backup._repo_password(db_session)


def test_db_env_ca_and_incomplete(pdb, tmp_path, monkeypatch):
    ca = tmp_path / "ca.pem"
    ca.write_text("not-a-real-cert")
    monkeypatch.setattr(config, "DATABASE_URL", f"mysql+pymysql://u:not-the-db-pass@h:3306/d?ssl_ca={ca}")
    assert platform_backup._db_env()["DB_CA"] == "not-a-real-cert"
    monkeypatch.setattr(config, "DATABASE_URL", f"mysql+pymysql://u:not-the-db-pass@h:3306/d?ssl_ca={tmp_path}/missing")
    with pytest.raises(ValueError, match="CA"):
        platform_backup._db_env()
    monkeypatch.setattr(config, "DATABASE_URL", "mysql+pymysql://h/")
    with pytest.raises(ValueError, match="incomplete"):
        platform_backup._db_env()


def test_start_failures_finish_run(pdb, db_session, monkeypatch):
    runner = FakeBackupRunner()
    runner.fail_start = True
    backups.tick(db_session, runner, T0)
    (r,) = _runs(db_session)
    assert r.status == "failed" and "could not be started" in r.error
    # incomplete DATABASE_URL -> build_env ValueError path
    monkeypatch.setattr(config, "DATABASE_URL", "mysql+pymysql://h/")
    runner.fail_start = False
    backups.tick(db_session, runner, T0 + timedelta(hours=2))
    assert _runs(db_session)[-1].status == "failed" and "incomplete" in _runs(db_session)[-1].error


def test_poll_states_missing_timeout_and_status_error(pdb, db_session):
    runner = FakeBackupRunner()
    backups.tick(db_session, runner, T0)
    name = runner.started[0].name
    runner.outcomes[name] = JobStatus("running")
    backups.tick(db_session, runner, T0 + timedelta(minutes=1))
    assert _runs(db_session)[0].status == "running"
    # status raising is skipped
    orig = runner.status
    runner.status = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    backups.tick(db_session, runner, T0 + timedelta(minutes=2))
    assert _runs(db_session)[0].status == "running"
    runner.status = orig
    # still running past the deadline -> stopped
    late = T0 + timedelta(seconds=config.BACKUP_JOB_DEADLINE_SECONDS + 600)
    backups.tick(db_session, runner, late)
    r = _runs(db_session)[0]
    assert r.status == "failed" and "too long" in r.error
    # missing job -> interrupted
    backups.tick(db_session, runner, late + timedelta(hours=2))
    name2 = runner.started[-1].name
    runner.outcomes[name2] = JobStatus("missing")
    backups.tick(db_session, runner, late + timedelta(hours=2, minutes=1))
    assert _runs(db_session)[-1].error == backups.INTERRUPTED_MSG


def test_finish_cleanup_error_and_run_pruning(pdb, db_session, monkeypatch):
    runner = FakeBackupRunner()
    monkeypatch.setattr(platform_backup, "KEPT_RUNS", 2)
    runner.cleanup = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    now = T0
    for _ in range(4):
        backups.tick(db_session, runner, now)
        runner.finish(runner.started[-1].name)
        now += timedelta(minutes=1)
        backups.tick(db_session, runner, now)
        now += timedelta(hours=30)
    assert len(_runs(db_session)) <= 3


def test_tick_never_raises_and_enqueue_dedup(pdb, db_session, monkeypatch):
    monkeypatch.setattr(platform_backup, "_poll", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    platform_backup.tick(db_session, FakeBackupRunner(), T0)
    assert platform_backup.enqueue(db_session, "manual", T0) is not None
    assert platform_backup.enqueue(db_session, "manual", T0) is None
    assert platform_backup.active_job_names(db_session) == set()


def test_summary_stale_when_old_success(pdb, db_session):
    db_session.add(models.PlatformBackupRun(trigger="scheduled", status="succeeded", created_at=T0 - timedelta(days=30),
                                            finished_at=T0 - timedelta(days=30), repo_bytes=1))
    db_session.commit()
    s = platform_backup.summary(db_session, now=T0)
    assert s["stale"] is True and s["last_success_at"]


# --- projects helpers / routes ---

def test_get_personal_project_recreates_missing(make_client, db_session):
    c = make_client()
    register_and_login(c, "solo@example.com")
    u = db_session.query(models.User).filter_by(email="solo@example.com").one()
    p = db_session.query(models.Project).filter_by(slug=projects.personal_slug(u.id)).one()
    db_session.query(models.ProjectMember).filter_by(project_id=p.id).delete()
    db_session.delete(p)
    db_session.commit()
    again = projects.get_personal_project(db_session, u)
    assert again.is_personal and again.slug == projects.personal_slug(u.id)


def test_owner_emails_helpers(make_client, db_session):
    c = make_client()
    register_and_login(c, "own@example.com")
    pid = c.post("/projects", json={"name": "Crew"}).json()["id"]
    assert projects.owner_emails_map(db_session, [None]) == {}
    assert projects.owner_emails_map(db_session, [pid]) == {pid: ["own@example.com"]}
    app = models.App(name="x", image="i", port=1, owner_id=1)
    assert projects.owners_label(db_session, app, "fallback") == "fallback"
    app.project_id = pid
    assert projects.owners_label(db_session, app) == "own@example.com"


def test_invite_blank_email_and_revoke_missing(make_client):
    c = make_client()
    register_and_login(c, "own@example.com")
    pid = c.post("/projects", json={"name": "Crew"}).json()["id"]
    r = c.post(f"/projects/{pid}/invites", json={"email": "   ", "role": "member"})
    assert r.status_code == 201
    assert c.delete(f"/projects/{pid}/invites/99999").status_code == 404
    iid = c.get(f"/projects/{pid}/invites").json()[0]["id"]
    assert c.delete(f"/projects/{pid}/invites/{iid}").status_code in (200, 204)
    assert c.delete(f"/projects/{pid}/invites/{iid}").status_code == 404  # already revoked
