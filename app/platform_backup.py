"""Self-backup of the PLATFORM's own database (not tenant databases).

Reuses the backup machinery of app/backups.py: the same BackupRunner (one Job per run in the platform backup namespace), the
same restic image/script (MODE=backup KIND=db) and the same bucket and credentials, but its own restic repository
(`<BACKUP_PREFIX>platform/db`), its own random repository password (Fernet-encrypted in `platform_settings`, key
`platform_backup_repo_password`), and its own run table (`platform_backup_runs`; runs have no app). Retention is the
script's `forget --prune` with the PLATFORM_BACKUP_KEEP_* policy. Inert unless backups are usable, PLATFORM_BACKUP_ENABLED is
on and DATABASE_URL is MySQL. Restore steps: docs/OPERATIONS.md "Restoring the platform database".
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import audit, backups, config, crypto, models, platform_settings
from app.providers import cloud
from app.providers.backup import BackupJobSpec, BackupRunner, BackupRunnerError

log = logging.getLogger(__name__)

PASSWORD_KEY = "platform_backup_repo_password"
KEPT_RUNS = 60
ACTIVE = ("queued", "running")
RETRY_AFTER_FAILURE = timedelta(hours=1)
EXPORT = "export"  # trigger of an admin download of one snapshot (not a backup; see "admin download" below)
EXPORT_SCRATCH_MIB = 256  # /work for the compressed dump: the Job's root filesystem is read-only (same size as an app's db export)
EXPORT_ROW_KEEP = timedelta(days=1)  # finished export rows are dropped after this (the object itself is swept after the TTL)


def _backups_only():
    """SQL filter: real backups, not admin-download export rows (which share this table, see enqueue_export)."""
    return models.PlatformBackupRun.trigger != EXPORT


def _now(now: Optional[datetime] = None) -> datetime:
    return backups._now(now)


def active() -> bool:
    """Feature on, the backup bucket configured, and the platform DB is MySQL (the dump tool the Job has)."""
    return backups.usable() and config.PLATFORM_BACKUP_ENABLED and make_url(config.DATABASE_URL).get_backend_name() == "mysql"


def repo_url() -> str:
    return cloud.backup_target().repository_url("platform/db")


def _repo_password(session: Session) -> str:
    row = session.get(models.PlatformSetting, PASSWORD_KEY)
    if row is None:
        row = models.PlatformSetting(key=PASSWORD_KEY, value=crypto.encrypt(secrets.token_urlsafe(32)),
                                     updated_at=datetime.now(timezone.utc).replace(tzinfo=None), updated_by="system")
        session.add(row)
        session.commit()  # persist before any Job can write to the repo
    pw = crypto.try_decrypt(row.value)
    if pw is None:
        raise ValueError("the stored platform backup password can't be read (platform key changed)")
    return pw


def _db_env() -> dict[str, str]:
    url = make_url(config.DATABASE_URL)
    env = {"ENGINE": "mysql", "DB_HOST": url.host or "", "DB_PORT": str(url.port or 3306), "DB_NAME": url.database or "",
           "DB_USER": url.username or "", "DB_PASSWORD": url.password or ""}
    ca = url.query.get("ssl_ca")
    ca = ca[0] if isinstance(ca, tuple) else ca
    if ca:
        try:
            env["DB_CA"] = Path(ca).read_text()
        except OSError:
            raise ValueError("the platform database CA certificate can't be read") from None
    if not (env["DB_HOST"] and env["DB_NAME"] and env["DB_USER"]):
        raise ValueError("DATABASE_URL is incomplete")
    return env


def _repo_env(session: Session) -> dict[str, str]:
    return {
        "RESTIC_REPOSITORY": repo_url(), "RESTIC_PASSWORD": _repo_password(session),
        "AWS_ACCESS_KEY_ID": config.BACKUP_ACCESS_KEY_ID, "AWS_SECRET_ACCESS_KEY": config.BACKUP_SECRET_ACCESS_KEY,
        "AWS_DEFAULT_REGION": config.BACKUP_REGION or "us-east-1", "LIMIT_UPLOAD_KIB": str(config.BACKUP_UPLOAD_LIMIT_KIB)}


def build_env(session: Session, run: models.PlatformBackupRun) -> dict[str, str]:
    return {
        **_repo_env(session),
        "MODE": "backup", "KIND": "db", "TAG": run.trigger,
        "KEEP_DAILY": str(config.PLATFORM_BACKUP_KEEP_DAILY), "KEEP_WEEKLY": str(config.PLATFORM_BACKUP_KEEP_WEEKLY),
        "KEEP_MONTHLY": str(config.PLATFORM_BACKUP_KEEP_MONTHLY), **_db_env(),
    }


def active_job_names(session: Session) -> set[str]:
    return set(session.execute(select(models.PlatformBackupRun.job_name).where(
        models.PlatformBackupRun.status == "running", models.PlatformBackupRun.job_name.is_not(None))).scalars())


def enqueue(session: Session, trigger: str, now: Optional[datetime] = None) -> Optional[models.PlatformBackupRun]:
    """Queue a run unless one is already in flight (returns None then)."""
    now = _now(now)
    if session.execute(select(models.PlatformBackupRun.id).where(models.PlatformBackupRun.status.in_(ACTIVE), _backups_only())
                       .limit(1)).first():  # a download in flight never blocks a backup
        return None
    run = models.PlatformBackupRun(trigger=trigger, status="queued", created_at=now)
    session.add(run)
    session.commit()
    return run


def _finish(session: Session, run: models.PlatformBackupRun, runner: Optional[BackupRunner], *, ok: bool,
            result: Optional[dict[str, Any]] = None, error: Optional[str] = None, now: datetime) -> None:
    result = result or {}
    run.status = "succeeded" if ok else "failed"
    run.finished_at = now
    run.error = None if ok else (backups.redact(error) or "the backup failed")[:255]
    if ok and run.trigger == EXPORT:  # snapshot_id stays: it names the snapshot that was exported
        run.bytes_added = backups._int(result.get("bytes"))
    elif ok:
        run.snapshot_id = str(result.get("snapshot_id") or "")[:64] or None
        run.bytes_added = backups._int(result.get("bytes_added"))
        run.repo_bytes = backups._int(result.get("repo_bytes"))
    session.commit()
    if runner is not None and run.job_name:
        try:
            runner.cleanup(run.job_name, None)
        except Exception as exc:  # noqa: BLE001 - the Secret sweep retries
            log.warning("platform backup %s: cleanup failed (%s)", run.id, type(exc).__name__)
    if run.trigger == EXPORT:
        audit.record(session, None, "platform_backup.export_succeeded" if ok else "platform_backup.export_failed", "platform", "db",
                     f"export={run.id}" + ("" if ok else f": {run.error}"), actor_email="system")
    else:
        audit.record(session, None, "platform_backup.succeeded" if ok else "platform_backup.failed", "platform", "db",
                     f"run={run.id} trigger={run.trigger}" + ("" if ok else f": {run.error}"), actor_email="system")
    # Retention counts real backups only; export rows are kept for a day (long enough for the hourly limit and the 1 h TTL),
    # so a burst of downloads can't push backup history out and a live download is never pruned.
    old = session.execute(select(models.PlatformBackupRun.id).where(_backups_only())
                          .order_by(models.PlatformBackupRun.id.desc()).offset(KEPT_RUNS)).scalars().all()
    if old:
        session.query(models.PlatformBackupRun).filter(models.PlatformBackupRun.id.in_(old)).delete(synchronize_session=False)
    session.query(models.PlatformBackupRun).filter(
        models.PlatformBackupRun.trigger == EXPORT, models.PlatformBackupRun.status.notin_(ACTIVE),
        models.PlatformBackupRun.created_at < now - EXPORT_ROW_KEEP).delete(synchronize_session=False)
    session.commit()


def _poll(session: Session, runner: BackupRunner, now: datetime) -> None:
    grace = timedelta(seconds=config.BACKUP_JOB_DEADLINE_SECONDS + 120)
    for run in session.execute(select(models.PlatformBackupRun).where(models.PlatformBackupRun.status == "running")).scalars().all():
        try:
            st = runner.status(run.job_name or "", None)
        except Exception as exc:  # noqa: BLE001
            log.warning("platform backup %s: status check failed (%s)", run.id, type(exc).__name__)
            continue
        if st.state == "succeeded":
            _finish(session, run, runner, ok=True, result=st.result, now=now)
        elif st.state == "failed":
            _finish(session, run, runner, ok=False, error=st.error, now=now)
        elif st.state == "missing":
            _finish(session, run, runner, ok=False, error=backups.INTERRUPTED_MSG, now=now)
        elif run.started_at is not None and now - backups._naive(run.started_at) > grace:
            _finish(session, run, runner, ok=False, error="the backup took too long and was stopped", now=now)


def _due(session: Session, now: datetime) -> bool:
    last = session.execute(select(models.PlatformBackupRun).where(_backups_only())
                           .order_by(models.PlatformBackupRun.id.desc()).limit(1)).scalar()
    if last is None:
        return True
    wait = timedelta(hours=config.PLATFORM_BACKUP_INTERVAL_HOURS) if last.status != "failed" else RETRY_AFTER_FAILURE
    return now - backups._naive(last.created_at) >= wait


def _start_queued(session: Session, runner: BackupRunner, now: datetime) -> None:
    for run in session.execute(select(models.PlatformBackupRun).where(models.PlatformBackupRun.status == "queued")
                               .order_by(models.PlatformBackupRun.id)).scalars().all():
        try:
            env = build_export_env(session, run) if run.trigger == EXPORT else build_env(session, run)
            spec = BackupJobSpec(name=f"pbk-{run.id}", run_id=run.id, app_id=0, kind="db", env=env,
                                 scratch_mib=EXPORT_SCRATCH_MIB if run.trigger == EXPORT else 0)
        except ValueError as exc:
            _finish(session, run, None, ok=False, error=str(exc), now=now)
            continue
        try:
            runner.start(spec)
        except BackupRunnerError as exc:
            log.warning("platform backup %s: %s", run.id, exc)
            _finish(session, run, None, ok=False, error="the backup job could not be started", now=now)
            continue
        run.status, run.started_at, run.job_name = "running", now, spec.name
        session.commit()


def tick(session: Session, runner: BackupRunner, now: Optional[datetime] = None) -> None:
    """Leader-only, called from backups.tick. Never raises."""
    if not active():
        return
    now = _now(now)
    try:
        _poll(session, runner, now)
        if _due(session, now):
            enqueue(session, "scheduled", now)
        _start_queued(session, runner, now)
    except Exception:  # noqa: BLE001
        session.rollback()
        log.exception("platform backup tick failed")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def summary(session: Session, limit: int = 10, now: datetime | None = None, admin_id: Optional[int] = None) -> dict[str, Any]:
    """Admin Health > Backups block. `now` (naive UTC) is for tests; default is the wall clock. Downloads shown (`export`,
    `failed_download`) are only the viewing admin's own (`admin_id`), so the UI never resumes one it would get a 404 for."""
    runs = session.execute(select(models.PlatformBackupRun).where(_backups_only())
                           .order_by(models.PlatformBackupRun.id.desc()).limit(limit)).scalars().all()
    ok = next((r for r in runs if r.status == "succeeded"), None) or session.execute(
        select(models.PlatformBackupRun).where(models.PlatformBackupRun.status == "succeeded", _backups_only())
        .order_by(models.PlatformBackupRun.id.desc()).limit(1)).scalar()
    can_download = active() and config.backup_export_prefix_ok()
    live = live_exports_by_snapshot(session, _now(now), admin_id)
    failed = recent_failed_export(session, _now(now), admin_id)
    stale = ok is None or backups._now(now) - backups._naive(ok.finished_at or ok.created_at) > timedelta(
        hours=config.PLATFORM_BACKUP_INTERVAL_HOURS * 1.5 + 1)
    return {
        "enabled": config.PLATFORM_BACKUP_ENABLED, "active": active(), "interval_hours": config.PLATFORM_BACKUP_INTERVAL_HOURS,
        "keep": {"daily": config.PLATFORM_BACKUP_KEEP_DAILY, "weekly": config.PLATFORM_BACKUP_KEEP_WEEKLY,
                 "monthly": config.PLATFORM_BACKUP_KEEP_MONTHLY},
        "last_success_at": _iso(ok.finished_at) if ok else None, "repo_bytes": ok.repo_bytes if ok else None,
        "stale": bool(active() and stale),
        "download_per_hour": int(platform_settings.get(session, "backup_export_max_per_hour")),
        "failed_download": _failure(failed),
        "runs": [{"id": r.id, "trigger": r.trigger, "status": r.status, "error": r.error, "created_at": _iso(r.created_at),
                  "finished_at": _iso(r.finished_at), "bytes_added": r.bytes_added, "repo_bytes": r.repo_bytes,
                  "snapshot_id": r.snapshot_id,
                  "downloadable": bool(can_download and r.status == "succeeded" and r.snapshot_id),
                  "export": _export_brief(live.get(r.snapshot_id or ""), _now(now)) if r.status == "succeeded" else None}
                 for r in runs],
    }


