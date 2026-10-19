from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import crypto, models, schemas
from app.db import get_db
from app.dependencies import get_current_user
from app.env_refs import EnvValidationError, validate_env_value
from app.naming import generate_unique_hostname
from app.schemas import MASKED_VALUE

router = APIRouter(prefix="/apps", tags=["apps"])

DOMAIN_SUFFIX = "seaburr.io"


def _env_var_out(env_var: models.AppEnvVar) -> schemas.EnvVarOut:
    value = MASKED_VALUE if env_var.is_secret else crypto.decrypt(env_var.value_encrypted)
    return schemas.EnvVarOut(key=env_var.key, value=value, is_secret=env_var.is_secret)


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
        status=app.status,
        created_at=app.created_at,
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
    current_user: models.User = Depends(get_current_user),
):
    if not current_user.is_uberadmin:
        if _active_app_count(db, current_user.id) >= current_user.app_limit:
            raise HTTPException(
                status_code=403,
                detail=f"app limit reached ({current_user.app_limit}); contact the uberadmin to raise it",
            )

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
    return _app_out(app)


@router.get("", response_model=List[schemas.AppOut])
def list_apps(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
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
    current_user: models.User = Depends(get_current_user),
):
    app = _get_owned_app_or_404(db, app_id, current_user.id)
    return _app_out(app)


@router.patch("/{app_id}", response_model=schemas.AppOut)
def update_app(
    app_id: int,
    payload: schemas.AppUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    app = _get_owned_app_or_404(db, app_id, current_user.id)

    if payload.name is not None:
        app.name = payload.name
    if payload.image is not None:
        app.image = payload.image
    if payload.port is not None:
        app.port = payload.port
    if payload.env is not None:
        _validate_env(payload.env, want_db=app.want_db, want_storage=app.want_storage)
        app.env_vars.clear()
        for item in payload.env:
            app.env_vars.append(
                models.AppEnvVar(
                    key=item.key,
                    value_encrypted=crypto.encrypt(item.value),
                    is_secret=item.is_secret,
                )
            )

    db.commit()
    db.refresh(app)
    return _app_out(app)


@router.delete("/{app_id}", status_code=204)
def delete_app(
    app_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    app = _get_owned_app_or_404(db, app_id, current_user.id)
    app.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return None
