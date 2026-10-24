import csv
import io
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from cryptography import x509
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app import audit, backups, config, crypto, health, models, naming, quota, resources, schemas, volumes
from app.config import APP_DOMAIN
from app.db import get_db
from app.dependencies import get_db_server_checker, get_kube, require_platform_admin
from app.providers.dbcheck import DbServerCheckError
from app.schemas import ResourcesOut
from app.routers.apps import get_mysql_admin_factory, get_object_store
from app.security import hash_password

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])

log = logging.getLogger(__name__)

WARN_PCT = 80.0


class AdminUserOut(BaseModel):
    id: int
    email: str
    role: str
    app_limit: int
    app_limit_effective: int
    app_count: int
    status: str
    created_at: datetime
    terms_accepted_at: Optional[datetime] = None


class AdminAppOut(BaseModel):
    id: int
    name: str
    hostname: str
    status: str
    owner_id: int
    owner_email: str
    image: str
    created_at: datetime
    resources: Optional[ResourcesOut] = None
    exceeds_platform_caps: Optional[bool] = None  # only set on the PATCH /resources response
    replicas: int = 1
    volume: Optional[schemas.VolumeOut] = None
    want_db: bool = False
    want_storage: bool = False
    db_engine: Optional[str] = None  # null when the app has no database
    platform_host: Optional[str] = None
    custom_domain: Optional[str] = None
    restarts_app: Optional[bool] = None  # only set on the PATCH /resources response


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    at: datetime
    actor_email: Optional[str]
    action: str
    target_type: Optional[str]
    target_id: Optional[str]
    detail: Optional[str]


class AdminUserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    app_limit: int = Field(ge=0)


class DbServerOut(BaseModel):
    id: int
    name: str
    host: str
    port: int
    status: str
    engine: str = "mysql"
    max_databases: int
    used: int
    pct: float
    warn: bool


def _validate_ca_pem(v: Optional[str]) -> Optional[str]:
    if v is None or v.strip() == "":
        return None
    try:
        x509.load_pem_x509_certificate(v.encode())
    except Exception:
        raise ValueError("CA certificate must be PEM text starting with -----BEGIN CERTIFICATE-----.")
    return v


class DbServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    name: str = Field(min_length=1, max_length=255)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=3306, gt=0, le=65535)
    engine: Literal["mysql", "pg"] = "mysql"
    admin_user: str = Field(min_length=1, max_length=255)
    admin_password: str = Field(min_length=1)
    max_databases: int = Field(gt=0)
    ca_cert: Optional[str] = Field(default=None, max_length=20000)

    _check_ca = field_validator("ca_cert")(_validate_ca_pem)


class DbServerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, never passed through (PENTEST #12)
    status: Optional[Literal["active", "draining", "full"]] = None
    max_databases: Optional[int] = Field(default=None, gt=0)
    ca_cert: Optional[str] = Field(default=None, max_length=20000)  # "" clears it
    admin_password: Optional[str] = Field(default=None, min_length=1)  # write-only; verified before it is stored

    _check_ca = field_validator("ca_cert")(_validate_ca_pem)


def _user_out(db: Session, user: models.User) -> dict:
    count = (
        db.query(models.App)
        .filter(models.App.owner_id == user.id, models.App.deleted_at.is_(None))
        .count()
    )
    return dict(id=user.id, email=user.email, role=user.role, app_limit=user.app_limit,
                app_limit_effective=resources.effective_app_limit(user),
                app_count=count, status=user.status, created_at=user.created_at,
                terms_accepted_at=user.terms_accepted_at)


def _server_out(db: Session, s: models.DbServer) -> DbServerOut:
    used = db.query(func.count(models.AppDatabase.id)).filter(models.AppDatabase.db_server_id == s.id).scalar()
    pct = round(used * 100.0 / s.max_databases, 1) if s.max_databases else 100.0
    return DbServerOut(id=s.id, name=s.name, host=s.host, port=s.port, status=s.status, engine=s.engine,
                       max_databases=s.max_databases, used=used, pct=pct, warn=pct >= WARN_PCT)


@router.get("/users", response_model=List[AdminUserOut])
def list_users(db: Session = Depends(get_db)):
    return [
        _user_out(db, u)
        for u in db.query(models.User).filter(models.User.status != "deleted").order_by(models.User.id).all()
    ]


def _get_user_or_404(db: Session, user_id: int) -> models.User:
    user = db.get(models.User, user_id)
    if user is None or user.status == "deleted":
        raise HTTPException(status_code=404, detail="We can't find that user. They may have been deleted. Refresh the list.")
    return user


def _active_admin_count(db: Session) -> int:
    # FOR UPDATE on the admin rows serialises concurrent demotes on MySQL (no-op on SQLite), so two admins
    # demoting each other at once can't both pass the "not the last admin" check.
    return len(db.query(models.User.id)
               .filter(models.User.role == "platform_admin", models.User.status == "active")
               .with_for_update().all())


def _protect_platform_admin(user: models.User, db: Optional[Session] = None) -> None:
    if user.is_platform_admin:
        if db is not None and user.status == "active" and _active_admin_count(db) <= 1:
            raise HTTPException(status_code=409, detail="This is the last active administrator. Promote another user first.")
        raise HTTPException(status_code=400, detail="The main administrator account can't be suspended or deleted.")


def _live_apps(db: Session, owner_id: int) -> list:
    return db.query(models.App).filter(models.App.owner_id == owner_id, models.App.deleted_at.is_(None)).all()


@router.post("/users/{user_id}/approve", response_model=AdminUserOut)
def approve_user(user_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_platform_admin)):
    user = _get_user_or_404(db, user_id)
    if user.status != "pending":
        if user.status == "active":
            msg = "This user was already approved. Refresh the list."
        else:
            msg = f"This user isn't waiting for approval (their status is {user.status}). Refresh the list."
        raise HTTPException(status_code=409, detail=msg)
    user.status = "active"
    db.commit()
    audit.record(db, admin, "user.approve", "user", user.id)
    return _user_out(db, user)


@router.post("/users/{user_id}/suspend", response_model=AdminUserOut)
def suspend_user(user_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_platform_admin)):
    user = _get_user_or_404(db, user_id)
    _protect_platform_admin(user, db)
    user.status = "suspended"
    for a in _live_apps(db, user.id):
        if a.status != "suspended":  # already admin-suspended apps stay individually suspended
            a.status = "suspended"
            a.suspended_by_user = True
            a.status_detail = None
    db.commit()
    audit.record(db, admin, "user.suspend", "user", user.id)
    return _user_out(db, user)


@router.post("/users/{user_id}/unsuspend", response_model=AdminUserOut)
def unsuspend_user(user_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_platform_admin)):
    user = _get_user_or_404(db, user_id)
    if user.status != "suspended":
        raise HTTPException(status_code=409, detail="This user isn't suspended. Refresh the list.")
    user.status = "active"
    for a in _live_apps(db, user.id):
        if a.status == "suspended" and a.suspended_by_user:
            a.status = "pending"
            a.suspended_by_user = False
            a.status_detail = None
            a.reset_retry()
    db.commit()
    audit.record(db, admin, "user.unsuspend", "user", user.id)
    return _user_out(db, user)


@router.post("/users/{user_id}/promote", response_model=AdminUserOut)
def promote_user(user_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_platform_admin)):
    user = _get_user_or_404(db, user_id)
    if user.status != "active":
        raise HTTPException(status_code=409, detail=f"Only active users can be promoted (this user is {user.status}).")
    if user.is_platform_admin:
        raise HTTPException(status_code=409, detail="This user is already an administrator. Refresh the list.")
    user.role = "platform_admin"
    db.commit()
    audit.record(db, admin, "user.promote", "user", user.id)
    return _user_out(db, user)


@router.post("/users/{user_id}/demote", response_model=AdminUserOut)
def demote_user(user_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_platform_admin)):
    user = _get_user_or_404(db, user_id)
    if not user.is_platform_admin:
        raise HTTPException(status_code=409, detail="This user isn't an administrator. Refresh the list.")
    if user.status == "active" and _active_admin_count(db) <= 1:
        raise HTTPException(status_code=409, detail="This is the last active administrator. Promote another user first.")
    user.role = "user"
    db.commit()
    audit.record(db, admin, "user.demote", "user", user.id)
    return _user_out(db, user)


