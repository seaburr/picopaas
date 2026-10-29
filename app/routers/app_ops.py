"""Operator actions on one app: container logs and rolling restart."""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import audit, config, resources, image_check, image_history, k8s_manifests, models, projects, schemas
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


class PodInfoOut(BaseModel):
    name: str
    created_at: Optional[datetime] = None
    phase: Optional[str] = None
    ready: bool = False
    current: bool = False  # running+ready and part of the newest rollout; false for old/terminating/crashlooping pods
    started_at: Optional[datetime] = None  # when this pod's container started (pod start/creation time if unknown)
    uptime_seconds: Optional[int] = None  # now - started_at; null unless the pod is Running


class LogsOut(BaseModel):
    """`lines`/`pod` are the flat view (pod is null when several pods are merged; lines are grouped under
    "=== pod <name> ===" headers). `pods` lists every pod you can pass as ?pod=; `sections` is per-pod."""
    lines: List[str]
    pod: Optional[str] = None
    note: Optional[str] = None
    pods: List[str] = []
    sections: List[PodLogsOut] = []
    pod_info: List[PodInfoOut] = []  # one per entry of `pods`
    uptime_seconds: Optional[int] = None  # Status card: longest uptime among current running pods (null when none is running)


def _get_app_or_404(db: Session, app_id: int, user: models.User) -> models.App:
    """Project member or administrator; anyone else gets 404 (no existence leak)."""
    return projects.get_app_for_member(db, app_id, user, allow_admin=True, detail="App not found.")


def _uptime(info, now: datetime) -> Optional[int]:
    start = info.started_at or info.created_at
    if start is None or info.phase != "Running":
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return max(0, int((now - start).total_seconds()))


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
    now = datetime.now(timezone.utc)
    infos = [PodInfoOut(name=i.name, created_at=i.created_at, phase=i.phase, ready=i.ready, current=i.current,
                        started_at=i.started_at, uptime_seconds=_uptime(i, now)) for i in result.pod_info]
    ups = [i.uptime_seconds for i in infos if i.current and i.uptime_seconds is not None]
    return LogsOut(
        lines=result.lines, pod=result.pod, note=result.note, pods=result.pods, pod_info=infos,
        uptime_seconds=max(ups) if ups else None,
        sections=[PodLogsOut(pod=x.pod, lines=x.lines, note=x.note, restarts=x.restarts) for x in result.sections],
    )


EVENTS_NOTE = "Kubernetes keeps events for only about 1 hour, so older events are gone."


def _iso(dt: Optional[datetime]) -> str:
    return dt.isoformat().replace("+00:00", "Z") if dt else "-"


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


@router.get("/{app_id}/pods/{pod}/events")
def app_pod_events(
    app_id: int,
    pod: str,
    format: str = Query("text", pattern="^(text|json)$"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
    kube: KubeProvider = Depends(get_kube),
):
    """Kubernetes Events about one pod, oldest first, as a text download (or `?format=json`)."""
    app = _get_app_or_404(db, app_id, user)
    namespace = k8s_manifests.namespace_name(app.id)  # from the app id only
    try:
        events = kube.pod_events(namespace, pod)
    except LookupError:
        raise HTTPException(status_code=404, detail="Pod not found.")
    except Exception as exc:  # noqa: BLE001
        log.warning("app %s: reading pod events failed: %s", app.id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Couldn't read the pod events right now. Try again in a moment.")
    if format == "json":
        return [{"at": e.get("at"), "type": e.get("type"), "reason": e.get("reason"),
                 "count": e.get("count"), "message": e.get("message")} for e in events]
    lines = [f"# {EVENTS_NOTE}"]
    lines += [f"{_iso(e.get('at'))}  {_one_line(e.get('type') or '')}  {_one_line(e.get('reason') or '')}  "
              f"x{e.get('count') or 1}  {_one_line(e.get('message') or '')}" for e in events]
    if not events:
        lines.append("(no events)")
    slug = "".join(c if c.isalnum() or c in "-_." else "-" for c in (app.name or str(app.id))) or str(app.id)
    fname = f"{slug}-{''.join(c if c.isalnum() or c in '-_.' else '-' for c in pod)}-events.txt"
    return PlainTextResponse("\n".join(lines) + "\n", headers={"Content-Disposition": f'attachment; filename="{fname}"'})


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


def _event_or_404(db: Session, app: models.App, event_id: int) -> models.AppEvent:
    ev = db.query(models.AppEvent).filter(models.AppEvent.id == event_id, models.AppEvent.app_id == app.id).first()
    if ev is None:
        raise HTTPException(status_code=404, detail="Alert not found.")
    return ev


@router.post("/{app_id}/alerts/{kind}/dismiss", response_model=schemas.AppOut)
def dismiss_alert(app_id: int, kind: Literal["event", "image_update"], db: Session = Depends(get_db),
                  user: models.User = Depends(get_active_user)):
    """Dismiss the alert currently shown on the app (`event` = the newest restart event, `image_update` = the image update
    error). Owner only. It stays dismissed until a NEW occurrence (a new event / a different update error)."""
    app = projects.get_app_for_member(db, app_id, user, detail="App not found.")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if kind == "event":
        if app.events and app.events[0].dismissed_at is None:
            app.events[0].dismissed_at = now
    elif app.image_update_error:
        app.image_update_error_dismissed = True
    db.commit()
    audit.record(db, user, "app.alert_dismiss", "app", app.id, kind)
    return _app_out(app)


@router.post("/{app_id}/events/{event_id}/dismiss", response_model=schemas.AppEventOut)
def dismiss_event(app_id: int, event_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    """Dismiss an alert (e.g. 'Restarted 17 h ago: exited with code 137'). It stays dismissed; only a NEW occurrence shows again."""
    app = _get_app_or_404(db, app_id, user)
    ev = _event_or_404(db, app, event_id)
    if ev.dismissed_at is None:
        ev.dismissed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
    return ev


@router.delete("/{app_id}/events/{event_id}/dismiss", response_model=schemas.AppEventOut)
def undismiss_event(app_id: int, event_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_active_user)):
    """Show a dismissed alert again."""
    app = _get_app_or_404(db, app_id, user)
    ev = _event_or_404(db, app, event_id)
    if ev.dismissed_at is not None:
        ev.dismissed_at = None
        db.commit()
    return ev


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
