import logging
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ConfigDict, BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import audit, config, crypto, custom_domains, image_check, k8s_manifests, models, projects, reconciler, resources, schemas, volumes
from app.config import APP_DOMAIN
from app.db import get_db
from app.dependencies import get_active_user, get_kube, get_registry
from app.env_refs import EnvValidationError, validate_env_value
from app.naming import generate_unique_hostname
from app.providers.base import (
    KubeProvider,
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



_ROTATE_FAILED = "Couldn't rotate the credentials, so the old ones still work. Try again; if it keeps failing, ask an administrator."


def app_limit_message(limit: int) -> str:
    return f"You've reached your limit of {limit} app{'' if limit == 1 else 's'}. Delete one, or ask an administrator to raise your limit."


def _check_image_cap(registry: Registry, image: str, app: Optional[models.App] = None) -> tuple[Optional[int], Optional[str]]:
    """Enforce the image checks (size cap, non-root; ARCHITECTURE.md §2). Returns (run_as_uid, digest to pin).

    Any registry failure (not found, private, unparsable manifest) is
    reported the same way — a precise reason wouldn't be actionable for the
    caller and would narrow a probing surface.
    """
    try:
        info = registry.inspect(image)
    except RegistryError:
        raise HTTPException(status_code=422, detail="Couldn't find that image, or it's private. Check the name and tag, and make the image public in your registry.")
    try:
        run_as_uid = image_check.evaluate(info, resources.image_size_bytes(app))
    except image_check.ImageRejected as rej:
        raise HTTPException(status_code=422, detail=rej.message)
    return run_as_uid, (image_check.digest_of_ref(image) or info.digest or None)


def _env_var_out(env_var: models.AppEnvVar) -> schemas.EnvVarOut:
    plain = crypto.try_decrypt(env_var.value_encrypted)
    bad = plain is None
    if bad:
        log.warning("app %s: env var %s is undecryptable", env_var.app_id, env_var.key)
    value = MASKED_VALUE if env_var.is_secret else (plain or "")
    return schemas.EnvVarOut(key=env_var.key, value=value, is_secret=env_var.is_secret, undecryptable=bad)


def _volume_usage(app: models.App) -> Optional[schemas.UsageOut]:
    if not app.volume_size_gi or app.volume_used_bytes is None:
        return None
    limit = app.volume_size_gi * 1024**3
    return schemas.UsageOut(size_bytes=app.volume_used_bytes, limit_bytes=limit, over_quota=app.volume_used_bytes >= limit)


def _quota_out(app: models.App) -> schemas.AppQuotaOut:
    db_row, st_row = app.database, app.storage
    return schemas.AppQuotaOut(
        db=None if db_row is None else schemas.UsageOut(
            size_bytes=db_row.size_bytes, limit_bytes=resources.db_quota_bytes(app), over_quota=db_row.over_quota),
        storage=None if st_row is None else schemas.UsageOut(
            size_bytes=st_row.size_bytes, limit_bytes=resources.storage_quota_bytes(app), over_quota=st_row.over_quota),
        volume=_volume_usage(app),
    )


def _custom_domain_out(app: models.App) -> Optional[schemas.CustomDomainOut]:
    if not app.custom_domain:
        return None
    return schemas.CustomDomainOut(
        host=app.custom_domain,
        status=app.custom_domain_status or "pending",
        verified_at=app.custom_domain_verified_at,
        error=app.custom_domain_error,
        challenge_host=custom_domains.challenge_record(app.custom_domain),
        challenge_token=app.custom_domain_token or "",
        cname_target=config.CUSTOM_DOMAIN_CNAME_TARGET,
    )


def _app_out(app: models.App) -> schemas.AppOut:
    return schemas.AppOut(
        id=app.id,
        owner_id=app.owner_id,
        project_id=app.project_id,
        name=app.name,
        hostname=f"{app.hostname}.{APP_DOMAIN}",
        image=app.image,
        image_digest=app.image_digest,
        auto_update=app.auto_update,
        image_checked_at=app.image_checked_at,
        image_update_error=None if app.image_update_error_dismissed else app.image_update_error,
        port=app.port,
        want_db=app.want_db,
        db_engine=app.db_engine,
        want_storage=app.want_storage,
        health_check_enabled=app.health_check_enabled,
        health_check_path=app.health_check_path,
        health_check_timeout_seconds=app.health_check_timeout_seconds,
        replicas=app.replicas,
        volume=volumes.out(app),
        status=app.status,
        status_detail=app.status_detail,
        status_reason=app.status_reason if app.status == "error" else None,
        retry_at=reconciler.retry_at(app),
        created_at=app.created_at,
        last_event=reconciler.last_event(app),
        resources=resources.describe(app),
        quota=_quota_out(app),
        custom_domain=_custom_domain_out(app),
        platform_subdomain=app.platform_subdomain,
        platform_host=f"{app.platform_subdomain}.{APP_DOMAIN}" if app.platform_subdomain else None,
        env=[_env_var_out(e) for e in app.env_vars],
    )


def _get_owned_app_or_404(db: Session, app_id: int, user: models.User) -> models.App:
    return projects.get_app_for_member(db, app_id, user)


def _forbid_if_suspended(app: models.App) -> None:
    if app.status == "suspended":
        raise HTTPException(status_code=403, detail="This app is suspended by an administrator, so it can't be changed. Contact an administrator to get it restored.")


def _validate_env(env: List[schemas.EnvVarIn], want_db: bool, want_storage: bool) -> None:
    seen = set()
    for item in env:
        if item.key in seen:
            raise HTTPException(status_code=422, detail=f"{item.key} is listed twice. Env var names must be unique.")
        seen.add(item.key)
        try:
            validate_env_value(item.value, want_db=want_db, want_storage=want_storage)
        except EnvValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc))