@router.post("/users/{user_id}/reset-password", response_model=schemas.TemporaryPasswordOut)
def reset_password(user_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_platform_admin)):
    """Set a random temporary password (shown once, never stored in clear or logged). The user is flagged
    must_change_password and should change it via POST /auth/change-password after logging in."""
    user = _get_user_or_404(db, user_id)
    temp = secrets.token_urlsafe(12)  # 16 chars, comfortably over the password minimum
    user.password_hash = hash_password(temp)
    user.must_change_password = True
    db.commit()
    audit.record(db, admin, "user.reset_password", "user", user.id)
    return schemas.TemporaryPasswordOut(temporary_password=temp)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_platform_admin)):
    user = _get_user_or_404(db, user_id)
    _protect_platform_admin(user, db)
    now = datetime.now(timezone.utc)
    for a in _live_apps(db, user.id):
        a.deleted_at = now  # the reconciler tears these down
    for k in db.query(models.ApiKey).filter(models.ApiKey.user_id == user.id, models.ApiKey.revoked_at.is_(None)):
        k.revoked_at = now
    user.status = "deleted"
    db.commit()
    audit.record(db, admin, "user.delete", "user", user.id)
    return None


def _app_admin_out(a: models.App) -> AdminAppOut:
    return AdminAppOut(id=a.id, name=a.name, hostname=f"{a.hostname}.{APP_DOMAIN}", status=a.status,
                       owner_id=a.owner_id, owner_email=a.owner.email, image=a.image, created_at=a.created_at,
                       resources=resources.describe(a), replicas=a.replicas, volume=volumes.out(a), want_db=a.want_db,
                       want_storage=a.want_storage, db_engine=a.db_engine if a.want_db else None,
                       platform_host=f"{a.platform_subdomain}.{APP_DOMAIN}" if a.platform_subdomain else None,
                       custom_domain=a.custom_domain)


def _get_app_or_404(db: Session, app_id: int) -> models.App:
    a = db.get(models.App, app_id)
    if a is None or a.deleted_at is not None:
        raise HTTPException(status_code=404, detail="We can't find that app. It may have been deleted. Refresh the list.")
    return a


@router.get("/apps", response_model=List[AdminAppOut])
def list_all_apps(db: Session = Depends(get_db)):
    apps = (
        db.query(models.App).options(joinedload(models.App.owner))
        .filter(models.App.deleted_at.is_(None)).order_by(models.App.id).all()
    )
    return [_app_admin_out(a) for a in apps]


@router.post("/apps/{app_id}/suspend", response_model=AdminAppOut)
def suspend_app(app_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_platform_admin)):
    a = _get_app_or_404(db, app_id)
    a.status = "suspended"
    a.suspended_by_user = False
    a.status_detail = None
    db.commit()
    audit.record(db, admin, "app.suspend", "app", a.id)
    return _app_admin_out(a)


@router.post("/apps/{app_id}/unsuspend", response_model=AdminAppOut)
def unsuspend_app(app_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_platform_admin)):
    a = _get_app_or_404(db, app_id)
    if a.status != "suspended":
        raise HTTPException(status_code=409, detail="This app isn't suspended. Refresh the list.")
    a.status = "pending"
    a.suspended_by_user = False
    a.status_detail = None
    a.reset_retry()
    db.commit()
    audit.record(db, admin, "app.unsuspend", "app", a.id)
    return _app_admin_out(a)


class PlatformHostSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subdomain: str = Field(min_length=1, max_length=63)


def _requeue(a: models.App) -> None:
    """Re-render the app's Ingress on the next reconcile (suspended apps stay suspended; they have no Ingress)."""
    if a.status != "suspended":
        a.status = "pending"
        a.reset_retry()


