import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app import models
from app.db import Base, engine, get_db
from app.routers import admin, api_keys, app_ops, apps, auth, spec, templates, usage

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

        scheduler.rotate_encrypted_data()
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
app.include_router(app_ops.router)
app.include_router(usage.router)
app.include_router(api_keys.router)
app.include_router(spec.router)
app.include_router(templates.router)
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


# The same URL serves the SPA (navigation) and JSON (fetch), so caches must key on these request headers.
_VARY = "Sec-Fetch-Mode, Accept"


def _spa_response() -> FileResponse:
    """index.html: revalidate every time (it names the hashed asset files) and vary on navigation-vs-fetch,
    so a browser never replays the HTML for a fetch() or the JSON for a page load (UX-1)."""
    return FileResponse(_INDEX_HTML, headers={"Cache-Control": "no-cache", "Vary": _VARY})


def _prefers_json(request: Request) -> bool:
    """Accept lists application/json first (or only JSON-ish types) and not text/html first."""
    accept = request.headers.get("accept", "")
    first = accept.split(",", 1)[0].split(";", 1)[0].strip().lower()
    return first == "application/json" or first.endswith("+json")


def _is_browser_navigation(request: Request) -> bool:
    """A top-level page load (typing a URL, refresh, link click), as opposed to the SPA's own fetch() calls,
    API clients, or curl. Browsers send Sec-Fetch-Mode: navigate on navigations and never on fetch().
    A request that explicitly prefers JSON is never a navigation, whatever else it sends."""
    if _prefers_json(request):
        return False
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
        return _spa_response()
    return await call_next(request)


def _host(request: Request) -> str:
    return request.headers.get("host", "").split(":")[0].lower()


def _page(name: str):
    f = _STATIC_DIR / name
    return FileResponse(f) if f.is_file() else None


def _to_platform(request: Request) -> RedirectResponse:
    url = f"https://{config.PLATFORM_HOST}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    return RedirectResponse(url, status_code=301)


_FALLBACK_404 = "<!doctype html><title>Not found</title><h1>404</h1><p>No app lives at this address.</p>"


def _is_unknown_app_host(host: str) -> bool:
    """A subdomain of APP_DOMAIN that isn't the platform, apex or a legacy host. Real apps are routed by their
    own Ingress and never reach this service, so anything arriving here is an app that doesn't exist."""
    d = config.APP_DOMAIN.lower()
    return (
        host.endswith("." + d)
        and host != config.PLATFORM_HOST
        and host != config.APEX_HOST
        and host not in config.LEGACY_HOSTS
    )


_SUSPENDED_TTL = 10.0
_suspended_cache: dict[str, tuple[float, bool]] = {}
_FALLBACK_SUSPENDED = "<!doctype html><title>Suspended</title><h1>503</h1><p>This app is suspended.</p>"


def _is_suspended_host(host: str) -> bool:
    """Is there a suspended (not deleted) app with this hostname? Cached ~10s; hostname is a unique index."""
    now = time.monotonic()
    hit = _suspended_cache.get(host)
    if hit and now - hit[0] < _SUSPENDED_TTL:
        return hit[1]
    label = host[: -(len(config.APP_DOMAIN) + 1)]
    gen = app.dependency_overrides.get(get_db, get_db)()
    db = next(gen)
    try:
        row = (
            db.query(models.App.status)
            .filter(models.App.hostname == label, models.App.deleted_at.is_(None))
            .first()
        )
        result = row is not None and row[0] == "suspended"
    except Exception:
        result = False
    finally:
        gen.close()
    if len(_suspended_cache) > 1000:
        _suspended_cache.clear()
    _suspended_cache[host] = (now, result)
    return result


def _unknown_app_page(host: str = ""):
    headers = {"Cache-Control": "no-store"}
    if host and _is_suspended_host(host):
        f = _STATIC_DIR / "suspended.html"
        if f.is_file():
            return FileResponse(f, status_code=503, headers=headers)
        return HTMLResponse(_FALLBACK_SUSPENDED, status_code=503, headers=headers)
    f = _STATIC_DIR / "404.html"
    if f.is_file():
        return FileResponse(f, status_code=404, headers=headers)
    return HTMLResponse(_FALLBACK_404, status_code=404, headers=headers)


@app.middleware("http")
async def host_routing(request: Request, call_next):
    """Legacy hosts 301 to the platform host; the apex serves the landing/privacy pages and redirects the rest
    (so apex/login works); the platform host also serves /privacy. Registered after spa_on_navigation, so it runs first."""
    host = _host(request)
    path = request.url.path
    if host in config.LEGACY_HOSTS:
        return _to_platform(request)
    if _is_unknown_app_host(host):
        return _unknown_app_page(host)
    if host == config.APEX_HOST:
        if path.startswith(("/static/", "/healthz")):
            return await call_next(request)
        page = {"/": "landing.html", "/privacy": "privacy.html"}.get(path)
        if page and request.method in ("GET", "HEAD"):
            return _page(page) or await call_next(request)
        return _to_platform(request)
    if host == config.PLATFORM_HOST and path == "/privacy" and request.method in ("GET", "HEAD"):
        return _page("privacy.html") or await call_next(request)
    return await call_next(request)


def _csp() -> str:
    # Checked against the real build (static/): one module script, one stylesheet, no eval, no external fonts or
    # scripts. style-src needs 'unsafe-inline' for the <style> blocks / style attributes on the static pages and
    # Vue's style bindings. img-src: data: for the select-arrow SVG in the CSS; the platform host because the
    # apex landing/privacy pages load icons from it.
    return (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: https://{config.PLATFORM_HOST}; font-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )


def _add_security_headers(response, path: str) -> None:
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    # FastAPI's /docs and /redoc pull Swagger/ReDoc from a CDN with inline scripts; a strict CSP would break them.
    if path not in ("/docs", "/redoc"):
        response.headers["Content-Security-Policy"] = _csp()


@app.middleware("http")
async def robots_and_noindex(request: Request, call_next):
    """Outermost middleware (registered last): /robots.txt on every host, and X-Robots-Tag on every response.
    Only affects responses from this service, never tenant apps (served by their own pods)."""
    if request.url.path == "/robots.txt" and request.method in ("GET", "HEAD"):
        response = PlainTextResponse("User-agent: *\nDisallow: /\n")
    else:
        response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    _add_security_headers(response, request.url.path)
    if response.headers.get("content-type", "").startswith("application/json"):
        # API responses are per-user and live: never cache them, and key whatever cache exists on the headers
        # that decide between the SPA and the JSON for the same URL.
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = _VARY
    return response


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    """Serve the built SPA for any non-API path, falling back to index.html
    (client-side routing) for everything frontend/src/router doesn't know
    about either — same as any other single-page app host."""
    if full_path.startswith(_API_PREFIXES):
        raise HTTPException(status_code=404, detail="That API path doesn't exist. Check the URL against the API docs at /guide/api.")
    if _INDEX_HTML.is_file():
        return _spa_response()
    raise HTTPException(status_code=404, detail="frontend not built: run `npm run build` in frontend/")
