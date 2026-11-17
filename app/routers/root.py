"""Break-glass over HTTP: reset a platform admin's password with the optional ROOT_MANAGEMENT_KEY (app/config.py).

For an operator who is locked out and has no cluster access for `python -m app.manage`. The key is a startup env var
and is never stored in the database. With no key set the route answers 404. It only ever resets the password of an
existing platform admin: it cannot create or promote accounts, and it returns nothing else."""
import hashlib
import hmac
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import audit, config, models, ratelimit
from app.db import get_db
from app.security import hash_password

router = APIRouter(prefix="/root", tags=["root"])
ACTOR = "root-management-key"


class AdminResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: Optional[str] = Field(default=None, max_length=255)  # may be left out when there is exactly one admin


class AdminResetOut(BaseModel):
    email: str
    temporary_password: str


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", "replace")).digest()


def _require_root_key(request: Request, db: Session = Depends(get_db)) -> None:
    """A dependency, so it runs before the body is validated: with no key set every request is a plain 404, and a bad
    body never gets a 422 (which would reveal the route exists and echo the input)."""
    key = config.ROOT_MANAGEMENT_KEY
    if not key:
        raise HTTPException(status_code=404, detail="Not Found")  # same body as an unknown path
    given = request.headers.get("authorization", "")
    presented = given[7:] if given[:7].lower() == "bearer " else ""
    # equal-length digests, so the comparison time says nothing about the key or its length
    if not (presented and hmac.compare_digest(_digest(presented), _digest(key))):
        ratelimit.record_root_key_failure(db, request)  # 429 past the cap, before another audit row is written
        audit.record(db, None, "root.auth_failed", None, None, f"ip={ratelimit.client_ip(request)}", actor_email=ACTOR)
        raise HTTPException(status_code=401, detail="The root management key is missing or wrong.",
                            headers={"WWW-Authenticate": "Bearer"})


@router.post("/admin-reset", response_model=AdminResetOut)
def admin_reset(request: Request, response: Response, payload: Optional[AdminResetIn] = None,
                db: Session = Depends(get_db), _key: None = Depends(_require_root_key)):
    """Set a random temporary password on an active platform admin and return it once. The admin must change it at the
    next login; failed-login lockout for that email is cleared. Sessions and API keys are left as they are."""
    admins = (db.query(models.User).filter(models.User.role == "platform_admin", models.User.status == "active")
              .order_by(models.User.id).all())
    email = (payload.email or "").strip().lower() if payload else ""
    if email:
        user = next((u for u in admins if u.email == email), None)
        if user is None:
            raise HTTPException(status_code=404, detail="No active platform administrator has that email.")
    elif len(admins) == 1:
        user = admins[0]
    elif not admins:
        raise HTTPException(status_code=409, detail=(
            "There is no active platform administrator to reset. Use `python -m app.manage grant-admin <email>` "
            "inside an API pod."))
    else:
        raise HTTPException(status_code=409, detail=(
            "There are several platform administrators. Send {\"email\": ...} with one of: "
            + ", ".join(u.email for u in admins) + "."))
    temp = secrets.token_urlsafe(18)
    user.password_hash = hash_password(temp)
    user.must_change_password = True
    db.commit()
    ratelimit.clear_login_failures(db, user.email)
    audit.record(db, None, "user.reset_password", "user", user.id, f"via=root-key ip={ratelimit.client_ip(request)}",
                 actor_email=ACTOR)
    response.headers["Cache-Control"] = "no-store"
    return AdminResetOut(email=user.email, temporary_password=temp)
