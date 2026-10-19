from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ConfigDict, BaseModel, Field, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, crypto, models, schemas
from app.db import get_db
from app.dependencies import get_active_user, get_registry
from app.naming import generate_unique_hostname
from app.providers.base import Registry
from app.routers.apps import _active_app_count, _app_out, _check_image_cap, _validate_env
from app.schemas import MASKED_VALUE
from app.spec import MAX_SPEC_BYTES, SpecError, parse_spec

router = APIRouter(prefix="/apps", tags=["apps"])


class SpecDeploy(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    spec: str = Field(max_length=MAX_SPEC_BYTES)
    secrets: Dict[str, str] = Field(default_factory=dict)


class SpecDeployOut(schemas.AppOut):
    created: bool


@router.post("/spec", response_model=SpecDeployOut)
def deploy_spec(
    payload: SpecDeploy,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_active_user),
    registry: Registry = Depends(get_registry),
):
    try:
        spec = parse_spec(payload.spec)
    except SpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    extra = set(payload.secrets) - set(spec.secrets)
    if extra:
        raise HTTPException(
            status_code=422,
            detail=f"secret values supplied for names not listed in spec.secrets: {', '.join(sorted(extra))}",
        )

    app = (
        db.query(models.App)
        .filter(
            models.App.owner_id == current_user.id,
            models.App.name == spec.name,
            models.App.deleted_at.is_(None),
        )
        .first()
    )
    created = app is None
    if app is not None and app.status == "suspended":
        raise HTTPException(status_code=403, detail="app suspended")

    existing_secrets: Dict[str, str] = {}
    if app is not None:
        existing_secrets = {v.key: v.value_encrypted for v in app.env_vars if v.is_secret}
        if app.want_db and not spec.resources.db or app.want_storage and not spec.resources.storage:
            raise HTTPException(
                status_code=422,
                detail="removing a db/storage resource from an existing app is not supported via spec",
            )
    missing = [s for s in spec.secrets if s not in payload.secrets and s not in existing_secrets]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing values for secrets: {', '.join(missing)}")

    try:
        items = [schemas.EnvVarIn(key=k, value=v) for k, v in spec.env.items()]
        items += [
            schemas.EnvVarIn(key=s, value=payload.secrets.get(s, MASKED_VALUE), is_secret=True)
            for s in spec.secrets
        ]
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="; ".join(e["msg"] for e in exc.errors()))

    if created and not current_user.is_uberadmin:
        if _active_app_count(db, current_user.id) >= current_user.app_limit:
            raise HTTPException(
                status_code=403,
                detail=f"app limit reached ({current_user.app_limit}); contact the uberadmin to raise it",
            )

    _check_image_cap(registry, spec.image)
    _validate_env(items, want_db=spec.resources.db, want_storage=spec.resources.storage)

    if created:
        app = models.App(
            owner_id=current_user.id,
            name=spec.name,
            hostname=generate_unique_hostname(db),
            image=spec.image,
            port=spec.port,
            want_db=spec.resources.db,
            want_storage=spec.resources.storage,
            health_check_enabled=spec.healthCheck.enabled,
            health_check_path=spec.healthCheck.path,
            status="pending",
        )
        db.add(app)
    else:
        old_env = {v.key: (crypto.try_decrypt(v.value_encrypted), v.is_secret) for v in app.env_vars}
        changed = (
            (app.image, app.port, app.want_db, app.want_storage, app.health_check_enabled, app.health_check_path)
            != (spec.image, spec.port, spec.resources.db, spec.resources.storage,
                spec.healthCheck.enabled, spec.healthCheck.path)
        )
        app.image = spec.image
        app.port = spec.port
        app.want_db = spec.resources.db
        app.want_storage = spec.resources.storage
        app.health_check_enabled = spec.healthCheck.enabled
        app.health_check_path = spec.healthCheck.path
        app.env_vars.clear()
        db.flush()

    for item in items:
        if item.is_secret and item.key not in payload.secrets:
            enc = existing_secrets[item.key]
        else:
            enc = crypto.encrypt(item.value)
        app.env_vars.append(models.AppEnvVar(key=item.key, value_encrypted=enc, is_secret=item.is_secret))

    if not created:
        new_env = {v.key: (crypto.try_decrypt(v.value_encrypted), v.is_secret) for v in app.env_vars}
        if changed or new_env != old_env:
            app.status = "pending"  # reconciler re-applies and the env-checksum rolls the pod
            app.status_detail = None

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="hostname collision, please retry")
    db.refresh(app)
    audit.record(db, current_user, "app.create" if created else "app.update", "app", app.id,
                 f"name={app.name} via=spec")
    return SpecDeployOut(**_app_out(app).model_dump(), created=created)
