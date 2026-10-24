"""Backup orphan sweeper (docs/design/backups.md "Failure handling"): a leader-only periodic pass (app/scheduler.py::backup_sweep_job)
that cleans up what a crash, restart or lost Job leaves behind, so nothing stays "running" forever or costs storage unseen.

Per sweep (bounded by BACKUP_SWEEP_LIMIT per phase, idempotent, each phase isolated so one failure doesn't stop the rest):
  1. runs   - a `running` run older than its deadline is reconciled from the Job's real state (succeeded -> finished normally,
              failed -> failed with the Job's reason, gone -> failed "interrupted (platform restart or job lost)", still going
              past deadline+grace -> stopped). A `queued` run that never got a slot (BACKUP_QUEUED_MAX_SECONDS) fails. A source
              backup stuck in `deleting` without a live delete run goes back to `succeeded` (+ event); a set stuck `deleting`
              is settled. A run younger than its deadline is never touched.
  2. k8s    - labelled Jobs, pods and per-run Secrets (platform and tenant namespaces) that match no running run and are older
              than BACKUP_ORPHAN_MIN_AGE_SECONDS are deleted.
  3. blobs  - expired export objects (forced, ignoring the 5 minute throttle), incomplete multipart uploads older than
              BACKUP_STALE_UPLOAD_SECONDS. Repository prefixes without an app config row are REPORTED only; restic snapshots
              without a DB row are never deleted.
  4. rows   - backup sets with no runs, runs pointing at a set that no longer exists, ready exports whose source run is gone
              (their object is deleted too).
The result of the last sweep is kept in the platform_settings table (key `backup_sweeper_last`, compact JSON, not a registry
setting) so every replica's admin Health payload can show it. A sweep that cleaned something writes one `backup.orphan_cleaned`
audit row with the counts.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, backups, config, k8s_manifests, models
from app.providers.backup import BackupRunner

log = logging.getLogger(__name__)

STATE_KEY = "backup_sweeper_last"
MAX_ERRORS = 3
EMPTY_SET_MIN_AGE = timedelta(hours=1)
CLEAN_KEYS = ("runs_failed", "runs_finished", "queued_failed", "deletes_reverted", "jobs", "pods", "secrets", "exports",
              "uploads_aborted", "sets_removed", "runs_relinked", "export_rows")


def _active_jobs(session: Session) -> set[str]:
    return {r.job_name for r in backups._running(session) if r.job_name}


# --- 1. runs -----------------------------------------------------------------------------------

def _sweep_runs(session: Session, runner: BackupRunner, now: datetime, limit: int, cleaned: dict[str, int], errors: list[str]) -> None:
    deadline = timedelta(seconds=config.BACKUP_JOB_DEADLINE_SECONDS)
    grace = deadline + timedelta(seconds=120)
    running = [r for r in backups._running(session)
               if now - backups._naive(r.started_at or r.created_at) > deadline][:limit]
    for run in running:
        try:
            st = runner.status(run.job_name or "", backups.run_namespace(run)) if run.job_name else None
        except Exception as exc:  # noqa: BLE001 - API blip: next sweep
            errors.append(f"run {run.id}: status check failed ({type(exc).__name__})")
            continue
        if st is not None and st.state == "succeeded":
            backups._finish(session, run, runner, ok=True, result=st.result, now=now)
            cleaned["runs_finished"] += 1
        elif st is not None and st.state == "failed":
            backups._finish(session, run, runner, ok=False, error=st.error, now=now)
            cleaned["runs_failed"] += 1
        elif st is None or st.state == "missing":
            backups._finish(session, run, runner, ok=False, error=backups.INTERRUPTED_MSG, now=now)
            cleaned["runs_failed"] += 1
        elif now - backups._naive(run.started_at or run.created_at) > grace:
            backups._finish(session, run, runner, ok=False, error="the backup took too long and was stopped", now=now)
            cleaned["runs_failed"] += 1
    stale = now - timedelta(seconds=config.BACKUP_QUEUED_MAX_SECONDS)
    queued = session.execute(select(models.BackupRun).where(models.BackupRun.status == "queued", models.BackupRun.created_at < stale)
                             .order_by(models.BackupRun.id).limit(limit)).scalars().all()
    for run in queued:
        backups._finish(session, run, None, ok=False, error="never started (platform restart or queue lost)", now=now)
        cleaned["queued_failed"] += 1
    _sweep_deletes(session, now, limit, cleaned)


def _sweep_deletes(session: Session, now: datetime, limit: int, cleaned: dict[str, int]) -> None:
    deadline = timedelta(seconds=config.BACKUP_JOB_DEADLINE_SECONDS)
    for src in session.execute(select(models.BackupRun).where(models.BackupRun.status == "deleting").limit(limit)).scalars().all():
        dels = session.execute(select(models.BackupRun).where(models.BackupRun.trigger == "delete",
                                                              models.BackupRun.source_run_id == src.id)).scalars().all()
        if any(d.status in backups.ACTIVE for d in dels):
            continue
        last = max((backups._naive(d.finished_at or d.created_at) for d in dels), default=None)
        if last is not None and now - last < deadline:
            continue
        src.status = "succeeded"
        backups._event(session, src.app_id, src.id,
                       f"Deleting your {backups.KIND_LABEL.get(src.kind, src.kind)} backup was interrupted: "
                       f"{backups.INTERRUPTED_MSG}. The backup is still there.", now)
        backups._settle_set_delete(session, src.set_id)
        cleaned["deletes_reverted"] += 1
    for st in session.execute(select(models.BackupSet).where(models.BackupSet.delete_status == "deleting").limit(limit)).scalars().all():
        backups._settle_set_delete(session, st.id)
    session.commit()


# --- 2. k8s objects ----------------------------------------------------------------------------

def _sweep_k8s(session: Session, runner: BackupRunner, now: datetime, limit: int, cleaned: dict[str, int], errors: list[str]) -> None:
    namespaces = [config.BACKUP_NAMESPACE] + [k8s_manifests.namespace_name(i) for i in backups.tenant_app_ids(session, now)]
    out = runner.sweep_orphans(namespaces, _active_jobs(session), config.BACKUP_ORPHAN_MIN_AGE_SECONDS, limit)
    for k in ("jobs", "pods", "secrets"):
        cleaned[k] += int(out.get(k, 0))


# --- 3. blobs ----------------------------------------------------------------------------------

def _sweep_blobs(session: Session, runner: BackupRunner, now: datetime, limit: int, cleaned: dict[str, int],
                 errors: list[str], extra: dict[str, Any]) -> None:
    try:
        keys = runner.sweep_exports(config.BACKUP_EXPORT_TTL_SECONDS)
        if keys:
            backups._audit_export_deletes(session, keys)
            cleaned["exports"] += len(keys)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"export objects: {type(exc).__name__}")
    try:
        cleaned["uploads_aborted"] += runner.abort_stale_uploads(config.BACKUP_STALE_UPLOAD_SECONDS, limit)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"incomplete uploads: {type(exc).__name__}")
    try:  # report only: a repo is customer data, never deleted here (purge handles apps past retention)
        known = set(session.execute(select(models.AppBackupConfig.app_id)).scalars())
        extra["orphan_repos"] = sorted(runner.repo_app_ids() - known)[:10]
    except Exception as exc:  # noqa: BLE001
        errors.append(f"repository listing: {type(exc).__name__}")


# --- 4. rows -----------------------------------------------------------------------------------

def _sweep_rows(session: Session, runner: BackupRunner, now: datetime, limit: int, cleaned: dict[str, int]) -> None:
    run_with_set = select(models.BackupRun.set_id).where(models.BackupRun.set_id.is_not(None))
    empty = session.execute(select(models.BackupSet).where(
        models.BackupSet.created_at < now - EMPTY_SET_MIN_AGE, models.BackupSet.id.not_in(run_with_set)).limit(limit)).scalars().all()
    for st in empty:
        session.delete(st)
        cleaned["sets_removed"] += 1
    dangling = session.execute(select(models.BackupRun).where(
        models.BackupRun.set_id.is_not(None), models.BackupRun.set_id.not_in(select(models.BackupSet.id))).limit(limit)).scalars().all()
    for r in dangling:
        r.set_id = None
        cleaned["runs_relinked"] += 1
    orphan_exports = session.execute(select(models.BackupRun).where(
        models.BackupRun.trigger == "export", models.BackupRun.kind != "package", models.BackupRun.status == "succeeded",
        models.BackupRun.source_run_id.is_not(None),
        models.BackupRun.source_run_id.not_in(select(models.BackupRun.id))).limit(limit)).scalars().all()
    if orphan_exports:
        keys = [e.export_key for e in orphan_exports if e.export_key]
        if keys:
            runner.delete_exports(keys)
        for e in orphan_exports:
            e.status = "expired"
            cleaned["export_rows"] += 1
    session.commit()


# --- the sweep ---------------------------------------------------------------------------------

def sweep(session: Session, runner: BackupRunner, now: Optional[datetime] = None, limit: Optional[int] = None) -> dict[str, Any]:
    """One sweep. Never raises; returns (and stores) {last_run_at, cleaned, errors, orphan_repos}."""
    now = backups._now(now)
    limit = limit or config.BACKUP_SWEEP_LIMIT
    cleaned = {k: 0 for k in CLEAN_KEYS}
    errors: list[str] = []
    extra: dict[str, Any] = {"orphan_repos": []}
    phases: list[tuple[str, Callable[[], None]]] = [
        ("runs", lambda: _sweep_runs(session, runner, now, limit, cleaned, errors)),
        ("kubernetes objects", lambda: _sweep_k8s(session, runner, now, limit, cleaned, errors)),
        ("blobs", lambda: _sweep_blobs(session, runner, now, limit, cleaned, errors, extra)),
        ("rows", lambda: _sweep_rows(session, runner, now, limit, cleaned)),
    ]
    for name, fn in phases:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - one broken phase must not stop the others
            session.rollback()
            errors.append(f"{name}: {type(exc).__name__}")
            log.warning("backup sweeper: %s failed (%s)", name, type(exc).__name__)
    result = {"last_run_at": now.isoformat(), "cleaned": {k: v for k, v in cleaned.items() if v}, "errors": errors[:MAX_ERRORS],
              "orphan_repos": extra["orphan_repos"]}
    try:
        total = sum(cleaned.values())
        if total:
            audit.record(session, None, "backup.orphan_cleaned", "platform", None,
                         " ".join(f"{k}={v}" for k, v in result["cleaned"].items()), actor_email="system")
        _store(session, result, now)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.warning("backup sweeper: could not record the result (%s)", type(exc).__name__)
    return result


def _store(session: Session, result: dict[str, Any], now: datetime) -> None:
    slim = dict(result)
    slim["errors"] = [e[:40] for e in slim["errors"]]
    value = json.dumps(slim, separators=(",", ":"))
    if len(value) > 255:  # the column is 255 chars: shed the long-tail detail first
        slim["orphan_repos"] = slim["orphan_repos"][:5]
        slim["errors"] = [e[:20] for e in slim["errors"]]
        value = json.dumps(slim, separators=(",", ":"))
    row = session.get(models.PlatformSetting, STATE_KEY)
    if row is None:
        session.add(models.PlatformSetting(key=STATE_KEY, value=value[:255], updated_at=now, updated_by="system"))
    else:
        row.value, row.updated_at = value[:255], now
    session.commit()


def last_sweep(session: Session) -> dict[str, Any]:
    """The stored result of the last sweep, for the admin Health payload (never raises)."""
    empty: dict[str, Any] = {"last_run_at": None, "cleaned": {}, "errors": [], "orphan_repos": []}
    try:
        row = session.get(models.PlatformSetting, STATE_KEY)
        if row is None:
            return empty
        data = json.loads(row.value)
        return {**empty, **{k: data[k] for k in empty if k in data}}
    except Exception:  # noqa: BLE001
        session.rollback()
        return empty