@router.put("/apps/{app_id}/platform-host", response_model=schemas.AppOut)
def set_platform_host(app_id: int, payload: PlatformHostSet, db: Session = Depends(get_db),
                      admin: models.User = Depends(require_platform_admin)):
    """Give an app a fixed `<label>.<APP_DOMAIN>` host in addition to its generated one. Covered by the wildcard
    cert, so only an extra Ingress rule is needed (no per-host cert)."""
    a = _get_app_or_404(db, app_id)
    try:
        label = naming.normalize_platform_subdomain(payload.subdomain)
    except naming.PlatformSubdomainError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    taken = db.query(models.App.id).filter(models.App.platform_subdomain == label, models.App.id != a.id,
                models.App.deleted_at.is_(None)).first()
    if taken is not None:
        raise HTTPException(status_code=409, detail="That subdomain is already assigned to another app.")
    # The label must not equal any live app's generated hostname either: two Ingresses (different namespaces)
    # would claim the same host.
    clash = db.query(models.App.id).filter(models.App.hostname == label, models.App.id != a.id,
                models.App.deleted_at.is_(None)).first()
    if clash is not None:
        raise HTTPException(status_code=409, detail="That subdomain is already in use as another app's address.")
    a.platform_subdomain = label
    _requeue(a)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="That subdomain is already assigned to another app.")
    audit.record(db, admin, "app.platform_host.set", "app", a.id, f"label={label}")
    from app.routers.apps import _app_out
    return _app_out(a)


@router.delete("/apps/{app_id}/platform-host", response_model=schemas.AppOut)
def clear_platform_host(app_id: int, db: Session = Depends(get_db),
                        admin: models.User = Depends(require_platform_admin)):
    a = _get_app_or_404(db, app_id)
    if a.platform_subdomain:
        label = a.platform_subdomain
        a.platform_subdomain = None
        _requeue(a)
        db.commit()
        audit.record(db, admin, "app.platform_host.clear", "app", a.id, f"label={label}")
    from app.routers.apps import _app_out
    return _app_out(a)


def _object_store_or_none(request: Request):
    """The object store, or None when Spaces isn't configured (the immediate quota check then skips storage)."""
    factory = request.app.dependency_overrides.get(get_object_store, get_object_store)
    try:
        return factory()
    except Exception:  # noqa: BLE001
        return None


class AppResourcesPatch(BaseModel):
    """Omitted field = unchanged; explicit null = reset to the platform default."""
    model_config = ConfigDict(extra="forbid")
    cpu_request_m: Optional[int] = Field(default=None, ge=resources.CPU_MIN_M, le=resources.CPU_MAX_M)
    cpu_limit_m: Optional[int] = Field(default=None, ge=resources.CPU_MIN_M, le=resources.CPU_MAX_M)
    mem_request_mi: Optional[int] = Field(default=None, ge=resources.MEM_MIN_MI, le=resources.MEM_MAX_MI)
    mem_limit_mi: Optional[int] = Field(default=None, ge=resources.MEM_MIN_MI, le=resources.MEM_MAX_MI)
    db_max_connections: Optional[int] = Field(
        default=None, ge=config.OVERRIDE_DB_CONNECTIONS_MIN, le=config.OVERRIDE_DB_CONNECTIONS_MAX)
    ephemeral_limit_mi: Optional[int] = Field(
        default=None, ge=config.OVERRIDE_EPHEMERAL_MIN_MI, le=config.OVERRIDE_EPHEMERAL_MAX_MI)
    db_quota_mb: Optional[int] = Field(
        default=None, ge=config.OVERRIDE_DB_QUOTA_MIN_MB, le=config.OVERRIDE_DB_QUOTA_MAX_MB)
    storage_quota_mb: Optional[int] = Field(
        default=None, ge=config.OVERRIDE_STORAGE_QUOTA_MIN_MB, le=config.OVERRIDE_STORAGE_QUOTA_MAX_MB)
    image_size_mb: Optional[int] = Field(
        default=None, ge=config.OVERRIDE_IMAGE_SIZE_MIN_MB, le=config.OVERRIDE_IMAGE_SIZE_MAX_MB)
    egress_mbit: Optional[int] = Field(
        default=None, ge=config.OVERRIDE_EGRESS_MIN_MBIT, le=config.OVERRIDE_EGRESS_MAX_MBIT)
    # Grow-only (Kubernetes PVC expansion); the app must already have a volume. Explicit null is rejected.
    volume_size_gi: Optional[int] = Field(
        default=None, ge=config.OVERRIDE_VOLUME_MIN_GI, le=config.OVERRIDE_VOLUME_MAX_GI)


