"""Backup enable/disable (owner + admin lock), storage ceilings, max backups held, rolloff order, rate limits."""
from datetime import datetime, timedelta

import pytest

from app import backups, config, crypto, models
from tests.test_backups import (NOW, _finish_export, _ghost_like, _succeeded, admin, dl, on, other, runner, user)  # noqa: F401


def rows(db, action):
    db.expire_all()
    return db.query(models.AuditLog).filter_by(action=action).order_by(models.AuditLog.id).all()


def mkset(db, aid, age_min, status="succeeded", kind="db", snap=None, trigger="manual"):
    """A backup set `age_min` minutes before NOW with one part. status: succeeded | failed."""
    at = NOW - timedelta(minutes=age_min)
    st = models.BackupSet(app_id=aid, trigger=trigger, created_at=at)
    db.add(st)
    db.flush()
    db.add(models.BackupRun(app_id=aid, kind=kind, trigger=trigger, status=status, created_at=at, finished_at=at, set_id=st.id,
                            snapshot_id=(snap or f"s{st.id:07d}") if status == "succeeded" else None,
                            error=None if status == "succeeded" else "boom"))
    db.commit()
    return st.id


def setting(admin, **kv):
    r = admin.put("/admin/settings", json={"settings": kv})
    assert r.status_code == 200, r.text


def payload(c, aid):
    return c.get(f"/apps/{aid}/backups").json()


@pytest.fixture()
def api(runner):
    from app.main import app as fastapi_app
    from app.routers.backups import get_backup_runner
    fastapi_app.dependency_overrides[get_backup_runner] = lambda: runner
    yield
    fastapi_app.dependency_overrides.pop(get_backup_runner, None)


@pytest.fixture()
def aid(on, api, user, db_session):
    a = _ghost_like(user, db_session, "devlog", with_db=True)
    backups.ensure_config(db_session, db_session.get(models.App, a), NOW)
    cfg = db_session.get(models.AppBackupConfig, a)
    cfg.next_backup_at = NOW + timedelta(days=30)
    cfg.repo_password_enc = crypto.encrypt("not-the-repo-password")
    db_session.commit()
    return a


# --- rolloff order ---------------------------------------------------------------------------

def test_rolloff_order_failed_first_then_oldest_never_newest_success(db_session, aid):
    old_ok = mkset(db_session, aid, 500)
    new_fail = mkset(db_session, aid, 5, "failed")
    mid_ok = mkset(db_session, aid, 100)
    old_fail = mkset(db_session, aid, 300, "failed")
    newest_ok = mkset(db_session, aid, 10)
    order = [s.id for s in backups.rolloff_candidates(db_session, aid)]
    # failed ones first (oldest first) even though the successful ones are older; the newest success is not a candidate
    assert order == [old_fail, new_fail, old_ok, mid_ok]
    assert newest_ok not in order


def test_rolloff_order_partial_counts_as_failed_and_single_success_is_kept(db_session, aid):
    only = mkset(db_session, aid, 50)
    assert backups.rolloff_candidates(db_session, aid) == []
    part = mkset(db_session, aid, 10)
    db_session.add(models.BackupRun(app_id=aid, kind="bucket", trigger="manual", status="failed", created_at=NOW, set_id=part, error="x"))
    db_session.commit()
    assert [s.id for s in backups.rolloff_candidates(db_session, aid)] == [part]
    assert only


# --- max held --------------------------------------------------------------------------------

def test_max_held_deletes_failed_first_and_frees_storage(admin, db_session, aid, runner):
    setting(admin, backup_max_held_per_app=3)
    ids = [mkset(db_session, aid, 400), mkset(db_session, aid, 300), mkset(db_session, aid, 200)]
    bad = mkset(db_session, aid, 100, "failed")
    ids.append(mkset(db_session, aid, 50))
    backups.tick(db_session, runner, NOW)  # 5 held, max 3: the failed one vanishes, then one forget Job for the oldest
    db_session.expire_all()
    assert db_session.get(models.BackupSet, bad).delete_status == "deleted"
    forget = [s for s in runner.started if s.env["MODE"] == "forget"]
    assert len(forget) == 1 and forget[0].env["SNAPSHOT"] == db_session.query(models.BackupRun).filter_by(set_id=ids[0], kind="db", trigger="manual").one().snapshot_id
    assert db_session.get(models.BackupSet, ids[0]).delete_status == "deleting"
    assert rows(db_session, "backup.rolled_off")


def test_manual_backups_count_and_newest_success_survives(admin, db_session, aid, runner):
    setting(admin, backup_max_held_per_app=1)
    a = mkset(db_session, aid, 100, trigger="manual")
    b = mkset(db_session, aid, 10, trigger="manual")
    backups.tick(db_session, runner, NOW)
    db_session.expire_all()
    assert db_session.get(models.BackupSet, a).delete_status == "deleting"
    assert db_session.get(models.BackupSet, b).delete_status is None


