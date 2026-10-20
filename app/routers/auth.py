import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, models, schemas
from app import resources
from app.config import (
    DB_QUOTA_BYTES,
    DEFAULT_APP_LIMIT,
    DEFAULT_CPU_LIMIT_M,
    DEFAULT_CPU_REQUEST_M,
    DEFAULT_MEM_LIMIT_MI,
    DEFAULT_MEM_REQUEST_MI,
    EPHEMERAL_LIMIT_MI,
    JWT_EXPIRE_DAYS,
    MAX_IMAGE_SIZE_BYTES,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE,
    STORAGE_QUOTA_BYTES,
)
from app.db import get_db
from app.dependencies import get_current_user
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

_SESSION_MAX_AGE = JWT_EXPIRE_DAYS * 24 * 60 * 60


def _set_session_cookie(response: Response, user_id: int) -> None:
    token = create_access_token(user_id)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        max_age=_SESSION_MAX_AGE,
    )


def _try_insert(db: Session, email: str, password: str, first: bool,
                terms_accepted: bool = False) -> models.User:
    user = models.User(
        email=email.lower(),
        password_hash=hash_password(password),
        role="uberadmin" if first else "user",
        status="active" if first else "pending",
        app_limit=DEFAULT_APP_LIMIT,
        terms_accepted_at=datetime.now(timezone.utc).replace(tzinfo=None) if terms_accepted else None,
    )
    db.add(user)
    if first:
        # Singleton row with a fixed PK: only one transaction can ever insert it.
        db.add(models.PlatformInit(id=1))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise
    return user


class DuplicateEmail(Exception):
    """The email is already taken. Never surfaced to the caller as such (see register)."""


def create_user(db: Session, email: str, password: str, first_hint: bool = None,
                terms_accepted: bool = False) -> models.User:
    """Create a user; only the one that wins the platform_init insert becomes uberadmin."""
    first = (db.query(models.User).count() == 0) if first_hint is None else first_hint
    try:
        return _try_insert(db, email, password, first, terms_accepted)
    except IntegrityError:
        if not first:
            raise DuplicateEmail()
    # Lost the first-user race (or duplicate email): retry as a normal pending user.
    try:
        return _try_insert(db, email, password, False, terms_accepted)
    except IntegrityError:
        raise DuplicateEmail()


@router.post("/register", response_model=schemas.UserOut, status_code=201)
def register(payload: schemas.UserRegister, response: Response, db: Session = Depends(get_db)):
    try:
        user = create_user(db, payload.email, payload.password, terms_accepted=True)
    except DuplicateEmail:
        # Enumeration-safe: answer exactly like a fresh pending signup (201, same body shape, a session cookie)
        # so the response never reveals that the address has an account. We send no email, so there is nobody
        # to notify; the existing owner just logs in with their own password. The cookie names user 0, which
        # never exists, so it authenticates nobody (a later /auth/me is 401, the page falls back to login).
        top = db.query(func.max(models.User.id)).scalar() or 0
        audit.record(db, None, "user.register_duplicate", "user", None, "email already in use",
                     actor_email=payload.email.lower()[:255])
        _set_session_cookie(response, 0)
        return schemas.UserOut(
            id=top + 1, email=payload.email.lower(), role="user", app_limit=DEFAULT_APP_LIMIT,
            status="pending", created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    db.refresh(user)
    audit.record(db, user, "user.register", "user", user.id, f"status={user.status}")
    _set_session_cookie(response, user.id)
    return user


@router.post("/login", response_model=schemas.UserOut)
def login(payload: schemas.UserLogin, response: Response, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == payload.email.lower()).first()
    if user is None or user.status == "deleted" or not verify_password(payload.password, user.password_hash):
        audit.record(db, None, "login.failed", "user", user.id if user else None,
                     "invalid credentials", actor_email=payload.email.lower()[:255])
        raise HTTPException(
            status_code=401,
            detail="Email or password is wrong. Try again, or ask an admin to reset your password.",
        )
    _set_session_cookie(response, user.id)
    return user


@router.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME)
    return None


def _spaces_configured() -> bool:
    return all(os.environ.get(k) for k in ("DIGITALOCEAN_TOKEN", "SPACES_ACCESS_KEY_ID", "SPACES_SECRET_ACCESS_KEY"))


def _db_placement_available(db: Session) -> bool:
    """True when a new app database could be placed now: an 'active' DbServer with a free slot (draining/full don't count)."""
    from app.db_placement import NoDbCapacity, choose_server

    try:
        choose_server(db)
        return True
    except NoDbCapacity:
        return False


@router.get("/me", response_model=schemas.MeOut)
def me(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    out = schemas.UserOut.model_validate(current_user).model_dump()
    return schemas.MeOut(
        **out,
        features=schemas.Features(
            databases=_db_placement_available(db),
            storage=_spaces_configured(),
        ),
        limits=schemas.Limits(
            cpu_limit_m=DEFAULT_CPU_LIMIT_M,
            mem_limit_mi=DEFAULT_MEM_LIMIT_MI,
            cpu_request_m=DEFAULT_CPU_REQUEST_M,
            mem_request_mi=DEFAULT_MEM_REQUEST_MI,
            image_mb=MAX_IMAGE_SIZE_BYTES // (1024 * 1024),
            db_mb=DB_QUOTA_BYTES // (1024 * 1024),
            bucket_mb=STORAGE_QUOTA_BYTES // (1024 * 1024),
            ephemeral_mi=EPHEMERAL_LIMIT_MI,
            override=schemas.LimitOverride(
                cpu_min_m=resources.CPU_MIN_M, cpu_max_m=resources.CPU_MAX_M,
                mem_min_mi=resources.MEM_MIN_MI, mem_max_mi=resources.MEM_MAX_MI,
            ),
        ),
    )


@router.post("/change-password", status_code=204)
def change_password(
    payload: schemas.ChangePassword,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Works for pending/suspended users too (they can still log in); needs the current password."""
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Your current password is wrong. Check it and try again.")
    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=422, detail="Choose a new password that is different from your current one.")
    current_user.password_hash = hash_password(payload.new_password)
    current_user.must_change_password = False
    db.commit()
    audit.record(db, current_user, "user.change_password", "user", current_user.id)
    return Response(status_code=204)