def _grow_volume(a: models.App, size_gi: Optional[int]) -> bool:
    """Admin volume size override: grow-only, needs an existing volume. True if the size changed."""
    volumes.require_enabled()
    if size_gi is None:
        raise HTTPException(status_code=422, detail="A volume size can't be reset. Volumes only grow.")
    if a.volume_size_gi is None:
        raise HTTPException(status_code=422, detail="This app has no persistent volume. The owner has to add one first.")
    if size_gi < a.volume_size_gi:
        raise HTTPException(status_code=422, detail=f"A volume can't shrink (it is {a.volume_size_gi} GiB). Volumes only grow.")
    changed = size_gi != a.volume_size_gi
    a.volume_size_gi = size_gi
    return changed


def _res_str(a: models.App) -> str:
    return ",".join(f"{f}={getattr(a, f)}" for f in resources.FIELDS)


@router.patch("/apps/{app_id}/resources", response_model=AdminAppOut)
def patch_app_resources(app_id: int, payload: AppResourcesPatch, db: Session = Depends(get_db),
                        admin: models.User = Depends(require_platform_admin),
                        mysql_admin_factory=Depends(get_mysql_admin_factory),
                        object_store=Depends(_object_store_or_none)):
    a = _get_app_or_404(db, app_id)
    changes = {f: getattr(payload, f) for f in payload.model_fields_set if f != "volume_size_gi"}
    volume_grown = False
    old_volume = a.volume_size_gi
    if "volume_size_gi" in payload.model_fields_set:
        volume_grown = _grow_volume(a, payload.volume_size_gi)
    new = {f: changes.get(f, getattr(a, f)) for f in resources.FIELDS}
    eff = resources.effective(type("N", (), new))  # unclamped check below uses raw merged values
    cpu_req = new["cpu_request_m"] or resources.DEFAULT_CPU_REQUEST_M
    mem_req = new["mem_request_mi"] or resources.DEFAULT_MEM_REQUEST_MI
    if cpu_req > eff["cpu_limit_m"]:
        raise HTTPException(status_code=422, detail="CPU request can't exceed the CPU limit.")
    if mem_req > eff["mem_limit_mi"]:
        raise HTTPException(status_code=422, detail="Memory request can't exceed the memory limit.")
    old = _res_str(a)
    restarts = any(new[f] != getattr(a, f) for f in resources.POD_FIELDS)
    quota_changed = any(new[f] != getattr(a, f) for f in ("db_quota_mb", "storage_quota_mb"))
    conns_changed = new["db_max_connections"] != a.db_max_connections
    for f, v in new.items():
        setattr(a, f, v)
    if (restarts or volume_grown) and a.status != "suspended":  # suspended apps pick this up when unsuspended
        a.status = "pending"  # the reconciler re-renders Deployment + LimitRange + quota, so pods roll
        a.status_detail = None
        a.reset_retry()
    db.commit()
    vol = f",volume_size_gi={old_volume} -> {a.volume_size_gi}" if volume_grown else ""
    audit.record(db, admin, "app.resources", "app", a.id, f"{old} -> {_res_str(a)}{vol}")
    if conns_changed and a.want_db and a.database is not None:
        try:
            want_limit = resources.effective(a)["db_max_connections"]
            mysql_admin_factory(a.database.server).set_user_connection_limit(a.id, want_limit)
            a.database.db_conn_limit_applied = want_limit
            db.commit()
        except Exception as exc:  # noqa: BLE001 - the reconciler sweep re-applies it
            log.warning("app %s: applying db connection limit failed: %s", a.id, type(exc).__name__)
    if quota_changed:  # restore (or revoke) right away instead of at the next poll
        quota.check_app_quota(db, a, mysql_admin_factory=mysql_admin_factory, object_store=object_store)
        db.refresh(a)
    out = _app_admin_out(a)
    out.exceeds_platform_caps = out.resources.exceeds_platform_caps
    out.restarts_app = restarts and a.status != "suspended"
    return out


class ReplicasSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replicas: int = Field(ge=1, le=config.MAX_REPLICAS)


