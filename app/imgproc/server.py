"""Minimal single-threaded HTTP server for the image pod. One request at a time (the API also limits its queue).

POST /process?hint=raster|svg   body = the raw file (Content-Length required, capped; chunked bodies are refused)
  200 -> body = processed bytes; headers X-Mime, X-Width, X-Height, X-Notes (JSON list)
  4xx/5xx -> JSON {"code", "message"}
GET /healthz -> 200 "ok"
No authentication: the pod's NetworkPolicy admits only the API pods. Nothing here logs request content."""
import json
import logging
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from app import config
from app.providers.base import ImageProcessError

from . import core

log = logging.getLogger("app.imgproc")
_STATUS = {"unsupported": 415, "corrupt": 422, "too_big": 422, "animated": 422, "timeout": 504, "busy": 503}


class _Timeout(Exception):
    pass


_alarm_ready = False  # set by main() once the SIGALRM handler is installed (never in tests or other threads)


def _on_alarm(signum, frame):
    raise _Timeout()


class Handler(BaseHTTPRequestHandler):
    server_version = "kaisen-imgproc"
    timeout = 10  # socket timeout: a stalled sender cannot hold the single worker

    def log_message(self, *args):  # no per-request logging
        pass

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: str, message: str):
        self._send(_STATUS.get(code, 422), json.dumps({"code": code, "message": message}).encode(), "application/json")

    def do_GET(self):
        if urlsplit(self.path).path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"", "text/plain")

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path != "/process":
            return self._send(404, b"", "text/plain")
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return self._error("too_big", "Chunked bodies are not accepted.")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._error("too_big", "Content-Length is required.")
        if length < 0 or length > max(config.KAISEN_UPLOAD_MAX_BYTES_IN, config.KAISEN_UPLOAD_SVG_BYTES):
            return self._error("too_big", "That file is too large.")
        data = self.rfile.read(length)
        if len(data) != length:
            return self._error("corrupt", "The upload was cut short.")
        hint = parse_qs(url.query).get("hint", [""])[0]
        if _alarm_ready:
            signal.alarm(config.KAISEN_UPLOAD_IMG_TIMEOUT)
        try:
            res = core.process(data, hint)
        except ImageProcessError as exc:
            return self._error(exc.code, exc.message)
        except (_Timeout, MemoryError) as exc:
            return self._error("timeout" if isinstance(exc, _Timeout) else "too_big", "That file took too long to process.")
        except Exception:  # noqa: BLE001 - never leak parser internals; the class name is enough for the pod log
            log.exception("image processing failed")
            return self._error("corrupt", "That file couldn't be processed.")
        finally:
            if _alarm_ready:
                signal.alarm(0)
        self._send(200, res.data, res.mime, {
            "X-Mime": res.mime, "X-Width": str(res.width or ""), "X-Height": str(res.height or ""),
            "X-Notes": json.dumps(res.notes)})


def make_server(host: str = "0.0.0.0", port: int | None = None) -> HTTPServer:
    srv = HTTPServer((host, config.KAISEN_IMGPROC_PORT if port is None else port), Handler)
    srv.request_queue_size = 8
    return srv


def main() -> None:  # pragma: no cover - process entry point
    import resource
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    limit = config.KAISEN_UPLOAD_IMG_MEM_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))  # defense in depth; the container memory limit is the real fence
    global _alarm_ready
    signal.signal(signal.SIGALRM, _on_alarm)
    _alarm_ready = True
    srv = make_server()
    log.info("kaisen-imgproc listening on %s", srv.server_address[1])
    srv.serve_forever()
