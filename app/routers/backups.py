"""Backups API (docs/design/backups.md, phase 1a): list runs + status, on-demand run, per-app setting, admin overview.

The API never touches the cluster: POST only queues a `BackupRun`; the leader's scheduler tick (app/backups.py) starts
the Job. Read-scope API keys can list but not create (the global read-only gate blocks every non-GET). Owner or
platform admin only; anyone else gets 404.

Download: `POST /apps/{id}/backups/{run_id}/download` queues an export Job (the snapshot as one .sql.gz / .pgdump / .tar.gz file in
the backup bucket's exports/ prefix, swept after an hour); `GET /apps/{id}/backups/exports/{export_id}` is polled and returns a
10-minute presigned URL once the file is ready.

Admin restore (`POST /admin/backups/{run_id}/restore`) queues a restore Job like a backup: it is admin-only, restores
into ANOTHER app by default, and only over the source app when `overwrite_source` is set and that app is suspended.
"""
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import audit, backups, config, models
from app.db import get_db
from app.dependencies import get_active_user, require_platform_admin
from app.providers.backup import BackupRunner, BackupRunnerError, KubeBackupRunner
from app.routers.app_ops import _get_app_or_404

router = APIRouter(prefix="/apps", tags=["backups"])
admin_router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])

DISABLED_MSG = "Backups aren't enabled on this platform."
MISCONFIGURED_MSG = "Backups are enabled but the backup storage isn't configured yet. Ask an administrator."
RUNS_SHOWN = 30


def get_backup_runner() -> BackupRunner:
    """Signs download URLs only (the API never starts Jobs). Overridden in tests with FakeBackupRunner."""
    return KubeBackupRunner()


class RunExportOut(BaseModel):
    """The latest live download of a backup run (queued, running or ready), so a UI can show "Ready to download"."""
    id: int
    status: str  # queued | running | ready
    available_until: Optional[datetime] = None  # once ready


class BackupRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    trigger: str
    status: str  # queued | running | succeeded | failed | deleting (an owner delete is in progress)
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    bytes_added: Optional[int] = None
    repo_bytes: Optional[int] = None
    snapshot_id: Optional[str] = None
    error: Optional[str] = None
    source_run_id: Optional[int] = None  # restore runs: the backup run that was restored
    export: Optional[RunExportOut] = None  # GET /apps/{id}/backups only


class BackupKindOut(BaseModel):
    kind: str
    last_backup_at: Optional[datetime] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    last_bytes: Optional[int] = None
    repo_bytes: int = 0


class BackupsOut(BaseModel):
    feature_enabled: bool  # BACKUPS_ENABLED on this platform
    configured: bool  # backup bucket + key present
    enabled: bool  # this app's setting
    schedule: str
    state: str  # disabled | pending | ok | stale | failing
    kinds: List[BackupKindOut] = []
    last_backup_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    next_backup_at: Optional[datetime] = None
    repo_bytes: int = 0
    cap_bytes: int
    runs: List[BackupRunOut] = []


class BackupCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kinds: Optional[List[Literal["db", "volume", "bucket"]]] = None  # default: everything this app has


class BackupConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    schedule: Optional[Literal["daily", "weekly", "off"]] = None


def _run_out(run: models.BackupRun, export: Optional[models.BackupRun]) -> BackupRunOut:
    out = BackupRunOut.model_validate(run)
    if export is not None:
        state = backups.export_state(export)
        out.export = RunExportOut(id=export.id, status=state, available_until=backups.export_available_until(export) if state == "ready" else None)
    return out


