"""Admin-editable platform banner, stored as banner_* rows in platform_settings (defaults in app/config.py).

Plain text only. Public read (`GET /api/banner`, it shows on /login); admin read/write under `/admin/banner`.
Reads go through the platform_settings per-database cache, so the public route costs no query on a hit.
"""
import re
import unicodedata
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app import audit, config, models, platform_settings
from app.db import get_db
from app.dependencies import require_platform_admin

# Cc control, Cf format (bidi overrides/isolates, zero-width, joiners, BOM), Zl/Zp line/paragraph separators, Cs, Co, Cn
_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})
# Links are blocked (K134: a hijacked admin could phish on /login): URL schemes, www., scheme-like prefixes, bare domains
_LINK_RE = re.compile(
    r"://|(?<![a-z0-9])www\.|(?<![a-z0-9])(?:mailto|javascript|vbscript)\s*:|(?<![a-z0-9])data:\S"
    r"|(?<![\w.-])[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?![\w-])",
    re.IGNORECASE)
LINK_ERROR = "Links aren't allowed in the banner."
KEYS = ("banner_enabled", "banner_level", "banner_title", "banner_text")


class BannerOut(BaseModel):
    enabled: bool
    level: Literal["info", "warning"]
    title: str
    text: str


class BannerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    level: Literal["info", "warning"]
    title: str = Field(max_length=config.BANNER_TITLE_MAX)
    text: str = Field(max_length=config.BANNER_TEXT_MAX)

    @field_validator("title", "text")
    @classmethod
    def _plain(cls, v: str) -> str:
        v = v.strip()
        if any(ord(c) < 32 or ord(c) == 127 or unicodedata.category(c) in _BAD_CATEGORIES for c in v):
            raise ValueError("can't contain newlines, control or invisible formatting characters")
        if _LINK_RE.search(v):
            raise ValueError(LINK_ERROR)
        return v

    @model_validator(mode="after")
    def _text_when_enabled(self):
        if self.enabled and not self.text:
            raise ValueError("text can't be empty while the banner is enabled")
        return self


def current(db: Session) -> BannerOut:
    raw = platform_settings._overrides(db)
    level = raw.get("banner_level", config.BANNER_LEVEL)
    return BannerOut(
        enabled=raw["banner_enabled"] == "1" if "banner_enabled" in raw else config.BANNER_ENABLED,
        level=level if level in config.BANNER_LEVELS else config.BANNER_LEVEL,
        title=raw.get("banner_title", config.BANNER_TITLE),
        text=raw.get("banner_text", config.BANNER_TEXT),
    )


public_router = APIRouter(tags=["banner"])
admin_router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])


@public_router.get("/api/banner", response_model=BannerOut)
def get_public_banner(db: Session = Depends(get_db)):
    return current(db)


@admin_router.get("/banner", response_model=BannerOut)
def get_admin_banner(db: Session = Depends(get_db)):
    return current(db)


@admin_router.put("/banner", response_model=BannerOut)
def put_banner(payload: BannerIn, db: Session = Depends(get_db),
               admin: models.User = Depends(require_platform_admin)):
    before = current(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    values = {"banner_enabled": "1" if payload.enabled else "0", "banner_level": payload.level,
              "banner_title": payload.title, "banner_text": payload.text}
    rows = {r.key: r for r in db.query(models.PlatformSetting).filter(models.PlatformSetting.key.in_(KEYS)).all()}
    for k, v in values.items():
        if k in rows:
            rows[k].value, rows[k].updated_at, rows[k].updated_by = v, now, admin.email
        else:
            db.add(models.PlatformSetting(key=k, value=v, updated_at=now, updated_by=admin.email))
    db.commit()
    platform_settings.invalidate()
    after = current(db)
    audit.record(db, admin, "banner.update", "setting", "banner",
                 f"enabled {before.enabled} -> {after.enabled}; level {before.level} -> {after.level}; "
                 f"title and text {'changed' if (before.title, before.text) != (after.title, after.text) else 'unchanged'}")
    return after
