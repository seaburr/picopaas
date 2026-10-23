"""Image checks shared by the API (create/update/check-image/restart) and the floating-tag watcher.

An app's image is resolved to a manifest digest and the Deployment is pinned to `repo@sha256:...`, so a
re-pushed tag only goes live when we have re-checked it (size cap, non-root) and decided to roll it out.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, models, resources
from app.config import MAX_IMAGE_SIZE_BYTES, NAMED_USER_RUN_AS_UID
from app.providers.base import ImageInfo, Registry, RegistryError

log = logging.getLogger(__name__)

ROOT_MESSAGE = "Image runs as root, which isn't allowed. Add USER 10001 to your Dockerfile, rebuild, and push again."


class ImageRejected(Exception):
    """The image fails a platform check. `message` is for the API caller; `kind` is 'size' or 'root'."""

    def __init__(self, kind: str, message: str, size_mb: int = 0) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.size_mb = size_mb


def is_digest_ref(image: str) -> bool:
    return "@" in image


def image_repo(image: str) -> str:
    """The image reference without its tag or digest."""
    name = image.split("@", 1)[0]
    if name.rfind(":") > name.rfind("/"):
        name = name.rsplit(":", 1)[0]
    return name


def image_tag(image: str) -> str:
    if is_digest_ref(image):
        return ""
    return image.rsplit(":", 1)[1] if image.rfind(":") > image.rfind("/") else "latest"


def digest_of_ref(image: str) -> Optional[str]:
    return image.split("@", 1)[1] if is_digest_ref(image) else None


def pinned_image(app: models.App) -> str:
    """What the Deployment runs: a digest ref as typed, else repo@digest once resolved, else the plain image."""
    if is_digest_ref(app.image) or not app.image_digest:
        return app.image
    return f"{image_repo(app.image)}@{app.image_digest}"


def short_digest(digest: Optional[str]) -> str:
    return (digest or "").removeprefix("sha256:")[:12]


def evaluate(info: ImageInfo, max_bytes: Optional[int] = None) -> Optional[int]:
    """Platform checks on an inspected image. Returns the run_as_uid to force (None = numeric USER)."""
    uid = info.user.split(":", 1)[0].strip()
    if uid in ("", "0", "root"):
        raise ImageRejected("root", ROOT_MESSAGE)
    size = info.size_bytes
    cap = max_bytes or MAX_IMAGE_SIZE_BYTES
    if size > cap:
        mb = math.ceil(size / 1024 / 1024)
        raise ImageRejected(
            "size",
            f"Image is {mb} MB; the limit is {cap // 1024 // 1024} MB. "
            "Slim it down (for example a -slim or alpine base, or a multi-stage build).",
            size_mb=mb,
        )
    return None if uid.isdigit() else NAMED_USER_RUN_AS_UID


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def refresh_app_image(db: Session, app: models.App, registry: Registry, actor: Optional[models.User] = None) -> str:
    """Re-resolve a tag-referenced app's image. Returns one of:
    'pinned' (digest ref, nothing to do), 'unchanged', 'filled' (first digest recorded, no rollout needed),
    'updated' (new digest passed the checks; app set pending), 'rejected' (new digest fails; old keeps running,
    image_update_error set), 'error' (registry problem; nothing changed)."""
    if is_digest_ref(app.image):
        return "pinned"
    try:
        digest = registry.resolve_digest(app.image)
    except RegistryError as exc:
        log.warning("app %s: image resolve failed: %s", app.id, exc)
        return "error"
    if digest == app.rejected_digest and digest != app.image_digest:
        # Failed to start (auto-rollback) or was rolled back by the user: don't redeploy it until a newer digest appears.
        app.image_checked_at = _utcnow()
        db.commit()
        return "unchanged"
    if digest == app.image_digest:
        app.image_checked_at = _utcnow()
        app.image_update_error = None
        db.commit()
        return "unchanged"
    try:
        info = registry.inspect(app.image)
    except RegistryError as exc:
        log.warning("app %s: image inspect failed: %s", app.id, exc)
        return "error"
    digest = info.digest or digest
    tag = image_tag(app.image)
    try:
        cap = resources.image_size_bytes(app)
        run_as_uid = evaluate(info, cap)
    except ImageRejected as rej:
        if rej.kind == "size":
            msg = (f"New image for :{tag} is {rej.size_mb} MB, over the {cap // 1024 // 1024} MB "
                   "limit; still running the previous version.")
        else:
            msg = f"New image for :{tag} runs as root, which isn't allowed; still running the previous version."
        app.image_checked_at = _utcnow()
        app.image_update_error = msg[:255]
        db.commit()
        return "rejected"
    old = app.image_digest
    app.image_digest = digest
    app.rejected_digest = None
    app.image_checked_at = _utcnow()
    app.image_update_error = None
    if old is None:
        # Backfill: the drift sweep re-renders with the pinned ref; no user-visible update.
        app.run_as_uid = run_as_uid
        db.commit()
        return "filled"
    app.run_as_uid = run_as_uid
    if app.status != "suspended":
        app.status = "pending"
        app.status_detail = None
        app.status_reason = None
        app.reset_retry()
    db.commit()
    audit.record(db, actor, "app.image_updated", "app", app.id,
                 f":{tag} {short_digest(old)} -> {short_digest(digest)}", actor_email=None if actor else "system")
    return "updated"


def watch_all(db: Session, registry: Registry) -> dict[str, int]:
    """One watcher pass. Never raises; one registry round-trip per eligible app."""
    counts: dict[str, int] = {}
    ids = db.execute(
        select(models.App.id).where(
            models.App.deleted_at.is_(None), models.App.status != "suspended", models.App.auto_update.is_(True)
        )
    ).scalars().all()
    for app_id in ids:
        try:
            app = db.get(models.App, app_id)
            if app is None or app.deleted_at is not None or app.status == "suspended" or is_digest_ref(app.image):
                continue
            outcome = refresh_app_image(db, app, registry)
            counts[outcome] = counts.get(outcome, 0) + 1
        except Exception:  # noqa: BLE001
            log.exception("image watch failed for app %s", app_id)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return counts