def test_busy_app_is_left_alone(admin, db_session, aid, runner):
    setting(admin, backup_max_held_per_app=1)
    mkset(db_session, aid, 100)
    mkset(db_session, aid, 10)
    db_session.add(models.BackupRun(app_id=aid, kind="db", trigger="manual", status="running", created_at=NOW))
    db_session.commit()
    backups._enforce_retention(db_session, runner, NOW)
    assert not [s for s in runner.started if s.env["MODE"] == "forget"]


# --- ceilings --------------------------------------------------------------------------------

def test_over_app_ceiling_rolls_off_on_tick(admin, db_session, aid, runner, monkeypatch):
    old = mkset(db_session, aid, 100)
    new = mkset(db_session, aid, 10)
    db_session.get(models.AppBackupConfig, aid).db_repo_bytes = config.BACKUP_MAX_BYTES_PER_APP + 1
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    db_session.expire_all()
    assert db_session.get(models.BackupSet, old).delete_status == "deleting"
    assert db_session.get(models.BackupSet, new).delete_status is None


def test_manual_backup_refused_when_full_and_rolls_off(user, admin, db_session, aid, runner):
    old = mkset(db_session, aid, 100)
    mkset(db_session, aid, 70)
    cfg = db_session.get(models.AppBackupConfig, aid)
    cfg.db_repo_bytes = 3 * 1024**2
    cfg.max_bytes_override = 2 * 1024**2
    db_session.commit()
    p = payload(user, aid)
    assert p["storage_full"] is True and p["cap_bytes"] == 2 * 1024**2 and p["cap_override"] is True
    r = user.post(f"/apps/{aid}/backups")
    assert r.status_code == 409 and "Backup storage for this app is full (3 MB of 2 MB)" in r.json()["detail"]
    db_session.expire_all()
    assert db_session.get(models.BackupSet, old).delete_status == "deleting"


def test_manual_backup_refused_when_only_backup_and_full(user, db_session, aid):
    mkset(db_session, aid, 100)
    cfg = db_session.get(models.AppBackupConfig, aid)
    cfg.db_repo_bytes = config.BACKUP_MAX_BYTES_PER_APP
    db_session.commit()
    r = user.post(f"/apps/{aid}/backups")
    assert r.status_code == 409 and "is full" in r.json()["detail"] and "Delete a backup" in r.json()["detail"]


def test_scheduled_skipped_with_recorded_reason_when_full(db_session, aid, runner):
    cfg = db_session.get(models.AppBackupConfig, aid)
    cfg.next_backup_at = NOW - timedelta(minutes=1)
    cfg.db_repo_bytes = config.BACKUP_MAX_BYTES_PER_APP
    db_session.commit()
    mkset(db_session, aid, 100)
    backups.tick(db_session, runner, NOW)
    skipped = db_session.query(models.BackupRun).filter(models.BackupRun.status == "failed", models.BackupRun.trigger == "scheduled").all()
    assert skipped and all("Backup storage for this app is full" in r.error for r in skipped)
    assert not [s for s in runner.started if s.env["MODE"] == "backup"]
    assert db_session.get(models.AppBackupConfig, aid).next_backup_at > NOW


def test_global_ceiling_skips_scheduled_and_surfaces_in_health(admin, db_session, aid, runner):
    setting(admin, backup_max_bytes_total=1024**2, backup_max_bytes_per_app=1024**2)
    other_id = _ghost_like(admin, db_session, "big")
    backups.ensure_config(db_session, db_session.get(models.App, other_id), NOW).db_repo_bytes = 5 * 1024**2
    cfg = db_session.get(models.AppBackupConfig, aid)
    cfg.next_backup_at = NOW - timedelta(minutes=1)
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    run = db_session.query(models.BackupRun).filter_by(app_id=aid, trigger="scheduled").first()
    assert run.status == "failed" and "platform is full" in run.error or "for the platform is full" in run.error
    run.created_at = datetime.utcnow()  # the health view lists failures from the last N days of the REAL clock, not NOW
    db_session.commit()
    h = admin.get("/admin/health/backups").json()
    assert h["storage"]["over_cap"] is True and "AT the platform cap" in h["storage"]["warning"]
    assert any(f["app_id"] == aid and "platform is full" in f["error"] for f in h["failures"])


# --- owner toggle + admin lock ---------------------------------------------------------------

