"""Backups (REQ-9, docs/design/backups.md), phase 1a: database dumps into per-app restic repos.

The API only writes rows (`BackupRun` status `queued`); everything that touches the cluster happens in `tick`, which the
leader-only scheduler job runs every BACKUP_TICK_SECONDS (app/scheduler.py::backup_job). `tick` polls running Jobs and
records their results, sweeps leftover Secrets, schedules due apps, starts queued runs (concurrency-limited, one dump per
DB server at a time) and purges the repos of apps deleted more than BACKUP_DELETED_RETENTION_DAYS ago.

Everything is inert unless config.BACKUPS_ENABLED and the backup bucket/key are configured (`usable()`); the flag is read
at call time so tests can switch it. Volume backups (kind "volume") are the one Job that runs in the TENANT namespace (it
mounts the app's PVC read-only); they additionally need config.BACKUP_KEY_SCOPED (an admin's statement that the backup key is
scoped to the backup bucket), and their retention prune runs afterwards in the platform namespace (trigger "prune").

One backup is a BackupSet: every enqueue (manual, scheduled, final) creates one set and one BackupRun per kind it covers
(db, bucket, volume), linked by `set_id`. The owner sees, downloads (one .tar.gz package) and deletes the set as a unit.

Kind "bucket" (blob storage) copies the app's objects to a scratch emptyDir with the app's own bucket key, then restic
backs the directory up. Admin restore runs (`trigger == "restore"`, `enqueue_restore`) reuse the same queue/Job/Secret
lifecycle: the run row belongs to the TARGET app and points at the source run whose snapshot is restored.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import audit, config, crypto, k8s_manifests, models, platform_settings, resources
from app.providers.backup import BackupJobSpec, BackupRunner, BackupRunnerError

log = logging.getLogger(__name__)

KINDS = ("db", "bucket", "volume")
KIND_LABEL = {"db": "database", "volume": "volume", "bucket": "blob storage"}
RESTORABLE = ("db", "bucket")  # admin restore (volume restore is phase 2)
DOWNLOADABLE = ("db", "bucket", "volume")  # per-run download and delete
SCHEDULES = ("daily", "weekly", "off")
ACTIVE = ("queued", "running")
FINAL_MAX_AGE = timedelta(hours=2)  # a final (pre-delete) backup that is still not done after this no longer blocks deletion
STALE_DAILY = timedelta(hours=36)
STALE_WEEKLY = timedelta(days=8)
EVENT_KEPT = 20
INTERRUPTED_MSG = "interrupted (platform restart or job lost)"
TENANT_SWEEP_DAYS = 30  # how long after a volume backup the app's namespace is still checked for a leftover run Secret


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
    if len(config.BACKUP_BUCKET) >= 4:
        t = t.replace(config.BACKUP_BUCKET, "<bucket>")  # an error text may quote the bucket name
    t = re.sub(r"(?<![\w:])/(?:[\w.@+-]+/)+[\w.@+-]*", "<path>", t)  # absolute file paths inside the Job or platform
    t = re.sub(r"(?s)Traceback \(most recent call last\).*", "an internal error", t)
    return " ".join(t.split())[:limit]


def volume_backups_on() -> bool:
    """Volume backups run in the tenant namespace with the backup key in a Secret there, so they need the admin's
    BACKUP_KEY_SCOPED=1 (the key is limited to the backup bucket) on top of a usable backup feature."""
    return usable() and config.BACKUP_KEY_SCOPED


def has_volume(app: models.App) -> bool:
    return bool(config.VOLUMES_ENABLED and app.volume_size_gi)


def eligible_kinds(app: models.App) -> list[str]:
    """Kinds this app has data for AND can back up now (a volume only while volume backups are on)."""
    return (["db"] if app.database is not None else []) + (["bucket"] if app.storage is not None else []) \
        + (["volume"] if has_volume(app) and volume_backups_on() else [])


def volume_backup_note(app: models.App) -> Optional[str]:
    """Why an app's volume is NOT part of its backups (None = it is, or there is no volume)."""
    if has_volume(app) and not volume_backups_on():
        return config.VOLUME_BACKUP_NEEDS_KEY_MSG
    return None


def wants_backups(app: models.App) -> bool:
    """True when the app has (or asked for) a database, blob storage or volume, i.e. something worth backing up."""
    return bool(app.want_db or app.want_storage or app.volume_size_gi)


def default_enabled(app: models.App, session: Optional[Session] = None) -> bool:
    """Initial value of the per-app "scheduled backups" setting: the admin setting `backups_enabled_default` (else
    BACKUP_DEFAULT_ENABLED, off), and only for apps with something to back up. Existing rows keep their value; manual
    backups work either way."""
    return bool(platform_settings.get(session, "backups_enabled_default")) and wants_backups(app)


ADMIN_DISABLED_MSG = "Backups for this app were disabled by a platform admin."
ADMIN_DISABLED_KEPT = " Existing backups are kept."


def _mb(n: int) -> str:
    return f"{n / 1024**2:.0f} MB" if n < 1024**3 else f"{n / 1024**3:.1f} GB"


def app_cap_bytes(session: Session, cfg: Optional[models.AppBackupConfig]) -> int:
    """This app's backup storage ceiling: the admin's per-app override, else the platform default."""
    if cfg is not None and cfg.max_bytes_override:
        return int(cfg.max_bytes_override)
    return int(platform_settings.get(session, "backup_max_bytes_per_app"))


def total_cap_bytes(session: Session) -> int:
    return int(platform_settings.get(session, "backup_max_bytes_total"))


def total_repo_bytes(session: Session) -> int:
    return int(session.execute(select(func.coalesce(func.sum(
        models.AppBackupConfig.db_repo_bytes + models.AppBackupConfig.vol_repo_bytes
        + models.AppBackupConfig.bucket_repo_bytes), 0))).scalar() or 0)


def app_full_msg(session: Session, cfg: Optional[models.AppBackupConfig]) -> str:
    return f"Backup storage for this app is full ({_mb(repo_bytes(cfg))} of {_mb(app_cap_bytes(session, cfg))})."


PLATFORM_FULL_MSG = "Backup storage for the platform is full; new scheduled backups are paused until an administrator frees space."


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
        cfg = models.AppBackupConfig(app_id=app.id, enabled=default_enabled(app, session), schedule="daily",
                                     next_backup_at=_now(now))  # first backup right away, then the daily slot
        session.add(cfg)
        session.commit()
    return cfg


def lock_app(session: Session, app: models.App) -> models.AppBackupConfig:
    """Serialise enqueue paths per app: `SELECT ... FOR UPDATE` on the app's AppBackupConfig row, held until the caller commits.
    Call BEFORE the "anything active?" and rate-limit checks. The open transaction is committed first so InnoDB's
    REPEATABLE READ snapshot starts after the lock is won and the checks see what the other request just committed.
    SQLite ignores FOR UPDATE but serialises writers; the checks after the lock are still correct on their own."""
    ensure_config(session, app)
    session.commit()
    cfg = session.execute(select(models.AppBackupConfig).where(models.AppBackupConfig.app_id == app.id)
                          .with_for_update().execution_options(populate_existing=True)).scalar_one()
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
        select(models.BackupRun).where(models.BackupRun.app_id == app_id, models.BackupRun.status.in_(ACTIVE),
                                       models.BackupRun.trigger != "export")  # a download never blocks a backup
        .order_by(models.BackupRun.id).limit(1)
    ).scalar_one_or_none()


def enqueue(session: Session, app: models.App, kind: str, trigger: str, now: Optional[datetime] = None,
            set_id: Optional[int] = None) -> models.BackupRun:
    run = models.BackupRun(app_id=app.id, kind=kind, trigger=trigger, status="queued", created_at=_now(now), set_id=set_id)
    session.add(run)
    session.flush()
    return run


def enqueue_set(session: Session, app: models.App, kinds: list[str], trigger: str,
                now: Optional[datetime] = None) -> tuple[models.BackupSet, list[models.BackupRun]]:
    """ONE backup: a set plus one queued run per kind (taken one after another by the tick, not at one instant)."""
    st = models.BackupSet(app_id=app.id, trigger=trigger, created_at=_now(now))
    session.add(st)
    session.flush()
    return st, [enqueue(session, app, kind, trigger, now, set_id=st.id) for kind in kinds]


# --- sets -------------------------------------------------------------------------------------

def set_parts(session: Session, st: models.BackupSet) -> list[models.BackupRun]:
    return list(session.execute(select(models.BackupRun).where(
        models.BackupRun.set_id == st.id, models.BackupRun.trigger.in_(BACKUP_TRIGGERS), models.BackupRun.status != "deleted")
        .order_by(models.BackupRun.id)).scalars())