def _target_project(db: Session, user: models.User, project_id: Optional[int]) -> models.Project:
    """Where a new app goes: the given project (caller must be a member, else 404) or their personal project."""
    if project_id is None:
        return projects.get_personal_project(db, user)
    project, _ = projects.get_project_for_member(db, project_id, user)
    return project


@router.post("", response_model=schemas.AppOut, status_code=201)
def create_app(
    payload: schemas.AppCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_active_user),
    registry: Registry = Depends(get_registry),
):
    project = _target_project(db, current_user, payload.project_id)
    projects.check_app_limit(db, project)

    run_as_uid, digest = _check_image_cap(registry, payload.image)
    _validate_env(payload.env, want_db=payload.want_db, want_storage=payload.want_storage)

    hostname = generate_unique_hostname(db)
    app = models.App(
        owner_id=current_user.id,
        project_id=project.id,
        name=payload.name,
        hostname=hostname,
        image=payload.image,
        image_digest=digest,
        auto_update=payload.auto_update,
        image_checked_at=datetime.now(timezone.utc).replace(tzinfo=None) if digest else None,
        run_as_uid=run_as_uid,
        port=payload.port,
        want_db=payload.want_db,
        db_engine=payload.db_engine,
        want_storage=payload.want_storage,
        health_check_enabled=payload.health_check_enabled,
        health_check_path=payload.health_check_path,
        health_check_timeout_seconds=payload.health_check_timeout_seconds,
        status="pending",
    )
    if payload.volume is not None:
        volumes.apply_request(db, app, payload.volume.size_gi, payload.volume.mount_path, new_app=True)
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
        raise HTTPException(status_code=409, detail="Couldn't reserve a hostname for the app. Try again in a moment.")
    db.refresh(app)
    audit.record(db, current_user, "app.create", "app", app.id, f"name={app.name} project={project.id}")
    return _app_out(app)


@router.get("", response_model=List[schemas.AppOut])
def list_apps(
    project_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_active_user),
):
    q = db.query(models.App).filter(projects.member_app_filter(db, current_user), models.App.deleted_at.is_(None))
    if project_id is not None:
        projects.get_project_for_member(db, project_id, current_user)  # 404 for non-members
        q = q.filter(models.App.project_id == project_id)
    apps = (
        q
        .options(
            selectinload(models.App.env_vars),
            selectinload(models.App.database),
            selectinload(models.App.storage),
            selectinload(models.App.events),
        )
        .order_by(models.App.created_at.desc())
        .all()
    )
    return [_app_out(a) for a in apps]


class AppMove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int


@router.post("/{app_id}/move", response_model=schemas.AppOut)
def move_app(
    app_id: int,
    payload: AppMove,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_active_user),
):
    """Move an app to another project. Needs the owner role in both projects; the target's app limit applies."""
    app = _get_owned_app_or_404(db, app_id, current_user)
    _forbid_if_suspended(app)
    if app.project_id is None:
        raise HTTPException(status_code=409, detail="This app isn't in a project yet; try again in a moment.")
    projects.get_project_for_member(db, app.project_id, current_user, owner=True)
    target, _ = projects.get_project_for_member(db, payload.project_id, current_user, owner=True)
    if target.id == app.project_id:
        raise HTTPException(status_code=409, detail="The app is already in that project.")
    clash = db.query(models.App).filter(models.App.project_id == target.id, models.App.name == app.name,
                                        models.App.deleted_at.is_(None)).first()
    if clash is not None:
        raise HTTPException(status_code=409, detail="That project already has an app with this name. Rename one first.")
    projects.check_app_limit(db, target)
    old = app.project_id
    app.project_id = target.id
    db.commit()
    audit.record(db, current_user, "app.move", "app", app.id, f"from_project={old} to_project={target.id}")
    return _app_out(app)


