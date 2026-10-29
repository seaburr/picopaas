"""Per-app image history (last N digests that reached 'running'), manual rollback and deploy auto-rollback."""
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app import audit, models

HISTORY_KEPT = 5
ROLLBACK_MESSAGE = "New image failed to start; rolled back to the previous version."


def _now() -> datetime:
    return models._utcnow()


def record_running(session: Session, app: models.App) -> bool:
    """Remember the digest the app is running on. De-dupes a repeat of the newest entry; keeps the newest 5."""
    digest = app.image_digest
    if not digest or app.status != "running":
        return False
    hist = list(app.image_history)
    if hist and hist[0].digest == digest:
        return False
    session.add(models.AppImageHistory(
        app_id=app.id, digest=digest, image_ref=app.image, run_as_uid=app.run_as_uid, created_at=_now()))
    session.flush()
    session.expire(app, ["image_history"])
    for old in list(app.image_history)[HISTORY_KEPT:]:
        session.delete(old)
    session.commit()
    return True


def known_digests(app: models.App) -> set[str]:
    return {h.digest for h in app.image_history}


def previous_good(app: models.App) -> Optional[models.AppImageHistory]:
    """Newest history entry other than the digest currently deployed."""
    for h in app.image_history:
        if h.digest != app.image_digest:
            return h
    return None


def _apply_entry(app: models.App, entry: models.AppImageHistory, rejected: Optional[str]) -> None:
    app.image = entry.image_ref
    app.image_digest = entry.digest
    app.run_as_uid = entry.run_as_uid
    app.rejected_digest = rejected
    app.status = "pending"
    app.status_reason = None
    app.deploying_since = None
    app.reset_retry()


def manual_rollback(session: Session, app: models.App, entry: models.AppImageHistory, actor: models.User) -> None:
    old = app.image_digest
    _apply_entry(app, entry, rejected=old if old != entry.digest else None)
    app.status_detail = None
    app.image_update_error = None
    session.commit()
    audit.record(session, actor, "app.rollback", "app", app.id, f"{(old or '')[:19]} -> {entry.digest[:19]}")


def maybe_auto_rollback(session: Session, app: models.App) -> bool:
    """Called when a rollout failed. Rolls back only if the currently deployed digest never ran successfully
    (so env-only changes and failures of a known-good image are left alone) and a previous good digest exists."""
    if app.status == "suspended" or not app.image_digest or app.image_digest in known_digests(app):
        return False
    prev = previous_good(app)
    if prev is None:
        return False
    bad = app.image_digest
    _apply_entry(app, prev, rejected=bad)
    app.status_detail = ROLLBACK_MESSAGE
    if app.image_update_error != ROLLBACK_MESSAGE:
        app.image_update_error_dismissed = False
    app.image_update_error = ROLLBACK_MESSAGE
    session.add(models.AppEvent(
        app_id=app.id, at=_now(), kind="rollback", message=ROLLBACK_MESSAGE,
        dedupe_key=f"rollback:{bad}:{_now().isoformat()}"[:160]))
    session.commit()
    audit.record(session, None, "app.auto_rollback", "app", app.id,
                 f"{bad[:19]} -> {prev.digest[:19]}", actor_email="system")
    return True
