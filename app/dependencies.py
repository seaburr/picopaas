from datetime import datetime, timezone
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


_kube_client = None


def get_kube():
    """Lazy real KubeClient (tests override with FakeKubeProvider)."""
    global _kube_client
    if _kube_client is None:
        from app.providers.kube import KubeClient

        _kube_client = KubeClient()
    return _kube_client


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
            api_key.last_used_at = datetime.now(timezone.utc)
            db.commit()
            request.state.auth_via = "api_key"
            user = db.get(models.User, api_key.user_id)
            if user is not None and user.status != "deleted":
                return user

    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    user = _user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="You're logged out. Log in again, or check that your API key (PICOPAAS_TOKEN) is valid and not revoked.")
    return user


def get_active_user(current_user: models.User = Depends(get_current_user)) -> models.User:
    """Shared gate for /apps*, /api/keys* and spec endpoints (cookie and API key alike)."""
    if current_user.status == "pending":
        raise HTTPException(status_code=403, detail="Your account is waiting for admin approval. You'll be able to deploy once it's approved.")
    if current_user.status != "active":
        raise HTTPException(status_code=403, detail="Your account is suspended and your apps are stopped. Contact an admin to restore it.")
    return current_user


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> Optional[models.User]:
    try:
        return _user_from_request(request, db)
    except Exception:
        return None


def require_uberadmin(current_user: models.User = Depends(get_current_user)) -> models.User:
    if not current_user.is_uberadmin:
        raise HTTPException(status_code=403, detail="This needs admin access.")
    return current_user
