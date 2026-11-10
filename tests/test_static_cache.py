"""Static caching + gzip (app/static_cache.py). Uses a temporary static dir so no frontend build is needed."""
import pytest

from app import config, main

BIG = "console.log('x');\n" * 200


@pytest.fixture()
def static_client(client, tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-ABC123.js").write_text(BIG)
    (tmp_path / "theme-init.js").write_text(BIG)
    (tmp_path / "index.html").write_text("<html>" + "<p>hi</p>" * 300 + "</html>")
    monkeypatch.setattr(main, "_INDEX_HTML", tmp_path / "index.html")
    monkeypatch.setattr(main, "_STATIC_DIR", tmp_path)
    from starlette.staticfiles import StaticFiles
    from starlette.routing import Mount
    monkeypatch.setattr(main.app.router, "routes", [
        Mount("/static", StaticFiles(directory=tmp_path), name="static"), *main.app.router.routes])
    return client


GZ = {"Accept-Encoding": "gzip"}


def test_hashed_asset_immutable_and_gzip(static_client):
    r = static_client.get("/static/assets/index-ABC123.js", headers=GZ)
    assert r.status_code == 200
    assert r.headers["cache-control"] == f"public, max-age={config.STATIC_ASSET_MAX_AGE_S}, immutable"
    assert r.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in r.headers["vary"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in r.headers
    assert r.text == BIG


def test_asset_not_compressed_without_accept_encoding(static_client):
    r = static_client.get("/static/assets/index-ABC123.js", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers


def test_unhashed_static_short_cache_not_immutable(static_client):
    r = static_client.get("/static/theme-init.js", headers=GZ)
    assert r.headers["cache-control"] == f"public, max-age={config.STATIC_OTHER_MAX_AGE_S}"
    assert "immutable" not in r.headers["cache-control"]


def test_missing_asset_404_not_immutable(static_client):
    r = static_client.get("/static/assets/nope-XYZ.js", headers=GZ)
    assert r.status_code == 404
    assert "immutable" not in r.headers.get("cache-control", "")


def test_conditional_request_gets_304_with_cache_header(static_client):
    first = static_client.get("/static/assets/index-ABC123.js")
    r = static_client.get("/static/assets/index-ABC123.js", headers={"If-None-Match": first.headers["etag"]})
    assert r.status_code == 304
    assert "immutable" in r.headers["cache-control"]


def test_json_api_not_compressed_and_no_store(static_client):
    r = static_client.get("/healthz", headers={**GZ, "Accept": "application/json"})
    assert "content-encoding" not in r.headers
    r = static_client.get("/api/banner", headers={**GZ, "Accept": "application/json"})
    assert r.headers["content-type"].startswith("application/json")
    assert "content-encoding" not in r.headers
    assert r.headers["cache-control"] == "no-store"


def test_index_html_no_cache_vary_merged_and_gzip(static_client):
    r = static_client.get("/some/spa/route", headers={**GZ, "Accept": "text/html", "Sec-Fetch-Mode": "navigate"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["content-encoding"] == "gzip"
    vary = [v.strip() for v in r.headers["vary"].split(",")]
    assert {"Sec-Fetch-Mode", "Accept", "Accept-Encoding"} <= set(vary)
    assert "content-security-policy" in r.headers
