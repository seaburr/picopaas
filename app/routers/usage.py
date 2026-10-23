"""Live usage and the storage recovery path.

An over-quota app only holds a read-only Spaces key, so it cannot delete its own objects. The platform deletes
for it (with the platform's own credentials), re-measures, and restores the read-write key straight away when
the bucket is back under its cap, rather than waiting for the next poll.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
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
