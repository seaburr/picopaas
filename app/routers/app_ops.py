"""Operator actions on one app: container logs and rolling restart."""
import json
import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import audit, config, resources, image_check, image_history, k8s_manifests, models, schemas
from app.db import get_db
from app.dependencies import get_active_user, get_kube, get_registry
from app.providers.base import KubeProvider, Registry
from app.routers.apps import _app_out

log = logging.getLogger(__name__)
router = APIRouter(prefix="/apps", tags=["apps"])

MAX_LOG_TAIL = 1000


class PodLogsOut(BaseModel):
    pod: str
    lines: List[str]
    note: Optional[str] = None
    restarts: int = 0


class LogsOut(BaseModel):
    """`lines`/`pod` are the flat view (pod is null when several pods are merged; lines are grouped under
    "=== pod <name> ===" headers). `pods` lists every pod you can pass as ?pod=; `sections` is per-pod."""
    lines: List[str]
    pod: Optional[str] = None
    note: Optional[str] = None
    pods: List[str] = []
    sections: List[PodLogsOut] = []


def _get_app_or_404(db: Session, app_id: int, user: models.User) -> models.App:
    """Owner or administrator; anyone else gets 404 (no existence leak)."""
    q = db.query(models.App).filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    if not user.is_platform_admin:
        q = q.filter(models.App.owner_id == user.id)
    app = q.first()
    if app is None:
        raise HTTPException(status_code=404, detail="App not found.")
    return app