# --- admin download (export) ------------------------------------------------------------------
# The platform repo is restic, so a download is the same export Job app backups use (MODE=export KIND=db: `restic dump
# <snapshot> db.sql | gzip` to one object under exports/, swept after BACKUP_EXPORT_TTL_SECONDS with all the others; the download is only offered for PLATFORM_EXPORT_READY_SECONDS, to requested_by). Bookkeeping
# lives in platform_backup_runs with trigger="export" (no schema change): snapshot_id = the exported snapshot, bytes_added = the
# file size, the object key is derived from the row id (export_key). Exports are excluded from the backup list, "last success",
# scheduling, the in-flight check and KEPT_RUNS pruning.

def export_key(run: models.PlatformBackupRun) -> str:
    return f"{config.BACKUP_EXPORT_PREFIX}platform/{run.id}.sql.gz"


def export_state(run: models.PlatformBackupRun, now: Optional[datetime] = None) -> str:
    """queued | running | ready | failed | expired (same rules as backups.export_state)."""
    if run.status == "succeeded":
        age = _now(now) - backups._naive(run.finished_at or run.created_at)
        return "expired" if age >= timedelta(seconds=config.PLATFORM_EXPORT_READY_SECONDS) else "ready"
    return run.status


def export_available_until(run: models.PlatformBackupRun) -> datetime:
    return backups._naive(run.finished_at or run.created_at) + timedelta(seconds=config.PLATFORM_EXPORT_READY_SECONDS)


