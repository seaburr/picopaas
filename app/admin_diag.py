"""Admin Health diagnostics: backup storage used, failing/stuck backups (GET /admin/health/backups)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import backup_sweeper, backups, config, models, platform_settings

TOP_APPS = 10
BACKUP_KINDS = ("db", "bucket", "volume")
MAX_FAILURES = 100
STORAGE_NOTE = ("Sizes are the restic repository sizes each app's last backup recorded (already deduplicated). They "
                "exclude short-lived download exports and any object-store overhead, so the bucket itself can be a bit larger.")


def backup_total_cap_bytes(db: Optional[Session] = None) -> int:
    """The platform-wide backup storage ceiling (admin setting `backup_max_bytes_total`, else the config default)."""
    return int(platform_settings.get(db, "backup_max_bytes_total"))


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def storage(db: Session) -> dict[str, Any]:
    cap = backup_total_cap_bytes(db)
    rows = db.execute(
        select(models.AppBackupConfig, models.App, models.User.email)
        .join(models.App, models.App.id == models.AppBackupConfig.app_id)
        .join(models.User, models.User.id == models.App.owner_id)
    ).all()
    apps = []
    for cfg, app, email in rows:
        total = backups.repo_bytes(cfg)
        if app.deleted_at is not None and (cfg.purged_at is not None or total <= 0):
            continue  # nothing left to show for a deleted app
        apps.append({"app_id": app.id, "name": app.name, "owner_email": email, "deleted": app.deleted_at is not None,
                     "purge_after": _iso(cfg.deleted_retention_until) if app.deleted_at is not None else None,
                     "repo_bytes": total, "db_bytes": cfg.db_repo_bytes or 0, "volume_bytes": cfg.vol_repo_bytes or 0,
                     "bucket_bytes": cfg.bucket_repo_bytes or 0, "cap_bytes": backups.app_cap_bytes(db, cfg),
                     "full": total >= backups.app_cap_bytes(db, cfg), "admin_disabled": cfg.admin_disabled})
    used = sum(a["repo_bytes"] for a in apps)
    apps.sort(key=lambda a: (-a["repo_bytes"], a["app_id"]))
    return {"used_bytes": used, "cap_bytes": cap, "used_pct": round(100.0 * used / cap, 1) if cap > 0 else None,
            "apps_with_backups": sum(1 for a in apps if a["repo_bytes"] > 0), "top_apps": apps[:TOP_APPS],
            "over_cap": cap > 0 and used >= cap,
            "warning": ("Backup storage is AT the platform cap: new scheduled backups are being skipped until space is freed."
                        if cap > 0 and used >= cap else
                        "Backup storage is above 80% of the platform cap." if cap > 0 and used >= 0.8 * cap else None),
            "note": STORAGE_NOTE}


def _run_out(run: models.BackupRun, app: Optional[models.App], email: Optional[str], **extra: Any) -> dict[str, Any]:
    return {"run_id": run.id, "app_id": run.app_id, "app_name": app.name if app else None,
            "app_deleted": bool(app and app.deleted_at is not None), "owner_email": email, "kind": run.kind,
            "trigger": run.trigger, "status": run.status, "error": run.error,
            "at": _iso(run.finished_at or run.started_at or run.created_at), **extra}


def failures_and_stuck(db: Session, days: int = 7, now: Optional[datetime] = None) -> dict[str, Any]:
    now = _now(now)
    since = now - timedelta(days=days)
    Run = models.BackupRun
    base = (select(Run, models.App, models.User.email)
            .join(models.App, models.App.id == Run.app_id).join(models.User, models.User.id == models.App.owner_id))
    failed = db.execute(base.where(Run.status == "failed", Run.kind.in_(BACKUP_KINDS), models.App.deleted_at.is_(None),
                                   Run.created_at >= since).order_by(Run.id.desc()).limit(MAX_FAILURES)).all()
    # "latest": no newer backup-type run of the same app+kind has succeeded, i.e. the app is still failing.
    out = []
    for run, app, email in failed:
        newer_ok = db.execute(select(Run.id).where(
            Run.app_id == run.app_id, Run.kind == run.kind, Run.status == "succeeded", Run.id > run.id,
            Run.trigger.in_(("scheduled", "manual", "final"))).limit(1)).first()
        out.append(_run_out(run, app, email, still_failing=newer_ok is None))
    grace = timedelta(seconds=config.BACKUP_JOB_DEADLINE_SECONDS + 120)
    stuck = []
    for run, app, email in db.execute(base.where(Run.status.in_(("queued", "running"))).order_by(Run.id)).all():
        started = run.started_at if run.status == "running" else run.created_at
        age = now - (started or run.created_at)
        limit = grace if run.status == "running" else grace * 2  # queued waits for a free slot
        if age > limit:
            stuck.append(_run_out(run, app, email, age_seconds=int(age.total_seconds())))
    return {"days": days, "failures": out, "stuck": stuck}


def backup_health(db: Session, days: int = 7) -> dict[str, Any]:
    return {"enabled": backups.enabled(), "configured": config.backups_configured(),
            "storage": storage(db), **failures_and_stuck(db, days), "sweeper": backup_sweeper.last_sweep(db)}
