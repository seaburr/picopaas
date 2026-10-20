"""Operator actions on one app: container logs and rolling restart."""
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import audit, k8s_manifests, models, schemas
from app.db import get_db
from app.dependencies import get_active_user, get_kube
from app.providers.base import KubeProvider
from app.routers.apps import _app_out

log = logging.getLogger(__name__)
router = APIRouter(prefix="/apps", tags=["apps"])

MAX_LOG_TAIL = 1000


class LogsOut(BaseModel):
    lines: List[str]
    pod: Optional[str] = None
    note: Optional[str] = None


def _get_app_or_404(db: Session, app_id: int, user: models.User) -> models.App:
    """Owner or administrator; anyone else gets 404 (no existence leak)."""
    q = db.query(models.App).filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    if not user.is_uberadmin:
        q = q.filter(models.App.owner_id == user.id)
    app = q.first()
    if app is None:
        raise HTTPException(status_code=404, detail="App not found.")
    return app


@router.get("/{app_id}/logs", response_model=LogsOut)
def app_logs(
    app_id: int,
    tail: int = Query(200, ge=1, description=f"Number of lines (capped at {MAX_LOG_TAIL})"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
    kube: KubeProvider = Depends(get_kube),
):
    app = _get_app_or_404(db, app_id, user)
    tail = min(tail, MAX_LOG_TAIL)
    # The namespace comes only from the app id: this can never read a platform pod.
    namespace = k8s_manifests.namespace_name(app.id)
    try:
        result = kube.pod_logs(namespace, tail)
    except Exception as exc:  # noqa: BLE001
        log.warning("app %s: reading logs failed: %s", app.id, type(exc).__name__)
        return LogsOut(lines=[], pod=None, note="Couldn't read the logs right now. Try again in a moment.")
    return LogsOut(lines=result.lines, pod=result.pod, note=result.note)


@router.post("/{app_id}/restart", response_model=schemas.AppOut)
def restart_app(
    app_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
):
    app = _get_app_or_404(db, app_id, user)
    if app.status == "suspended":
        raise HTTPException(status_code=409, detail="This app is suspended, so it can't be restarted.")
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
