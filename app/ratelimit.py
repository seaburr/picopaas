"""DB-backed fixed-window throttle for /auth/login and /auth/register.

State lives in the platform database (table auth_throttle) so all replicas share it. Every entry point FAILS OPEN:
if the store errors we log and allow the request rather than lock people out on a DB hiccup.

Client IP: Traefik (untrusted forwarded-headers, the default) discards any client-supplied X-Forwarded-For and
sets it to the TCP peer it saw. We take the RIGHTMOST entry, which is the one our own proxy wrote, so a client
cannot spoof it by sending its own header. Traefik's Service uses externalTrafficPolicy Local, so that peer is the real client IP (measured 2026-11-18).
In-cluster callers appear as Traefik pod IPs and share buckets. The per-email login lockout stays the primary login control.
"""
import hashlib
import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import audit, config, models

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
        keep = max(config.LOGIN_WINDOW_SECONDS, config.REGISTER_WINDOW_SECONDS, config.API_KEY_HOUR_WINDOW_SECONDS,
                   config.INVALID_API_KEY_RETENTION_SECONDS)
        cutoff = _now() - timedelta(seconds=keep + 60)
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


def check_bundle_fetch(db: Session, request: Request) -> None:
    """Count a public Kaisen bundle download (valid token or not) against the client IP; 429 past the cap."""
    if not config.RATE_LIMIT_ENABLED:
        return

    def run():
        w, n = config.KAISEN_BUNDLE_FETCH_WINDOW_SECONDS, config.KAISEN_BUNDLE_FETCH_MAX
        key = _key("kaisen-bundle-ip", client_ip(request))
        wait = _remaining(db, key, n, w)
        if wait:
            raise _deny(wait)
        _prune(db)
        _bump(db, key, w)

    _safe(db, "check_bundle_fetch", run)


def record_root_key_failure(db: Session, request: Request) -> None:
    """Count a wrong ROOT_MANAGEMENT_KEY against the client IP; 429 past the cap. Only wrong keys are counted and only
    wrong keys are refused: the right key always works, so a flood of bad attempts cannot lock the operator out of the
    recovery route. The key's length is what stops guessing; this bounds the audit rows a flood can write."""
    if not config.RATE_LIMIT_ENABLED:
        return

    def run():
        w, n = config.ROOT_KEY_WINDOW_SECONDS, config.ROOT_KEY_MAX_FAILS
        key = _key("root-key-ip", client_ip(request))
        wait = _remaining(db, key, n, w)
        if wait:
            raise _deny(wait)
        _prune(db)
        _bump(db, key, w)

    _safe(db, "record_root_key_failure", run)


def check_improve_brief(db: Session, request: Request, user_id: int) -> None:
    """Count a Kaisen "Improve brief" press (a billed model call) against the user; 429 past the cap."""
    if not config.RATE_LIMIT_ENABLED:
        return

    def run():
        w, n = config.KAISEN_IMPROVE_RATE_WINDOW_SECONDS, config.KAISEN_IMPROVE_RATE_MAX
        key = _key("kaisen-improve-user", str(user_id))
        wait = _remaining(db, key, n, w)
        if wait:
            raise _deny(wait)
        _prune(db)
        _bump(db, key, w)

    _safe(db, "check_improve_brief", run)


def check_upload(db: Session, request: Request, user_id: int, project_id: int) -> None:
    """Count a Kaisen upload attempt (rejected ones too) against the user, the project and the client IP; 429 past the cap."""
    if not config.RATE_LIMIT_ENABLED:
        return

    def run():
        w, n = config.KAISEN_UPLOAD_RATE_WINDOW_SECONDS, config.KAISEN_UPLOAD_RATE_MAX
        keys = [_key("kaisen-upload-user", str(user_id)), _key("kaisen-upload-project", str(project_id)),
                _key("kaisen-upload-ip", client_ip(request))]
        wait = max(_remaining(db, k, n, w) for k in keys)
        if wait:
            raise _deny(wait)
        _prune(db)
        for k in keys:
            _bump(db, k, w)

    _safe(db, "check_upload", run)


def check_api_key_budget(db: Session, api_key: "models.ApiKey") -> None:
    """Count one request authenticated by `api_key` (any scope) against its per-minute and per-hour budget; 429 past
    either. Keyed by the key's database id, never the key text. Check first, then bump (one write per request), so a
    refused request does not extend its own lockout. The first refusal in a window writes one audit row."""
    if not config.RATE_LIMIT_ENABLED:
        return

    def run():
        limits = (("minute", config.API_KEY_RATE_PER_MINUTE, config.API_KEY_MINUTE_WINDOW_SECONDS),
                  ("hour", config.API_KEY_RATE_PER_HOUR, config.API_KEY_HOUR_WINDOW_SECONDS))
        keys = {name: _key(f"api-key-{name}", str(api_key.id)) for name, _, _ in limits}
        for name, limit, window in limits:
            wait = _remaining(db, keys[name], limit, window)
            if wait:
                _audit_first_refusal(db, api_key, name, _key(f"api-key-refused-{name}", str(api_key.id)), window)
                raise HTTPException(
                    status_code=429, headers={"Retry-After": str(wait)},
                    detail=f"This API key made too many requests ({limit} per {name}). Try again in {wait} seconds.")
        _prune(db)
        for name, _, window in limits:
            _bump(db, keys[name], window)

    _safe(db, "check_api_key_budget", run)


