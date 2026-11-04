"""Review of the self-harm guards (docs/PENTEST.md K87-K90): secret scan recall/precision, runtime path edge cases.
Credential-shaped strings are built at run time from pieces so no fixture looks like a secret."""
import functools
import http.client
import http.server
import importlib.util
import threading
from pathlib import Path

import pytest

from app.kaisen import secretscan

BOOT = Path(__file__).resolve().parent.parent / "images" / "kaisen-runtime" / "boot.py"
MIXED = "Ab12" * 5


@pytest.mark.parametrize("text", [
    "SECRET" + "_KEY = '" + MIXED + "'",
    "DB_" + "PASSWORD" + " = \"" + MIXED + "\"",
    "my" + "sql://app:" + "Zx9" + "kQ2mPw7" + "@db.internal/app",
    "token = '" + "dop" + "_v1_" + "a1" * 32 + "'",
])
def test_scan_catches(text):
    assert secretscan.find(text, code=True)


@pytest.mark.parametrize("text", [
    "color: #a1b2c3d4e5f6; --token-color: #ff00ff",
    "postgres://postgres:postgres@localhost:5432/app",
    "mysql://root:password@localhost/app",
    "mysql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}/app",
    "const token = getToken(req.headers.authorization)",
    "var a='" + "abcdef0123456789" * 20 + "';",
    "password = process.env.PASSWORD_VALUE_1234",
])
def test_scan_precision(text):
    assert secretscan.find(text, code=True) is None and secretscan.find(text) is None


def test_refusal_never_echoes_value():
    val = "Zx9" + "kQ2mPw7" + "Rt5yUi8o"
    hit = secretscan.find("api_key = '" + val + "'", code=True)
    assert hit and val not in secretscan.tool_refusal(*hit) and val not in secretscan.refusal_for_upload(*hit)


def _boot():
    spec = importlib.util.spec_from_file_location("boot_sec", BOOT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_runtime_nul_byte_is_404_and_root_config_js_served(tmp_path):
    boot = _boot()
    (tmp_path / ".env").write_text("x")
    (tmp_path / "config.js").write_text("x")
    (tmp_path / "config.json").write_text("x")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(boot.StaticHandler, directory=str(tmp_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(p):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
            c.request("GET", p)
            return c.getresponse().status
        assert get("/.env%00") == 404 and get("/.env%00.txt") == 404 and get("/%2eenv") == 404
        assert get("/config.js") == 200 and get("/config.json") == 404
    finally:
        srv.shutdown()
        srv.server_close()
