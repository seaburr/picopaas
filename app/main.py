import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.db import Base, engine
from app.routers import admin, api_keys, apps, auth, spec

# No Alembic auto-run here: migrations are applied explicitly (`alembic upgrade head`).
# create_all is a no-op against tables that already exist, which keeps the SQLite
# test suite (in-memory, no migrations run) working without duplicating schema logic.
Base.metadata.create_all(bind=engine)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Scheduler/bootstrap are opt-in so tests and `uvicorn --reload` stay inert.
    sched = None
    if os.environ.get("RECONCILER_ENABLED") == "1":
        from app import scheduler

        scheduler.bootstrap_db_server()
        sched = scheduler.start_scheduler()
    try:
        yield
    finally:
        if sched is not None:
            sched.shutdown(wait=False)


app = FastAPI(title="PicoPaaS API", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(apps.router)
app.include_router(api_keys.router)
app.include_router(spec.router)
app.include_router(admin.router)


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
_API_PREFIXES = ("auth", "apps", "admin", "api", "healthz", "static")

if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# Paths a browser may navigate to that must NOT be swapped for the SPA.
_NAV_PASSTHROUGH = ("/static/", "/healthz", "/docs", "/redoc", "/openapi.json")


def _is_browser_navigation(request: Request) -> bool:
    """A top-level page load (typing a URL, refresh, link click), as opposed to the SPA's own fetch() calls,
    API clients, or curl. Browsers send Sec-Fetch-Mode: navigate on navigations and never on fetch()."""
    mode = request.headers.get("sec-fetch-mode")
    if mode is not None:
        return mode == "navigate"
    return request.headers.get("accept", "").startswith("text/html")


@app.middleware("http")
async def spa_on_navigation(request: Request, call_next):
    """SPA pages share paths with API routes (/admin, /admin/health, /apps/...). On a browser navigation, serve
    index.html instead of the API's JSON so refreshes and deep links work; API callers are unaffected."""
    if (
        request.method in ("GET", "HEAD")
        and _INDEX_HTML.is_file()
        and not request.url.path.startswith(_NAV_PASSTHROUGH)
        and _is_browser_navigation(request)
    ):
        return FileResponse(_INDEX_HTML)
    return await call_next(request)


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
