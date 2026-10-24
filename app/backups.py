"""Backups (REQ-9, docs/design/backups.md), phase 1a: database dumps into per-app restic repos.

The API only writes rows (`BackupRun` status `queued`); everything that touches the cluster happens in `tick`, which the
leader-only scheduler job runs every BACKUP_TICK_SECONDS (app/scheduler.py::backup_job). `tick` polls running Jobs and
records their results, sweeps leftover Secrets, schedules due apps, starts queued runs (concurrency-limited, one dump per
DB server at a time) and purges the repos of apps deleted more than BACKUP_DELETED_RETENTION_DAYS ago.

Everything is inert unless config.BACKUPS_ENABLED and the backup bucket/key are configured (`usable()`); the flag is read
at call time so tests can switch it. Volume backups (kind "volume") are phase 1b: the columns exist, nothing creates them.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, config, crypto, models
from app.providers.backup import BackupJobSpec, BackupRunner, BackupRunnerError

log = logging.getLogger(__name__)

KINDS = ("db",)  # kinds this phase can run ("volume" arrives in phase 1b)
KIND_LABEL = {"db": "database", "volume": "volume"}
SCHEDULES = ("daily", "weekly", "off")
ACTIVE = ("queued", "running")
FINAL_MAX_AGE = timedelta(hours=2)  # a final (pre-delete) backup that is still not done after this no longer blocks deletion
STALE_DAILY = timedelta(hours=36)
STALE_WEEKLY = timedelta(days=8)
EVENT_KEPT = 20


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _now(now: Optional[datetime] = None) -> datetime:
    return _naive(now) if now else datetime.now(timezone.utc).replace(tzinfo=None)


def enabled() -> bool:
    return config.BACKUPS_ENABLED


def usable() -> bool:
    """Feature on AND the backup bucket + its own key are configured. Off or misconfigured: nothing runs."""
    return config.BACKUPS_ENABLED and config.backups_configured()


def redact(text: Optional[str], limit: int = 200) -> str:
    """Short, URL- and key-free summary of an error text before it is stored or shown."""
    t = re.sub(r"\w+://\S+", "<url>", str(text or ""))
    t = re.sub(r"(?i)(password|secret|token|key)\s*[=:]\s*\S+", r"\1=<redacted>", t)
    return " ".join(t.split())[:limit]


def eligible_kinds(app: models.App) -> list[str]:
    """Kinds this app has data for AND this phase can back up."""
    return ["db"] if app.database is not None else []


def wants_backups(app: models.App) -> bool:
    """Default for the per-app setting: on for apps with a database or volume (the unrecoverable resources)."""
    return bool(app.want_db or app.volume_size_gi)


# --- scheduling -------------------------------------------------------------------------------

def compute_next(app_id: int, after: datetime, schedule: str) -> Optional[datetime]:
    """Next backup time after `after` (naive UTC): BACKUP_SCHEDULE_HOUR plus a per-app minute offset so apps don't
    all start at once. weekly = six days later than the next daily slot. None when the schedule is off."""
    if schedule == "off":
        return None
    slot = after.replace(hour=config.BACKUP_SCHEDULE_HOUR % 24, minute=(app_id * 7) % 60, second=0, microsecond=0)
    if slot <= after:
        slot += timedelta(days=1)
    return slot + timedelta(days=6) if schedule == "weekly" else slot


def ensure_config(session: Session, app: models.App, now: Optional[datetime] = None) -> models.AppBackupConfig:
    cfg = session.get(models.AppBackupConfig, app.id)
    if cfg is None:
        cfg = models.AppBackupConfig(app_id=app.id, enabled=wants_backups(app), schedule="daily",
                                     next_backup_at=_now(now))  # first backup right away, then the daily slot
        session.add(cfg)
        session.commit()
    return cfg


def repo_password(cfg: models.AppBackupConfig) -> str:
    """The app's restic password (random, Fernet-encrypted at rest); generated on first use. Raises ValueError if the
    stored value can't be decrypted (FERNET_KEY lost): the repo is then unreadable and must not be re-initialised."""
    if cfg.repo_password_enc is None:
        cfg.repo_password_enc = crypto.encrypt(secrets.token_urlsafe(32))
    return crypto.decrypt(cfg.repo_password_enc)


def repo_url(app_id: int, kind: str) -> str:
    prefix = config.BACKUP_PREFIX.strip("/")
    prefix = f"{prefix}/" if prefix else ""
    return f"s3:{config.backup_endpoint()}/{config.BACKUP_BUCKET}/{prefix}apps/{app_id}/{kind}"


# --- queueing ---------------------------------------------------------------------------------

