"""Live usage and the storage recovery path.

An over-quota app only holds a read-only Spaces key, so it cannot delete its own objects. The platform deletes
for it (with the platform's own credentials), re-measures, and restores the read-write key straight away when
the bucket is back under its cap, rather than waiting for the next poll.
"""
import logging
import re
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app import audit, models, quota, resources
from app.db import get_db
from app.dependencies import get_active_user
from app.providers.base import MySQLAdminError, ObjectStore, ObjectStoreError
from app.routers.app_ops import _get_app_or_404
from app.routers.apps import get_mysql_admin_factory, get_object_store
from app.schemas import UsageOut

log = logging.getLogger(__name__)
router = APIRouter(prefix="/apps", tags=["usage"])

MAX_DELETE_PER_CALL = 5000
_UNAVAILABLE = "Couldn't reach the service to check usage right now. Try again in a moment."


class PrefixUsage(BaseModel):
    prefix: str
    size_bytes: int
    objects: int


class StorageUsageOut(UsageOut):
    objects: int
    top_prefixes: List[PrefixUsage] = []


class StorageDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: Optional[str] = Field(default=None, min_length=1, max_length=1024)
    all: bool = False

    @model_validator(mode="after")
    def _one_of(self):
        if self.all == (self.prefix is not None):
            raise ValueError('Send exactly one of {"prefix": "..."} or {"all": true}.')
        return self


class StorageDeleteOut(BaseModel):
    deleted: int
    size_bytes: int
    truncated: bool = False
    over_quota: bool = False


# --- presigned object transfer (REQ-8) ----------------------------------------------------------------------
PRESIGN_DEFAULT_SECONDS = 600
PRESIGN_MAX_SECONDS = 3600
PRESIGN_PER_MINUTE = 30
RESERVED_KEY_PREFIXES: tuple = ()  # the platform keeps no marker objects in app buckets today
_presign_hits: Dict[int, Deque[float]] = defaultdict(deque)


