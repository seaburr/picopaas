"""Caching and compression for the SPA's static files (ASGI middleware; in app/main.py it sits outside spa_on_navigation/host_routing, inside metrics and robots_and_noindex).

- /static/assets/* is content-hashed by Vite: one year, immutable. Only on 200/304, so a 404 is never cached hard.
- Other /static/* files are not hashed: a short max-age, never immutable.
- gzip only for /static/* and browser navigations (the SPA index.html and the HTML pages). JSON API responses are
  never compressed (BREACH: they carry per-user secrets). Starlette's GZipMiddleware merges Vary: Accept-Encoding.
"""
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import config

ASSETS_PREFIX = "/static/assets/"
STATIC_PREFIX = "/static/"


def _header(scope: Scope, name: bytes) -> str:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return ""


def _wants_html(scope: Scope) -> bool:
    accept = _header(scope, b"accept")
    first = accept.split(",", 1)[0].split(";", 1)[0].strip().lower()
    if first == "application/json" or first.endswith("+json"):
        return False
    mode = _header(scope, b"sec-fetch-mode")
    return mode == "navigate" if mode else accept.startswith("text/html")


class StaticCacheMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=config.STATIC_GZIP_MIN_BYTES)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        is_static = path.startswith(STATIC_PREFIX)
        if is_static:
            if path.startswith(ASSETS_PREFIX):
                value = f"public, max-age={config.STATIC_ASSET_MAX_AGE_S}, immutable"
            else:
                value = f"public, max-age={config.STATIC_OTHER_MAX_AGE_S}"
            inner_send = send

            async def send(message: Message) -> None:  # noqa: F811
                if message["type"] == "http.response.start" and message["status"] in (200, 304):
                    headers = list(message.get("headers", []))
                    if not any(k.lower() == b"cache-control" for k, _ in headers):  # keep a route's own choice
                        headers.append((b"cache-control", value.encode()))
                        message = {**message, "headers": headers}
                await inner_send(message)

        if is_static or _wants_html(scope):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)
