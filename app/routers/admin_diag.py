"""Admin Health extras: backup storage/failures and platform component logs. Admin only; platform namespaces only."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app import admin_diag, audit, models
from app.log_redact import redact_line
from app.db import get_db
from app.dependencies import get_kube, require_admin_session, require_platform_admin
from app.providers.base import is_platform_namespace

router = APIRouter(prefix="/admin/health", tags=["admin"], dependencies=[Depends(require_platform_admin)])
log = logging.getLogger(__name__)

NS_PATTERN = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
POD_PATTERN = r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$"
CONTAINER_PATTERN = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
VIEW_DEFAULT_TAIL, VIEW_MAX_TAIL = 200, 2000
DOWNLOAD_MAX_TAIL = 10000
VIEW_MAX_BYTES = 512 * 1024
DOWNLOAD_MAX_BYTES = 5 * 1024 * 1024
MAX_SINCE_SECONDS = 7 * 86400


@router.get("/backups")
def health_backups(days: int = Query(7, ge=1, le=30), db: Session = Depends(get_db)):
    """Backup storage vs the platform cap (+ top apps), failed backups in the last `days` days with the reason, stuck runs."""
    return admin_diag.backup_health(db, days)


@router.post("/backups/platform-db/run", status_code=202, dependencies=[Depends(require_admin_session)])
def platform_db_backup_run(db: Session = Depends(get_db), admin=Depends(require_platform_admin)):
    """Queue a backup of the platform's own database now (the scheduler tick starts it within BACKUP_TICK_SECONDS)."""
    from app import audit, platform_backup
    if not platform_backup.active():
        raise HTTPException(status_code=409, detail="Platform database backups aren't active (backups off, bucket not configured, or the platform DB isn't MySQL).")
    run = platform_backup.enqueue(db, "manual")
    if run is None:
        raise HTTPException(status_code=409, detail="A platform database backup is already running.")
    audit.record(db, admin, "platform_backup.create", "platform", "db", f"run={run.id} trigger=manual")
    return {"run_id": run.id, "status": run.status}


@router.get("/logs/pods", dependencies=[Depends(require_admin_session)])
def platform_pods(kube=Depends(get_kube)):
    """Platform components (pods in platform namespaces, discovered; never app-* namespaces) and their containers."""
    try:
        pods = kube.platform_pods()
    except Exception as exc:  # noqa: BLE001 - never echo exception text
        log.warning("platform pod list failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="We couldn't list the platform pods right now.")
    pods = [p for p in pods if is_platform_namespace(p["namespace"])]
    return {"namespaces": sorted({p["namespace"] for p in pods}), "pods": pods}


def _read(kube, namespace: str, pod: str, container: str, tail: int, since: Optional[int], previous: bool,
          max_bytes: int) -> tuple[list[str], bool]:
    if not is_platform_namespace(namespace):
        raise HTTPException(status_code=403, detail="Only platform namespaces can be read here.")
    try:
        lines = kube.platform_log(namespace, pod, container, tail, since, previous)
    except ValueError:
        raise HTTPException(status_code=403, detail="Only platform namespaces can be read here.")
    except LookupError:
        raise HTTPException(status_code=404, detail="We can't find that pod or container.")
    except Exception as exc:  # noqa: BLE001
        log.warning("platform log read failed (%s/%s): %s", namespace, pod, type(exc).__name__)
        raise HTTPException(status_code=502, detail="We couldn't read those logs right now.")
    lines = [redact_line(ln) for ln in lines[-tail:]]
    kept: list[str] = []
    size = 0
    for ln in reversed(lines):  # keep the newest lines inside the byte budget
        size += len(ln.encode("utf-8", "replace")) + 1
        if size > max_bytes:
            break
        kept.append(ln)
    kept.reverse()
    return kept, len(kept) < len(lines)


@router.get("/logs", dependencies=[Depends(require_admin_session)])
def platform_logs(namespace: str = Query(..., pattern=NS_PATTERN), pod: str = Query(..., pattern=POD_PATTERN),
                  container: str = Query(..., pattern=CONTAINER_PATTERN),
                  tail: int = Query(VIEW_DEFAULT_TAIL, ge=1, le=VIEW_MAX_TAIL),
                  since_seconds: Optional[int] = Query(None, ge=1, le=MAX_SINCE_SECONDS), previous: bool = False,
                  kube=Depends(get_kube)):
    lines, truncated = _read(kube, namespace, pod, container, tail, since_seconds, previous, VIEW_MAX_BYTES)
    return {"namespace": namespace, "pod": pod, "container": container, "previous": previous, "lines": lines,
            "truncated": truncated}


@router.get("/logs/download", dependencies=[Depends(require_admin_session)])
def platform_logs_download(namespace: str = Query(..., pattern=NS_PATTERN), pod: str = Query(..., pattern=POD_PATTERN),
                           container: str = Query(..., pattern=CONTAINER_PATTERN),
                           tail: int = Query(DOWNLOAD_MAX_TAIL, ge=1, le=DOWNLOAD_MAX_TAIL),
                           since_seconds: Optional[int] = Query(None, ge=1, le=MAX_SINCE_SECONDS),
                           previous: bool = False, kube=Depends(get_kube), db: Session = Depends(get_db),
                           admin: models.User = Depends(require_platform_admin)):
    lines, _ = _read(kube, namespace, pod, container, tail, since_seconds, previous, DOWNLOAD_MAX_BYTES)
    audit.record(db, admin, "platform_logs.download", "pod", f"{namespace}/{pod}",
                 f"container={container} lines={len(lines)} previous={previous}")
    return Response("\n".join(lines) + ("\n" if lines else ""), media_type="text/plain; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{namespace}_{pod}_{container}.log"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
