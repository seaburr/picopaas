from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import crypto, models
from app.db import get_db
from app.dependencies import require_uberadmin

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_uberadmin)])

WARN_PCT = 80.0


class AdminUserOut(BaseModel):
    id: int
    email: str
    role: str
    app_limit: int
    app_count: int
    created_at: datetime


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
                app_count=count, created_at=user.created_at)


def _server_out(db: Session, s: models.DbServer) -> DbServerOut:
    used = db.query(func.count(models.AppDatabase.id)).filter(models.AppDatabase.db_server_id == s.id).scalar()
    pct = round(used * 100.0 / s.max_databases, 1) if s.max_databases else 100.0
    return DbServerOut(id=s.id, name=s.name, host=s.host, port=s.port, status=s.status,
                       max_databases=s.max_databases, used=used, pct=pct, warn=pct >= WARN_PCT)


@router.get("/users", response_model=List[AdminUserOut])
def list_users(db: Session = Depends(get_db)):
    return [_user_out(db, u) for u in db.query(models.User).order_by(models.User.id).all()]


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
