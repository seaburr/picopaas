"""Coverage for the 2026-10-25 settings rows / admin app-limit pair / backup override ceiling / admin backup download."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import backups, models, platform_settings, resources
from tests.test_backups import NOW, admin, on, other, runner, user  # noqa: F401
from tests.test_backups_sets import _mk_set, api, app_ctx, rows, scoped, vols  # noqa: F401

OK = ("succeeded", None, 100, None)


def _put(c, **kw):
    return c.put("/admin/settings", json={"settings": kw})


def _uid(admin, email):
    return next(u["id"] for u in admin.get("/admin/users").json() if u["email"] == email)


def test_settings_catalog_rows_columns_and_pairs(admin):
    r = admin.get("/admin/settings")
    assert r.status_code == 200
    items = {x["key"]: x for g in r.json()["groups"] for x in g["settings"]}
    for key, row in items.items():
        if row.get("ceiling_key"):
            partner = items[row["ceiling_key"]]
            assert partner["default_key"] == key and partner["row"] == row["row"]
            assert (row["column"], partner["column"]) == ("default", "ceiling")
    assert items["backup_max_bytes_per_app"]["ceiling_key"] == "backup_override_max_bytes_per_app"
    assert items["backup_override_max_bytes_per_app"]["row"] == items["backup_max_bytes_per_app"]["row"]


def test_backup_ceiling_validation(admin):
    items = {x["key"]: x for g in admin.get("/admin/settings").json()["groups"] for x in g["settings"]}
    total = items["backup_max_bytes_total"]["value"]
    r = _put(admin, backup_override_max_bytes_per_app=total + 1)
    assert r.status_code == 422 and "backup_max_bytes_total" in r.text
    cur = items["backup_override_max_bytes_per_app"]["value"]
    r = _put(admin, backup_max_bytes_per_app=cur + 1)
    assert r.status_code == 422 and "ceiling" in r.text


def test_effective_app_limit_admin_vs_user(admin, user, db_session):
    adm = db_session.query(models.User).filter_by(email="admin@example.com").one()
    usr = db_session.query(models.User).filter_by(email="user@example.com").one()
    assert _put(admin, admin_default_app_limit=3, max_apps_per_admin=6, max_apps_per_user=4, default_app_limit=2).status_code == 200
    adm.app_limit = 0
    assert resources.effective_app_limit(adm) == 3  # floor = admin default
    adm.app_limit = 5
    assert resources.effective_app_limit(adm) == 5  # raised by own limit
    adm.app_limit = 50
    assert resources.effective_app_limit(adm) == 6  # capped by the admin ceiling
    usr.app_limit = 50
    assert resources.effective_app_limit(usr) == 4  # users: user ceiling, ignores admin pair
    usr.app_limit = 1
    assert resources.effective_app_limit(usr) == 1


def test_effective_app_limit_non_orm_object():
    admin_like = SimpleNamespace(is_platform_admin=True, app_limit=0)
    assert resources.effective_app_limit(admin_like) == platform_settings.get(None, "admin_default_app_limit")
    user_like = SimpleNamespace(is_platform_admin=False, app_limit=1)
    assert resources.effective_app_limit(user_like) == 1


def test_auth_me_max_apps_differs_for_admin(admin, user):
    assert _put(admin, max_apps_per_admin=9, max_apps_per_user=4).status_code == 200
    assert admin.get("/auth/me").json()["max_apps"] == 9
    assert user.get("/auth/me").json()["max_apps"] == 4


def test_patch_user_limit_checked_against_targets_ceiling(admin, user):
    assert _put(admin, max_apps_per_user=4, max_apps_per_admin=8, admin_default_app_limit=2).status_code == 200
    uid, aid = _uid(admin, "user@example.com"), _uid(admin, "admin@example.com")
    assert admin.patch(f"/admin/users/{uid}", json={"app_limit": 5}).status_code == 422
    r = admin.patch(f"/admin/users/{uid}", json={"app_limit": 4})
    assert r.status_code == 200 and r.json()["app_limit"] == 4 and r.json()["app_limit_effective"] == 4
    # the same number is fine for an admin target (higher ceiling), but not above it
    assert admin.patch(f"/admin/users/{aid}", json={"app_limit": 8}).status_code == 200
    assert admin.patch(f"/admin/users/{aid}", json={"app_limit": 9}).status_code == 422
    assert admin.patch("/admin/users/99999", json={"app_limit": 1}).status_code == 404


def test_admin_backups_get_and_patch_ceiling(app_ctx, admin, user, db_session):
    aid = app_ctx
    assert admin.get(f"/admin/apps/{aid}/backups").status_code == 200
    assert admin.get("/admin/apps/99999/backups").status_code == 404
    assert user.get(f"/admin/apps/{aid}/backups").status_code == 403
    ceiling = backups.override_cap_bytes(db_session)
    r = admin.put(f"/admin/apps/{aid}/backups", json={"max_bytes": ceiling + 1})
    assert r.status_code == 422
    ok = admin.put(f"/admin/apps/{aid}/backups", json={"max_bytes": ceiling})
    assert ok.status_code == 200
    assert db_session.get(models.AppBackupConfig, aid).max_bytes_override == ceiling
    # lowering the Settings ceiling below the platform total shrinks what can be granted
    assert _put(admin, backup_max_bytes_per_app=1024 * 1024, backup_override_max_bytes_per_app=2 * 1024 * 1024).status_code == 200
    assert backups.override_cap_bytes(db_session) == 2 * 1024 * 1024
    assert admin.put(f"/admin/apps/{aid}/backups", json={"max_bytes": 3 * 1024 * 1024}).status_code == 422
    assert admin.put(f"/admin/apps/{aid}/backups", json={"max_bytes": 2 * 1024 * 1024}).status_code == 200


def test_admin_backup_download_404s_and_refusals(app_ctx, admin, db_session):
    aid = app_ctx
    sid, _ = _mk_set(db_session, aid, {"db": OK})
    assert admin.get("/admin/apps/99999/backup-sets").status_code == 404
    assert admin.post("/admin/apps/99999/backup-sets/1/download").status_code == 404
    assert admin.post(f"/admin/apps/{aid}/backup-sets/99999/download").status_code == 404
    assert admin.get(f"/admin/apps/{aid}/backup-exports/99999").status_code == 404
    # second request while the first is queued is refused (one active download per app)
    assert admin.post(f"/admin/apps/{aid}/backup-sets/{sid}/download").status_code == 202
    sid2, _ = _mk_set(db_session, aid, {"db": OK})
    r = admin.post(f"/admin/apps/{aid}/backup-sets/{sid2}/download")
    assert r.status_code == 409


def test_export_of_deleted_app_refused_when_purged(app_ctx, admin, db_session, runner):
    aid = app_ctx
    sid, _ = _mk_set(db_session, aid, {"db": OK})
    app = db_session.get(models.App, aid)
    app.deleted_at = datetime.utcnow()
    cfg = db_session.get(models.AppBackupConfig, aid)
    cfg.deleted_retention_until = datetime.utcnow() + timedelta(days=14)
    db_session.commit()
    r = admin.post(f"/admin/apps/{aid}/backup-sets/{sid}/download")
    assert r.status_code == 202
    ex_id = r.json()["export_id"]
    # the repo is purged before the scheduler picks the export up
    cfg.purged_at = datetime.utcnow()
    db_session.commit()
    backups.tick(db_session, runner, datetime.utcnow())
    assert runner.started == [] or all("export" not in j.name for j in runner.started)
    got = admin.get(f"/admin/apps/{aid}/backup-exports/{ex_id}").json()
    assert got["status"] == "failed"
    assert admin.get(f"/admin/apps/{aid}/backup-sets").json()["purged"] is True


def test_enqueue_export_refuses_purged_repo(app_ctx, db_session):
    aid = app_ctx
    _mk_set(db_session, aid, {"db": OK})
    run = db_session.query(models.BackupRun).filter_by(app_id=aid, kind="db").first()
    app = db_session.get(models.App, aid)
    db_session.delete(db_session.get(models.AppBackupConfig, aid))
    db_session.flush()
    with pytest.raises(backups.ExportRefused) as e:
        backups.enqueue_export(db_session, app, run)
    assert e.value.status == 410


def test_owner_manual_backup_blocked_when_storage_full(app_ctx, user, admin, db_session, monkeypatch):
    aid = app_ctx
    cfg = backups.ensure_config(db_session, db_session.get(models.App, aid))
    cfg.max_bytes_override = 1000
    cfg.db_repo_bytes = 5000
    db_session.commit()
    r = user.post(f"/apps/{aid}/backups")
    assert r.status_code == 409 and "Delete a backup" in r.json()["detail"]


def test_platform_total_full_blocks_manual_backup(app_ctx, user, admin, db_session):
    aid = app_ctx
    total = backups.total_cap_bytes(db_session)
    backups.ensure_config(db_session, db_session.get(models.App, aid))
    db_session.add(models.AppBackupConfig(app_id=aid + 1000, db_repo_bytes=total))  # someone else's repos fill the platform
    db_session.commit()
    r = user.post(f"/apps/{aid}/backups")
    assert r.status_code == 409 and "platform is full" in r.json()["detail"]
