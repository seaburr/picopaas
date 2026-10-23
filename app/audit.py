"""Audit log helper. Never raises into the request path; never put secrets in `detail`."""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import models

log = logging.getLogger(__name__)


def record(
    db: Session,
    actor: Optional[models.User],
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[object] = None,
    detail: Optional[str] = None,
    actor_email: Optional[str] = None,
) -> None:
    try:
        db.add(
            models.AuditLog(
                actor_id=actor.id if actor is not None else None,
                actor_email=actor.email if actor is not None else actor_email,
                action=action,
                target_type=target_type,
                target_id=None if target_id is None else str(target_id),
                detail=detail[:255] if detail else None,
            )
        )
        db.commit()
    except Exception:
        log.exception("audit log write failed for action %s", action)
        try:
            db.rollback()
        except Exception:
            pass


PURGE_BATCH = 5000


def purge_old(db: Session, days: int, now: Optional[datetime] = None, batch: int = PURGE_BATCH) -> int:
    """Delete audit rows older than `days` in id batches (portable to MySQL and SQLite). days <= 0 keeps all.
    Returns rows deleted; writes one `audit.purge` row (actor system) when that is > 0."""
    if days <= 0:
        return 0
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=days)
    total = 0
    while True:
        ids = list(db.execute(
            select(models.AuditLog.id).where(models.AuditLog.at < cutoff).order_by(models.AuditLog.id).limit(batch)
        ).scalars())
        if not ids:
            break
        db.execute(delete(models.AuditLog).where(models.AuditLog.id.in_(ids)))
        db.commit()
        total += len(ids)
    if total:
        record(db, None, "audit.purge", "audit", None, f"cutoff={cutoff.isoformat()} rows={total}",
               actor_email="system")
    return total