@router.put("/apps/{app_id}/replicas", response_model=schemas.AppOut)
def set_replicas(app_id: int, payload: ReplicasSet, db: Session = Depends(get_db),
                 admin: models.User = Depends(require_platform_admin)):
    """Set the Deployment replica count. A suspended app stays suspended (scaled to 0) and picks it up on unsuspend."""
    a = _get_app_or_404(db, app_id)
    if a.volume_size_gi is not None and payload.replicas > 1:
        raise HTTPException(status_code=409, detail=volumes.REPLICAS_WITH_VOLUME_MSG)
    old = a.replicas
    a.replicas = payload.replicas
    _requeue(a)
    db.commit()
    audit.record(db, admin, "app.replicas", "app", a.id, f"{old} -> {a.replicas}")
    from app.routers.apps import _app_out
    return _app_out(a)


@router.delete("/apps/{app_id}", status_code=204)
def admin_delete_app(app_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_platform_admin)):
    a = _get_app_or_404(db, app_id)
    a.deleted_at = datetime.now(timezone.utc)
    db.commit()
    audit.record(db, admin, "app.delete", "app", a.id, "by admin")
    return None


AUDIT_EXPORT_MAX = 50_000
_AUDIT_FIELDS = ["id", "at", "actor_email", "action", "target_type", "target_id", "detail"]


def _csv_safe(v) -> str:
    """Neutralise spreadsheet formula injection: cells starting with = + - @ tab or CR get a leading quote."""
    text = "" if v is None else str(v)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


_EXPORT_BATCH = 1000


def _audit_export_rows(bind, upper_id: int, limit: int):
    """Yield audit rows as plain tuples, newest first, in keyset batches by id. Opens its own session: FastAPI
    0.115 tears down yield-dependency sessions before a StreamingResponse body is sent."""
    from sqlalchemy.orm import sessionmaker

    cols = [getattr(models.AuditLog, k) for k in _AUDIT_FIELDS]
    session = sessionmaker(bind=bind, autoflush=False, expire_on_commit=False)()
    try:
        last, left = upper_id + 1, limit
        while left > 0:
            batch = session.query(*cols).filter(models.AuditLog.id < last).order_by(
                models.AuditLog.id.desc()).limit(min(_EXPORT_BATCH, left)).all()
            if not batch:
                break
            for row in batch:
                yield tuple(row)
            last = batch[-1][0]
            left -= len(batch)
            session.expire_all()
    finally:
        session.close()


def _audit_export_stream(bind, upper_id: int, limit: int, fmt: str):
    if fmt == "json":
        first = True
        for row in _audit_export_rows(bind, upper_id, limit):
            item = {k: (v.isoformat() if k == "at" else v) for k, v in zip(_AUDIT_FIELDS, row)}
            yield ("[\n" if first else ",\n") + json.dumps(item)
            first = False
        yield "\n]" if not first else "[]"
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_AUDIT_FIELDS)
    for n, row in enumerate(_audit_export_rows(bind, upper_id, limit), 1):
        w.writerow([_csv_safe(v.isoformat() if k == "at" else v) for k, v in zip(_AUDIT_FIELDS, row)])
        if n % 200 == 0:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue()