def set_status(st: models.BackupSet, parts: list[models.BackupRun]) -> str:
    """running | succeeded | partial | failed | deleting | deleted. `parts` excludes deleted runs."""
    if st.delete_status == "deleted" or not parts:
        return "deleted"
    if st.delete_status == "deleting" or any(p.status == "deleting" for p in parts):
        return "deleting"
    if any(p.status in ACTIVE for p in parts):
        return "running"
    ok = [p for p in parts if p.status == "succeeded"]
    if len(ok) == len(parts):
        return "succeeded"
    return "partial" if ok else "failed"


def set_finished_at(parts: list[models.BackupRun]) -> Optional[datetime]:
    if not parts or any(p.status in ACTIVE for p in parts):
        return None
    done = [p.finished_at for p in parts if p.finished_at is not None]
    return max(done) if done else None


def set_size(parts: list[models.BackupRun]) -> int:
    """New data this backup stored, summed over its succeeded parts (restic deduplicates, so later backups add little)."""
    return sum(p.bytes_added or 0 for p in parts if p.status == "succeeded")


def listed_sets(session: Session, app_id: int, limit: int) -> list[tuple[models.BackupSet, list[models.BackupRun]]]:
    """The app's backups, newest first, with their (non-deleted) parts. Deleted sets are not listed."""
    sets = list(session.execute(select(models.BackupSet).where(
        models.BackupSet.app_id == app_id, models.BackupSet.delete_status.is_distinct_from("deleted"))
        .order_by(models.BackupSet.created_at.desc(), models.BackupSet.id.desc()).limit(limit)).scalars())
    if not sets:
        return []
    by_set: dict[int, list[models.BackupRun]] = {st.id: [] for st in sets}
    for r in session.execute(select(models.BackupRun).where(
            models.BackupRun.set_id.in_(list(by_set)), models.BackupRun.trigger.in_(BACKUP_TRIGGERS),
            models.BackupRun.status != "deleted").order_by(models.BackupRun.id)).scalars():
        by_set[r.set_id].append(r)
    return [(st, by_set[st.id]) for st in sets if by_set[st.id]]


def set_tag(run: models.BackupRun) -> str:
    return f" set={run.set_id}" if run.set_id else ""


def manual_cooldown(session: Session, app_id: int, now: Optional[datetime] = None) -> Optional[int]:
    """Seconds until another on-demand backup is allowed (None = allowed now): `backup_manual_per_hour` per app per hour."""
    now = _now(now)
    limit = int(platform_settings.get(session, "backup_manual_per_hour"))
    recent = sorted(_naive(c) for c in session.execute(select(models.BackupSet.created_at).where(
        models.BackupSet.app_id == app_id, models.BackupSet.trigger == "manual",
        models.BackupSet.created_at > now - timedelta(hours=1))).scalars())
    if len(recent) < limit:
        return None
    return int((recent[len(recent) - limit] + timedelta(hours=1) - now).total_seconds()) + 1


USER_MANUAL_FACTOR = 5  # one account may start at most this many times the per-app limit per hour across all its apps


def user_manual_cooldown(session: Session, owner_id: int, now: Optional[datetime] = None) -> Optional[int]:
    """The same hourly limit per account, across its apps (so many apps can't be used to multiply the manual rate)."""
    now = _now(now)
    limit = int(platform_settings.get(session, "backup_manual_per_hour")) * USER_MANUAL_FACTOR
    recent = sorted(_naive(c) for c in session.execute(
        select(models.BackupSet.created_at).join(models.App, models.App.id == models.BackupSet.app_id).where(
            models.App.owner_id == owner_id, models.BackupSet.trigger == "manual",
            models.BackupSet.created_at > now - timedelta(hours=1))).scalars())
    if len(recent) < limit:
        return None
    return int((recent[len(recent) - limit] + timedelta(hours=1) - now).total_seconds()) + 1


# --- results ----------------------------------------------------------------------------------

def prefix(kind: str) -> str:
    """Column prefix on AppBackupConfig for a kind (db_*, vol_*, bucket_*)."""
    return {"db": "db", "bucket": "bucket"}.get(kind, "vol")


_prefix = prefix


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
    if not ok and run.trigger == "export" and "repository is not configured" in (run.error or "").lower():
        run.error = ("The backup tooling on the platform is older than this download needs. An administrator has to update it; "
                     "try again later.")
    if ok and run.trigger == "export":
        run.bytes_added = _int(result.get("bytes"))
    elif ok and run.trigger == "prune":
        run.repo_bytes = _int(result.get("repo_bytes"))
    elif ok and run.trigger not in ("purge", "restore", "delete"):
        run.snapshot_id = str(result.get("snapshot_id") or "")[:64] or None
        run.bytes_added = _int(result.get("bytes_added"))
        run.repo_bytes = _int(result.get("repo_bytes"))
    cfg = session.get(models.AppBackupConfig, run.app_id)
    if run.trigger in ("restore", "export"):
        pass  # neither changes anything about the app's own backup summary
    elif run.trigger == "prune":
        if ok and cfg is not None and run.repo_bytes is not None:
            setattr(cfg, f"{_prefix(run.kind)}_repo_bytes", run.repo_bytes)
    elif run.trigger == "delete":
        _finish_delete(session, run, cfg, ok=ok, result=result, now=now)
    elif cfg is not None and run.trigger == "purge":
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
    if not ok and run.trigger == "delete":
        _event(session, run.app_id, run.id,
               f"Deleting your {KIND_LABEL.get(run.kind, run.kind)} backup failed: {run.error}. The backup is still there.", now)
    elif not ok and run.trigger not in ("purge", "restore", "export", "prune"):
        _event(session, run.app_id, run.id,
               f"Backup of your {KIND_LABEL.get(run.kind, run.kind)} failed: {run.error}. "
               "See the Backups card on the app page for details.", now)
    if ok and run.kind == "volume" and run.trigger in BACKUP_TRIGGERS:
        # Retention prune never runs inside the tenant namespace (restic prune is the memory-hungry part): queue it here, in the
        # platform namespace, now that the volume Job is done with the repo.
        enqueue(session, models_app(session, run), "volume", "prune", now)
    session.commit()
    if runner is not None and run.job_name:
        try:
            runner.cleanup(run.job_name, run_namespace(run))
        except Exception as exc:  # noqa: BLE001 - the Secret sweep retries
            log.warning("backup run %s: cleanup failed (%s)", run.id, type(exc).__name__)
    detail = f"{run.kind} {run.trigger}{set_tag(run)}" + ("" if ok else f": {run.error}")
    if run.trigger == "prune":
        audit.record(session, None, "backup.pruned" if ok else "backup.prune_failed", "app", run.app_id,
                     f"run={run.id} kind={run.kind} keep=daily:{config.BACKUP_KEEP_DAILY},weekly:{config.BACKUP_KEEP_WEEKLY},"
                     f"monthly:{config.BACKUP_KEEP_MONTHLY}" + ("" if ok else f": {run.error}"), actor_email="system")
        return
    if run.trigger == "purge":
        audit.record(session, None, "backup.purged", "app", run.app_id,
                     f"run={run.id} kind={run.kind} result={'succeeded' if ok else 'failed'}" + ("" if ok else f": {run.error}"),
                     actor_email="system")
        return
    if run.trigger == "delete":
        src = session.get(models.BackupRun, run.source_run_id) if run.source_run_id else None
        audit.record(session, None, "backup.deleted" if ok else "backup.delete_failed", "app", run.app_id,
                     f"run={run.source_run_id} kind={run.kind} snapshot={((src.snapshot_id if src else None) or '')[:8]} delete_run={run.id}{set_tag(run)}"
                     + ("" if ok else f": {run.error}"), actor_email="system")
        return
    if run.trigger == "export":
        audit.record(session, None, "backup.export_succeeded" if ok else "backup.export_failed", "app", run.app_id,
                     f"run={run.id} from_run={run.source_run_id} {detail}", actor_email="system")
        return
    if run.trigger == "restore":
        audit.record(session, None, "backup.restore_succeeded" if ok else "backup.restore_failed", "app", run.app_id,
                     f"run={run.id} from_run={run.source_run_id} {detail}", actor_email="system")
        return
    audit.record(session, None, "backup.succeeded" if ok else "backup.failed", "app", run.app_id,
                 f"run={run.id} {detail}", actor_email="system")
    if ok and run.kind != "volume":  # db/bucket Jobs end with `forget --prune` (a volume's prune is its own "prune" run)
        audit.record(session, None, "backup.pruned", "app", run.app_id,
                     f"run={run.id} kind={run.kind} keep=daily:{config.BACKUP_KEEP_DAILY},weekly:{config.BACKUP_KEEP_WEEKLY},"
                     f"monthly:{config.BACKUP_KEEP_MONTHLY}", actor_email="system")