@router.get("/{app_id}/logs", response_model=LogsOut)
def app_logs(
    app_id: int,
    tail: int = Query(200, ge=1, description=f"Number of lines PER POD (capped at {MAX_LOG_TAIL})"),
    pod: Optional[str] = Query(None, max_length=253, description="Only this pod (one of `pods` in the response)"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
    kube: KubeProvider = Depends(get_kube),
):
    app = _get_app_or_404(db, app_id, user)
    tail = min(tail, MAX_LOG_TAIL)
    # The namespace comes only from the app id: this can never read a platform pod.
    namespace = k8s_manifests.namespace_name(app.id)
    try:
        result = kube.pod_logs(namespace, tail, pod)
    except LookupError:
        raise HTTPException(status_code=404, detail="Pod not found.")
    except Exception as exc:  # noqa: BLE001
        log.warning("app %s: reading logs failed: %s", app.id, type(exc).__name__)
        return LogsOut(lines=[], pod=None, note="Couldn't read the logs right now. Try again in a moment.")
    return LogsOut(
        lines=result.lines, pod=result.pod, note=result.note, pods=result.pods,
        sections=[PodLogsOut(pod=x.pod, lines=x.lines, note=x.note, restarts=x.restarts) for x in result.sections],
    )


class PodUsage(BaseModel):
    cpu_m: int
    mem_mi: int


class MetricPoint(BaseModel):
    ts: datetime
    cpu_m: int  # SUM over the pods running at that moment
    mem_mi: int  # SUM over the pods running at that moment
    pods: int = 1  # how many pods were summed (can differ from `replicas`, e.g. before a scale change or during a rollout)
    per_pod: Dict[str, PodUsage] = {}  # pod name -> usage; empty on samples taken before per-pod tracking


class MetricsHistoryOut(BaseModel):
    """Usage is summed over pods, so draw it against the AGGREGATE capacity, not the per-pod limit.
    Gauge: latest point vs cpu_capacity_m / mem_capacity_mi. A historical point's own capacity is
    point.pods * cpu_limit_m (per-pod limit)."""
    points: List[MetricPoint]
    cpu_limit_m: int  # per pod
    mem_limit_mi: int  # per pod
    replicas: int  # desired replica count now
    pods: int  # pods in the latest sample (or `replicas` with no samples)
    cpu_capacity_m: int  # max(replicas, pods) x cpu_limit_m
    mem_capacity_mi: int


@router.get("/{app_id}/metrics-history", response_model=MetricsHistoryOut)
def app_metrics_history(
    app_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
):
    """Recent CPU/memory samples, oldest first (at most METRICS_RETENTION_POINTS)."""
    app = _get_app_or_404(db, app_id, user)
    rows = (
        db.query(models.AppMetricSample)
        .filter(models.AppMetricSample.app_id == app.id)
        .order_by(models.AppMetricSample.id.desc())
        .limit(config.METRICS_RETENTION_POINTS)
        .all()
    )
    eff = resources.effective(app)
    replicas = max(1, app.replicas or 1)
    latest_pods = max(1, rows[0].pods or 1) if rows else replicas
    slots = max(replicas, latest_pods)
    return MetricsHistoryOut(
        points=[MetricPoint(ts=r.ts, cpu_m=r.cpu_m, mem_mi=r.mem_mi, pods=max(1, r.pods or 1),
                            per_pod=_per_pod(r.per_pod)) for r in reversed(rows)],
        cpu_limit_m=eff["cpu_limit_m"], mem_limit_mi=eff["mem_limit_mi"],
        replicas=replicas, pods=latest_pods,
        cpu_capacity_m=slots * eff["cpu_limit_m"], mem_capacity_mi=slots * eff["mem_limit_mi"],
    )


def _per_pod(raw: Optional[str]) -> dict:
    try:
        data = json.loads(raw) if raw else {}
        return {str(k): PodUsage(cpu_m=int(v[0]), mem_mi=int(v[1])) for k, v in data.items()}
    except Exception:  # noqa: BLE001 - a malformed historical row must never break the endpoint
        return {}


@router.get("/{app_id}/events", response_model=List[schemas.AppEventOut])
def app_events(
    app_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
):
    """Recent restart events (disk, out of memory, crash), newest first; at most the last 20."""
    app = _get_app_or_404(db, app_id, user)
    return list(app.events)


@router.post("/{app_id}/restart", response_model=schemas.AppOut)
def restart_app(
    app_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
    registry: Registry = Depends(get_registry),
):
    app = _get_app_or_404(db, app_id, user)
    if app.status == "suspended":
        raise HTTPException(status_code=409, detail="This app is suspended, so it can't be restarted.")
    # Restart = "pull latest": re-resolve the tag first. A new digest that fails the checks (or a registry
    # hiccup) restarts the previous one; the reason is recorded in image_update_error.
    try:
        image_check.refresh_app_image(db, app, registry, actor=user)
    except Exception:  # noqa: BLE001
        log.exception("app %s: image refresh on restart failed", app.id)
        db.rollback()
        app = _get_app_or_404(db, app_id, user)
    # A new nonce changes the pod template (and so the rendered hash): the pod rolls and the drift sweep agrees.
    app.restart_nonce = uuid.uuid4().hex[:16]
    app.status = "pending"
    app.status_detail = None
    app.status_reason = None
    app.reset_retry()
    db.commit()
    db.refresh(app)
    audit.record(db, user, "app.restart", "app", app.id)
    return _app_out(app)


@router.post("/{app_id}/check-image", response_model=schemas.AppOut)
def check_image(
    app_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
    registry: Registry = Depends(get_registry),
):
    """Re-resolve the app's image tag now; a new image that passes the checks is rolled out."""
    app = _get_app_or_404(db, app_id, user)
    if app.status == "suspended":
        raise HTTPException(status_code=409, detail="This app is suspended, so its image can't be updated.")
    outcome = image_check.refresh_app_image(db, app, registry, actor=user)
    if outcome == "error":
        raise HTTPException(status_code=502, detail="Couldn't reach the registry to check the image. Try again in a moment.")
    db.refresh(app)
    return _app_out(app)


@router.get("/{app_id}/image-history", response_model=List[schemas.ImageVersionOut])
def image_history_list(
    app_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
):
    """Recent image versions that ran successfully, newest first (at most 5)."""
    app = _get_app_or_404(db, app_id, user)
    return [
        schemas.ImageVersionOut(digest=h.digest, image_ref=h.image_ref, created_at=h.created_at,
                                current=h.digest == app.image_digest)
        for h in app.image_history
    ]


@router.post("/{app_id}/rollback", response_model=schemas.AppOut)
def rollback_app(
    app_id: int,
    payload: schemas.RollbackRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
):
    """Redeploy a previous image version from this app's history."""
    app = _get_app_or_404(db, app_id, user)
    if app.status == "suspended":
        raise HTTPException(status_code=409, detail="This app is suspended, so it can't be rolled back.")
    entry = next((h for h in app.image_history if h.digest == payload.digest), None)
    if entry is None:
        raise HTTPException(status_code=400, detail="That version isn't in this app's recent history.")
    image_history.manual_rollback(db, app, entry, user)
    db.refresh(app)
    return _app_out(app)
