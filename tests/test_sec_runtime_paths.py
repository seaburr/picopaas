"""P1b: path tricks must not serve an uploads/ file without the CSP; dir listing under uploads/ is refused."""
import functools
import http.client
import http.server
import importlib.util
import threading
from pathlib import Path

import pytest

BOOT = Path(__file__).resolve().parent.parent / "images" / "kaisen-runtime" / "boot.py"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"/>'


@pytest.fixture
def port(tmp_path):
    spec = importlib.util.spec_from_file_location("kaisen_boot_sec", BOOT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    (tmp_path / "uploads" / "sub").mkdir(parents=True)
    (tmp_path / "uploads" / "a.svg").write_bytes(SVG)
    (tmp_path / "uploads" / "sub" / "b.svg").write_bytes(SVG)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(m.StaticHandler, directory=str(tmp_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _req(port, path, method="GET", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}


@pytest.mark.parametrize("path", [
    "/uploads/a.svg", "//uploads/a.svg", "/uploads//a.svg", "/./uploads/a.svg", "/x/../uploads/a.svg",
    "/%75ploads/a.svg", "/uploads/%61.svg", "/uploads/a.svg?x=1", "/uploads/a.svg#f", "/uploads/sub/../a.svg",
    "/uploads/%2e%2e/uploads/a.svg", "/uploads/sub/b.svg", "/uploads/a.SVG",
])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_any_spelling_that_serves_an_upload_has_csp(port, path, method):
    status, h = _req(port, path, method)
    assert h.get("x-content-type-options") == "nosniff"
    if status == 200:
        assert "sandbox" in h.get("content-security-policy", "")


def test_conditional_get_304_keeps_headers(port):
    _, h = _req(port, "/uploads/a.svg")
    status, h2 = _req(port, "/uploads/a.svg", headers={"If-Modified-Since": "Fri, 01 Jan 2100 00:00:00 GMT"})
    assert status == 304 and "sandbox" in h2["content-security-policy"] and h2["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", ["/uploads/", "/uploads/sub/", "/uploads"])
def test_uploads_listing_refused(port, path):
    status, _ = _req(port, path)
    assert status in (301, 404)
