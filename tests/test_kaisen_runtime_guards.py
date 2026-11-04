"""Sensitive-path guard in the preview runtime's static server (docs/design/kaisen.md 16.10)."""
import functools
import http.client
import http.server
import importlib.util
import threading
from pathlib import Path

import pytest

BOOT = Path(__file__).resolve().parent.parent / "images" / "kaisen-runtime" / "boot.py"

BLOCKED_EVERYWHERE = [
    ".env", ".env.local", ".env.production", "sub/.env", ".git/config", ".git/HEAD", ".svn/entries", ".hg/hgrc",
    ".DS_Store", ".htpasswd", ".aws/credentials", ".npmrc", ".kaisen-uploads.json", "server.pem", "tls.key", "a.p12",
    "a.pfx", "id_rsa", "id_rsa.pub", "id_ed25519", "data.sqlite", "data.sqlite3", "app.db", "dump.sql", "index.html.bak",
    "index.html.swp", "index.html~", "debug.log", "npm-debug.log", "wp-config.php", "wp-config.php.bak", "config.php",
    "phpinfo.php", "app.py", "run.sh", "secrets.json", "secrets.yml", "credentials", "credentials.json",
    "docker-compose.yml", "docker-compose.prod.yml", "kaisen.json", "Dockerfile", "terraform.tfstate", "prod.env",
    "node_modules/x/package.json", "ID_RSA", ".ENV", "%2eenv", "%2e%2e/.env", "a/../.env", ".well-known/.env",
]
BLOCKED_ROOT_ONLY = ["backup.zip", "site.tar.gz", "bundle.tgz", "config.json", "config.yml", "settings.json"]
SERVED = ["index.html", "app.js", "style.css", "logo.png", "data/items.json", "docs/readme.txt", ".well-known/security.txt"]


@pytest.fixture(scope="module")
def boot():
    spec = importlib.util.spec_from_file_location("kaisen_boot_guards", BOOT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fill(root: Path):
    for rel in SERVED + BLOCKED_ROOT_ONLY + [p for p in BLOCKED_EVERYWHERE if "%" not in p and ".." not in p]:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("payload")


def _serve(boot, directory, **kw):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(boot.StaticHandler, directory=str(directory), **kw))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _status(port, path, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, "/" + path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


@pytest.fixture(params=[False, True], ids=["repo-root", "public"])
def site(boot, tmp_path, request):
    _fill(tmp_path)
    srv = _serve(boot, tmp_path, public_mode=request.param)
    yield srv.server_address[1], request.param
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize("path", BLOCKED_EVERYWHERE)
def test_sensitive_paths_are_404(site, path):
    port, _ = site
    status, body = _status(port, path)
    assert status == 404 and b"payload" not in body
    assert _status(port, path, "HEAD")[0] == 404


@pytest.mark.parametrize("path", BLOCKED_ROOT_ONLY)
def test_archives_and_config_blocked_only_when_serving_repo_root(site, path):
    port, public = site
    status, _ = _status(port, path)
    assert status == (200 if public else 404)


@pytest.mark.parametrize("path", SERVED)
def test_normal_assets_still_served(site, path):
    port, _ = site
    status, body = _status(port, path)
    assert status == 200 and body == b"payload"


def test_directory_listing_never_shown(site):
    port, _ = site
    status, body = _status(port, "docs/")
    assert status == 404 and b"readme" not in body


def test_symlink_out_of_tree_is_404(boot, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("payload")
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "link.txt").symlink_to(outside)
    srv = _serve(boot, site_dir, public_mode=True)
    try:
        assert _status(srv.server_address[1], "link.txt")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_is_sensitive_unit(boot):
    assert boot.is_sensitive([".env"], True) and boot.is_sensitive(["a", ".git", "config"], True)
    assert not boot.is_sensitive([".well-known", "acme-challenge", "x"], True)
    assert boot.is_sensitive(["x", ".well-known"], True)
    assert not boot.is_sensitive(["."], False)