def link_seconds(run: models.PlatformBackupRun, now: Optional[datetime] = None) -> int:
    """Lifetime of a presigned link: BACKUP_EXPORT_URL_SECONDS, but never past the end of the availability window. 0 = no link."""
    left = int((export_available_until(run) - _now(now)).total_seconds())
    return max(0, min(config.BACKUP_EXPORT_URL_SECONDS, left))


def owned_by(run: models.PlatformBackupRun, admin_id: Optional[int]) -> bool:
    """A download belongs to the admin who started it; rows without an owner (older ones) belong to nobody."""
    return admin_id is not None and run.requested_by == admin_id


def source_run(session: Session, run: models.PlatformBackupRun) -> Optional[models.PlatformBackupRun]:
    """The succeeded backup an export was made from (may be pruned by now)."""
    if not run.snapshot_id:
        return None
    return session.execute(select(models.PlatformBackupRun).where(
        models.PlatformBackupRun.snapshot_id == run.snapshot_id, models.PlatformBackupRun.status == "succeeded", _backups_only())
        .order_by(models.PlatformBackupRun.id.desc()).limit(1)).scalar()


def export_filename(session: Session, run: models.PlatformBackupRun) -> str:
    src = source_run(session, run)
    when = backups._naive((src.finished_at or src.created_at) if src is not None else run.created_at)
    return f"picopaas-platform-db-{when:%Y-%m-%d}.sql.gz"