BACKUP_TRIGGERS = ("scheduled", "manual", "final")


def run_namespace(run: models.BackupRun) -> Optional[str]:
    """Namespace of a run's Job: the app's own for a volume backup, else None (the platform backup namespace)."""
    return k8s_manifests.namespace_name(run.app_id) if run.kind == "volume" and run.trigger in BACKUP_TRIGGERS else None


def models_app(session: Session, run: models.BackupRun) -> models.App:
    app = session.get(models.App, run.app_id)
    assert app is not None
    return app


def _finish_delete(session: Session, run: models.BackupRun, cfg: Optional[models.AppBackupConfig], *, ok: bool,
                   result: dict[str, Any], now: datetime) -> None:
    """End of a delete run. Failure: the source run is a normal succeeded backup again. Success: the source row is kept with
    status `deleted` (so restore/export `source_run_id` history still resolves) but never listed or usable; the kind's
    repo size comes from the Job's result, and when the deleted run was the kind's latest backup the summary is recomputed."""
    src = session.get(models.BackupRun, run.source_run_id) if run.source_run_id else None
    if src is None:
        return
    if not ok:
        if src.status == "deleting":
            src.status = "succeeded"
        _settle_set_delete(session, run.set_id)
        return
    src.status = "deleted"
    _settle_set_delete(session, run.set_id)
    if cfg is None:
        return
    p = _prefix(src.kind)
    size = _int(result.get("repo_bytes")) if "repo_bytes" in result else None
    if size is not None:
        setattr(cfg, f"{p}_repo_bytes", size)
    if src.finished_at is not None and getattr(cfg, f"{p}_last_backup_at") == src.finished_at:
        session.flush()
        _recompute_summary(session, cfg, src.app_id, src.kind)


def _settle_set_delete(session: Session, set_id: Optional[int]) -> None:
    """When the last per-part delete of a set ends: the set is `deleted` if every part is gone, else it is a normal set again
    (with the parts that could not be deleted) and the failed delete has already raised its own event."""
    st = session.get(models.BackupSet, set_id) if set_id else None
    if st is None or st.delete_status != "deleting":
        return
    session.flush()
    if session.execute(select(models.BackupRun.id).where(
            models.BackupRun.set_id == st.id, models.BackupRun.trigger == "delete", models.BackupRun.status.in_(ACTIVE)).limit(1)).first():
        return
    st.delete_status = "deleted" if not set_parts(session, st) else None


def _latest_backup(session: Session, app_id: int, kind: Optional[str] = None) -> Optional[models.BackupRun]:
    q = select(models.BackupRun).where(models.BackupRun.app_id == app_id, models.BackupRun.trigger.in_(BACKUP_TRIGGERS),
                                       models.BackupRun.status.in_(("succeeded", "failed")),
                                       models.BackupRun.finished_at.is_not(None))
    if kind is not None:
        q = q.where(models.BackupRun.kind == kind)
    return session.execute(q.order_by(models.BackupRun.finished_at.desc(), models.BackupRun.id.desc()).limit(1)).scalar_one_or_none()


