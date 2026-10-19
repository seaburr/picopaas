"""Browser navigations to SPA routes that share a path with the API get index.html, not JSON."""
import pytest

from app import main

NAV = {"sec-fetch-mode": "navigate", "accept": "text/html,application/xhtml+xml"}


@pytest.fixture
def built_index(tmp_path, monkeypatch):
    index = tmp_path / "index.html"
    index.write_text("<!doctype html><title>PicoPaaS</title>")
    monkeypatch.setattr(main, "_INDEX_HTML", index)
    return index


@pytest.mark.parametrize("path", ["/admin", "/admin/health", "/apps/new", "/apps/3/edit", "/keys", "/"])
def test_navigation_gets_spa(make_client, built_index, path):
    r = make_client().get(path, headers=NAV)
    assert r.status_code == 200 and "<title>PicoPaaS</title>" in r.text


def test_fetch_and_curl_still_get_api(make_client, built_index):
    client = make_client()
    r = client.get("/admin/health", headers={"sec-fetch-mode": "cors", "accept": "*/*"})
    assert r.headers["content-type"].startswith("application/json")
    r = client.get("/admin/health", headers={"accept": "*/*"})  # curl / CLI
    assert r.headers["content-type"].startswith("application/json")


def test_passthrough_paths_not_swapped(make_client, built_index):
    r = make_client().get("/healthz", headers=NAV)
    assert r.json() == {"status": "ok"}


def test_non_navigation_without_fetch_metadata_uses_accept(make_client, built_index):
    r = make_client().get("/admin/health", headers={"accept": "text/html"})
    assert "<title>PicoPaaS</title>" in r.text
