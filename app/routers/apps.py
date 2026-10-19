import logging
from datetime import datetime, timezone
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ConfigDict, BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, crypto, models, reconciler, resources, schemas
from app.config import MAX_IMAGE_SIZE_BYTES
from app.db import get_db
from app.dependencies import get_active_user, get_registry
from app.env_refs import EnvValidationError, validate_env_value
from app.naming import generate_unique_hostname
from app.providers.base import (
    MySQLAdmin,
    MySQLAdminError,
    ObjectStore,
    ObjectStoreError,
    Registry,
    RegistryError,
    generate_password,
)
from app.schemas import MASKED_VALUE

log = logging.getLogger(__name__)
router = APIRouter(prefix="/apps", tags=["apps"])

DOMAIN_SUFFIX = "raisehellpraisedale.xyz"


def _check_non_root(user: str) -> None:
    """Root images aren't supported (ARCHITECTURE.md "No root images"). Pods run with
    runAsNonRoot, which kubelet can only verify for a numeric UID, so require one up front
    instead of letting the pod fail to start later."""
    uid = user.split(":", 1)[0].strip()
    if uid in ("", "0", "root"):
        raise HTTPException(
            status_code=422,
            detail="image runs as root, which isn't supported. Add a non-root numeric user to the Dockerfile, e.g. `USER 10001`",
        )
    if not uid.isdigit():
        raise HTTPException(
            status_code=422,
            detail=f"image USER '{user}' must be a numeric UID (e.g. `USER 10001`) so the platform can verify it isn't root",
        )


def _check_image_cap(registry: Registry, image: str) -> None:
    """Enforce the image size cap (ARCHITECTURE.md §2 "Image size").

    Any registry failure (not found, private, unparsable manifest) is
    reported the same way — a precise reason wouldn't be actionable for the
    caller and would narrow a probing surface.
    """
    try:
        info = registry.inspect(image)
    except RegistryError:
        raise HTTPException(status_code=422, detail="image not found or not public")
    _check_non_root(info.user)
    size = info.size_bytes
    if size > MAX_IMAGE_SIZE_BYTES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"image is {size} bytes, over the {MAX_IMAGE_SIZE_BYTES} byte "
                f"({MAX_IMAGE_SIZE_BYTES // 1024 // 1024}MB) platform cap"
            ),
        )


def _env_var_out(env_var: models.AppEnvVar) -> schemas.EnvVarOut:
    plain = crypto.try_decrypt(env_var.value_encrypted)
    bad = plain is None
    if bad:
        log.warning("app %s: env var %s is undecryptable", env_var.app_id, env_var.key)
    value = MASKED_VALUE if env_var.is_secret else (plain or "")
    return schemas.EnvVarOut(key=env_var.key, value=value, is_secret=env_var.is_secret, undecryptable=bad)


def _app_out(app: models.App) -> schemas.AppOut:
    return schemas.AppOut(
        id=app.id,
        owner_id=app.owner_id,
        name=app.name,
        hostname=f"{app.hostname}.{DOMAIN_SUFFIX}",
        image=app.image,
        port=app.port,
        want_db=app.want_db,
        want_storage=app.want_storage,
        health_check_enabled=app.health_check_enabled,
        health_check_path=app.health_check_path,
        status=app.status,
        status_detail=app.status_detail,
        retry_at=reconciler.retry_at(app),
        created_at=app.created_at,
        resources=resources.describe(app),
        env=[_env_var_out(e) for e in app.env_vars],
    )


def _get_owned_app_or_404(db: Session, app_id: int, owner_id: int) -> models.App:
    app = (
        db.query(models.App)
        .filter(models.App.id == app_id, models.App.owner_id == owner_id, models.App.deleted_at.is_(None))
        .first()
    )
    if app is None:
        raise HTTPException(status_code=404, detail="app not found")
    return app


def _forbid_if_suspended(app: models.App) -> None:
    if app.status == "suspended":
        raise HTTPException(status_code=403, detail="app suspended")


def _validate_env(env: List[schemas.EnvVarIn], want_db: bool, want_storage: bool) -> None:
    seen = set()
    for item in env:
        if item.key in seen:
            raise HTTPException(status_code=422, detail=f"duplicate env key {item.key!r}")
        seen.add(item.key)
        try:
            validate_env_value(item.value, want_db=want_db, want_storage=want_storage)
        except EnvValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc))