def active_run(session: Session, app_id: int) -> Optional[models.BackupRun]:
    return session.execute(
        select(models.BackupRun).where(models.BackupRun.app_id == app_id, models.BackupRun.status.in_(ACTIVE))
        .order_by(models.BackupRun.id).limit(1)
    ).scalar_one_or_none()


def enqueue(session: Session, app: models.App, kind: str, trigger: str, now: Optional[datetime] = None) -> models.BackupRun:
    run = models.BackupRun(app_id=app.id, kind=kind, trigger=trigger, status="queued", created_at=_now(now))
    session.add(run)
    session.flush()
    return run


def manual_cooldown(session: Session, app_id: int, now: Optional[datetime] = None) -> Optional[int]:
    """Seconds until another on-demand backup is allowed (None = allowed now)."""
    last = session.execute(
        select(func.max(models.BackupRun.created_at)).where(models.BackupRun.app_id == app_id,
                                                           models.BackupRun.trigger == "manual")
    ).scalar()
    if last is None:
        return None
    wait = (_naive(last) + timedelta(minutes=config.BACKUP_MANUAL_MIN_INTERVAL_MINUTES) - _now(now)).total_seconds()
    return int(wait) + 1 if wait > 0 else None


# --- results ----------------------------------------------------------------------------------

def _prefix(kind: str) -> str:
    return "db" if kind == "db" else "vol"


def _event(session: Session, app_id: int, run_id: int, message: str, at: datetime) -> None:
    session.add(models.AppEvent(app_id=app_id, at=at, kind="backup_failed", message=message[:255],
                                dedupe_key=f"backup-{run_id}"))
    session.flush()
    old = session.execute(
        select(models.AppEvent.id).where(models.AppEvent.app_id == app_id)
        .order_by(models.AppEvent.at.desc(), models.AppEvent.id.desc()).offset(EVENT_KEPT)
    ).scalars().all()
    if old:
        session.query(models.AppEvent).filter(models.AppEvent.id.in_(old)).delete(synchronize_session=False)


def _finish(session: Session, run: models.BackupRun, runner: Optional[BackupRunner], *, ok: bool,
            result: Optional[dict[str, Any]] = None, error: Optional[str] = None, now: datetime) -> None:
    """Record the outcome of a run (and clean its Job + Secret up), the per-app summary, an event on failure."""
    result = result or {}
    run.status = "succeeded" if ok else "failed"
    run.finished_at = now
    run.error = None if ok else (redact(error) or "the backup failed")[:255]
    if ok and run.trigger != "purge":
        run.snapshot_id = str(result.get("snapshot_id") or "")[:64] or None
        run.bytes_added = _int(result.get("bytes_added"))
        run.repo_bytes = _int(result.get("repo_bytes"))
    cfg = session.get(models.AppBackupConfig, run.app_id)
    if cfg is not None and run.trigger == "purge":
        if ok:
            cfg.purged_at = now
            setattr(cfg, f"{_prefix(run.kind)}_repo_bytes", 0)
        else:  # try again tomorrow rather than every tick
            cfg.deleted_retention_until = now + timedelta(days=1)
    elif cfg is not None:
        p = _prefix(run.kind)
        setattr(cfg, f"{p}_last_backup_at", now)
        setattr(cfg, f"{p}_last_status", run.status)
        setattr(cfg, f"{p}_last_error", run.error)
        cfg.last_backup_at, cfg.last_status, cfg.last_error = now, run.status, run.error
        if ok:
            setattr(cfg, f"{p}_last_bytes", run.bytes_added)
            if run.repo_bytes is not None:
                setattr(cfg, f"{p}_repo_bytes", run.repo_bytes)
            cfg.last_success_at = now
    if not ok and run.trigger != "purge":
        _event(session, run.app_id, run.id,
               f"Backup of your {KIND_LABEL.get(run.kind, run.kind)} failed: {run.error}. "
               "Open Settings > Backups for details.", now)
    session.commit()
    if runner is not None and run.job_name:
        try:
            runner.cleanup(run.job_name)
        except Exception as exc:  # noqa: BLE001 - the Secret sweep retries
            log.warning("backup run %s: cleanup failed (%s)", run.id, type(exc).__name__)
    detail = f"{run.kind} {run.trigger}" + ("" if ok else f": {run.error}")
    audit.record(session, None, "backup.succeeded" if ok else "backup.failed", "app", run.app_id,
                 f"run={run.id} {detail}", actor_email="system")


def _int(v: Any) -> Optional[int]:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return None


# --- the tick ---------------------------------------------------------------------------------