def _out(db: Session, app: models.App) -> BackupsOut:
    cfg = db.get(models.AppBackupConfig, app.id)
    runs = []
    if backups.enabled():
        runs = (db.query(models.BackupRun)
                .filter(models.BackupRun.app_id == app.id, models.BackupRun.trigger.notin_(("purge", "export", "delete")),
                        models.BackupRun.status != "deleted")
                .order_by(models.BackupRun.created_at.desc(), models.BackupRun.id.desc()).limit(RUNS_SHOWN).all())
    live = backups.live_exports_by_run(db, app.id, [r.id for r in runs]) if runs else {}
    kinds = []
    for kind in backups.eligible_kinds(app):
        p = backups.prefix(kind)
        kinds.append(BackupKindOut(
            kind=kind, last_backup_at=getattr(cfg, f"{p}_last_backup_at", None), last_status=getattr(cfg, f"{p}_last_status", None),
            last_error=getattr(cfg, f"{p}_last_error", None), last_bytes=getattr(cfg, f"{p}_last_bytes", None),
            repo_bytes=getattr(cfg, f"{p}_repo_bytes", 0) or 0))
    on = cfg.enabled if cfg is not None else (backups.default_enabled(app) and backups.enabled())
    return BackupsOut(
        feature_enabled=backups.enabled(), configured=config.backups_configured(), enabled=on,
        schedule=cfg.schedule if cfg is not None else "daily",
        state=backups.app_state(cfg) if cfg is not None else ("pending" if on else "disabled"),
        kinds=kinds, last_backup_at=cfg.last_backup_at if cfg else None, last_success_at=cfg.last_success_at if cfg else None,
        last_status=cfg.last_status if cfg else None, last_error=cfg.last_error if cfg else None,
        next_backup_at=cfg.next_backup_at if cfg else None, repo_bytes=backups.repo_bytes(cfg),
        cap_bytes=config.BACKUP_MAX_BYTES_PER_APP, runs=[_run_out(r, live.get(r.id)) for r in runs])


def _require_usable() -> None:
    if not backups.enabled():
        raise HTTPException(status_code=422, detail=DISABLED_MSG)
    if not config.backups_configured():
        raise HTTPException(status_code=503, detail=MISCONFIGURED_MSG)


