"""Audit log helper. Never raises into the request path; never put secrets in `detail`."""
import logging
from typing import Optional

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