def _active_app_count(db: Session, owner_id: int) -> int:
    return (
        db.query(models.App)
        .filter(models.App.owner_id == owner_id, models.App.deleted_at.is_(None))
        .count()
    )


@router.post("", response_model=schemas.AppOut, status_code=201)
def create_app(
    payload: schemas.AppCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_active_user),
    registry: Registry = Depends(get_registry),
):
    if not current_user.is_uberadmin:
        if _active_app_count(db, current_user.id) >= current_user.app_limit:
            raise HTTPException(
                status_code=403,
                detail=f"app limit reached ({current_user.app_limit}); contact the uberadmin to raise it",
            )

    _check_image_cap(registry, payload.image)
    _validate_env(payload.env, want_db=payload.want_db, want_storage=payload.want_storage)

    hostname = generate_unique_hostname(db)
    app = models.App(
        owner_id=current_user.id,
        name=payload.name,
        hostname=hostname,
        image=payload.image,
        port=payload.port,
        want_db=payload.want_db,
        want_storage=payload.want_storage,
        health_check_enabled=payload.health_check_enabled,
        health_check_path=payload.health_check_path,
        status="pending",
    )
    for item in payload.env:
        app.env_vars.append(
            models.AppEnvVar(
                key=item.key,
                value_encrypted=crypto.encrypt(item.value),
                is_secret=item.is_secret,
            )
        )
    db.add(app)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="hostname collision, please retry")
    db.refresh(app)
    audit.record(db, current_user, "app.create", "app", app.id, f"name={app.name}")
    return _app_out(app)


@router.get("", response_model=List[schemas.AppOut])
def list_apps(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_active_user),
):
    apps = (
        db.query(models.App)
        .filter(models.App.owner_id == current_user.id, models.App.deleted_at.is_(None))
        .order_by(models.App.created_at.desc())
        .all()
    )
    return [_app_out(a) for a in apps]


@router.get("/{app_id}", response_model=schemas.AppOut)
def get_app(
    app_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_active_user),
):
    app = _get_owned_app_or_404(db, app_id, current_user.id)
    return _app_out(app)


@router.patch("/{app_id}", response_model=schemas.AppOut)
def update_app(
    app_id: int,
    payload: schemas.AppUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_active_user),
    registry: Registry = Depends(get_registry),
):
    app = _get_owned_app_or_404(db, app_id, current_user.id)
    _forbid_if_suspended(app)

    workload_changed = False  # anything that changes the rendered Deployment/Secret
    if payload.name is not None:
        app.name = payload.name
    if payload.image is not None:
        _check_image_cap(registry, payload.image)
        if payload.image != app.image:
            workload_changed = True
        app.image = payload.image
    if payload.port is not None:
        if payload.port != app.port:
            workload_changed = True
        app.port = payload.port
    health_changed = False
    if payload.health_check_enabled is not None and payload.health_check_enabled != app.health_check_enabled:
        app.health_check_enabled = payload.health_check_enabled
        health_changed = True
    if payload.health_check_path is not None and payload.health_check_path != app.health_check_path:
        app.health_check_path = payload.health_check_path
        health_changed = True
    if health_changed:
        workload_changed = True
    if payload.env is not None:
        _validate_env(payload.env, want_db=app.want_db, want_storage=app.want_storage)
        # Clients only ever see MASKED_VALUE for secrets, so sending it back means "unchanged":
        # keep the stored ciphertext instead of overwriting the secret with the mask.
        existing_secrets = {v.key: v.value_encrypted for v in app.env_vars if v.is_secret}
        old_env = {v.key: (crypto.try_decrypt(v.value_encrypted), v.is_secret) for v in app.env_vars}
        app.env_vars.clear()
        db.flush()  # delete old rows before inserting, or the (app_id, key) unique constraint trips
        for item in payload.env:
            if item.is_secret and item.value == MASKED_VALUE and item.key in existing_secrets:
                value_encrypted = existing_secrets[item.key]
            else:
                value_encrypted = crypto.encrypt(item.value)
            app.env_vars.append(
                models.AppEnvVar(key=item.key, value_encrypted=value_encrypted, is_secret=item.is_secret)
            )
        new_env = {v.key: (crypto.try_decrypt(v.value_encrypted), v.is_secret) for v in app.env_vars}
        if new_env != old_env:
            workload_changed = True

    if workload_changed:
        # The reconciler only sweeps pending/deploying apps: re-render so the pod rolls
        # (the env-checksum annotation changes with env). Suspended apps can't reach here.
        app.status = "pending"
        app.status_detail = None
        app.reset_retry()
    db.commit()
    db.refresh(app)
    audit.record(db, current_user, "app.update", "app", app.id)
    return _app_out(app)