@router.get("/{app_id}", response_model=schemas.AppOut)
def get_app(
    app_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_active_user),
    kube: KubeProvider = Depends(get_kube),
):
    app = _get_owned_app_or_404(db, app_id, current_user)
    out = _app_out(app)
    out.uptime_seconds = _app_uptime(app, kube)
    return out


def _app_uptime(app: models.App, kube: KubeProvider) -> Optional[int]:
    """Seconds since the current pods' containers started (longest of the current Running pods); None if unknown/none."""
    if app.status == "suspended":
        return None
    try:
        info = kube.pod_logs(k8s_manifests.namespace_name(app.id), 1).pod_info
    except Exception:  # noqa: BLE001 - uptime is cosmetic
        return None
    now = datetime.now(timezone.utc)
    ups = []
    for i in info:
        start = i.started_at or i.created_at
        if i.current and i.phase == "Running" and start is not None:
            ups.append(max(0, int((now - (start if start.tzinfo else start.replace(tzinfo=timezone.utc))).total_seconds())))
    return max(ups) if ups else None


@router.patch("/{app_id}", response_model=schemas.AppOut)
def update_app(
    app_id: int,
    payload: schemas.AppUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_active_user),
    registry: Registry = Depends(get_registry),
):
    app = _get_owned_app_or_404(db, app_id, current_user)
    _forbid_if_suspended(app)

    workload_changed = False  # anything that changes the rendered Deployment/Secret
    if payload.name is not None:
        app.name = payload.name
    # The edit form re-sends the whole payload: an unchanged image string is not a deploy (a moved tag is auto_update's job,
    # or the owner changing the image), so renaming never touches the registry or rolls the pod.
    if payload.image is not None and payload.image != app.image:
        run_as_uid, digest = _check_image_cap(registry, payload.image, app)
        workload_changed = True
        app.image = payload.image
        app.run_as_uid = run_as_uid
        app.image_digest = digest
        app.image_update_error = None
        app.rejected_digest = None  # an explicit deploy overrides an earlier rejection
        if digest:
            app.image_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if payload.auto_update is not None:
        app.auto_update = payload.auto_update
    if payload.port is not None:
        if payload.port != app.port:
            workload_changed = True
        app.port = payload.port
    if "volume" in payload.model_fields_set:
        if payload.volume is None:
            raise HTTPException(status_code=422, detail="A persistent volume can't be detached. Delete the app to remove its data.")
        if volumes.apply_request(db, app, payload.volume.size_gi, payload.volume.mount_path):
            workload_changed = True
    health_changed = False
    if payload.health_check_enabled is not None and payload.health_check_enabled != app.health_check_enabled:
        app.health_check_enabled = payload.health_check_enabled
        health_changed = True
    if payload.health_check_path is not None and payload.health_check_path != app.health_check_path:
        app.health_check_path = payload.health_check_path
        health_changed = True
    # The form sends null while the check is off (the timeout only matters to the probes): not a change then.
    timeout_ignored = payload.health_check_timeout_seconds is None and not app.health_check_enabled
    if "health_check_timeout_seconds" in payload.model_fields_set and not timeout_ignored and \
            payload.health_check_timeout_seconds != app.health_check_timeout_seconds:
        app.health_check_timeout_seconds = payload.health_check_timeout_seconds
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
    app = _get_owned_app_or_404(db, app_id, current_user)
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
    from app.providers.factory import db_admin_for

    return db_admin_for


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
    app = _get_owned_app_or_404(db, app_id, user)
    _forbid_if_suspended(app)
    if body.target == "db":
        row = app.database
        if row is None:
            raise HTTPException(status_code=409, detail="Your database is still being created. Try again in a minute.")
        # Persist-first (issue #2): commit the new password BEFORE ALTER USER, so a failed
        # commit leaves MySQL untouched and the stored password valid. If ALTER then fails,
        # restore the old stored password. If even that restore fails, the stored password
        # is wrong, but the next rotation repairs it (ALTER USER doesn't need the old one).
        old_encrypted = row.password_encrypted
        new_password = generate_password()
        row.password_encrypted = crypto.encrypt(new_password)
        row.db_conn_limit_applied = None  # re-assert the connection cap on the next sweep
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise HTTPException(status_code=502, detail=_ROTATE_FAILED)
        try:
            mysql_admin_factory(row.server).set_password(app.id, new_password)
        except MySQLAdminError:
            try:
                row.password_encrypted = old_encrypted
                db.commit()
            except Exception:
                db.rollback()
            raise HTTPException(status_code=502, detail=_ROTATE_FAILED)
    else:
        row = app.storage
        if row is None:
            raise HTTPException(status_code=409, detail="Your storage bucket is still being created. Try again in a minute.")
        # Create new key -> commit -> only then revoke the old key (issue #2). If the commit
        # fails, discard the NEW key (best effort); the old key is still stored and working.
        try:
            creds = object_store.rotate_key(app.id)
        except ObjectStoreError:
            raise HTTPException(status_code=502, detail=_ROTATE_FAILED)
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
            raise HTTPException(status_code=502, detail=_ROTATE_FAILED)
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


