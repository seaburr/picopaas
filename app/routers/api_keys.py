"""Owner-scoped API key management (`pp_sk_...` keys for CLI/YAML use).

The raw key is returned exactly once, at creation (POST). Everything else
(GET, DELETE) only ever sees the non-secret prefix — see app/models.py::ApiKey
and app/security.py for the hashing scheme, and app/dependencies.py for how
these keys authenticate requests.
"""
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, schemas
from app.db import get_db
from app.dependencies import get_current_user
from app.security import api_key_prefix_for_display, generate_api_key, hash_api_key

router = APIRouter(prefix="/api/keys", tags=["api-keys"])


def _get_owned_key_or_404(db: Session, key_id: int, owner_id: int) -> models.ApiKey:
    api_key = (
        db.query(models.ApiKey)
        .filter(models.ApiKey.id == key_id, models.ApiKey.user_id == owner_id)
        .first()
    )
    if api_key is None:
        raise HTTPException(status_code=404, detail="api key not found")
    return api_key


@router.post("", response_model=schemas.ApiKeyCreateOut, status_code=201)
def create_api_key(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    raw_key = generate_api_key()
    api_key = models.ApiKey(
        user_id=current_user.id,
        hashed=hash_api_key(raw_key),
        prefix=api_key_prefix_for_display(raw_key),
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    return schemas.ApiKeyCreateOut(
        id=api_key.id,
        key=raw_key,
        prefix=api_key.prefix,
        created_at=api_key.created_at,
    )


@router.get("", response_model=List[schemas.ApiKeyOut])
def list_api_keys(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    keys = (
        db.query(models.ApiKey)
        .filter(models.ApiKey.user_id == current_user.id)
        .order_by(models.ApiKey.created_at.desc())
        .all()
    )
    return keys


@router.delete("/{key_id}", status_code=204)
def revoke_api_key(
    key_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    api_key = _get_owned_key_or_404(db, key_id, current_user.id)
    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return None