def tick(session: Session, runner: BackupRunner, now: Optional[datetime] = None) -> None:
    """One leader-only pass. Never raises for a single bad run."""
    if not usable():
        return
    now = _now(now)
    _poll(session, runner, now)
    try:
        swept = runner.sweep({r.job_name for r in _running(session) if r.job_name})
        if swept:
            log.warning("backup: removed %d leftover job secret(s)", swept)
    except Exception as exc:  # noqa: BLE001
        log.warning("backup: secret sweep failed (%s)", type(exc).__name__)
    _schedule(session, now)
    _enqueue_purges(session, now)
    _start_queued(session, runner, now)


def _running(session: Session) -> list[models.BackupRun]:
    return list(session.execute(select(models.BackupRun).where(models.BackupRun.status == "running")).scalars())


def _poll(session: Session, runner: BackupRunner, now: datetime) -> None:
    grace = timedelta(seconds=config.BACKUP_JOB_DEADLINE_SECONDS + 600)
    for run in _running(session):
        try:
            st = runner.status(run.job_name or "")
        except Exception as exc:  # noqa: BLE001 - API blip: look again next tick
            log.warning("backup run %s: status check failed (%s)", run.id, type(exc).__name__)
            continue
        if st.state == "succeeded":
            _finish(session, run, runner, ok=True, result=st.result, now=now)
        elif st.state == "failed":
            _finish(session, run, runner, ok=False, error=st.error, now=now)
        elif st.state == "missing":
            _finish(session, run, runner, ok=False, error="the backup job disappeared", now=now)
        elif run.started_at is not None and now - _naive(run.started_at) > grace:
            _finish(session, run, runner, ok=False, error="the backup took too long and was stopped", now=now)


def _schedule(session: Session, now: datetime) -> None:
    """Give every app with a database a config row, then queue a run for each app that is due."""
    have = set(session.execute(select(models.AppBackupConfig.app_id)).scalars())
    for app in session.execute(
        select(models.App).where(models.App.deleted_at.is_(None))
    ).scalars():
        if app.id not in have and wants_backups(app) and app.database is not None:
            ensure_config(session, app, now)
    due = session.execute(
        select(models.AppBackupConfig, models.App).join(models.App, models.App.id == models.AppBackupConfig.app_id)
        .where(models.App.deleted_at.is_(None), models.AppBackupConfig.enabled.is_(True),
               models.AppBackupConfig.schedule != "off", models.AppBackupConfig.next_backup_at <= now)
    ).all()
    for cfg, app in due:
        kinds = eligible_kinds(app)
        if not kinds or active_run(session, app.id) is not None:
            continue
        for kind in kinds:
            enqueue(session, app, kind, "scheduled", now)
        cfg.next_backup_at = compute_next(app.id, now, cfg.schedule)
    session.commit()


def _enqueue_purges(session: Session, now: datetime) -> None:
    rows = session.execute(
        select(models.AppBackupConfig).where(models.AppBackupConfig.purged_at.is_(None),
                                             models.AppBackupConfig.deleted_retention_until.is_not(None),
                                             models.AppBackupConfig.deleted_retention_until <= now)
    ).scalars().all()
    for cfg in rows:
        if active_run(session, cfg.app_id) is not None:
            continue
        app = session.get(models.App, cfg.app_id)
        kinds = [k for k, p in (("db", "db"), ("volume", "vol"))
                 if (getattr(cfg, f"{p}_repo_bytes") or 0) > 0 or getattr(cfg, f"{p}_last_status") == "succeeded"]
        if not kinds:
            cfg.purged_at = now
            continue
        for kind in kinds:
            enqueue(session, app, kind, "purge", now)
    session.commit()