def _exports(session: Session) -> list[models.PlatformBackupRun]:
    return list(session.execute(select(models.PlatformBackupRun).where(models.PlatformBackupRun.trigger == EXPORT)
                                .order_by(models.PlatformBackupRun.id.desc())).scalars())


def live_exports_by_snapshot(session: Session, now: Optional[datetime] = None,
                             admin_id: Optional[int] = None) -> dict[str, models.PlatformBackupRun]:
    """Latest queued / running / ready export per snapshot id, among the exports `admin_id` started (none for None)."""
    now, found = _now(now), {}
    for e in _exports(session):
        if owned_by(e, admin_id) and e.snapshot_id and e.snapshot_id not in found and export_state(e, now) in ("queued", "running", "ready"):
            found[e.snapshot_id] = e
    return found


def recent_failed_export(session: Session, now: Optional[datetime] = None,
                         admin_id: Optional[int] = None) -> Optional[models.PlatformBackupRun]:
    """The newest failed export of `admin_id` from the last hour, unless a newer live one of theirs exists (so the UI can say
    why a retry is on offer). Another admin's export is never shown."""
    now = _now(now)
    for e in _exports(session):
        if not owned_by(e, admin_id):
            continue
        st = export_state(e, now)
        if st in ("queued", "running", "ready"):
            return None
        if st == "failed":
            return e if now - backups._naive(e.finished_at or e.created_at) < timedelta(hours=1) else None
    return None


