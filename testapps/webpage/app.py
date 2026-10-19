"""Static page + tiny JSON API, stdlib only. Listens on $PORT (default 8080)."""
import json
import os
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
FILES = {
    "/": ("index.html", "text/html; charset=utf-8", "no-cache"),
    "/style.css": ("style.css", "text/css; charset=utf-8", "public, max-age=300"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8", "public, max-age=300"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml", "public, max-age=86400"),
}
CSP = "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self._send(200, b"ok", "text/plain; charset=utf-8")
        if path == "/api/info":
            info = {"hostname": socket.gethostname(), "version": os.environ.get("APP_VERSION", "dev"),
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            return self._send(200, json.dumps(info).encode(), "application/json")
        if path in FILES:
            name, ctype, cache = FILES[path]
            with open(os.path.join(STATIC, name), "rb") as f:
                return self._send(200, f.read(), ctype, cache)
        self._send(404, b"not found", "text/plain; charset=utf-8")

    do_GET = do_HEAD = _route

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
