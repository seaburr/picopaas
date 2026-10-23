"""Persistent volume rules: mount-path validation, size limits, capacity. The k8s objects are in k8s_manifests.py."""
from __future__ import annotations

import posixpath
from typing import Any, Optional

from fastapi import HTTPException

from app import config

DISABLED_MSG = "Persistent volumes aren't enabled on this platform."
FORBIDDEN_PREFIXES = ("/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/tmp", "/run", "/var/run", "/var/lock")
MOUNT_PATH_MAX = 255


def validate_mount_path(path: str) -> str:
    """Absolute, normalised, not "/", no "..", not under a system directory. Returns the path or raises ValueError."""
    p = (path or "").strip()
    if not p.startswith("/"):
        raise ValueError("Mount path must be an absolute path starting with /, for example /data.")
    if len(p) > MOUNT_PATH_MAX:
        raise ValueError(f"Mount path must be {MOUNT_PATH_MAX} characters or fewer.")
    if not p.isprintable() or any(c in p for c in " \t\\:,\"'"):
        raise ValueError("Mount path can't contain spaces or special characters.")
    if ".." in p.split("/"):
        raise ValueError("Mount path can't contain '..'.")
    # posixpath.normpath keeps exactly two leading slashes ("//etc" stays "//etc"), which would slip past the prefix
    # checks below while the container runtime resolves it to /etc. Collapse any run of leading slashes first.
    norm = posixpath.normpath("/" + p.lstrip("/"))
    if norm == "/":
        raise ValueError("Mount path can't be the root directory.")
    for pre in FORBIDDEN_PREFIXES:
        if norm == pre or norm.startswith(pre + "/"):
            raise ValueError(f"Mount path can't be {pre} or inside it. Pick an app directory such as /data.")
    return norm


def require_enabled() -> None:
    if not config.VOLUMES_ENABLED:
        raise HTTPException(status_code=422, detail=DISABLED_MSG)


def _volume_apps_query(db):
    from app import models
    return db.query(models.App.id).filter(models.App.deleted_at.is_(None), models.App.volume_size_gi.isnot(None))


def total_in_use(db) -> int:
    return _volume_apps_query(db).count()


def _locked_total_in_use(db) -> int:
    # FOR UPDATE on the counted rows (and, under InnoDB REPEATABLE READ, the scanned range) serialises concurrent
    # enables on MySQL until the caller commits; a no-op on SQLite. Same approach as admin._active_admin_count.
    return len(_volume_apps_query(db).with_for_update().all())


def require_capacity(db) -> None:
    if _locked_total_in_use(db) >= config.VOLUMES_MAX_TOTAL:
        raise HTTPException(
            status_code=409,
            detail="The platform has no persistent volume capacity left right now. Try again later or ask an administrator.",
        )


def apply_request(db, app: Any, size_gi: Optional[int], mount_path: Optional[str], *, new_app: bool = False) -> bool:
    """Validate and set the app's volume from a user request (API or spec). Returns True if anything changed.

    Rules: feature flag on; no detaching (callers reject null); size is grow-only and capped at the user ceiling
    (a smaller request keeps the size already granted, it is not an error); replicas must be 1; platform capacity checked when enabling."""
    require_enabled()
    new_size = size_gi if size_gi is not None else (app.volume_size_gi or config.VOLUME_DEFAULT_GI)
    new_path = validate_mount_path(mount_path) if mount_path is not None else (app.volume_mount_path or config.VOLUME_DEFAULT_MOUNT_PATH)
    old_size = app.volume_size_gi
    if new_size < config.OVERRIDE_VOLUME_MIN_GI:
        raise HTTPException(status_code=422, detail=f"Volume size must be at least {config.OVERRIDE_VOLUME_MIN_GI} GiB.")
    if old_size is None:
        if new_size > config.VOLUME_MAX_GI:
            raise HTTPException(status_code=422, detail=f"Volume size can be at most {config.VOLUME_MAX_GI} GiB. Ask an administrator for more.")
        if (app.replicas or 1) > 1:
            raise HTTPException(status_code=409, detail=_REPLICAS_MSG)
        require_capacity(db)
    else:
        if new_size <= old_size:
            new_size = old_size  # never shrink: keep the current (possibly admin-granted) size, no error
        elif new_size > config.VOLUME_MAX_GI:
            raise HTTPException(status_code=422, detail=f"Volume size can be at most {config.VOLUME_MAX_GI} GiB. Ask an administrator for more.")
    changed = (old_size, app.volume_mount_path) != (new_size, new_path)
    app.volume_size_gi = new_size
    app.volume_mount_path = new_path
    if old_size is None:
        app.volume_phase = None
    return changed


_REPLICAS_MSG = ("An app with a persistent volume runs exactly one copy (the volume can only be attached to one pod). "
                 "Ask an administrator to set its replicas to 1 first.")
REPLICAS_WITH_VOLUME_MSG = ("This app has a persistent volume, which can only be attached to one pod, so it can't run "
                            "more than 1 replica.")


def out(app: Any) -> Optional[dict]:
    if app.volume_size_gi is None:
        return None
    return {"size_gi": app.volume_size_gi, "mount_path": app.volume_mount_path or config.VOLUME_DEFAULT_MOUNT_PATH,
            "phase": app.volume_phase}