@router.get("/audit/export")
def export_audit(format: Literal["csv", "json"] = Query("csv"), db: Session = Depends(get_db),
                 admin: models.User = Depends(require_platform_admin)):
    """Download the audit log (newest first, at most 50,000 rows) as CSV or JSON, streamed."""
    upper = db.query(func.max(models.AuditLog.id)).scalar() or 0
    count = db.query(func.count(models.AuditLog.id)).filter(models.AuditLog.id <= upper).scalar() or 0
    rows = min(count, AUDIT_EXPORT_MAX)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    # Audited BEFORE streaming (a streamed response can't add it afterwards); not part of this download.
    audit.record(db, admin, "audit.export", "audit", None, f"format={format} rows={rows}")
    media = "application/json" if format == "json" else "text/csv; charset=utf-8"
    return StreamingResponse(_audit_export_stream(db.get_bind(), upper, AUDIT_EXPORT_MAX, format), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="picopaas-audit-{stamp}.{format}"'})


@router.get("/audit", response_model=List[AuditOut])
def list_audit(limit: int = Query(100, ge=1, le=1000), db: Session = Depends(get_db)):
    return (
        db.query(models.AuditLog)
        .order_by(models.AuditLog.id.desc())
        .limit(limit)
        .all()
    )


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def patch_user(user_id: int, payload: AdminUserPatch, db: Session = Depends(get_db),
               admin: models.User = Depends(require_platform_admin)):
    user = _get_user_or_404(db, user_id)  # 404 for missing or deleted users
    from app import config
    if payload.app_limit > config.MAX_APPS_PER_USER:
        raise HTTPException(status_code=422, detail="That's above the platform's per-account app limit.")
    user.app_limit = payload.app_limit
    db.commit()
    audit.record(db, admin, "user.update", "user", user.id, f"app_limit={payload.app_limit}")
    db.refresh(user)
    return _user_out(db, user)


@router.get("/db-servers", response_model=List[DbServerOut])
def list_db_servers(db: Session = Depends(get_db)):
    return [_server_out(db, s) for s in db.query(models.DbServer).order_by(models.DbServer.id).all()]


@router.get("/health")
def admin_health(db: Session = Depends(get_db), kube=Depends(get_kube),
                 mysql_admin_factory=Depends(get_mysql_admin_factory)):
    servers = [_server_out(db, s).model_dump() for s in db.query(models.DbServer).order_by(models.DbServer.id).all()]
    return {**health.build(db, kube, servers, mysql_admin_factory), **health.db_counts(db),
            "backups": backups.health_summary(db)}


def _check_connection(checker, *, engine, host, port, user, password, ca_cert) -> None:
    try:
        checker.check(engine=engine, host=host, port=port, user=user, password=password, ca_cert=ca_cert)
    except DbServerCheckError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/db-servers", response_model=DbServerOut, status_code=201)
def create_db_server(payload: DbServerCreate, db: Session = Depends(get_db),
                     admin: models.User = Depends(require_platform_admin),
                     checker=Depends(get_db_server_checker)):
    _check_connection(checker, engine=payload.engine, host=payload.host, port=payload.port,
                      user=payload.admin_user, password=payload.admin_password, ca_cert=payload.ca_cert)
    server = models.DbServer(
        name=payload.name, host=payload.host, port=payload.port, admin_user=payload.admin_user,
        admin_password_encrypted=crypto.encrypt(payload.admin_password),
        max_databases=payload.max_databases, status="active", ca_cert=payload.ca_cert, engine=payload.engine,
    )
    db.add(server)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"A database server named \"{payload.name}\" already exists. Pick another name.")
    db.refresh(server)
    audit.record(db, admin, "dbserver.create", "db_server", server.id,
                 f"name={server.name} max_databases={server.max_databases} ca_cert={'set' if server.ca_cert else 'none'}")
    return _server_out(db, server)


@router.patch("/db-servers/{server_id}", response_model=DbServerOut)
def patch_db_server(server_id: int, payload: DbServerPatch, db: Session = Depends(get_db),
                    admin: models.User = Depends(require_platform_admin),
                    checker=Depends(get_db_server_checker)):
    server = db.get(models.DbServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="We can't find that database server. Refresh the list.")
    new_ca = payload.ca_cert if "ca_cert" in payload.model_fields_set else server.ca_cert
    ca_changed = "ca_cert" in payload.model_fields_set and (new_ca or None) != (server.ca_cert or None)
    new_pw = payload.admin_password
    if new_pw is not None or ca_changed:
        # Verify first; nothing is saved (old password and CA stay) unless the server accepts the new settings.
        _check_connection(checker, engine=server.engine, host=server.host, port=server.port, user=server.admin_user,
                          password=new_pw if new_pw is not None else crypto.decrypt(server.admin_password_encrypted),
                          ca_cert=new_ca or None)
    if new_pw is not None:
        server.admin_password_encrypted = crypto.encrypt(new_pw)
    if payload.status is not None:
        server.status = payload.status
    if payload.max_databases is not None:
        server.max_databases = payload.max_databases
    if "ca_cert" in payload.model_fields_set:
        server.ca_cert = payload.ca_cert
    db.commit()
    db.refresh(server)
    if new_pw is not None or ca_changed:
        health.invalidate_db_server(server.id)  # drop cached metrics/errors from the old credentials
    changed = sorted(payload.model_fields_set)
    audit.record(db, admin, "dbserver.update", "db_server", server.id,
                 "fields=" + ",".join(changed) + (f" status={payload.status}" if payload.status else "")
                 + (" admin_password=changed" if new_pw is not None else ""))
    return _server_out(db, server)
