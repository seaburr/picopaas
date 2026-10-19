"""Live: deploy the webpage testapp and prove HTML, CSS and JS render. See testapps/README.md."""
import os

import pytest

from tests.e2e.harness import IMG, app_url, get_json, http_get, poll, rand_name, skip_unless_live

pytestmark = [pytest.mark.e2e, skip_unless_live]
APP_PORT = int(os.environ.get("E2E_APP_PORT", "8080"))


@pytest.fixture(scope="module")
def webpage(user, admin):
    r = user.req("POST", "/apps", json={
        "name": rand_name(), "image": IMG["webpage"], "port": APP_PORT, "env": [],
        "want_db": False, "want_storage": False,
        "health_check_enabled": True, "health_check_path": "/healthz",
    })
    assert r.status_code == 201, f"create: {r.status_code} {r.text[:300]}"
    app = r.json()
    user.created.append(app["id"])
    try:
        user.wait_status(app["id"])
        yield app
    finally:
        admin.delete_app(app["id"])


def _get(app, path):
    return poll(lambda: (r if (r := http_get(app_url(app, path))) is not None and r.status_code == 200 else None),
                120, desc=f"GET {path}")


def test_assets_and_api(webpage):
    r = _get(webpage, "/")
    assert r.headers["content-type"].startswith("text/html")
    assert "css-check" in r.text and "js-check" in r.text
    assert _get(webpage, "/style.css").headers["content-type"].startswith("text/css")
    assert "javascript" in _get(webpage, "/app.js").headers["content-type"]
    info = poll(lambda: get_json(app_url(webpage, "/api/info")), 60, desc="/api/info")
    assert info["hostname"] and "version" in info and "time" in info


def test_renders_in_browser(webpage):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("pip install playwright && python -m playwright install chromium")
    _get(webpage, "/")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception:
            pytest.skip("browsers missing: python -m playwright install chromium")
        try:
            page = browser.new_page()
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(app_url(webpage, "/"))
            page.wait_for_selector('body[data-ok="1"]', timeout=20000)
            assert page.inner_text("#js-check") == "JS OK"
            assert page.eval_on_selector("#css-check", "e => getComputedStyle(e).color") == "rgb(12, 34, 56)"
            hostname = poll(lambda: get_json(app_url(webpage, "/api/info")), 30, desc="/api/info")["hostname"]
            assert hostname in page.inner_text("#info")
            assert errors == []
        finally:
            browser.close()
