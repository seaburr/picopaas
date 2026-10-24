"""Admin > Settings: global platform settings (registry and defaults in app/platform_settings.py)."""
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app import audit, models, platform_settings
from app.db import get_db
from app.dependencies import require_platform_admin

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])


class SettingsPatch(BaseModel):
    """`settings`: key -> new value, or null to reset that key to its default. Omitted keys are unchanged."""
    model_config = ConfigDict(extra="forbid")
    settings: Dict[str, Any]


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    return {"groups": platform_settings.describe_all(db)}


@router.put("/settings")
def put_settings(payload: SettingsPatch, db: Session = Depends(get_db),
                 admin: models.User = Depends(require_platform_admin)):
    if not payload.settings:
        raise HTTPException(status_code=422, detail="No settings given.")
    before = {k: platform_settings.get(db, k) for k in payload.settings if k in platform_settings.BY_KEY}
    errs = platform_settings.update(db, payload.settings, admin.email)
    if errs:
        raise HTTPException(status_code=422, detail="; ".join(errs))
    for k, v in payload.settings.items():
        new = platform_settings.get(db, k)
        audit.record(db, admin, "settings.update", "setting", k,
                     f"{before[k]} -> {new}" + (" (reset to default)" if v is None else ""))
    return {"groups": platform_settings.describe_all(db)}
