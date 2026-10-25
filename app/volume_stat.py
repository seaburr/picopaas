"""Measured persistent-volume usage, without widening platform RBAC (no nodes/proxy, no pods/exec).

Nothing in the platform can read how full an app's PVC is, so a tiny Job does it: the volume-backup pod pattern (the app's
namespace, the RWO claim mounted read-only, scheduled next to the app pod) running the backup image with MODE=stat, which
prints `df`-style used bytes of the mount. It is NOT a BackupRun: it never appears in the backups list, counts toward no cap or
cooldown and never shows "Backup running". State lives on the app row (volume_stat_job = Job in flight, volume_stat_at = when
the last one started, volume_used_bytes/_at = the measurement). Every volume backup's result carries the same number, too.

Runs from the leader-only backup tick (app/backups.py::tick), at most every VOLUME_STAT_INTERVAL_SECONDS per app. Needs the
volume-backup gate (BACKUP_KEY_SCOPED) because it reuses that pod's quota headroom. Needs the backup image rebuilt with MODE=stat
(an older image just fails the Job: no measurement, nothing else breaks).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, k8s_manifests, models
from app.providers.backup import BackupJobSpec, BackupRunner

log = logging.getLogger(__name__)


def job_name(app_id: int) -> str:
    return f"vs-{app_id}"


def record(app: Optional[models.App], used: Any, now: datetime) -> bool:
    """Store a measured used-bytes value (ignores anything that is not a non-negative int). Caller commits."""
    if app is None or isinstance(used, bool) or not isinstance(used, int) or used < 0:
        return False
    app.volume_used_bytes, app.volume_used_at = used, now
    return True


def _tiny(volume: dict[str, Any]) -> dict[str, Any]:
    """One `df` needs next to nothing: clamp the volume-backup pod's resources (never above the app's own LimitRange)."""
    out = dict(volume)
    out.update(cpu_limit_m=min(50, volume["cpu_limit_m"]), mem_limit_mi=min(32, volume["mem_limit_mi"]),
               eph_limit_mi=min(16, volume["eph_limit_mi"]), cpu_request_m=min(10, volume["cpu_request_m"]),
               mem_request_mi=min(16, volume["mem_request_mi"]), eph_request_mi=min(8, volume["eph_request_mi"]))
    return out


def _poll(session: Session, runner: BackupRunner, now: datetime) -> None:
    grace = timedelta(seconds=config.VOLUME_STAT_DEADLINE_SECONDS + 60)
    for app in session.execute(select(models.App).where(models.App.volume_stat_job.is_not(None))).scalars().all():
        ns = k8s_manifests.namespace_name(app.id)
        try:
            st = runner.status(app.volume_stat_job, ns)
        except Exception as exc:  # noqa: BLE001 - API blip: look again next tick
            log.warning("volume stat %s: status check failed (%s)", app.id, type(exc).__name__)
            continue
        started = app.volume_stat_at
        if st.state == "succeeded":
            record(app, (st.result or {}).get("used_bytes"), now)
        elif st.state == "running" and not (started is not None and now - started > grace):
            continue
        name, app.volume_stat_job = app.volume_stat_job, None
        session.commit()
        try:
            runner.cleanup(name, ns)
        except Exception as exc:  # noqa: BLE001 - the tenant sweep retries
            log.warning("volume stat %s: cleanup failed (%s)", app.id, type(exc).__name__)


def _due(session: Session, now: datetime) -> list[models.App]:
    from app import backups

    since = now - timedelta(seconds=config.VOLUME_STAT_INTERVAL_SECONDS)
    busy = set(session.execute(select(models.BackupRun.app_id).where(
        models.BackupRun.kind == "volume", models.BackupRun.status.in_(backups.ACTIVE))).scalars())
    apps = session.execute(select(models.App).where(
        models.App.deleted_at.is_(None), models.App.volume_size_gi.is_not(None), models.App.volume_phase == "Bound",
        models.App.status == "running", models.App.volume_stat_job.is_(None),
        (models.App.volume_stat_at.is_(None)) | (models.App.volume_stat_at <= since),
    ).order_by(models.App.volume_stat_at.is_not(None), models.App.volume_stat_at, models.App.id)).scalars().all()
    return [a for a in apps if a.id not in busy and backups.has_volume(a)]


def tick(session: Session, runner: BackupRunner, now: datetime) -> None:
    """Collect finished stat Jobs, then start a few for volumes not measured lately. Never raises."""
    from app import backups

    try:
        _poll(session, runner, now)
        if not backups.volume_backups_on():
            return
        started = 0
        for app in _due(session, now):
            if started >= config.VOLUME_STAT_MAX_PER_TICK:
                break
            try:
                spec = BackupJobSpec(name=job_name(app.id), run_id=0, app_id=app.id, kind="volume",
                                     env={"MODE": "stat", "KIND": "volume"}, namespace=k8s_manifests.namespace_name(app.id),
                                     volume=_tiny(backups._volume_job(app)), deadline_seconds=config.VOLUME_STAT_DEADLINE_SECONDS)
            except ValueError:
                continue
            app.volume_stat_at = now  # also throttles a failing start: the next try is an interval away
            try:
                runner.cleanup(spec.name, spec.namespace)  # a leftover of the same name would make the create conflict
                runner.start(spec)
            except Exception as exc:  # noqa: BLE001
                log.warning("volume stat %s: could not start (%s)", app.id, type(exc).__name__)
                session.commit()
                continue
            app.volume_stat_job = spec.name
            session.commit()
            started += 1
    except Exception as exc:  # noqa: BLE001 - measuring is best effort
        log.warning("volume stat pass failed (%s)", type(exc).__name__)
        session.rollback()
