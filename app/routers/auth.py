from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, models, schemas
from app.config import DEFAULT_APP_LIMIT, JWT_EXPIRE_DAYS, SESSION_COOKIE_NAME, SESSION_COOKIE_SECURE
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


def create_user(db: Session, email: str, password: str, first_hint: bool = None,
                terms_accepted: bool = False) -> models.User:
    """Create a user; only the one that wins the platform_init insert becomes uberadmin."""
    first = (db.query(models.User).count() == 0) if first_hint is None else first_hint
    try:
        return _try_insert(db, email, password, first, terms_accepted)
    except IntegrityError:
        if not first:
            raise HTTPException(status_code=409, detail="email already registered")
    # Lost the first-user race (or duplicate email): retry as a normal pending user.
    try:
        return _try_insert(db, email, password, False, terms_accepted)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="email already registered")


@router.post("/register", response_model=schemas.UserOut, status_code=201)
def register(payload: schemas.UserRegister, response: Response, db: Session = Depends(get_db)):
    user = create_user(db, payload.email, payload.password, terms_accepted=True)
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
        raise HTTPException(status_code=401, detail="invalid email or password")
    _set_session_cookie(response, user.id)
    return user


@router.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME)
    return None


@router.get("/me", response_model=schemas.UserOut)
def me(current_user: models.User = Depends(get_current_user)):
    return current_user