def _audit_first_refusal(db: Session, api_key: "models.ApiKey", window_name: str, dedupe_key: str, window: int) -> None:
    """One `api_key.rate_limited` audit row per key per window: a second AuthThrottle row marks 'already audited'."""
    if _remaining(db, dedupe_key, 1, window):
        return
    _bump(db, dedupe_key, window)
    owner = db.get(models.User, api_key.user_id)
    audit.record(db, None, "api_key.rate_limited", "user", api_key.user_id,
                 f"key_id={api_key.id} window={window_name}", actor_email=owner.email if owner else None)


def check_invalid_api_key_ip(db: Session, request: Request) -> None:
    """Called only for a bearer that matches no key. Refuse (429) once this client IP has sent too many wrong keys in
    a minute or an hour, else count this one (check first, then bump: one write per invalid request, on a per-IP row).
    Wrong keys only: a valid key never reaches this, so it is never blocked by an IP's bad-key history. In-cluster
    callers share the Traefik pod IPs but do not send invalid keys to the platform API in normal operation."""
    if not config.RATE_LIMIT_ENABLED:
        return

    def run():
        ip = client_ip(request)
        limits = ((config.INVALID_API_KEY_IP_MAX_PER_MINUTE, config.API_KEY_MINUTE_WINDOW_SECONDS),
                  (config.INVALID_API_KEY_IP_MAX_PER_HOUR, config.API_KEY_HOUR_WINDOW_SECONDS))
        keys = [_key(f"invalid-key-ip-{w}", ip) for _, w in limits]
        for k, (n, w) in zip(keys, limits):
            wait = _remaining(db, k, n, w)
            if wait:
                raise HTTPException(status_code=429, headers={"Retry-After": str(wait)},
                                    detail="Too many invalid API keys from this address. Try again later.")
        _prune(db)
        for k, (_, w) in zip(keys, limits):
            _bump(db, k, w)

    _safe(db, "check_invalid_api_key_ip", run)


def _invalid_key_bucket(now: datetime) -> str:
    return "invalid-api-key:global:" + now.strftime("%Y%m%d%H%M")


# Invalid bearers are counted in process memory and written in batches (K158): an anonymous flood of bad keys must not
# become one database write per request on a single hot row. Each process writes at most once per
# INVALID_API_KEY_FLUSH_SECONDS (and when the minute bucket changes), adding its pending count in one statement.
INVALID_API_KEY_FLUSH_SECONDS = 5.0
_invalid_lock = threading.Lock()
_invalid_pending: dict[str, int] = {}
_invalid_last_flush = 0.0


def _add_to_bucket(db: Session, key: str, n: int) -> None:
    row = db.get(models.AuthThrottle, key)
    if row is None:
        db.add(models.AuthThrottle(key=key, window_start=_now(), count=n))
        try:
            db.commit()
            return
        except Exception:
            db.rollback()  # another replica inserted it first: fall through and add
    db.execute(update(models.AuthThrottle).where(models.AuthThrottle.key == key)
               .values(count=models.AuthThrottle.count + n))
    db.commit()


def _flush_invalid(db: Session, force: bool = False) -> None:
    global _invalid_last_flush
    with _invalid_lock:
        due = force or time.monotonic() - _invalid_last_flush >= INVALID_API_KEY_FLUSH_SECONDS \
            or any(k != _invalid_key_bucket(_now()) for k in _invalid_pending)
        if not due or not _invalid_pending:
            return
        batch = dict(_invalid_pending)
        _invalid_pending.clear()
        _invalid_last_flush = time.monotonic()

    def run():
        _prune(db)
        for key, n in batch.items():
            _add_to_bucket(db, key, n)

    _safe(db, "flush_invalid_api_keys", run)


def record_invalid_api_key(db: Session) -> None:
    """Count one bearer that resolved to no API key in the current minute's global bucket. Never blocks and never stores
    the bearer. Buckets are named invalid-api-key:global:<UTC YYYYmmddHHMM>; read them with invalid_api_keys_last.
    Counted in memory and written in batches, so a flood costs at most one write per process every few seconds."""
    if not config.RATE_LIMIT_ENABLED:
        return
    with _invalid_lock:
        key = _invalid_key_bucket(_now())
        _invalid_pending[key] = _invalid_pending.get(key, 0) + 1
    _flush_invalid(db)


def invalid_api_keys_last(db: Session, minutes: int) -> int:
    """Invalid API-key bearers seen in the last `minutes` whole-minute buckets (the current one included). Retained for
    INVALID_API_KEY_RETENTION_SECONDS; 0 when rate limiting is disabled (nothing is counted)."""
    _flush_invalid(db, force=True)  # this process's pending count; other replicas flush within a few seconds
    now = _now()
    names = [_invalid_key_bucket(now - timedelta(minutes=i)) for i in range(max(1, minutes))]
    rows = db.query(models.AuthThrottle).filter(models.AuthThrottle.key.in_(names)).all()
    return sum(r.count for r in rows)