def _rate_limit_presign(user_id: int) -> None:
    """Best-effort per-process cap on presigns per user per minute."""
    now = time.monotonic()
    q = _presign_hits[user_id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= PRESIGN_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Too many transfer links requested. Wait a minute and try again.")
    q.append(now)


def normalise_key(key: str) -> str:
    key = unicodedata.normalize("NFC", key)
    if not 1 <= len(key) <= 512:
        raise ValueError("Object key must be 1 to 512 characters.")
    if key.startswith("/") or "\\" in key or any(unicodedata.category(c) == "Cc" for c in key):
        raise ValueError("Object key can't start with '/', contain a backslash or control characters.")
    if re.search(r"\.\.", key):
        raise ValueError("Object key can't contain '..'.")
    key = re.sub(r"/{2,}", "/", key)
    if any(key.startswith(p) for p in RESERVED_KEY_PREFIXES):
        raise ValueError("That key prefix is reserved by the platform.")
    return key


class PresignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["PUT", "GET"]
    key: str
    content_length: Optional[int] = Field(default=None, ge=1)
    expires_seconds: int = Field(default=PRESIGN_DEFAULT_SECONDS, ge=1, le=PRESIGN_MAX_SECONDS)

    @model_validator(mode="after")
    def _check(self):
        self.key = normalise_key(self.key)
        if self.method == "PUT" and self.content_length is None:
            raise ValueError("content_length (bytes) is required for PUT.")
        return self


class PresignOut(BaseModel):
    url: str
    method: str
    headers: Dict[str, str] = {}
    expires_at: datetime
    key: str


@router.post("/{app_id}/storage/presign", response_model=PresignOut)
def storage_presign(
    app_id: int,
    body: PresignIn,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
    object_store: ObjectStore = Depends(get_object_store),
):
    app = _get_app_or_404(db, app_id, user)
    row = app.storage
    if row is None:
        raise HTTPException(status_code=404, detail="This app has no storage bucket.")
    if body.method == "PUT":
        if getattr(request.state, "api_scope", "full") == "read":
            raise HTTPException(status_code=403, detail="This API key is read-only. Create a full-access key to upload.")
        if row.over_quota:
            raise HTTPException(status_code=409, detail="Your bucket is over its size cap and read-only. Delete objects first (picopaas storage-delete).")
    _rate_limit_presign(user.id)
    try:
        if body.method == "PUT":
            limit = resources.storage_quota_bytes(app)
            remaining = limit - object_store.bucket_size_bytes(app.id)
            if remaining <= 0:
                raise HTTPException(status_code=409, detail="Your bucket is at its size cap. Delete objects first.")
            if body.content_length > remaining:
                raise HTTPException(
                    status_code=413,
                    detail=f"That file is {body.content_length} bytes but only {remaining} bytes remain in your bucket quota.")
        signed = object_store.presign(app.id, body.method, body.key, body.expires_seconds, body.content_length)
    except ObjectStoreError:
        raise HTTPException(status_code=502, detail="Couldn't create the transfer link right now. Try again in a moment.")
    audit.record(db, user, "storage.presign", "app", app.id,
                 f"method={body.method} key={body.key[:150]} size={body.content_length if body.content_length else '-'}")
    return PresignOut(url=signed["url"], method=body.method, headers=signed.get("headers", {}),
                      expires_at=datetime.now(timezone.utc) + timedelta(seconds=body.expires_seconds), key=body.key)


@router.get("/{app_id}/database/usage", response_model=UsageOut)
def database_usage(
    app_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
    mysql_admin_factory=Depends(get_mysql_admin_factory),
):
    app = _get_app_or_404(db, app_id, user)
    row = app.database
    if row is None:
        raise HTTPException(status_code=404, detail="This app has no database.")
    try:
        size = mysql_admin_factory(row.server).db_size_bytes(app.id)
    except MySQLAdminError:
        raise HTTPException(status_code=502, detail=_UNAVAILABLE)
    row.size_bytes = size
    db.commit()
    return UsageOut(size_bytes=size, limit_bytes=resources.db_quota_bytes(app),
                    over_quota=size > resources.db_quota_bytes(app))


@router.get("/{app_id}/storage/usage", response_model=StorageUsageOut)
def storage_usage(
    app_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
    object_store: ObjectStore = Depends(get_object_store),
):
    app = _get_app_or_404(db, app_id, user)
    row = app.storage
    if row is None:
        raise HTTPException(status_code=404, detail="This app has no storage bucket.")
    try:
        u = object_store.usage(app.id)
    except ObjectStoreError:
        raise HTTPException(status_code=502, detail=_UNAVAILABLE)
    row.size_bytes = u["size_bytes"]
    db.commit()
    return StorageUsageOut(
        size_bytes=u["size_bytes"], limit_bytes=resources.storage_quota_bytes(app),
        over_quota=u["size_bytes"] > resources.storage_quota_bytes(app),
        objects=u["objects"], top_prefixes=u["top_prefixes"],
    )


@router.post("/{app_id}/storage/delete", response_model=StorageDeleteOut)
def storage_delete(
    app_id: int,
    body: StorageDeleteIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_active_user),
    object_store: ObjectStore = Depends(get_object_store),
):
    app = _get_app_or_404(db, app_id, user)
    row = app.storage
    if row is None:
        raise HTTPException(status_code=404, detail="This app has no storage bucket.")
    try:
        res = object_store.delete_objects(app.id, None if body.all else body.prefix, MAX_DELETE_PER_CALL)
        size = object_store.bucket_size_bytes(app.id)
    except ObjectStoreError:
        raise HTTPException(status_code=502, detail="Couldn't delete the objects right now. Try again in a moment.")
    # Audit scope and count only (a prefix is user data, but not a secret).
    audit.record(db, user, "app.storage.delete", "app", app.id,
                 f"{'all' if body.all else 'prefix=' + body.prefix[:100]} deleted={res['deleted']}")
    try:
        over = quota.apply_storage_state(db, row, object_store, size)  # restores the rw key now if under the cap
    except Exception:  # noqa: BLE001 - the delete itself worked; the next poll retries the flip
        db.rollback()
        log.warning("app %s: could not update storage state after delete", app.id)
        over = size > resources.storage_quota_bytes(app)
    return StorageDeleteOut(deleted=res["deleted"], size_bytes=size, truncated=res["truncated"], over_quota=over)
