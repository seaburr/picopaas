from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ConfigDict, BaseModel, Field, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, crypto, models, projects, schemas, volumes
from app.db import get_db
from app.dependencies import get_active_user, get_registry
from app.naming import generate_unique_hostname
from app.providers.base import Registry
from app.routers.apps import _app_out, _check_image_cap, _validate_env
from app.schemas import MASKED_VALUE
from app.spec import MAX_SPEC_BYTES, SpecError, parse_spec

router = APIRouter(prefix="/apps", tags=["apps"])


class SpecDeploy(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    spec: str = Field(max_length=MAX_SPEC_BYTES)
    secrets: Dict[str, str] = Field(default_factory=dict)
    project: Optional[str] = Field(default=None, min_length=1, max_length=80)  # slug; the spec's own `project:` wins


class SpecDeployOut(schemas.AppOut):
    created: bool


def _resolve_project(db: Session, user: models.User, ref: Optional[str]) -> models.Project:
    """`project:` value -> a project the caller belongs to. None/"personal" = their personal project; else the slug.
    Unknown slug and non-member look identical (404)."""
    if ref is None or ref == "personal":
        return projects.get_personal_project(db, user)
    project = (db.query(models.Project)
               .filter(models.Project.slug == ref, models.Project.deleted_at.is_(None)).first())
    if project is None or projects.get_membership(db, project.id, user.id) is None:
        raise HTTPException(status_code=404, detail=f"We can't find a project with the slug {ref}. Check `picopaas projects`.")
    return project


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
            detail=(
                f"You provided values for {', '.join(sorted(extra))}, but they aren't declared under secrets: "
                "in the spec. Add them there or remove the values."
            ),
        )

    ref = spec.project or payload.project
    if spec.project and payload.project and spec.project != payload.project:
        raise HTTPException(status_code=422, detail=(
            f"The spec says project: {spec.project} but the request asks for {payload.project}. Use one of them."))
    project = _resolve_project(db, current_user, ref)
    app = (
        db.query(models.App)
        .filter(
            models.App.project_id == project.id,
            models.App.name == spec.name,
            models.App.deleted_at.is_(None),
        )
        .first()
    )
    created = app is None
    if app is not None and app.status == "suspended":
        raise HTTPException(status_code=403, detail="This app is suspended by an administrator, so it can't be changed. Contact an administrator to get it restored.")

    existing_secrets: Dict[str, str] = {}
    if app is not None:
        existing_secrets = {v.key: v.value_encrypted for v in app.env_vars if v.is_secret}
        if app.want_db and not spec.resources.db or app.want_storage and not spec.resources.storage:
            raise HTTPException(
                status_code=422,
                detail=(
                    "A spec can't remove a database or storage bucket from an existing app. Delete and recreate "
                    "the app if you really want to drop it (this destroys the data)."
                ),
            )
    if app is not None and app.volume_size_gi is not None and spec.resources.volume is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "A spec can't remove a persistent volume from an existing app. Keep resources.volume in the spec, "
                "or delete and recreate the app if you really want to drop it (this destroys the data)."
            ),
        )
    if app is not None and app.want_db and spec.resources.db and app.db_engine != spec.resources.db_engine:
        raise HTTPException(
            status_code=422,
            detail=(
                "A spec can't change the database engine of an existing app. Delete and recreate "
                "the app if you really want to switch (this destroys the data)."
            ),
        )
    missing = [s for s in spec.secrets if s not in payload.secrets and s not in existing_secrets]
    if missing:
        raise HTTPException(status_code=422, detail=(
                f"Missing values for these secrets: {', '.join(missing)}. Pass --secret NAME=value (CLI) "
                "or fill them in the secrets box (YAML tab)."
            ))

    try:
        items = [schemas.EnvVarIn(key=k, value=v) for k, v in spec.env.items()]
        items += [
            schemas.EnvVarIn(key=s, value=payload.secrets.get(s, MASKED_VALUE), is_secret=True)
            for s in spec.secrets
        ]
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail="; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors()),
        )

    if created:
        projects.check_app_limit(db, project)

    run_as_uid, digest = _check_image_cap(registry, spec.image, app)
    _validate_env(items, want_db=spec.resources.db, want_storage=spec.resources.storage)

    if created:
        app = models.App(
            owner_id=current_user.id,
            project_id=project.id,
            name=spec.name,
            hostname=generate_unique_hostname(db),
            image=spec.image,
            image_digest=digest,
            image_checked_at=datetime.now(timezone.utc).replace(tzinfo=None) if digest else None,
            run_as_uid=run_as_uid,
            port=spec.port,
            want_db=spec.resources.db,
            db_engine=spec.resources.db_engine,
            want_storage=spec.resources.storage,
            health_check_enabled=spec.healthCheck.enabled,
            health_check_path=spec.healthCheck.path,
            health_check_timeout_seconds=spec.healthCheck.timeoutSeconds,
            status="pending",
        )
        if spec.resources.volume is not None:
            volumes.apply_request(db, app, spec.resources.volume.sizeGi, spec.resources.volume.mountPath, new_app=True)
        db.add(app)
    else:
        old_env = {v.key: (crypto.try_decrypt(v.value_encrypted), v.is_secret) for v in app.env_vars}
        changed = (
            (app.image, app.image_digest, app.run_as_uid, app.port, app.want_db, app.db_engine, app.want_storage, app.health_check_enabled, app.health_check_path,
             app.health_check_timeout_seconds)
            != (spec.image, digest, run_as_uid, spec.port, spec.resources.db, spec.resources.db_engine, spec.resources.storage,
                spec.healthCheck.enabled, spec.healthCheck.path, spec.healthCheck.timeoutSeconds)
        )
        if spec.resources.volume is not None and volumes.apply_request(
                db, app, spec.resources.volume.sizeGi, spec.resources.volume.mountPath):
            changed = True
        app.image = spec.image
        app.image_digest = digest
        app.image_update_error = None
        app.run_as_uid = run_as_uid
        app.port = spec.port
        app.want_db = spec.resources.db
        app.db_engine = spec.resources.db_engine
        app.want_storage = spec.resources.storage
        app.health_check_enabled = spec.healthCheck.enabled
        app.health_check_path = spec.healthCheck.path
        app.health_check_timeout_seconds = spec.healthCheck.timeoutSeconds
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
            app.reset_retry()

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Couldn't reserve a hostname for the app. Try again in a moment.")
    db.refresh(app)
    audit.record(db, current_user, "app.create" if created else "app.update", "app", app.id,
                 f"name={app.name} via=spec")
    return SpecDeployOut(**_app_out(app).model_dump(), created=created)
