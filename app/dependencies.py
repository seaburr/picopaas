from datetime import datetime, timedelta, timezone
import re
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import models
from app.config import SESSION_COOKIE_NAME
from app.db import get_db
from app.providers.base import Registry
from app.providers.registry import RegistryClient
from app.security import decode_access_token, hash_api_key

# One shared RegistryClient (its own httpx.Client is reused across requests).
# Overridden in tests via `app.dependency_overrides[get_registry]` with a
# `app.providers.fake.FakeRegistry`.
_registry_client = RegistryClient()

# Only write last_used_at when it is older than this, so busy keys don't write per request.
API_KEY_LAST_USED_THROTTLE = timedelta(minutes=5)

# A read-only API key may only make non-mutating requests. Session (cookie) auth is always full access.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


_kube_client = None


def get_kube():
    """Lazy real KubeClient (tests override with FakeKubeProvider)."""
    global _kube_client
    if _kube_client is None:
        from app.providers.kube import KubeClient

        _kube_client = KubeClient()
    return _kube_client


def get_db_server_checker():
    """Real connection checker for admin db-server create/rotate (tests override with FakeDbServerChecker)."""
    from app.providers.dbcheck import RealDbServerChecker

    return RealDbServerChecker()


def get_registry() -> Registry:
    return _registry_client


def _user_from_request(request: Request, db: Session) -> Optional[models.User]:
    """Resolve a user from either the session cookie (UI) or a Bearer API key (CLI/YAML).

    Sets request.state.auth_via to "cookie" or "api_key" accordingly.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        user_id = decode_access_token(token)
        if user_id is not None:
            user = db.get(models.User, user_id)
            if user is not None and user.status != "deleted":
                request.state.auth_via = "cookie"
                request.state.api_scope = "full"  # a browser session always has full access
                return user

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        raw_key = auth_header[len("Bearer "):]
        key_hash = hash_api_key(raw_key)
        api_key = (
            db.query(models.ApiKey)
            .filter(models.ApiKey.hashed == key_hash, models.ApiKey.revoked_at.is_(None))
            .first()
        )
        if api_key is not None:
            now = datetime.now(timezone.utc)
            last = api_key.last_used_at
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last is None or now - last > API_KEY_LAST_USED_THROTTLE:
                api_key.last_used_at = now
                db.commit()
            request.state.auth_via = "api_key"
            request.state.api_scope = api_key.scope or "full"
            user = db.get(models.User, api_key.user_id)
            if user is not None and user.status != "deleted":
                return user

    return None


_PRESIGN_PATH = re.compile(r"^(/api)?/apps/\d+/storage/presign/?$")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    user = _user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="You're logged out. Log in again, or check that your API key (PICOPAAS_TOKEN) is valid and not revoked.")
    # Read-only keys can authenticate anywhere but may only make non-mutating requests. Enforced at this single
    # choke point so it covers app, admin and spec routes alike (every authenticated route depends on this).
    # POST /apps/{id}/storage/presign is a POST that can be a pure read (GET presign); the route itself
    # rejects PUT presigns for read-scope keys.
    if (getattr(request.state, "api_scope", "full") == "read" and request.method not in SAFE_METHODS
            and not _PRESIGN_PATH.match(request.url.path)):
        raise HTTPException(
            status_code=403,
            detail="This API key is read-only. Create a full-access key to make changes.",
        )
    return user


def get_active_user(current_user: models.User = Depends(get_current_user)) -> models.User:
    """Shared gate for /apps*, /api/keys* and spec endpoints (cookie and API key alike)."""
    if current_user.status == "pending":
        raise HTTPException(status_code=403, detail="Your account is waiting for administrator approval. You'll be able to deploy once it's approved.")
    if current_user.status != "active":
        raise HTTPException(status_code=403, detail="Your account is suspended and your apps are stopped. Contact an administrator to restore it.")
    return current_user


def require_session_auth(request: Request, current_user: models.User = Depends(get_active_user)) -> models.User:
    """Guards actions an API key must never perform even at full scope — currently key management, so a leaked
    key can neither mint another key nor change a key's scope (Chas, 2026-10-20). Browser session only."""
    if getattr(request.state, "auth_via", None) != "cookie":
        raise HTTPException(
            status_code=403,
            detail="API keys are managed from the web UI only; an API key can't create or revoke keys.",
        )
    return current_user


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> Optional[models.User]:
    try:
        return _user_from_request(request, db)
    except Exception:
        return None


def require_platform_admin(current_user: models.User = Depends(get_current_user)) -> models.User:
    if not current_user.is_platform_admin or current_user.status != "active":
        raise HTTPException(status_code=403, detail="This needs administrator access.")
    return current_user


def require_admin_session(request: Request, current_user: models.User = Depends(require_platform_admin)) -> models.User:
    """Platform admin AND a browser session: for endpoints exposing platform internals (logs) that a leaked admin API key,
    even a read-only one, must not be able to pull."""
    if getattr(request.state, "auth_via", None) != "cookie":
        raise HTTPException(status_code=403, detail="This admin view can only be read from the web UI, not with an API key.")
    return current_user