# --- Custom domains (app/custom_domains.py, reconciler custom ingress + cert) ---

def _require_custom_domains_enabled() -> None:
    if not config.CUSTOM_DOMAINS_ENABLED:
        raise HTTPException(status_code=404, detail="Custom domains are not enabled on this platform.")


@router.put("/{app_id}/domain", response_model=schemas.AppOut)
def set_custom_domain(
    app_id: int,
    payload: schemas.CustomDomainSet,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
):
    """Attach (or replace) a custom domain. Generates the ownership token and returns the DNS records to add.
    The domain isn't served until it's verified (POST .../domain/verify)."""
    _require_custom_domains_enabled()
    app = _get_owned_app_or_404(db, app_id, user)
    _forbid_if_suspended(app)
    try:
        host = custom_domains.normalize_host(payload.host)
    except custom_domains.CustomDomainError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    taken = (
        db.query(models.App)
        .filter(models.App.custom_domain == host, models.App.id != app.id, models.App.deleted_at.is_(None))
        .first()
    )
    if taken is not None:
        raise HTTPException(status_code=409, detail="That domain is already attached to another app.")
    was_active = app.custom_domain_status == "active"
    app.custom_domain = host
    app.custom_domain_token = custom_domains.generate_token()
    app.custom_domain_status = "pending"
    app.custom_domain_error = None
    app.custom_domain_verified_at = None
    if was_active:  # replacing a live domain: requeue so the old custom ingress is torn down
        app.status = "pending"
        app.reset_retry()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="That domain is already attached to another app.")
    audit.record(db, user, "app.domain.set", "app", app.id, f"host={host}")
    return _app_out(app)


@router.post("/{app_id}/domain/verify", response_model=schemas.AppOut)
def verify_custom_domain(
    app_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
):
    """Check the ownership TXT and that the host points at our LB. On success, mark active and requeue the app so
    the reconciler renders the custom Ingress + cert. On failure, the error is stored (and shown) — status stays
    pending so the user can fix DNS and retry."""
    _require_custom_domains_enabled()
    app = _get_owned_app_or_404(db, app_id, user)
    _forbid_if_suspended(app)  # verify requeues the app (status='pending'), which would un-suspend it
    if not app.custom_domain:
        raise HTTPException(status_code=400, detail="This app has no custom domain to verify.")
    try:
        custom_domains.verify(app.custom_domain, app.custom_domain_token or "")
    except custom_domains.CustomDomainError as exc:
        app.custom_domain_status = "pending"
        app.custom_domain_error = str(exc)
        db.commit()
        return _app_out(app)
    app.custom_domain_status = "active"
    app.custom_domain_error = None
    app.custom_domain_verified_at = datetime.now(timezone.utc)
    app.status = "pending"  # requeue: render the custom Ingress + request the cert
    app.reset_retry()
    db.commit()
    audit.record(db, user, "app.domain.verify", "app", app.id, f"host={app.custom_domain}")
    return _app_out(app)


@router.delete("/{app_id}/domain", response_model=schemas.AppOut)
def clear_custom_domain(
    app_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
):
    """Detach the custom domain. If it was live, requeue so the reconciler deletes the custom Ingress (and
    cert-manager GCs its cert). The generated *.APP_DOMAIN subdomain is unaffected."""
    _require_custom_domains_enabled()
    app = _get_owned_app_or_404(db, app_id, user)
    _forbid_if_suspended(app)  # detaching a live domain requeues the app, which would un-suspend it
    if app.custom_domain:
        was_active = app.custom_domain_status == "active"
        host = app.custom_domain
        app.custom_domain = None
        app.custom_domain_token = None
        app.custom_domain_status = None
        app.custom_domain_error = None
        app.custom_domain_verified_at = None
        if was_active:
            app.status = "pending"
            app.reset_retry()
        db.commit()
        audit.record(db, user, "app.domain.clear", "app", app.id, f"host={host}")
    return _app_out(app)
