"""Admin > Security alerts: rule-based alerts from app/security_alerts.py. Web login only (reads and acknowledge)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, config, models, security_alerts
from app.db import get_db
from app.dependencies import require_admin_action, require_admin_session, require_platform_admin

router = APIRouter(prefix="/admin/security-alerts", tags=["admin"], dependencies=[Depends(require_platform_admin)])


class SecurityAlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    severity: str
    subject_type: Optional[str]
    subject_id: Optional[str]
    message: str
    evidence: Optional[dict[str, Any]]
    first_seen: datetime
    last_seen: datetime
    count: int
    acknowledged_at: Optional[datetime]
    acknowledged_by: Optional[int]


@router.get("", response_model=List[SecurityAlertOut], dependencies=[Depends(require_admin_session)])
def list_alerts(status: Literal["open", "all"] = Query("open"), db: Session = Depends(get_db)):
    q = select(models.SecurityAlert)
    if status == "open":
        q = q.where(models.SecurityAlert.acknowledged_at.is_(None))
    return db.scalars(q.order_by(models.SecurityAlert.last_seen.desc(), models.SecurityAlert.id.desc())
                      .limit(config.SECURITY_ALERT_LIST_MAX)).all()


@router.get("/summary", dependencies=[Depends(require_admin_session)])
def summary(db: Session = Depends(get_db)):
    return {"open": security_alerts.open_count(db)}


@router.post("/{alert_id}/acknowledge", response_model=SecurityAlertOut)
def acknowledge(alert_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_admin_action)):
    alert = db.get(models.SecurityAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found.")
    if alert.acknowledged_at is None:
        security_alerts.acknowledge(db, alert, admin)
        audit.record(db, admin, "security_alert.acknowledge", "security_alert", alert.id, f"kind={alert.kind}")
    return alert
