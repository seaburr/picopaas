from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, crypto, models
from app.db import get_db
from app.dependencies import require_uberadmin
from app.routers.apps import DOMAIN_SUFFIX

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_uberadmin)])

WARN_PCT = 80.0


class AdminUserOut(BaseModel):
    id: int
    email: str
    role: str
    app_limit: int
    app_count: int
    status: str
    created_at: datetime


class AdminAppOut(BaseModel):
    id: int
    name: str
    hostname: str
    status: str
    owner_id: int
    owner_email: str
    image: str
    created_at: datetime


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
    app_limit: int = Field(ge=0)


class DbServerOut(BaseModel):
    id: int
    name: str
    host: str
    port: int
    status: str
    max_databases: int
    used: int
    pct: float
    warn: bool


class DbServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=3306, gt=0, le=65535)
    admin_user: str = Field(min_length=1, max_length=255)
    admin_password: str = Field(min_length=1)
    max_databases: int = Field(gt=0)


class DbServerPatch(BaseModel):
    status: Optional[Literal["active", "draining", "full"]] = None
    max_databases: Optional[int] = Field(default=None, gt=0)


def _user_out(db: Session, user: models.User) -> dict:
    count = (
        db.query(models.App)
        .filter(models.App.owner_id == user.id, models.App.deleted_at.is_(None))
        .count()
    )
    return dict(id=user.id, email=user.email, role=user.role, app_limit=user.app_limit,
                app_count=count, status=user.status, created_at=user.created_at)


def _server_out(db: Session, s: models.DbServer) -> DbServerOut:
    used = db.query(func.count(models.AppDatabase.id)).filter(models.AppDatabase.db_server_id == s.id).scalar()
    pct = round(used * 100.0 / s.max_databases, 1) if s.max_databases else 100.0
    return DbServerOut(id=s.id, name=s.name, host=s.host, port=s.port, status=s.status,
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
        raise HTTPException(status_code=404, detail="user not found")
    return user


def _protect_uberadmin(user: models.User) -> None:
    if user.is_uberadmin:
        raise HTTPException(status_code=400, detail="the uberadmin cannot be suspended or deleted")


def _live_apps(db: Session, owner_id: int) -> list:
    return db.query(models.App).filter(models.App.owner_id == owner_id, models.App.deleted_at.is_(None)).all()


@router.post("/users/{user_id}/approve", response_model=AdminUserOut)
def approve_user(user_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_uberadmin)):
    user = _get_user_or_404(db, user_id)
    if user.status != "pending":
        raise HTTPException(status_code=409, detail="user is not pending")
    user.status = "active"
    db.commit()
    audit.record(db, admin, "user.approve", "user", user.id)
    return _user_out(db, user)


@router.post("/users/{user_id}/suspend", response_model=AdminUserOut)
def suspend_user(user_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_uberadmin)):
    user = _get_user_or_404(db, user_id)
    _protect_uberadmin(user)
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
def unsuspend_user(user_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_uberadmin)):
    user = _get_user_or_404(db, user_id)
    if user.status != "suspended":
        raise HTTPException(status_code=409, detail="user is not suspended")
    user.status = "active"
    for a in _live_apps(db, user.id):
        if a.status == "suspended" and a.suspended_by_user:
            a.status = "pending"
            a.suspended_by_user = False
            a.status_detail = None
    db.commit()
    audit.record(db, admin, "user.unsuspend", "user", user.id)
    return _user_out(db, user)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_uberadmin)):
    user = _get_user_or_404(db, user_id)
    _protect_uberadmin(user)
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
    return AdminAppOut(id=a.id, name=a.name, hostname=f"{a.hostname}.{DOMAIN_SUFFIX}", status=a.status,
                       owner_id=a.owner_id, owner_email=a.owner.email, image=a.image, created_at=a.created_at)


def _get_app_or_404(db: Session, app_id: int) -> models.App:
    a = db.get(models.App, app_id)
    if a is None or a.deleted_at is not None:
        raise HTTPException(status_code=404, detail="app not found")
    return a


@router.get("/apps", response_model=List[AdminAppOut])
def list_all_apps(db: Session = Depends(get_db)):
    apps = db.query(models.App).filter(models.App.deleted_at.is_(None)).order_by(models.App.id).all()
    return [_app_admin_out(a) for a in apps]


@router.post("/apps/{app_id}/suspend", response_model=AdminAppOut)
def suspend_app(app_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_uberadmin)):
    a = _get_app_or_404(db, app_id)
    a.status = "suspended"
    a.suspended_by_user = False
    a.status_detail = None
    db.commit()
    audit.record(db, admin, "app.suspend", "app", a.id)
    return _app_admin_out(a)


@router.post("/apps/{app_id}/unsuspend", response_model=AdminAppOut)
def unsuspend_app(app_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_uberadmin)):
    a = _get_app_or_404(db, app_id)
    if a.status != "suspended":
        raise HTTPException(status_code=409, detail="app is not suspended")
    a.status = "pending"
    a.suspended_by_user = False
    a.status_detail = None
    db.commit()
    audit.record(db, admin, "app.unsuspend", "app", a.id)
    return _app_admin_out(a)


@router.delete("/apps/{app_id}", status_code=204)
def admin_delete_app(app_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_uberadmin)):
    a = _get_app_or_404(db, app_id)
    a.deleted_at = datetime.now(timezone.utc)
    db.commit()
    audit.record(db, admin, "app.delete", "app", a.id, "by admin")
    return None


@router.get("/audit", response_model=List[AuditOut])
def list_audit(limit: int = Query(100, ge=1, le=1000), db: Session = Depends(get_db)):
    return (
        db.query(models.AuditLog)
        .order_by(models.AuditLog.id.desc())
        .limit(limit)
        .all()
    )


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def patch_user(user_id: int, payload: AdminUserPatch, db: Session = Depends(get_db)):
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    user.app_limit = payload.app_limit
    db.commit()
    db.refresh(user)
    return _user_out(db, user)


@router.get("/db-servers", response_model=List[DbServerOut])
def list_db_servers(db: Session = Depends(get_db)):
    return [_server_out(db, s) for s in db.query(models.DbServer).order_by(models.DbServer.id).all()]


@router.post("/db-servers", response_model=DbServerOut, status_code=201)
def create_db_server(payload: DbServerCreate, db: Session = Depends(get_db)):
    server = models.DbServer(
        name=payload.name, host=payload.host, port=payload.port, admin_user=payload.admin_user,
        admin_password_encrypted=crypto.encrypt(payload.admin_password),
        max_databases=payload.max_databases, status="active",
    )
    db.add(server)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="a db server with that name already exists")
    db.refresh(server)
    return _server_out(db, server)


@router.patch("/db-servers/{server_id}", response_model=DbServerOut)
def patch_db_server(server_id: int, payload: DbServerPatch, db: Session = Depends(get_db)):
    server = db.get(models.DbServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="db server not found")
    if payload.status is not None:
        server.status = payload.status
    if payload.max_databases is not None:
        server.max_databases = payload.max_databases
    db.commit()
    db.refresh(server)
    return _server_out(db, server)