def test_new_apps_follow_backups_enabled_default(admin, user, db_session, on, api):
    setting(admin, backups_enabled_default=False)
    a = _ghost_like(user, db_session, "one")
    assert payload(user, a)["enabled"] is False
    setting(admin, backups_enabled_default=True)
    b = _ghost_like(user, db_session, "two")
    assert payload(user, b)["enabled"] is True


def test_owner_toggle_is_audited(user, db_session, aid):
    r = user.patch(f"/apps/{aid}/backups", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = user.patch(f"/apps/{aid}/backups", json={"enabled": True})
    assert r.json()["enabled"] is True
    assert len(rows(db_session, "backup.config_changed")) == 2


def test_admin_lock_blocks_everything_but_keeps_backups(user, admin, other, db_session, aid, runner):
    keep = mkset(db_session, aid, 100)
    assert other.put(f"/admin/apps/{aid}/backups", json={"admin_disabled": True, "reason": "abuse"}).status_code in (403, 404)
    assert admin.put(f"/admin/apps/{aid}/backups", json={"admin_disabled": True}).status_code == 422  # reason required
    r = admin.put(f"/admin/apps/{aid}/backups", json={"admin_disabled": True, "reason": "too big"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["admin_disabled"] is True and p["enabled"] is False and p["admin_disabled_reason"] == "too big"
    assert "disabled by a platform admin" in p["admin_disabled_message"] and "Existing backups are kept" in p["admin_disabled_message"]
    assert p["state"] == "disabled" and [b["id"] for b in p["backups"]] == [keep]
    r = user.patch(f"/apps/{aid}/backups", json={"enabled": True})
    assert r.status_code == 403 and r.json()["detail"].startswith("Backups for this app were disabled by a platform admin")
    assert user.post(f"/apps/{aid}/backups").status_code == 403
    assert user.patch(f"/apps/{aid}/backups", json={"enabled": False}).status_code == 200  # off stays allowed
    cfg = db_session.get(models.AppBackupConfig, aid)
    cfg.next_backup_at = NOW - timedelta(minutes=1)
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    assert not [s for s in runner.started if s.env["MODE"] == "backup"]  # scheduled backups don't run
    assert admin.get(f"/admin/apps/{aid}/backups").json()["admin_disabled"] is True
    assert rows(db_session, "backup.admin_disabled")[0].detail.startswith("reason=too big")
    # lift
    r = admin.put(f"/admin/apps/{aid}/backups", json={"admin_disabled": False})
    assert r.json()["admin_disabled"] is False and r.json()["admin_disabled_reason"] is None
    assert rows(db_session, "backup.admin_enabled")
    assert user.patch(f"/apps/{aid}/backups", json={"enabled": True}).status_code == 200
    assert user.post(f"/apps/{aid}/backups").status_code == 202


def test_queued_run_fails_when_admin_locks_before_start(user, admin, db_session, aid, runner):
    assert user.post(f"/apps/{aid}/backups").status_code == 202
    admin.put(f"/admin/apps/{aid}/backups", json={"admin_disabled": True, "reason": "r"})
    backups.tick(db_session, runner, NOW)
    runs = db_session.query(models.BackupRun).filter_by(app_id=aid, trigger="manual").all()
    assert runs and all(r.status == "failed" and "disabled by a platform admin" in r.error for r in runs)


def test_admin_per_app_ceiling_set_and_clear(admin, db_session, aid):
    r = admin.put(f"/admin/apps/{aid}/backups", json={"max_bytes": 5 * 1024**2})
    assert r.json()["cap_bytes"] == 5 * 1024**2 and r.json()["cap_override"] is True
    assert admin.put(f"/admin/apps/{aid}/backups", json={"max_bytes": config.BACKUP_MAX_BYTES_TOTAL + 1}).status_code == 422
    r = admin.put(f"/admin/apps/{aid}/backups", json={"max_bytes": None})
    assert r.json()["cap_override"] is False and r.json()["cap_bytes"] == config.BACKUP_MAX_BYTES_PER_APP
    assert len(rows(db_session, "backup.ceiling_set")) == 2


# --- rate limits -----------------------------------------------------------------------------

def test_failed_exports_do_not_count_and_messages(dl, user, db_session, runner):
    aid, run_db, _ = dl
    for _ in range(4):  # four failures never lock the owner out
        r = user.post(f"/apps/{aid}/backups/{run_db}/download")
        assert r.status_code == 202, r.text
        backups.tick(db_session, runner, datetime.utcnow())
        runner.fail(runner.started[-1].name, "boom")
        backups.tick(db_session, runner, datetime.utcnow())
    r = user.post(f"/apps/{aid}/backups/{run_db}/download")
    assert r.status_code == 202
    backups.tick(db_session, runner, datetime.utcnow())
    runner.finish(runner.started[-1].name, {"exported": True, "bytes": 1})
    backups.tick(db_session, runner, datetime.utcnow())
    for _ in range(2):
        user.post(f"/apps/{aid}/backups/{run_db}/download")  # idempotent while ready; doesn't consume the limit
    run_bk = db_session.query(models.BackupRun).filter_by(app_id=aid, kind="bucket", trigger="scheduled").one().id
    for rid in (run_bk,):
        assert user.post(f"/apps/{aid}/backups/{rid}/download").status_code == 202
        _finish_export(db_session, runner)
    # a third successful export is still allowed (3 per hour), the 4th is not
    third = _succeeded(db_session, aid, "db", "ee11ee11")
    assert user.post(f"/apps/{aid}/backups/{third}/download").status_code == 202
    _finish_export(db_session, runner)
    fourth = _succeeded(db_session, aid, "db", "ff22ff22")
    r = user.post(f"/apps/{aid}/backups/{fourth}/download")
    assert r.status_code == 429 and r.json()["detail"].startswith("You can prepare 3 backup downloads per hour")


def test_manual_limit_message_and_per_setting(user, admin, db_session, aid):
    assert user.post(f"/apps/{aid}/backups").status_code == 202
    r = user.post(f"/apps/{aid}/backups")
    assert r.status_code == 409  # still queued
    for run in db_session.query(models.BackupRun).filter_by(app_id=aid).all():
        run.status, run.finished_at = "succeeded", NOW
    db_session.commit()
    r = user.post(f"/apps/{aid}/backups")
    assert r.status_code == 429 and r.json()["detail"].startswith("You can start 1 backup per hour")
    setting(admin, backup_manual_per_hour=2)
    assert user.post(f"/apps/{aid}/backups").status_code == 202


def test_per_user_manual_cap_across_apps(user, admin, db_session, aid, monkeypatch):
    monkeypatch.setattr(backups, "USER_MANUAL_FACTOR", 1)
    other_app = _ghost_like(user, db_session, "second")
    assert user.post(f"/apps/{aid}/backups").status_code == 202
    r = user.post(f"/apps/{other_app}/backups")
    assert r.status_code == 429 and "across your apps" in r.json()["detail"]


# --- failure reason ---------------------------------------------------------------------------

def test_failed_export_reason_in_payload_and_old_image_guard(dl, user, db_session, runner):
    aid, run_db, _ = dl
    ex = user.post(f"/apps/{aid}/backups/{run_db}/download").json()
    backups.tick(db_session, runner, datetime.utcnow())
    runner.fail(runner.started[-1].name, "the repository is not configured")
    backups.tick(db_session, runner, datetime.utcnow())
    e = user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()
    assert e["status"] == "failed" and "older than this download needs" in e["error"]
    run = next(r for r in payload(user, aid)["runs"] if r["id"] == run_db)
    assert run["failed_export"]["id"] == ex["export_id"] and run["failed_export"]["error"] == e["error"]


def test_failed_set_export_reason_on_set(user, db_session, aid, runner):
    sid = mkset(db_session, aid, 10)
    ex = user.post(f"/apps/{aid}/backups/sets/{sid}/download").json()
    backups.tick(db_session, runner, datetime.utcnow())
    runner.fail(runner.started[-1].name, "package step failed")
    backups.tick(db_session, runner, datetime.utcnow())
    st = next(b for b in payload(user, aid)["backups"] if b["id"] == sid)
    assert st["export"] is None and st["failed_export"] == {**st["failed_export"], "id": ex["export_id"], "error": "package step failed"}


# --- tenant sweep ----------------------------------------------------------------------------

def test_tenant_sweep_skips_deleted_apps(db_session, aid, runner):
    live = aid
    gone = _ghost_like  # noqa: F841
    db_session.add(models.BackupRun(app_id=live, kind="volume", trigger="scheduled", status="succeeded", created_at=NOW))
    db_session.commit()
    backups._sweep_tenant_secrets(db_session, runner, NOW)
    assert runner.swept_namespaces and runner.swept_namespaces[-1] == [f"app-{live}"]
    db_session.get(models.App, live).deleted_at = NOW
    db_session.commit()
    runner.swept_namespaces.clear()
    backups._sweep_tenant_secrets(db_session, runner, NOW)
    assert runner.swept_namespaces == []


def test_kube_sweep_treats_forbidden_quietly(caplog):
    from app.providers.backup import KubeBackupRunner

    class ForbiddenError(Exception):
        pass

    r = KubeBackupRunner.__new__(KubeBackupRunner)
    r._sweep_ns = lambda ns, active: (_ for _ in ()).throw(ForbiddenError())
    with caplog.at_level("WARNING"):
        assert r.sweep_namespaces(["app-159"], set()) == 0
    assert not caplog.records