def _start_queued(session: Session, runner: BackupRunner, now: datetime) -> None:
    queued = session.execute(
        select(models.BackupRun).where(models.BackupRun.status == "queued")
        .order_by((models.BackupRun.trigger != "final"), models.BackupRun.id)  # a pending delete goes first
    ).scalars().all()
    if not queued:
        return
    running = _running(session)
    busy_servers = _db_servers(session, [r for r in running if r.kind == "db"])
    total = session.execute(select(func.coalesce(func.sum(
        models.AppBackupConfig.db_repo_bytes + models.AppBackupConfig.vol_repo_bytes), 0))).scalar() or 0
    for run in queued:
        if len(running) >= max(1, config.BACKUP_MAX_CONCURRENT):
            break
        app = session.get(models.App, run.app_id)
        cfg = session.get(models.AppBackupConfig, run.app_id)
        if app is None or cfg is None:
            _finish(session, run, None, ok=False, error="the app no longer exists", now=now)
            continue
        if app.deleted_at is not None and run.trigger not in ("final", "purge"):
            _finish(session, run, None, ok=False, error="the app was deleted", now=now)
            continue
        server_id = app.database.db_server_id if app.database is not None else None
        if run.kind == "db" and run.trigger != "purge":
            if server_id is None:
                _finish(session, run, None, ok=False, error="the app has no database", now=now)
                continue
            if server_id in busy_servers:  # one dump at a time per DB server (connection budget)
                continue
            if (cfg.db_repo_bytes or 0) + (cfg.vol_repo_bytes or 0) >= config.BACKUP_MAX_BYTES_PER_APP:
                _finish(session, run, None, ok=False,
                        error=f"this app's backups reached the {config.BACKUP_MAX_BYTES_PER_APP // 1024**2} MB backup storage cap", now=now)
                continue
            if total >= config.BACKUP_MAX_BYTES_TOTAL:
                _finish(session, run, None, ok=False, error="the platform's backup storage is full", now=now)
                continue
        try:
            spec = BackupJobSpec(name=f"bk-{run.id}", run_id=run.id, app_id=app.id, kind=run.kind,
                                 env=_build_env(session, app, cfg, run))
        except ValueError as exc:
            _finish(session, run, None, ok=False, error=str(exc), now=now)
            continue
        try:
            runner.start(spec)
        except BackupRunnerError as exc:
            log.warning("backup run %s: %s", run.id, exc)
            _finish(session, run, None, ok=False, error="the backup job could not be started", now=now)
            continue
        run.status, run.started_at, run.job_name = "running", now, spec.name
        session.commit()
        running.append(run)
        if server_id is not None and run.kind == "db":
            busy_servers.add(server_id)


def _db_servers(session: Session, runs: list[models.BackupRun]) -> set[int]:
    out: set[int] = set()
    for r in runs:
        db = session.execute(select(models.AppDatabase.db_server_id).where(models.AppDatabase.app_id == r.app_id)).scalar()
        if db is not None:
            out.add(db)
    return out


def _build_env(session: Session, app: models.App, cfg: models.AppBackupConfig, run: models.BackupRun) -> dict[str, str]:
    """The per-run Secret's contents. Raises ValueError with a safe message when a credential can't be read."""
    try:
        password = repo_password(cfg)
    except ValueError:
        raise ValueError("the stored backup password can't be read (platform key changed)") from None
    session.commit()  # persist a freshly generated repo password before any Job can write to the repo
    env = {
        "MODE": "purge" if run.trigger == "purge" else "backup",
        "RESTIC_REPOSITORY": repo_url(app.id, run.kind),
        "RESTIC_PASSWORD": password,
        "AWS_ACCESS_KEY_ID": config.BACKUP_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": config.BACKUP_SECRET_ACCESS_KEY,
        "AWS_DEFAULT_REGION": config.BACKUP_REGION or "us-east-1",
        "KEEP_DAILY": str(config.BACKUP_KEEP_DAILY),
        "KEEP_WEEKLY": str(config.BACKUP_KEEP_WEEKLY),
        "KEEP_MONTHLY": str(config.BACKUP_KEEP_MONTHLY),
        "LIMIT_UPLOAD_KIB": str(config.BACKUP_UPLOAD_LIMIT_KIB),
        "TAG": run.trigger,
    }
    if run.trigger != "purge":
        db = app.database
        if db is None or db.server is None:
            raise ValueError("the app has no database")
        pw = crypto.try_decrypt(db.password_encrypted)
        if pw is None:
            raise ValueError("the database credentials can't be read (platform key changed)")
        env.update({
            "ENGINE": "pg" if db.server.engine == "pg" else "mysql",
            "DB_HOST": db.server.host, "DB_PORT": str(db.server.port), "DB_NAME": db.db_name,
            "DB_USER": db.db_user, "DB_PASSWORD": pw,
        })
        if db.server.ca_cert:
            env["DB_CA"] = db.server.ca_cert
    return env


# --- app delete -------------------------------------------------------------------------------

