"""Admin-only documentation served from the runtime image (docs/ADMIN_GUIDE.md)."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import require_platform_admin

router = APIRouter(prefix="/admin/docs", tags=["admin"], dependencies=[Depends(require_platform_admin)])

# Repo root in a checkout, /app in the image (the Dockerfile copies the guide to /app/docs/ADMIN_GUIDE.md).
ADMIN_GUIDE_PATH = Path(__file__).resolve().parent.parent.parent / "docs" / "ADMIN_GUIDE.md"


@router.get("/admin-guide")
def admin_guide():
    try:
        return {"markdown": ADMIN_GUIDE_PATH.read_text(encoding="utf-8")}
    except OSError:
        raise HTTPException(status_code=404, detail="The admin guide isn't available in this build (docs/ADMIN_GUIDE.md is missing).")
