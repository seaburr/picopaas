from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.db import Base, engine
from app.routers import api_keys, apps, auth

# No Alembic auto-run here: migrations are applied explicitly (`alembic upgrade head`).
# create_all is a no-op against tables that already exist, which keeps the SQLite
# test suite (in-memory, no migrations run) working without duplicating schema logic.
Base.metadata.create_all(bind=engine)

app = FastAPI(title="PicoPaaS API")

app.include_router(auth.router)
app.include_router(apps.router)
app.include_router(api_keys.router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# --- Frontend (Vue SPA, built by `frontend/` into ../static — see
# frontend/vite.config.ts outDir and docs/ARCHITECTURE.md "Platform service") ---
#
# `static/` doesn't exist until `npm run build` has been run (it's gitignored),
# so both the mount and the fallback route below degrade gracefully when it's
# missing — tests and a bare `uvicorn app.main:app` don't need a frontend build.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"
_API_PREFIXES = ("auth", "apps", "api", "healthz", "static")

if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    """Serve the built SPA for any non-API path, falling back to index.html
    (client-side routing) for everything frontend/src/router doesn't know
    about either — same as any other single-page app host."""
    if full_path.startswith(_API_PREFIXES):
        raise HTTPException(status_code=404, detail="not found")
    if _INDEX_HTML.is_file():
        return FileResponse(_INDEX_HTML)
    raise HTTPException(status_code=404, detail="frontend not built: run `npm run build` in frontend/")