@router.delete("/{app_id}", status_code=204)
def delete_app(
    app_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_active_user),
):
    app = _get_owned_app_or_404(db, app_id, current_user.id)
    _forbid_if_suspended(app)  # no delete+recreate to evade suspension; admin can still delete
    app.deleted_at = datetime.now(timezone.utc)
    db.commit()
    audit.record(db, current_user, "app.delete", "app", app_id)
    return None


# --- credential rotation ---------------------------------------------------

class RotateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    target: Literal["db", "storage"]


def get_mysql_admin_factory():
    """Overridden in tests with a FakeMySQLAdmin factory."""
    from app.providers.mysql import MySQLAdminClient

    return MySQLAdminClient


def get_object_store() -> ObjectStore:
    """Overridden in tests with a FakeObjectStore."""
    from app.providers.spaces import SpacesStore

    return SpacesStore.from_env()


@router.post("/{app_id}/rotate-credentials", response_model=schemas.AppOut)
def rotate_credentials(
    app_id: int,
    body: RotateIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
    mysql_admin_factory=Depends(get_mysql_admin_factory),
    object_store: ObjectStore = Depends(get_object_store),
):
    app = _get_owned_app_or_404(db, app_id, user.id)
    _forbid_if_suspended(app)
    if body.target == "db":
        row = app.database
        if row is None:
            raise HTTPException(status_code=409, detail="database not provisioned yet")
        # Persist-first (issue #2): commit the new password BEFORE ALTER USER, so a failed
        # commit leaves MySQL untouched and the stored password valid. If ALTER then fails,
        # restore the old stored password. If even that restore fails, the stored password
        # is wrong, but the next rotation repairs it (ALTER USER doesn't need the old one).
        old_encrypted = row.password_encrypted
        new_password = generate_password()
        row.password_encrypted = crypto.encrypt(new_password)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise HTTPException(status_code=502, detail="credential rotation failed")
        try:
            mysql_admin_factory(row.server).set_password(app.id, new_password)
        except MySQLAdminError:
            try:
                row.password_encrypted = old_encrypted
                db.commit()
            except Exception:
                db.rollback()
            raise HTTPException(status_code=502, detail="credential rotation failed")
    else:
        row = app.storage
        if row is None:
            raise HTTPException(status_code=409, detail="storage not provisioned yet")
        # Create new key -> commit -> only then revoke the old key (issue #2). If the commit
        # fails, discard the NEW key (best effort); the old key is still stored and working.
        try:
            creds = object_store.rotate_key(app.id)
        except ObjectStoreError:
            raise HTTPException(status_code=502, detail="credential rotation failed")
        row.access_key = creds["access_key"]
        row.secret_key_encrypted = crypto.encrypt(creds["secret_key"])
        try:
            db.commit()
        except Exception:
            db.rollback()
            try:
                object_store.delete_key(app.id, creds["access_key"])
            except ObjectStoreError:
                log.warning("could not discard unpersisted key for app %s", app.id)
            raise HTTPException(status_code=502, detail="credential rotation failed")
        try:
            object_store.revoke_other_keys(app.id, creds["access_key"])
        except ObjectStoreError:
            # New creds are stored and work; the quota poll retries the revoke.
            log.warning("could not revoke old storage key for app %s", app.id)
    # The reconciler re-renders the Secret from the stored creds and rolls the pod.
    app.status = "pending"
    app.reset_retry()
    db.commit()
    db.refresh(app)
    audit.record(db, user, "app.rotate", "app", app.id, f"target={body.target}")
    return _app_out(app)
