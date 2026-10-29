"""DB-backed fixed-window throttle for /auth/login and /auth/register.

State lives in the platform database (table auth_throttle) so all replicas share it. Every entry point FAILS OPEN:
if the store errors we log and allow the request rather than lock people out on a DB hiccup.

Client IP: Traefik (untrusted forwarded-headers, the default) discards any client-supplied X-Forwarded-For and
sets it to the TCP peer it saw. We take the RIGHTMOST entry, which is the one our own proxy wrote, so a client
cannot spoof it by sending its own header. Behind the DO load balancer in TCP mode without PROXY protocol that peer
may be the LB rather than the end user, so the IP buckets can be coarse. That is why the per-email login lockout is
the primary control and the IP limits are best-effort (login IP limit is looser than the email one).
"""
import hashlib
import logging
import random
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import config, models

log = logging.getLogger(__name__)

TOO_MANY = "Too many attempts. Wait a few minutes and try again."


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if parts:
        return parts[-1][:64]
    return request.client.host if request.client else "unknown"


def _key(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()[:64]


def email_key(email: str) -> str:
    return _key("login-email", email.strip().lower())


def login_ip_key(ip: str) -> str:
    return _key("login-ip", ip)


def register_ip_key(ip: str) -> str:
    return _key("register-ip", ip)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _remaining(db: Session, key: str, limit: int, window: int) -> int:
    """Seconds until `key` may try again; 0 if it is under the limit."""
    row = db.get(models.AuthThrottle, key)
    if row is None:
        return 0
    expires = row.window_start + timedelta(seconds=window)
    now = _now()
    if expires <= now or row.count < limit:
        return 0
    return max(1, int((expires - now).total_seconds()))


def _bump(db: Session, key: str, window: int) -> None:
    now = _now()
    row = db.get(models.AuthThrottle, key)
    if row is None:
        db.add(models.AuthThrottle(key=key, window_start=now, count=1))
        try:
            db.commit()
            return
        except Exception:
            db.rollback()  # another replica inserted it first: fall through and increment
    elif row.window_start + timedelta(seconds=window) <= now:
        row.window_start, row.count = now, 1
        db.commit()
        return
    db.execute(update(models.AuthThrottle).where(models.AuthThrottle.key == key)
               .values(count=models.AuthThrottle.count + 1))
    db.commit()


def _prune(db: Session) -> None:
    if random.random() < 0.02:
        cutoff = _now() - timedelta(seconds=max(config.LOGIN_WINDOW_SECONDS, config.REGISTER_WINDOW_SECONDS) + 60)
        db.query(models.AuthThrottle).filter(models.AuthThrottle.window_start < cutoff).delete()
        db.commit()


def _deny(retry_after: int) -> HTTPException:
    return HTTPException(status_code=429, detail=TOO_MANY, headers={"Retry-After": str(retry_after)})


def _safe(db: Session, what: str, fn):
    try:
        return fn()
    except HTTPException:
        raise
    except Exception:
        log.exception("auth throttle %s failed; failing open", what)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def check_login(db: Session, request: Request, email: str) -> None:
    """Raise 429 if this email or client IP is locked out. Same answer whether or not the account exists."""
    if not config.RATE_LIMIT_ENABLED:
        return

    def run():
        w = config.LOGIN_WINDOW_SECONDS
        wait = max(
            _remaining(db, email_key(email), config.LOGIN_EMAIL_MAX_FAILS, w),
            _remaining(db, login_ip_key(client_ip(request)), config.LOGIN_IP_MAX_FAILS, w),
        )
        if wait:
            raise _deny(wait)

    _safe(db, "check_login", run)


def record_login_failure(db: Session, request: Request, email: str) -> None:
    if not config.RATE_LIMIT_ENABLED:
        return

    def run():
        _prune(db)
        _bump(db, email_key(email), config.LOGIN_WINDOW_SECONDS)
        _bump(db, login_ip_key(client_ip(request)), config.LOGIN_WINDOW_SECONDS)

    _safe(db, "record_login_failure", run)


def clear_login_failures(db: Session, email: str) -> None:
    if not config.RATE_LIMIT_ENABLED:
        return

    def run():
        db.query(models.AuthThrottle).filter(models.AuthThrottle.key == email_key(email)).delete()
        db.commit()

    _safe(db, "clear_login_failures", run)


def check_register(db: Session, request: Request) -> None:
    """Count this signup attempt against the client IP and raise 429 once the cap is passed."""
    if not config.RATE_LIMIT_ENABLED:
        return

    def run():
        key = register_ip_key(client_ip(request))
        wait = _remaining(db, key, config.REGISTER_IP_MAX, config.REGISTER_WINDOW_SECONDS)
        if wait:
            raise _deny(wait)
        _prune(db)
        _bump(db, key, config.REGISTER_WINDOW_SECONDS)

    _safe(db, "check_register", run)


def check_invite_accept(db: Session, request: Request, user_id: int) -> None:
    """Count this invite-accept attempt (valid token or not) against the user and the client IP; 429 past the cap."""
    if not config.RATE_LIMIT_ENABLED:
        return

    def run():
        w, n = config.INVITE_ACCEPT_WINDOW_SECONDS, config.INVITE_ACCEPT_MAX
        keys = [_key("invite-user", str(user_id)), _key("invite-ip", client_ip(request))]
        wait = max(_remaining(db, k, n, w) for k in keys)
        if wait:
            raise _deny(wait)
        _prune(db)
        for k in keys:
            _bump(db, k, w)

    _safe(db, "check_invite_accept", run)