@router.get("/{app_id}/backups", response_model=BackupsOut)
def list_backups(app_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    """Backup status and recent runs. Works (with feature_enabled false) when backups are off, so UIs can degrade."""
    return _out(db, _get_app_or_404(db, app_id, user))


@router.post("/{app_id}/backups", response_model=List[BackupRunOut], status_code=202)
def create_backup(app_id: int, body: Optional[BackupCreateIn] = None, db: Session = Depends(get_db),
                  user: models.User = Depends(get_active_user)):
    """Queue an on-demand backup (one per app per hour). The scheduler starts it within a minute."""
    app = _get_app_or_404(db, app_id, user)
    _require_usable()
    available = backups.eligible_kinds(app)
    wanted = list(body.kinds) if body and body.kinds else list(available)
    if "volume" in wanted and "volume" not in available:
        raise HTTPException(status_code=422, detail="Volume backups aren't available yet." if app.volume_size_gi
                            else "This app has no volume to back up.")
    if "bucket" in wanted and "bucket" not in available:
        raise HTTPException(status_code=422, detail="This app has no blob storage to back up yet." if app.want_storage
                            else "This app has no blob storage to back up.")
    if not wanted or any(k not in available for k in wanted):
        raise HTTPException(status_code=422, detail="This app has no database to back up yet." if app.want_db
                            else "This app has no database, blob storage or volume to back up.")
    backups.lock_app(db, app)  # per-app row lock until the commit below: a double click gets 409/429, not a second run
    if backups.active_run(db, app.id) is not None:
        raise HTTPException(status_code=409, detail="A backup is already queued or running for this app.")
    wait = backups.manual_cooldown(db, app.id)
    if wait is not None:
        raise HTTPException(status_code=429, detail=f"You can start one backup per hour. Try again in {max(1, wait // 60)} minutes.",
                            headers={"Retry-After": str(wait)})
    runs = [backups.enqueue(db, app, kind, "manual") for kind in wanted]
    db.commit()
    audit.record(db, user, "backup.create", "app", app.id, "trigger=manual kinds=" + ",".join(wanted))
    return [BackupRunOut.model_validate(r) for r in runs]


@router.patch("/{app_id}/backups", response_model=BackupsOut)
def update_backup_config(app_id: int, body: BackupConfigIn, db: Session = Depends(get_db),
                         user: models.User = Depends(get_active_user)):
    """Turn an app's backups on/off or switch daily/weekly/off."""
    app = _get_app_or_404(db, app_id, user)
    _require_usable()
    if not backups.eligible_kinds(app) and not backups.wants_backups(app):
        raise HTTPException(status_code=422, detail="This app has no database, blob storage or volume to back up.")
    cfg = backups.ensure_config(db, app)
    if body.enabled is not None:
        cfg.enabled = body.enabled
    if body.schedule is not None:
        cfg.schedule = body.schedule
    if cfg.schedule == "off":
        cfg.next_backup_at = None
    elif cfg.enabled and (body.schedule is not None or body.enabled) and cfg.next_backup_at is None:
        cfg.next_backup_at = backups.compute_next(app.id, datetime.utcnow(), cfg.schedule)
    db.commit()
    audit.record(db, user, "backup.config_changed", "app", app.id, f"enabled={cfg.enabled} schedule={cfg.schedule}")
    return _out(db, app)


@admin_router.get("/backups")
def admin_backups(db: Session = Depends(get_db)):
    """Fleet overview: per app state/last success/bytes, platform total vs cap, apps retained after deletion."""
    return backups.overview(db)


class RestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_app_id: int
    overwrite_source: bool = False


@admin_router.post("/backups/{run_id}/restore", response_model=BackupRunOut, status_code=202)
def restore_backup(run_id: int, body: RestoreIn, db: Session = Depends(get_db),
                   admin: models.User = Depends(require_platform_admin)):
    """Queue a restore of a succeeded database or blob storage backup into the target app's database or bucket (replacing
    what is there). The scheduler starts the Job within a minute; poll GET /apps/{target}/backups for the result."""
    _require_usable()
    source = db.get(models.BackupRun, run_id)
    if source is None:
        raise HTTPException(status_code=404, detail="We can't find that backup run.")
    target = db.get(models.App, body.target_app_id)
    if target is None or target.deleted_at is not None:
        raise HTTPException(status_code=404, detail="We can't find the target app. It may have been deleted.")
    try:
        run = backups.enqueue_restore(db, source, target, body.overwrite_source)
    except backups.RestoreRefused as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    db.commit()
    audit.record(db, admin, "backup.restore", "app", target.id,
                 f"run={run.id} from_run={source.id} from_app={source.app_id} kind={source.kind}"
                 + (" overwrite_source" if target.id == source.app_id else ""))
    return BackupRunOut.model_validate(run)


class BackupExportOut(BaseModel):
    export_id: int
    run_id: int  # the backup run being downloaded
    kind: str
    status: str  # queued | running | ready | failed | expired
    filename: Optional[str] = None
    size_bytes: Optional[int] = None  # once ready
    url: Optional[str] = None  # only when status == ready: presigned GET, valid url_expires_in seconds from this response
    url_expires_in: Optional[int] = None
    available_until: Optional[datetime] = None  # the file is deleted from staging after this
    error: Optional[str] = None
    created_at: datetime


def _export_out(db: Session, app: models.App, run: models.BackupRun, runner: Optional[BackupRunner]) -> BackupExportOut:
    source = db.get(models.BackupRun, run.source_run_id) if run.source_run_id else None
    state = backups.export_state(run)
    ext = (run.export_key or "").split(".", 1)[-1]
    out = BackupExportOut(
        export_id=run.id, run_id=run.source_run_id or 0, kind=run.kind, status=state, created_at=run.created_at,
        filename=backups.export_filename(app, source, ext) if source is not None and run.export_key else None,
        error=run.error if state == "failed" else None)
    if state == "ready" and run.export_key and out.filename:
        out.size_bytes = run.bytes_added
        out.available_until = backups.export_available_until(run)
    if state == "ready" and runner is not None and run.export_key and out.filename:
        try:
            out.url = runner.presign_export(run.export_key, config.BACKUP_EXPORT_URL_SECONDS, out.filename)
        except BackupRunnerError:
            raise HTTPException(status_code=503, detail="We couldn't prepare the download link. Try again in a minute.") from None
        out.url_expires_in = config.BACKUP_EXPORT_URL_SECONDS
    return out


@router.post("/{app_id}/backups/{run_id}/download", response_model=BackupExportOut, status_code=202)
def start_download(app_id: int, run_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    """Prepare a succeeded database or blob storage backup as a single file (3 per app per hour). 202 + poll the export."""
    app = _get_app_or_404(db, app_id, user)
    _require_usable()
    source = db.get(models.BackupRun, run_id)
    if source is None or source.app_id != app.id:
        raise HTTPException(status_code=404, detail="We can't find that backup run.")
    try:
        run, new = backups.enqueue_export(db, app, source)
    except backups.ExportRefused as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=headers) from None
    db.commit()
    if new:
        audit.record(db, user, "backup.download", "app", app.id, f"run={source.id} kind={source.kind} export={run.id}")
    return _export_out(db, app, run, None)


@router.get("/{app_id}/backups/exports", response_model=List[BackupExportOut])
def list_downloads(app_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    """The app's downloads that are not expired (queued, running, ready, and failed ones from the last hour), newest first.
    `url` is always null here: GET the single export for a fresh 10-minute link while it is ready."""
    app = _get_app_or_404(db, app_id, user)
    return [_export_out(db, app, r, None) for r in backups.listed_exports(db, app.id)]


@router.get("/{app_id}/backups/exports/{export_id}", response_model=BackupExportOut)
def get_download(app_id: int, export_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user),
                 runner: BackupRunner = Depends(get_backup_runner)):
    """Poll a download. When status is ready, `url` is a fresh 10-minute presigned GET (keep polling for another)."""
    app = _get_app_or_404(db, app_id, user)
    run = db.get(models.BackupRun, export_id)
    if run is None or run.app_id != app.id or run.trigger != "export":
        raise HTTPException(status_code=404, detail="We can't find that download.")
    out = _export_out(db, app, run, runner)
    if out.url:  # never the URL, key or any credential in the detail
        audit.record(db, user, "backup.download_link", "app", app.id,
                     f"export={run.id} run={run.source_run_id} kind={run.kind} expires_in={config.BACKUP_EXPORT_URL_SECONDS}")
    return out


class BackupDeleteOut(BaseModel):
    run_id: int  # the backup run being deleted (as in the path)
    status: str  # always "deleting" here
    delete_run_id: int


@router.delete("/{app_id}/backups/{run_id}", response_model=BackupDeleteOut, status_code=202)
def delete_backup(app_id: int, run_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user),
                  runner: BackupRunner = Depends(get_backup_runner)):
    """Delete one succeeded database or blob storage backup (its restic snapshot, then prune). 202; the run shows
    `status: "deleting"` in GET /apps/{id}/backups until the Job ends, then disappears (or returns to `succeeded` on failure).
    409 while a backup, restore, download or delete is active for that repo. A ready download of the run is removed with it."""
    app = _get_app_or_404(db, app_id, user)
    _require_usable()
    source = db.get(models.BackupRun, run_id)
    if source is None or source.app_id != app.id or source.status == "deleted":
        raise HTTPException(status_code=404, detail="We can't find that backup run.")
    try:
        run, exports = backups.enqueue_delete(db, app, source, runner)
    except backups.DeleteRefused as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    db.commit()
    audit.record(db, user, "backup.delete", "app", app.id,
                 f"run={source.id} kind={source.kind} snapshot={(source.snapshot_id or '')[:8]}")
    for e in exports:
        audit.record(db, user, "backup.export_deleted", "app", app.id, f"export={e.id} run={source.id} reason=backup_deleted")
    return BackupDeleteOut(run_id=source.id, status="deleting", delete_run_id=run.id)