def _export_brief(e: Optional[models.PlatformBackupRun], now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    if e is None:
        return None
    state = export_state(e, now)
    return {"id": e.id, "status": state, "available_until": _iso(export_available_until(e)) if state == "ready" else None}


def _failure(e: Optional[models.PlatformBackupRun]) -> Optional[dict[str, Any]]:
    return None if e is None else {"id": e.id, "snapshot_id": e.snapshot_id, "error": e.error, "failed_at": _iso(e.finished_at or e.created_at)}


def build_export_env(session: Session, run: models.PlatformBackupRun) -> dict[str, str]:
    if not run.snapshot_id:
        raise ValueError("the backup to download is gone")
    return {**_repo_env(session), "MODE": "export", "KIND": "db", "SNAPSHOT": run.snapshot_id, "TAG": EXPORT,
            "EXPORT_ENDPOINT": cloud.backup_target().endpoint(), "EXPORT_BUCKET": config.BACKUP_BUCKET, "EXPORT_KEY": export_key(run)}


def enqueue_export(session: Session, source: models.PlatformBackupRun, admin_id: int,
                   now: Optional[datetime] = None) -> tuple[models.PlatformBackupRun, bool]:
    """Queue the materialisation of `source` (a succeeded backup) as one .sql.gz. Idempotent for a snapshot for which `admin_id`
    already has a live export (created=False); another admin's ready export doesn't count (they start their own), another
    admin's active one is the usual 409. One export in flight at a time, and `backup_export_max_per_hour` new ones per hour for the whole
    platform (failed ones don't count). Raises backups.ExportRefused. The caller commits."""
    now = _now(now)
    if not active() or not config.backup_export_prefix_ok():
        raise backups.ExportRefused("Downloads aren't available right now.", 503)
    if source.trigger == EXPORT or source.status != "succeeded" or not source.snapshot_id:
        raise backups.ExportRefused("That backup run didn't finish successfully, so there is nothing to download.")
    exports = _exports(session)
    live = live_exports_by_snapshot(session, now, admin_id).get(source.snapshot_id)
    if live is not None:
        return live, False
    if any(e.status in ACTIVE for e in exports):
        raise backups.ExportRefused("Another platform download is being prepared. Wait for it to finish.", 409)
    limit = int(platform_settings.get(session, "backup_export_max_per_hour"))
    recent = sorted(backups._naive(e.created_at) for e in exports if e.status != "failed" and now - backups._naive(e.created_at) < timedelta(hours=1))
    if len(recent) >= limit:
        wait = int((recent[len(recent) - limit] + timedelta(hours=1) - now).total_seconds()) + 1
        raise backups.ExportRefused(f"{limit} platform download{'' if limit == 1 else 's'} per hour is the limit (failed ones don't count). "
                                    f"Try again in {max(1, wait // 60)} minutes.", 429, retry_after=wait)
    run = models.PlatformBackupRun(trigger=EXPORT, status="queued", created_at=now, snapshot_id=source.snapshot_id,
                                  requested_by=admin_id)
    session.add(run)
    session.flush()
    return run, True