def final_gate(session: Session, app: models.App, now: Optional[datetime] = None) -> bool:
    """Called by the reconciler before it drops a deleted app's database. True = go ahead and delete. Takes ONE final
    backup attempt first (queued here, run by `tick`); a failed or stuck attempt does not block the deletion. When the
    app has backed-up data, its repos are kept BACKUP_DELETED_RETENTION_DAYS and purged afterwards."""
    if not usable() or app.database is None:
        return True
    now = _now(now)
    cfg = ensure_config(session, app, now)
    if cfg.purged_at is not None:
        return True
    if not cfg.enabled:
        # No final backup (the owner turned backups off), but repos from earlier runs still need the 14-day purge,
        # otherwise they would sit in the bucket forever.
        _retain(session, app, cfg, now)
        return True
    run = session.execute(
        select(models.BackupRun).where(models.BackupRun.app_id == app.id, models.BackupRun.trigger == "final")
        .order_by(models.BackupRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    if run is None:
        enqueue(session, app, "db", "final", now)
        session.commit()
        return False
    if run.status in ACTIVE:
        if now - _naive(run.created_at) <= FINAL_MAX_AGE:
            return False
        _finish(session, run, None, ok=False, error="the final backup did not finish in time", now=now)
    _retain(session, app, cfg, now)
    return True


def _retain(session: Session, app: models.App, cfg: models.AppBackupConfig, now: datetime) -> None:
    if cfg.deleted_retention_until is None and (cfg.last_success_at is not None or repo_bytes(cfg) > 0):
        cfg.deleted_retention_until = now + timedelta(days=config.BACKUP_DELETED_RETENTION_DAYS)
        session.commit()
        audit.record(session, None, "backup.retained", "app", app.id,
                     f"repos kept until {cfg.deleted_retention_until.date().isoformat()}", actor_email="system")


# --- status / health --------------------------------------------------------------------------

def app_state(cfg: Optional[models.AppBackupConfig], now: Optional[datetime] = None) -> str:
    """disabled | pending (no backup yet) | ok | stale | failing."""
    if cfg is None or not cfg.enabled or cfg.schedule == "off":
        return "disabled"
    now = _now(now)
    limit = STALE_WEEKLY if cfg.schedule == "weekly" else STALE_DAILY
    if cfg.last_status == "failed":
        return "failing"
    if cfg.last_success_at is None:
        return "stale" if now - _naive(cfg.created_at) > limit else "pending"
    return "stale" if now - _naive(cfg.last_success_at) > limit else "ok"


def repo_bytes(cfg: Optional[models.AppBackupConfig]) -> int:
    return (cfg.db_repo_bytes or 0) + (cfg.vol_repo_bytes or 0) if cfg is not None else 0


def overview(session: Session, now: Optional[datetime] = None) -> dict[str, Any]:
    """Fleet view for GET /admin/backups and the Health page row."""
    now = _now(now)
    out: dict[str, Any] = {
        "enabled": enabled(), "configured": config.backups_configured(),
        "cap_per_app_bytes": config.BACKUP_MAX_BYTES_PER_APP, "cap_total_bytes": config.BACKUP_MAX_BYTES_TOTAL,
        "total_repo_bytes": 0, "warning": None, "counts": {"ok": 0, "pending": 0, "stale": 0, "failing": 0, "disabled": 0},
        "apps": [], "retained": [],
    }
    if not enabled():
        return out
    rows = session.execute(
        select(models.AppBackupConfig, models.App, models.User.email)
        .join(models.App, models.App.id == models.AppBackupConfig.app_id)
        .join(models.User, models.User.id == models.App.owner_id).order_by(models.App.id)
    ).all()
    for cfg, app, email in rows:
        if app.deleted_at is not None:
            out["retained"].append({
                "app_id": app.id, "name": app.name, "owner_email": email, "repo_bytes": repo_bytes(cfg),
                "retained_until": cfg.deleted_retention_until, "purged_at": cfg.purged_at})
            out["total_repo_bytes"] += repo_bytes(cfg)
            continue
        state = app_state(cfg, now)
        out["counts"][state] += 1
        out["total_repo_bytes"] += repo_bytes(cfg)
        out["apps"].append({
            "app_id": app.id, "name": app.name, "owner_email": email, "state": state, "enabled": cfg.enabled,
            "schedule": cfg.schedule, "last_success_at": cfg.last_success_at, "last_status": cfg.last_status,
            "last_error": cfg.last_error, "next_backup_at": cfg.next_backup_at, "repo_bytes": repo_bytes(cfg)})
    if not out["configured"]:
        out["warning"] = "Backups are on but the backup bucket or key is not configured; nothing is being backed up."
    elif out["total_repo_bytes"] >= 0.8 * config.BACKUP_MAX_BYTES_TOTAL:
        out["warning"] = "Backup storage is above 80% of the platform cap."
    return out


def health_summary(session: Session) -> dict[str, Any]:
    """Compact version for GET /admin/health (`backups` key)."""
    o = overview(session)
    return {k: o[k] for k in ("enabled", "configured", "counts", "total_repo_bytes", "cap_total_bytes", "warning")}