def _recompute_summary(session: Session, cfg: models.AppBackupConfig, app_id: int, kind: str) -> None:
    """Per-kind and app-wide last-backup fields from the runs that remain (a deleted run no longer counts)."""
    p = _prefix(kind)
    last = _latest_backup(session, app_id, kind)
    ok = session.execute(select(models.BackupRun).where(
        models.BackupRun.app_id == app_id, models.BackupRun.kind == kind, models.BackupRun.trigger.in_(BACKUP_TRIGGERS),
        models.BackupRun.status == "succeeded").order_by(models.BackupRun.finished_at.desc(), models.BackupRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    setattr(cfg, f"{p}_last_backup_at", last.finished_at if last else None)
    setattr(cfg, f"{p}_last_status", last.status if last else None)
    setattr(cfg, f"{p}_last_error", last.error if last else None)
    setattr(cfg, f"{p}_last_bytes", ok.bytes_added if ok else None)
    top = _latest_backup(session, app_id)
    cfg.last_backup_at, cfg.last_status, cfg.last_error = (top.finished_at, top.status, top.error) if top else (None, None, None)
    best = session.execute(select(func.max(models.BackupRun.finished_at)).where(
        models.BackupRun.app_id == app_id, models.BackupRun.trigger.in_(BACKUP_TRIGGERS),
        models.BackupRun.status == "succeeded")).scalar()
    cfg.last_success_at = best


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
    _ensure_egress(runner)
    _poll(session, runner, now)
    try:
        swept = runner.sweep({r.job_name for r in _running(session) if r.job_name})
        if swept:
            log.warning("backup: removed %d leftover job secret(s)", swept)
    except Exception as exc:  # noqa: BLE001
        log.warning("backup: secret sweep failed (%s)", type(exc).__name__)
    _sweep_tenant_secrets(session, runner, now)
    _sweep_exports(runner, now, session)
    _enforce_retention(session, runner, now)
    _schedule(session, now)
    _enqueue_purges(session, now)
    _start_queued(session, runner, now)


def _ensure_egress(runner: BackupRunner) -> None:
    """Keep the namespace's NetworkPolicy for the Spaces endpoint's private VPC IPs current. Never raises."""
    try:
        runner.ensure_spaces_egress(config.backup_endpoint())
    except Exception as exc:  # noqa: BLE001
        log.warning("backup: could not update the Spaces egress policy (%s)", type(exc).__name__)


def tenant_app_ids(session: Session, now: datetime) -> list[int]:
    """Live apps that had a volume backup in the last TENANT_SWEEP_DAYS days: the only tenant namespaces a backup object can be in."""
    return list(session.execute(select(models.BackupRun.app_id).join(models.App, models.App.id == models.BackupRun.app_id).where(
        models.BackupRun.kind == "volume", models.BackupRun.trigger.in_(BACKUP_TRIGGERS), models.App.deleted_at.is_(None),
        models.BackupRun.created_at >= now - timedelta(days=TENANT_SWEEP_DAYS)).distinct()).scalars().all())


def _sweep_tenant_secrets(session: Session, runner: BackupRunner, now: datetime) -> None:
    """Volume backups keep their per-run Secret (with the backup key) in the app's namespace: sweep a leftover one there too
    (apps with a volume backup in the last SWEEP_DAYS days; a deleted app's namespace is gone). Not gated on BACKUP_KEY_SCOPED:
    turning the gate off again must still clean up what an earlier run left behind. Never raises."""
    try:
        # Only live apps: a deleted app's namespace is gone (and the reconciler's role may not read it any more).
        ids = session.execute(select(models.BackupRun.app_id).join(models.App, models.App.id == models.BackupRun.app_id).where(
            models.BackupRun.kind == "volume", models.BackupRun.trigger.in_(BACKUP_TRIGGERS), models.App.deleted_at.is_(None),
            models.BackupRun.created_at >= now - timedelta(days=TENANT_SWEEP_DAYS)).distinct()).scalars().all()
        if ids:
            runner.sweep_namespaces([k8s_manifests.namespace_name(i) for i in ids],
                                    {r.job_name for r in _running(session) if r.job_name})
    except Exception as exc:  # noqa: BLE001
        log.warning("backup: tenant secret sweep failed (%s)", type(exc).__name__)


_last_export_sweep: list[datetime] = []


def _audit_export_deletes(session: Session, keys: list[str]) -> None:
    """One `backup.export_deleted` row per swept object, mapped back to its export run by `export_key`."""
    rows = session.execute(select(models.BackupRun).where(models.BackupRun.trigger == "export",
                                                          models.BackupRun.export_key.in_(list(keys)))).scalars().all()
    for run in rows:
        audit.record(session, None, "backup.export_deleted", "app", run.app_id,
                     f"export={run.id} run={run.source_run_id}{set_tag(run)} reason=expired", actor_email="system")


def _sweep_exports(runner: BackupRunner, now: datetime, session: Optional[Session] = None) -> None:
    """Delete export objects older than the TTL (at most every 5 minutes). Never raises."""
    if _last_export_sweep and now - _last_export_sweep[0] < timedelta(minutes=5):
        return
    _last_export_sweep[:] = [now]
    try:
        keys = runner.sweep_exports(config.BACKUP_EXPORT_TTL_SECONDS)
        if keys:
            log.info("backup: removed %d expired export object(s)", len(keys))
            if session is not None:
                _audit_export_deletes(session, keys)
    except Exception as exc:  # noqa: BLE001
        log.warning("backup: export sweep failed (%s)", type(exc).__name__)


def _running(session: Session) -> list[models.BackupRun]:
    return list(session.execute(select(models.BackupRun).where(models.BackupRun.status == "running")).scalars())


def _poll(session: Session, runner: BackupRunner, now: datetime) -> None:
    # Kubernetes fails the Job at activeDeadlineSeconds; this is the backstop if it never reports that (a hung run must
    # not hold a concurrency slot or the per-server dump slot).
    grace = timedelta(seconds=config.BACKUP_JOB_DEADLINE_SECONDS + 120)
    for run in _running(session):
        try:
            st = runner.status(run.job_name or "", run_namespace(run))
        except Exception as exc:  # noqa: BLE001 - API blip: look again next tick
            log.warning("backup run %s: status check failed (%s)", run.id, type(exc).__name__)
            continue
        if st.state == "succeeded":
            _finish(session, run, runner, ok=True, result=st.result, now=now)
        elif st.state == "failed":
            _finish(session, run, runner, ok=False, error=st.error, now=now)
        elif st.state == "missing":
            _finish(session, run, runner, ok=False, error=INTERRUPTED_MSG, now=now)
        elif run.started_at is not None and now - _naive(run.started_at) > grace:
            _finish(session, run, runner, ok=False, error="the backup took too long and was stopped", now=now)


def _schedule(session: Session, now: datetime) -> None:
    """Give every app with something to back up a config row (only when scheduled backups are on by default; otherwise
    rows appear when the owner opts in or runs a manual backup), then queue a run for each enabled app that is due."""
    have = set(session.execute(select(models.AppBackupConfig.app_id)).scalars())
    for app in session.execute(
        select(models.App).where(models.App.deleted_at.is_(None))
    ).scalars():
        if app.id not in have and default_enabled(app, session) and eligible_kinds(app):
            ensure_config(session, app, now)
    due = session.execute(
        select(models.AppBackupConfig, models.App).join(models.App, models.App.id == models.AppBackupConfig.app_id)
        .where(models.App.deleted_at.is_(None), models.AppBackupConfig.enabled.is_(True),
               models.AppBackupConfig.schedule != "off", models.AppBackupConfig.next_backup_at <= now,
               models.AppBackupConfig.admin_disabled.is_(False))
    ).all()
    total_full = bool(due) and total_repo_bytes(session) >= total_cap_bytes(session)
    for cfg, app in due:
        kinds = eligible_kinds(app)
        if not kinds or active_run(session, app.id) is not None:
            continue
        reason = PLATFORM_FULL_MSG if total_full else (app_full_msg(session, cfg) if repo_bytes(cfg) >= app_cap_bytes(session, cfg) else None)
        if reason is not None:  # skipped, with the reason recorded where the owner and the admin Health page can see it
            record_skip(session, app, kinds, "scheduled", reason, now)
            cfg.next_backup_at = compute_next(app.id, now, cfg.schedule)
            session.commit()
            continue
        st, _ = enqueue_set(session, app, kinds, "scheduled", now)
        cfg.next_backup_at = compute_next(app.id, now, cfg.schedule)
        session.commit()
        audit.record(session, None, "backup.create", "app", app.id,
                     f"trigger=scheduled set={st.id} kinds=" + ",".join(kinds), actor_email="system")
    session.commit()


def record_skip(session: Session, app: models.App, kinds: list[str], trigger: str, reason: str, now: datetime) -> models.BackupSet:
    """A backup that was not started: a failed set carrying `reason` (the owner's Backups card and the admin Health page show
    it; failed backups are the first to roll off)."""
    st, runs = enqueue_set(session, app, kinds, trigger, now)
    for r in runs:
        _finish(session, r, None, ok=False, error=reason, now=now)
    return st


# --- retention: how many backups an app holds, and rolling off to stay under the ceiling ---------

def rolloff_order(items: list[tuple[models.BackupSet, str]]) -> list[models.BackupSet]:
    """Which backups go first. `items` = (set, status) for the app's deletable sets. Order: failed and partial backups (oldest
    first), THEN succeeded ones (oldest first). The newest succeeded backup is never in the list, so a roll-off can't leave an
    app with nothing to restore. Running / deleting sets are not candidates."""
    key = lambda st: (_naive(st.created_at), st.id)  # noqa: E731
    bad = sorted((st for st, status in items if status in ("failed", "partial")), key=key)
    ok = sorted((st for st, status in items if status == "succeeded"), key=key)
    return bad + ok[:-1]


def rolloff_candidates(session: Session, app_id: int) -> list[models.BackupSet]:
    sets = session.execute(select(models.BackupSet).where(
        models.BackupSet.app_id == app_id, models.BackupSet.delete_status.is_(None))).scalars().all()
    return rolloff_order([(st, set_status(st, set_parts(session, st))) for st in sets])


def held_count(session: Session, app_id: int) -> int:
    return int(session.execute(select(func.count(models.BackupSet.id)).where(
        models.BackupSet.app_id == app_id, models.BackupSet.delete_status.is_(None))).scalar() or 0)


def roll_off(session: Session, app: models.App, runner: BackupRunner, want: int, reason: str,
             now: Optional[datetime] = None) -> int:
    """Delete up to `want` backups in rolloff order through the owner-delete machinery (forget --prune Jobs, so storage is really
    freed). Failed backups hold nothing and vanish at once; stops after the first backup that needs a Job (the repo is then busy).
    Returns how many backups were put on the way out. Never raises."""
    done = 0
    for st in rolloff_candidates(session, app.id):
        if done >= want:
            break
        try:
            runs, _exports = enqueue_delete_set(session, app, st, runner, now)
        except DeleteRefused:
            session.rollback()
            continue
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            log.warning("backup: roll-off for app %s failed (%s)", app.id, type(exc).__name__)
            break
        session.commit()
        done += 1
        audit.record(session, None, "backup.rolled_off", "app", app.id,
                     f"set={st.id} reason={reason} runs={','.join(str(r.id) for r in runs) or '-'}", actor_email="system")
        if runs:
            break
    return done


def _enforce_retention(session: Session, runner: BackupRunner, now: datetime) -> None:
    """Per tick: apps that hold more backups than `backup_max_held_per_app`, or are at their storage ceiling, lose their next
    backup(s) in rolloff order. Skips apps with a backup in flight. Never raises."""
    try:
        max_held = int(platform_settings.get(session, "backup_max_held_per_app"))
        held = dict(session.execute(select(models.BackupSet.app_id, func.count(models.BackupSet.id)).where(
            models.BackupSet.delete_status.is_(None)).group_by(models.BackupSet.app_id)).all())
        rows = session.execute(select(models.AppBackupConfig, models.App).join(
            models.App, models.App.id == models.AppBackupConfig.app_id).where(
            models.App.deleted_at.is_(None), models.AppBackupConfig.purged_at.is_(None))).all()
        for cfg, app in rows:
            excess = held.get(app.id, 0) - max_held
            full = repo_bytes(cfg) >= app_cap_bytes(session, cfg)
            if excess <= 0 and not full:
                continue
            if active_run(session, app.id) is not None:
                continue
            roll_off(session, app, runner, max(excess, 1), "max_held" if excess > 0 else "storage_full", now)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.warning("backup: retention pass failed (%s)", type(exc).__name__)


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
        kinds = [k for k, p in (("db", "db"), ("volume", "vol"), ("bucket", "bucket"))
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
    busy_servers = _db_servers(session, [r for r in running if r.kind == "db" and r.trigger != "export"])
    total = total_repo_bytes(session)
    total_cap = total_cap_bytes(session)
    for run in queued:
        if len(running) >= max(1, config.BACKUP_MAX_CONCURRENT):
            break
        app = session.get(models.App, run.app_id)
        cfg = session.get(models.AppBackupConfig, run.app_id)
        restore = run.trigger == "restore"
        export = run.trigger == "export"
        if run.kind == "package" and any(r.kind == "package" for r in running):
            continue  # a package's scratch is the biggest Job there is: one at a time (deploy/platform/backup.yaml quota)
        if app is None or (cfg is None and not restore and not export):
            _finish(session, run, None, ok=False, error="the app no longer exists", now=now)
            continue
        if app.deleted_at is not None and run.trigger not in ("final", "purge", "prune"):
            _finish(session, run, None, ok=False, error="the app was deleted", now=now)
            continue
        server_id = app.database.db_server_id if app.database is not None else None
        # one dump or restore at a time per DB server (connection budget)
        if run.kind == "db" and run.trigger not in ("purge", "export", "delete"):
            if server_id is None:
                _finish(session, run, None, ok=False, error="the app has no database", now=now)
                continue
            if server_id in busy_servers:
                continue
        if run.kind in KINDS and run.trigger in BACKUP_TRIGGERS:
            if cfg.admin_disabled and run.trigger in ("scheduled", "manual"):
                _finish(session, run, None, ok=False, error=ADMIN_DISABLED_MSG, now=now)
                continue
            if repo_bytes(cfg) >= app_cap_bytes(session, cfg):
                _finish(session, run, None, ok=False, error=app_full_msg(session, cfg), now=now)
                continue
            if total >= total_cap:
                _finish(session, run, None, ok=False, error=PLATFORM_FULL_MSG, now=now)
                continue
            if run.kind == "bucket" and app.storage is not None and (app.storage.size_bytes or 0) > config.BACKUP_BUCKET_MAX_BYTES:
                _finish(session, run, None, ok=False,
                        error=f"the bucket is larger than the {config.BACKUP_BUCKET_MAX_BYTES // 1024**2} MB a backup can copy", now=now)
                continue
        try:
            if export:
                env = _build_package_env(session, run) if run.kind == "package" else _build_export_env(session, run)
            elif restore:
                env = _build_restore_env(session, run, app, now)
            elif run.trigger == "prune":
                env = _build_prune_env(session, app, cfg, run)
            else:
                env = _build_env(session, app, cfg, run)
            volume = _volume_job(app) if run.kind == "volume" and run.trigger in BACKUP_TRIGGERS else None
            spec = BackupJobSpec(name=f"bk-{run.id}", run_id=run.id, app_id=app.id, kind=run.kind, env=env,
                                 scratch_mib=_scratch_mib(session, run), namespace=run_namespace(run), volume=volume)
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


_MIB = 1024**2


def _scratch_mib(session: Session, run: models.BackupRun) -> int:
    if run.trigger == "export" and run.kind == "package":  # each part, compressed, before they are tarred and streamed out
        mib = 64
        for kind in {p.kind for p in _package_parts(session, session.get(models.BackupSet, run.set_id))} if run.set_id else ():
            mib += {"db": 256, "bucket": 2 * (config.BACKUP_BUCKET_MAX_BYTES // _MIB) + 64,
                    "volume": config.BACKUP_VOLUME_EXPORT_MAX_BYTES // _MIB + 64}.get(kind, 0)
        return mib
    if run.trigger == "export":  # bucket: the restored tree plus the tarball; db: the compressed dump; volume: the gzipped tar
        if run.kind == "volume":
            return config.BACKUP_VOLUME_EXPORT_MAX_BYTES // _MIB + 64
        return 2 * (config.BACKUP_BUCKET_MAX_BYTES // _MIB) + 64 if run.kind == "bucket" else 256
    if run.kind != "bucket" or run.trigger in ("purge", "delete", "prune"):
        return 0
    return config.BACKUP_BUCKET_MAX_BYTES // _MIB + 64


def _volume_job(app: models.App) -> dict[str, Any]:
    """Pod settings of a volume backup Job (app/providers/backup.py job_manifests). Raises ValueError (safe text) when it
    must not run: gate off, no volume, or the claim is not bound yet (the Job would wait for it until its deadline)."""
    if not volume_backups_on():
        raise ValueError(config.VOLUME_BACKUP_NEEDS_KEY_MSG)
    if not has_volume(app):
        raise ValueError("the app has no volume")
    if app.volume_phase != "Bound":
        raise ValueError("the volume isn't ready yet")
    suspended = app.status == "suspended"
    return {
        "claim": k8s_manifests.pvc_name(app.id),
        "uid": app.run_as_uid or config.VOLUME_FS_GROUP,  # the app's own UID reads what that UID wrote
        "fs_group": config.VOLUME_FS_GROUP,
        # RWO volume: with the app running the Job must land on the app pod's node; scaled to zero the volume is detached, so no affinity
        "app_pod_label": None if suspended else k8s_manifests._resource_name(app.id),
        **k8s_manifests.backup_pod_resources(resources.effective(app)),
    }


def _db_servers(session: Session, runs: list[models.BackupRun]) -> set[int]:
    out: set[int] = set()
    for r in runs:
        db = session.execute(select(models.AppDatabase.db_server_id).where(models.AppDatabase.app_id == r.app_id)).scalar()
        if db is not None:
            out.add(db)
    return out


def repo_base(app_id: int) -> str:
    return repo_url(app_id, "x").rsplit("/", 1)[0]


def _repo_env(session: Session, source_app_id: int, kind: str, cfg: models.AppBackupConfig) -> dict[str, str]:
    """Repository coordinates + the backup bucket's own key (never the app's)."""
    try:
        password = repo_password(cfg)
    except ValueError:
        raise ValueError("the stored backup password can't be read (platform key changed)") from None
    session.commit()  # persist a freshly generated repo password before any Job can write to the repo
    return {
        "RESTIC_REPOSITORY": repo_url(source_app_id, kind),
        "RESTIC_PASSWORD": password,
        "AWS_ACCESS_KEY_ID": config.BACKUP_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": config.BACKUP_SECRET_ACCESS_KEY,
        "AWS_DEFAULT_REGION": config.BACKUP_REGION or "us-east-1",
        "LIMIT_UPLOAD_KIB": str(config.BACKUP_UPLOAD_LIMIT_KIB),
    }


def _db_env(db: models.AppDatabase) -> dict[str, str]:
    """Connection settings for an app's own database role (the app's role, never the server admin's)."""
    if db.server is None:
        raise ValueError("the app has no database")
    pw = crypto.try_decrypt(db.password_encrypted)
    if pw is None:
        raise ValueError("the database credentials can't be read (platform key changed)")
    env = {
        "ENGINE": "pg" if db.server.engine == "pg" else "mysql",
        "DB_HOST": db.server.host, "DB_PORT": str(db.server.port), "DB_NAME": db.db_name,
        "DB_USER": db.db_user, "DB_PASSWORD": pw,
    }
    if db.server.ca_cert:
        env["DB_CA"] = db.server.ca_cert
    return env


def _bucket_env(st: models.AppStorage) -> dict[str, str]:
    """The app's own bucket key (the platform holds it; there is no separate read-only key per backup). Passed to the Job
    only through the per-run Secret, under names the script hands to s5cmd's environment alone."""
    sk = crypto.try_decrypt(st.secret_key_encrypted)
    if sk is None:
        raise ValueError("the storage credentials can't be read (platform key changed)")
    return {"SRC_ENDPOINT": st.endpoint, "SRC_BUCKET": st.bucket, "SRC_REGION": st.region or "us-east-1",
            "SRC_ACCESS_KEY": st.access_key, "SRC_SECRET_KEY": sk}


def _build_env(session: Session, app: models.App, cfg: models.AppBackupConfig, run: models.BackupRun) -> dict[str, str]:
    """The per-run Secret's contents. Raises ValueError with a safe message when a credential can't be read."""
    env = _repo_env(session, app.id, run.kind, cfg)
    if run.trigger == "delete":
        source = session.get(models.BackupRun, run.source_run_id) if run.source_run_id else None
        if source is None or source.app_id != app.id or not source.snapshot_id or cfg.purged_at is not None:
            raise ValueError("the backup to delete is gone")
        env.update({"MODE": "forget", "KIND": run.kind, "SNAPSHOT": source.snapshot_id, "TAG": "delete"})
        return env
    env.update({
        "MODE": "purge" if run.trigger == "purge" else "backup",
        "KIND": run.kind,
        "KEEP_DAILY": str(config.BACKUP_KEEP_DAILY),
        "KEEP_WEEKLY": str(config.BACKUP_KEEP_WEEKLY),
        "KEEP_MONTHLY": str(config.BACKUP_KEEP_MONTHLY),
        "TAG": run.trigger,
    })
    if run.trigger == "purge":
        return env
    if run.kind == "volume":
        # Runs in the app's namespace: the script backs up VOLUME_DIR (the read-only mount) and leaves retention to a prune run.
        env.update({"VOLUME_DIR": config.BACKUP_VOLUME_MOUNT, "LIMIT_UPLOAD_KIB": str(config.BACKUP_VOLUME_UPLOAD_LIMIT_KIB)})
    elif run.kind == "bucket":
        if app.storage is None:
            raise ValueError("the app has no blob storage")
        env.update(_bucket_env(app.storage))
    else:
        if app.database is None:
            raise ValueError("the app has no database")
        env.update(_db_env(app.database))
    return env


def _build_prune_env(session: Session, app: models.App, cfg: models.AppBackupConfig, run: models.BackupRun) -> dict[str, str]:
    """Retention for a repo whose backup Job ran in a tenant namespace: forget by policy + prune, in the platform namespace."""
    env = _repo_env(session, app.id, run.kind, cfg)
    env.update({"MODE": "prune", "KIND": run.kind, "KEEP_DAILY": str(config.BACKUP_KEEP_DAILY),
                "KEEP_WEEKLY": str(config.BACKUP_KEEP_WEEKLY), "KEEP_MONTHLY": str(config.BACKUP_KEEP_MONTHLY)})
    return env


# --- admin restore ----------------------------------------------------------------------------

class RestoreRefused(Exception):
    """Raised by enqueue_restore with a message that is safe to show to the admin."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def enqueue_restore(session: Session, source: models.BackupRun, target: models.App, overwrite_source: bool,
                    now: Optional[datetime] = None) -> models.BackupRun:
    """Queue a restore of `source` (a succeeded db/bucket run) into `target`'s database or bucket. Never restores over
    the source app unless overwrite_source is set AND that app is suspended (nothing is writing to it)."""
    if source.trigger in ("purge", "restore") or source.kind not in RESTORABLE:
        raise RestoreRefused("Only database and blob storage backups can be restored.")
    if source.status != "succeeded" or not source.snapshot_id:
        raise RestoreRefused("That backup run didn't finish successfully, so there is no snapshot to restore.")
    src_cfg = session.get(models.AppBackupConfig, source.app_id)
    if src_cfg is None or src_cfg.repo_password_enc is None or src_cfg.purged_at is not None:
        raise RestoreRefused("The repository for that backup has been purged.", 410)
    if target.id == source.app_id:
        if not overwrite_source:
            raise RestoreRefused("That would overwrite the app the backup came from. Restore into another app, "
                                 "or pass overwrite_source on a suspended app.", 409)
        if target.status != "suspended":
            raise RestoreRefused("Suspend the app first: restoring over the source app is only allowed while it is suspended.", 409)
    if source.kind == "db" and target.database is None:
        raise RestoreRefused("The target app has no database to restore into.")
    if source.kind == "bucket":
        if target.storage is None:
            raise RestoreRefused("The target app has no blob storage to restore into.")
        if target.storage.over_quota:
            raise RestoreRefused("The target app's bucket is over its quota (read-only). Free space first.")
    if active_run(session, target.id) is not None:
        raise RestoreRefused("A backup or restore is already queued or running for the target app.", 409)
    run = models.BackupRun(app_id=target.id, kind=source.kind, trigger="restore", status="queued",
                           created_at=_now(now), source_run_id=source.id)
    session.add(run)
    session.flush()
    return run


def _build_restore_env(session: Session, run: models.BackupRun, target: models.App, now: datetime) -> dict[str, str]:
    source = session.get(models.BackupRun, run.source_run_id) if run.source_run_id else None
    src_cfg = session.get(models.AppBackupConfig, source.app_id) if source is not None else None
    if source is None or src_cfg is None or not source.snapshot_id or src_cfg.purged_at is not None:
        raise ValueError("the backup to restore is gone")
    if source.app_id == target.id and target.status != "suspended":
        raise ValueError("the app is no longer suspended")
    env = _repo_env(session, source.app_id, source.kind, src_cfg)
    env.update({"MODE": "restore", "KIND": source.kind, "SNAPSHOT": source.snapshot_id, "TAG": "restore"})
    if source.kind == "bucket":
        if target.storage is None:
            raise ValueError("the target app has no blob storage")
        env.update(_bucket_env(target.storage))
    else:
        if target.database is None:
            raise ValueError("the target app has no database")
        env.update(_db_env(target.database))
    return env


# --- owner delete of one backup --------------------------------------------------------------

class DeleteRefused(Exception):
    """Raised by enqueue_delete with a message that is safe to show to the owner."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def _repo_busy(session: Session, app_id: int, kind: str) -> bool:
    """Any backup, restore (into this app, or from this repo into another app), export or delete queued/running that touches
    this app's repo of `kind`."""
    own = session.execute(select(models.BackupRun.id).where(
        models.BackupRun.app_id == app_id, models.BackupRun.kind == kind, models.BackupRun.status.in_(ACTIVE)).limit(1)).first()
    if own is not None:
        return True
    repo_runs = select(models.BackupRun.id).where(models.BackupRun.app_id == app_id, models.BackupRun.kind == kind)
    return session.execute(select(models.BackupRun.id).where(
        models.BackupRun.trigger == "restore", models.BackupRun.status.in_(ACTIVE),
        models.BackupRun.source_run_id.in_(repo_runs)).limit(1)).first() is not None


def enqueue_delete(session: Session, app: models.App, source: models.BackupRun, runner: BackupRunner,
                   now: Optional[datetime] = None) -> tuple[models.BackupRun, list[models.BackupRun]]:
    """Queue `restic forget <snapshot> --prune` for one succeeded db/bucket backup of `app`. Returns (delete run, exports
    removed). Takes the per-app lock first; the caller commits. The source goes to status `deleting` until the Job ends.
    A ready (finished, unexpired) export of this run is deleted from staging and marked `expired` so a download link can't
    outlive its backup; a queued or running export is a 409 (wait for it)."""
    now = _now(now)
    cfg = lock_app(session, app)
    session.refresh(source)
    if cfg.repo_password_enc is None or cfg.purged_at is not None:
        raise DeleteRefused("The repository for that backup has been purged.", 410)
    if source.status == "deleting":
        raise DeleteRefused("That backup is already being deleted.", 409)
    if (source.app_id != app.id or source.trigger not in BACKUP_TRIGGERS or source.kind not in DOWNLOADABLE
            or source.status != "succeeded" or not source.snapshot_id):
        raise DeleteRefused("Only a finished database, blob storage or volume backup can be deleted.")
    if _repo_busy(session, app.id, source.kind):
        raise DeleteRefused(f"A backup, restore, download or delete is already queued or running for this app's "
                            f"{KIND_LABEL.get(source.kind, source.kind)} backups. Try again when it finishes.", 409)
    # The exports that hold this backup: its own per-run download AND the whole-backup package of its set (a package is not
    # tied to one run id), so a deleted backup can't be fetched from a still-ready package for up to an hour.
    holders = models.BackupRun.source_run_id == source.id
    if source.set_id is not None:
        holders = or_(holders, (models.BackupRun.set_id == source.set_id) & (models.BackupRun.kind == "package"))
    exports = list(session.execute(select(models.BackupRun).where(
        models.BackupRun.app_id == app.id, models.BackupRun.trigger == "export", holders)).scalars())
    if any(e.status in ACTIVE for e in exports):
        raise DeleteRefused("A download of that backup is being prepared. Try again when it finishes.", 409)
    live = [e for e in exports if e.status == "succeeded" and export_state(e, now) == "ready" and e.export_key]
    if live:
        try:
            runner.delete_exports([e.export_key for e in live if e.export_key])
        except BackupRunnerError:
            raise DeleteRefused("We couldn't remove the prepared download of this backup. Try again in a minute.", 503) from None
        for e in live:
            e.status = "expired"
    source.status = "deleting"
    run = models.BackupRun(app_id=app.id, kind=source.kind, trigger="delete", status="queued", created_at=now,
                           source_run_id=source.id, set_id=source.set_id)
    session.add(run)
    session.flush()
    return run, live


def enqueue_delete_set(session: Session, app: models.App, st: models.BackupSet, runner: BackupRunner,
                       now: Optional[datetime] = None) -> tuple[list[models.BackupRun], list[models.BackupRun]]:
    """Delete a whole backup: one `forget --prune` per succeeded part (each in its own repo), failed parts just disappear.
    Returns (delete runs, exports removed). Per-app lock first; the caller commits. The set is `deleting` until the last delete
    Job ends, then `deleted`. Refused while a part is queued/running or any download of the set is being prepared (409)."""
    now = _now(now)
    cfg = lock_app(session, app)
    session.refresh(st)
    if cfg.repo_password_enc is None or cfg.purged_at is not None:
        raise DeleteRefused("The repository for that backup has been purged.", 410)
    if st.app_id != app.id or st.delete_status == "deleted":
        raise DeleteRefused("We can't find that backup.", 404)
    parts = set_parts(session, st)
    if st.delete_status == "deleting" or any(p.status == "deleting" for p in parts):
        raise DeleteRefused("That backup is already being deleted.", 409)
    if any(p.status in ACTIVE for p in parts):
        raise DeleteRefused("That backup is still being taken. Try again when it finishes.", 409)
    ok_parts = [p for p in parts if p.status == "succeeded" and p.snapshot_id]
    for kind in {p.kind for p in ok_parts}:
        if _repo_busy(session, app.id, kind):
            raise DeleteRefused(f"A backup, restore, download or delete is already queued or running for this app's "
                                f"{KIND_LABEL.get(kind, kind)} backups. Try again when it finishes.", 409)
    part_ids = [p.id for p in parts]
    exports = list(session.execute(select(models.BackupRun).where(
        models.BackupRun.app_id == app.id, models.BackupRun.trigger == "export",
        or_(models.BackupRun.set_id == st.id, models.BackupRun.source_run_id.in_(part_ids)))).scalars())
    if any(e.status in ACTIVE for e in exports):
        raise DeleteRefused("A download of that backup is being prepared. Try again when it finishes.", 409)
    live = [e for e in exports if e.status == "succeeded" and export_state(e, now) == "ready" and e.export_key]
    if live:
        try:
            runner.delete_exports([e.export_key for e in live if e.export_key])
        except BackupRunnerError:
            raise DeleteRefused("We couldn't remove the prepared download of this backup. Try again in a minute.", 503) from None
        for e in live:
            e.status = "expired"
    runs = []
    for p in parts:
        if p in ok_parts:
            p.status = "deleting"
            r = models.BackupRun(app_id=app.id, kind=p.kind, trigger="delete", status="queued", created_at=now,
                                 source_run_id=p.id, set_id=st.id)
            session.add(r)
            runs.append(r)
        else:
            p.status = "deleted"  # a failed part holds no snapshot: nothing to forget
    st.delete_status = "deleting" if runs else "deleted"
    session.flush()
    return runs, live


# --- owner download (export) ------------------------------------------------------------------

EXPORT_STATES = ("queued", "running", "ready", "failed", "expired")


class ExportRefused(Exception):
    """Raised by enqueue_export with a message that is safe to show to the caller."""

    def __init__(self, message: str, status: int = 422, retry_after: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def export_extension(app: models.App, kind: str) -> str:
    if kind in ("bucket", "volume", "package"):
        return "tar.gz"
    return "pgdump" if app.database is not None and app.database.server is not None and app.database.server.engine == "pg" else "sql.gz"


_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def export_filename(app: models.App, source: models.BackupRun, ext: str) -> str:
    """Download name. App names are free text (quotes, slashes, control characters, any script), so only a conservative
    ASCII subset reaches the Content-Disposition header and a CLI's local path."""
    stem = _FILENAME_UNSAFE.sub("_", app.name).strip("._-")[:60] or "app"
    return f"{stem}-{source.kind}-backup{source.id}.{ext}"


def _check_export_limits(exports: list[models.BackupRun], now: datetime, limit: Optional[int] = None) -> None:
    """One download being prepared per app at a time, and `backup_export_max_per_hour` new ones per hour. A FAILED download does
    not count (it produced nothing and the owner should be able to retry)."""
    limit = config.BACKUP_EXPORT_MAX_PER_HOUR if limit is None else limit
    if any(e.status in ACTIVE for e in exports):
        raise ExportRefused("Another download is being prepared for this app. Wait for it to finish.", 409)
    recent = sorted(_naive(e.created_at) for e in exports if e.status != "failed" and now - _naive(e.created_at) < timedelta(hours=1))
    if len(recent) >= limit:
        wait = int((recent[len(recent) - limit] + timedelta(hours=1) - now).total_seconds()) + 1
        raise ExportRefused(f"You can prepare {limit} backup download{'' if limit == 1 else 's'} per hour for this app "
                            f"(failed downloads don't count). Try again in {max(1, wait // 60)} minutes.", 429, retry_after=wait)


def enqueue_export(session: Session, app: models.App, source: models.BackupRun,
                   now: Optional[datetime] = None) -> tuple[models.BackupRun, bool]:
    """Queue the materialisation of `source` (a succeeded db/bucket backup of `app`) as ONE downloadable file. Idempotent for a
    snapshot that already has a live export (returned with created=False); at most one active export per app and BACKUP_EXPORT_MAX_PER_HOUR new ones per hour."""
    now = _now(now)
    if not config.backup_export_prefix_ok():
        raise ExportRefused("Downloads aren't available right now. Ask an administrator.", 503)
    if source.app_id != app.id or source.trigger not in BACKUP_TRIGGERS or source.kind not in DOWNLOADABLE:
        raise ExportRefused("Only database, blob storage and volume backups can be downloaded.")
    if source.status != "succeeded" or not source.snapshot_id:
        raise ExportRefused("That backup run didn't finish successfully, so there is nothing to download.")
    if session.get(models.AppBackupConfig, app.id) is None:
        raise ExportRefused("The repository for that backup has been purged.", 410)
    cfg = lock_app(session, app)  # serialise per app before the active / rate-limit checks
    if cfg.repo_password_enc is None or cfg.purged_at is not None:
        raise ExportRefused("The repository for that backup has been purged.", 410)
    exports = list(session.execute(
        select(models.BackupRun).where(models.BackupRun.app_id == app.id, models.BackupRun.trigger == "export")
        .order_by(models.BackupRun.id.desc())).scalars())
    live = [e for e in exports if e.source_run_id == source.id and (
        e.status in ACTIVE or (e.status == "succeeded" and e.finished_at is not None
                               and now - _naive(e.finished_at) < timedelta(seconds=config.BACKUP_EXPORT_TTL_SECONDS)))]
    if live:
        return live[0], False
    _check_export_limits(exports, now, int(platform_settings.get(session, "backup_export_max_per_hour")))
    run = models.BackupRun(app_id=app.id, kind=source.kind, trigger="export", status="queued", created_at=now,
                           source_run_id=source.id)
    session.add(run)
    session.flush()
    run.export_key = f"{config.BACKUP_EXPORT_PREFIX}{app.id}/{run.id}.{export_extension(app, source.kind)}"
    return run, True


def export_set_filename(app: models.App, set_id: int) -> str:
    stem = _FILENAME_UNSAFE.sub("_", app.name).strip("._-")[:60] or "app"
    return f"{stem}-backup{set_id}.tar.gz"


def _package_parts(session: Session, st: Optional[models.BackupSet]) -> list[models.BackupRun]:
    """The parts a package contains: the ones that succeeded and still hold a snapshot."""
    if st is None:
        return []
    return [p for p in set_parts(session, st) if p.status == "succeeded" and p.snapshot_id and p.kind in DOWNLOADABLE]


def enqueue_set_export(session: Session, app: models.App, st: models.BackupSet,
                       now: Optional[datetime] = None) -> tuple[models.BackupRun, bool]:
    """Queue ONE Job that reads every succeeded part of the backup from its repo and streams a single .tar.gz package (the
    parts + MANIFEST.json) to the export prefix. One Job rather than one per part plus a combine step: the repo password is
    shared by an app's repos, so a single Secret covers all parts, and there is no intermediate object to track or sweep.
    Idempotent for a set that already has a live package (created=False). Same limits as a per-run download."""
    now = _now(now)
    if not config.backup_export_prefix_ok():
        raise ExportRefused("Downloads aren't available right now. Ask an administrator.", 503)
    if st.app_id != app.id or st.delete_status is not None:
        raise ExportRefused("We can't find that backup.", 404)
    if session.get(models.AppBackupConfig, app.id) is None:
        raise ExportRefused("The repository for that backup has been purged.", 410)
    cfg = lock_app(session, app)
    session.refresh(st)
    if cfg.repo_password_enc is None or cfg.purged_at is not None:
        raise ExportRefused("The repository for that backup has been purged.", 410)
    if st.delete_status is not None:
        raise ExportRefused("That backup is being deleted.", 409)
    parts = set_parts(session, st)
    if any(p.status in ACTIVE or p.status == "deleting" for p in parts):
        raise ExportRefused("That backup is still being taken. Download it when it finishes.", 409)
    if not _package_parts(session, st):
        raise ExportRefused("No part of that backup finished successfully, so there is nothing to download.")
    exports = list(session.execute(
        select(models.BackupRun).where(models.BackupRun.app_id == app.id, models.BackupRun.trigger == "export")
        .order_by(models.BackupRun.id.desc())).scalars())
    live = [e for e in exports if e.set_id == st.id and (
        e.status in ACTIVE or (e.status == "succeeded" and e.finished_at is not None
                               and now - _naive(e.finished_at) < timedelta(seconds=config.BACKUP_EXPORT_TTL_SECONDS)))]
    if live:
        return live[0], False
    _check_export_limits(exports, now, int(platform_settings.get(session, "backup_export_max_per_hour")))
    run = models.BackupRun(app_id=app.id, kind="package", trigger="export", status="queued", created_at=now, set_id=st.id)
    session.add(run)
    session.flush()
    run.export_key = f"{config.BACKUP_EXPORT_PREFIX}{app.id}/{run.id}.tar.gz"
    return run, True


def _build_package_env(session: Session, run: models.BackupRun) -> dict[str, str]:
    st = session.get(models.BackupSet, run.set_id) if run.set_id else None
    cfg = session.get(models.AppBackupConfig, run.app_id)
    app = session.get(models.App, run.app_id)
    parts = _package_parts(session, st)
    if st is None or app is None or cfg is None or cfg.purged_at is not None or not parts or not run.export_key:
        raise ValueError("the backup to download is gone")
    env = _repo_env(session, run.app_id, parts[0].kind, cfg)
    env.pop("RESTIC_REPOSITORY")  # one repo per kind: the script derives each from REPO_BASE
    env.update({
        "MODE": "package", "REPO_BASE": repo_base(run.app_id), "TAG": "export",
        "PARTS": ",".join(f"{p.kind}={p.snapshot_id}" for p in parts),
        "APP_NAME": app.name, "SET_ID": str(st.id), "SET_CREATED_AT": _naive(st.created_at).isoformat() + "Z",
        "PICOPAAS_VERSION": config.PICOPAAS_VERSION, "MAX_VOLUME_BYTES": str(config.BACKUP_VOLUME_EXPORT_MAX_BYTES),
        "VOLUME_DIR": config.BACKUP_VOLUME_MOUNT,
        "EXPORT_ENDPOINT": config.backup_endpoint(), "EXPORT_BUCKET": config.BACKUP_BUCKET, "EXPORT_KEY": run.export_key,
    })
    return env


def _build_export_env(session: Session, run: models.BackupRun) -> dict[str, str]:
    source = session.get(models.BackupRun, run.source_run_id) if run.source_run_id else None
    cfg = session.get(models.AppBackupConfig, source.app_id) if source is not None else None
    if source is None or cfg is None or not source.snapshot_id or cfg.purged_at is not None or not run.export_key:
        raise ValueError("the backup to download is gone")
    env = _repo_env(session, source.app_id, source.kind, cfg)
    env.update({"MODE": "export", "KIND": source.kind, "SNAPSHOT": source.snapshot_id, "TAG": "export",
                "EXPORT_ENDPOINT": config.backup_endpoint(), "EXPORT_BUCKET": config.BACKUP_BUCKET,
                "EXPORT_KEY": run.export_key, "VOLUME_DIR": config.BACKUP_VOLUME_MOUNT,
                "MAX_VOLUME_BYTES": str(config.BACKUP_VOLUME_EXPORT_MAX_BYTES)})
    return env


def export_state(run: models.BackupRun, now: Optional[datetime] = None) -> str:
    """queued | running | ready | failed | expired (the object is swept EXPORT_TTL after the Job finished)."""
    if run.status == "succeeded":
        age = _now(now) - _naive(run.finished_at or run.created_at)
        return "expired" if age >= timedelta(seconds=config.BACKUP_EXPORT_TTL_SECONDS) else "ready"
    return run.status


def export_available_until(run: models.BackupRun) -> datetime:
    return _naive(run.finished_at or run.created_at) + timedelta(seconds=config.BACKUP_EXPORT_TTL_SECONDS)


def listed_exports(session: Session, app_id: int, now: Optional[datetime] = None) -> list[models.BackupRun]:
    """Exports worth showing: queued, running, ready, and failed within the last hour. Newest first."""
    now = _now(now)
    out = []
    for e in session.execute(select(models.BackupRun).where(models.BackupRun.app_id == app_id, models.BackupRun.trigger == "export")
                             .order_by(models.BackupRun.created_at.desc(), models.BackupRun.id.desc())).scalars():
        state = export_state(e, now)
        if state == "expired":
            continue
        if state == "failed" and now - _naive(e.finished_at or e.created_at) >= timedelta(hours=1):
            continue
        out.append(e)
    return out


def live_exports_by_set(session: Session, app_id: int, set_ids: list[int],
                        now: Optional[datetime] = None) -> dict[int, models.BackupRun]:
    """Latest queued/running/ready package download per set id."""
    found: dict[int, models.BackupRun] = {}
    for e in listed_exports(session, app_id, now):
        if e.set_id in set_ids and e.kind == "package" and export_state(e, now) != "failed" and e.set_id not in found:
            found[e.set_id] = e
    return found


def live_exports_by_run(session: Session, app_id: int, run_ids: list[int], now: Optional[datetime] = None) -> dict[int, models.BackupRun]:
    """Latest queued/running/ready export per source run id."""
    found: dict[int, models.BackupRun] = {}
    for e in listed_exports(session, app_id, now):
        if e.source_run_id in run_ids and export_state(e, now) != "failed" and e.source_run_id not in found:
            found[e.source_run_id] = e
    return found


# --- app delete -------------------------------------------------------------------------------

def final_gate(session: Session, app: models.App, now: Optional[datetime] = None) -> bool:
    """Called by the reconciler before it drops a deleted app's database/bucket. True = go ahead and delete. Takes ONE
    final backup attempt per kind first (queued here, run by `tick`); a failed or stuck attempt does not block the
    deletion. When the app has backed-up data, its repos are kept BACKUP_DELETED_RETENTION_DAYS and purged afterwards."""
    kinds = eligible_kinds(app)
    if not usable() or not kinds:
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
    runs = list(session.execute(
        select(models.BackupRun).where(models.BackupRun.app_id == app.id, models.BackupRun.trigger == "final")
        .order_by(models.BackupRun.id)
    ).scalars())
    if not runs:
        st, _ = enqueue_set(session, app, kinds, "final", now)
        session.commit()
        audit.record(session, None, "backup.create", "app", app.id,
                     f"trigger=final set={st.id} kinds=" + ",".join(kinds), actor_email="system")
        return False
    active = [r for r in runs if r.status in ACTIVE]
    if active and all(now - _naive(r.created_at) <= FINAL_MAX_AGE for r in active):
        return False
    for run in active:
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
    if cfg is None or not cfg.enabled or cfg.admin_disabled or cfg.schedule == "off":
        return "disabled"
    now = _now(now)
    limit = STALE_WEEKLY if cfg.schedule == "weekly" else STALE_DAILY
    if cfg.last_status == "failed" or "failed" in (cfg.db_last_status, cfg.bucket_last_status, cfg.vol_last_status):
        return "failing"
    if cfg.last_success_at is None:
        return "stale" if now - _naive(cfg.created_at) > limit else "pending"
    return "stale" if now - _naive(cfg.last_success_at) > limit else "ok"


def repo_bytes(cfg: Optional[models.AppBackupConfig]) -> int:
    if cfg is None:
        return 0
    return (cfg.db_repo_bytes or 0) + (cfg.vol_repo_bytes or 0) + (cfg.bucket_repo_bytes or 0)


def overview(session: Session, now: Optional[datetime] = None) -> dict[str, Any]:
    """Fleet view for GET /admin/backups and the Health page row."""
    now = _now(now)
    out: dict[str, Any] = {
        "enabled": enabled(), "configured": config.backups_configured(),
        "cap_per_app_bytes": int(platform_settings.get(session, "backup_max_bytes_per_app")),
        "cap_total_bytes": total_cap_bytes(session),
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
            "schedule": cfg.schedule, "admin_disabled": cfg.admin_disabled,
            "cap_bytes": app_cap_bytes(session, cfg), "last_success_at": cfg.last_success_at, "last_status": cfg.last_status,
            "last_error": cfg.last_error, "next_backup_at": cfg.next_backup_at, "repo_bytes": repo_bytes(cfg)})
    if not out["configured"]:
        out["warning"] = "Backups are on but the backup bucket or key is not configured; nothing is being backed up."
    elif out["total_repo_bytes"] >= 0.8 * out["cap_total_bytes"]:
        out["warning"] = "Backup storage is above 80% of the platform cap."
    return out


def health_summary(session: Session) -> dict[str, Any]:
    """Compact version for GET /admin/health (`backups` key)."""
    o = overview(session)
    return {k: o[k] for k in ("enabled", "configured", "counts", "total_repo_bytes", "cap_total_bytes", "warning")}
