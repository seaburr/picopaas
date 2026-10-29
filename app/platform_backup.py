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

from app import audit, backups, config, crypto, models
from app.providers.backup import BackupJobSpec, BackupRunner, BackupRunnerError

log = logging.getLogger(__name__)

PASSWORD_KEY = "platform_backup_repo_password"
KEPT_RUNS = 60
ACTIVE = ("queued", "running")
RETRY_AFTER_FAILURE = timedelta(hours=1)


def _now(now: Optional[datetime] = None) -> datetime:
    return backups._now(now)


def active() -> bool:
    """Feature on, the backup bucket configured, and the platform DB is MySQL (the dump tool the Job has)."""
    return backups.usable() and config.PLATFORM_BACKUP_ENABLED and make_url(config.DATABASE_URL).get_backend_name() == "mysql"


def repo_url() -> str:
    prefix = config.BACKUP_PREFIX.strip("/")
    prefix = f"{prefix}/" if prefix else ""
    return f"s3:{config.backup_endpoint()}/{config.BACKUP_BUCKET}/{prefix}platform/db"


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


def build_env(session: Session, run: models.PlatformBackupRun) -> dict[str, str]:
    return {
        "RESTIC_REPOSITORY": repo_url(), "RESTIC_PASSWORD": _repo_password(session),
        "AWS_ACCESS_KEY_ID": config.BACKUP_ACCESS_KEY_ID, "AWS_SECRET_ACCESS_KEY": config.BACKUP_SECRET_ACCESS_KEY,
        "AWS_DEFAULT_REGION": config.BACKUP_REGION or "us-east-1", "LIMIT_UPLOAD_KIB": str(config.BACKUP_UPLOAD_LIMIT_KIB),
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
    if session.execute(select(models.PlatformBackupRun.id).where(models.PlatformBackupRun.status.in_(ACTIVE)).limit(1)).first():
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
    if ok:
        run.snapshot_id = str(result.get("snapshot_id") or "")[:64] or None
        run.bytes_added = backups._int(result.get("bytes_added"))
        run.repo_bytes = backups._int(result.get("repo_bytes"))
    session.commit()
    if runner is not None and run.job_name:
        try:
            runner.cleanup(run.job_name, None)
        except Exception as exc:  # noqa: BLE001 - the Secret sweep retries
            log.warning("platform backup %s: cleanup failed (%s)", run.id, type(exc).__name__)
    audit.record(session, None, "platform_backup.succeeded" if ok else "platform_backup.failed", "platform", "db",
                 f"run={run.id} trigger={run.trigger}" + ("" if ok else f": {run.error}"), actor_email="system")
    old = session.execute(select(models.PlatformBackupRun.id).order_by(models.PlatformBackupRun.id.desc()).offset(KEPT_RUNS)).scalars().all()
    if old:
        session.query(models.PlatformBackupRun).filter(models.PlatformBackupRun.id.in_(old)).delete(synchronize_session=False)
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
    last = session.execute(select(models.PlatformBackupRun).order_by(models.PlatformBackupRun.id.desc()).limit(1)).scalar()
    if last is None:
        return True
    wait = timedelta(hours=config.PLATFORM_BACKUP_INTERVAL_HOURS) if last.status != "failed" else RETRY_AFTER_FAILURE
    return now - backups._naive(last.created_at) >= wait


def _start_queued(session: Session, runner: BackupRunner, now: datetime) -> None:
    for run in session.execute(select(models.PlatformBackupRun).where(models.PlatformBackupRun.status == "queued")
                               .order_by(models.PlatformBackupRun.id)).scalars().all():
        try:
            spec = BackupJobSpec(name=f"pbk-{run.id}", run_id=run.id, app_id=0, kind="db", env=build_env(session, run))
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


def summary(session: Session, limit: int = 10, now: datetime | None = None) -> dict[str, Any]:
    """Admin Health > Backups block. `now` (naive UTC) is for tests; default is the wall clock."""
    runs = session.execute(select(models.PlatformBackupRun).order_by(models.PlatformBackupRun.id.desc()).limit(limit)).scalars().all()
    ok = next((r for r in runs if r.status == "succeeded"), None) or session.execute(
        select(models.PlatformBackupRun).where(models.PlatformBackupRun.status == "succeeded")
        .order_by(models.PlatformBackupRun.id.desc()).limit(1)).scalar()
    stale = ok is None or backups._now(now) - backups._naive(ok.finished_at or ok.created_at) > timedelta(
        hours=config.PLATFORM_BACKUP_INTERVAL_HOURS * 1.5 + 1)
    return {
        "enabled": config.PLATFORM_BACKUP_ENABLED, "active": active(), "interval_hours": config.PLATFORM_BACKUP_INTERVAL_HOURS,
        "keep": {"daily": config.PLATFORM_BACKUP_KEEP_DAILY, "weekly": config.PLATFORM_BACKUP_KEEP_WEEKLY,
                 "monthly": config.PLATFORM_BACKUP_KEEP_MONTHLY},
        "last_success_at": _iso(ok.finished_at) if ok else None, "repo_bytes": ok.repo_bytes if ok else None,
        "stale": bool(active() and stale),
        "runs": [{"id": r.id, "trigger": r.trigger, "status": r.status, "error": r.error, "created_at": _iso(r.created_at),
                  "finished_at": _iso(r.finished_at), "bytes_added": r.bytes_added, "repo_bytes": r.repo_bytes,
                  "snapshot_id": r.snapshot_id} for r in runs],
    }
