"""Login/register throttling (DB-backed, fail-open), security headers, and APP_DOMAIN as the one domain source."""
import pytest

from app import config, main, models, ratelimit
from tests.conftest import DEFAULT_PASSWORD, register_and_login

BAD = {"password": "not-the-password"}


@pytest.fixture
def limiter_on(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "LOGIN_EMAIL_MAX_FAILS", 3)
    monkeypatch.setattr(config, "LOGIN_IP_MAX_FAILS", 100)
    monkeypatch.setattr(config, "REGISTER_IP_MAX", 2)


def _login(c, email, password="not-the-password", **kw):
    return c.post("/auth/login", json={"email": email, "password": password}, **kw)


def test_login_lockout_429_with_retry_after(make_client, limiter_on):
    c = make_client()
    register_and_login(c, "owner@example.com")
    for _ in range(3):
        assert _login(c, "owner@example.com").status_code == 401
    r = _login(c, "owner@example.com")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert "Too many attempts" in r.json()["detail"]
    # even the right password is refused while locked
    assert _login(c, "owner@example.com", DEFAULT_PASSWORD).status_code == 429


def test_lockout_does_not_reveal_account_existence(make_client, limiter_on):
    c = make_client()
    register_and_login(c, "owner@example.com")
    seen = []
    for email in ("owner@example.com", "ghost@example.com"):
        codes = [_login(c, email).status_code for _ in range(5)]
        seen.append(codes)
    assert seen[0] == seen[1] == [401, 401, 401, 429, 429]


def test_success_resets_email_counter(make_client, limiter_on):
    c = make_client()
    register_and_login(c, "owner@example.com")
    for _ in range(2):
        _login(c, "owner@example.com")
    assert _login(c, "owner@example.com", DEFAULT_PASSWORD).status_code == 200
    for _ in range(3):
        assert _login(c, "owner@example.com").status_code == 401  # counter restarted from zero
    assert _login(c, "owner@example.com").status_code == 429


def test_register_ip_cap(make_client, limiter_on):
    c = make_client()
    assert c.post("/auth/register", json={"email": "a@example.com", "password": DEFAULT_PASSWORD, "accept_terms": True}).status_code == 201
    assert c.post("/auth/register", json={"email": "b@example.com", "password": DEFAULT_PASSWORD, "accept_terms": True}).status_code == 201
    r = c.post("/auth/register", json={"email": "c@example.com", "password": DEFAULT_PASSWORD, "accept_terms": True})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    # a different client IP (as set by our proxy) is unaffected
    r = c.post("/auth/register", json={"email": "c@example.com", "password": DEFAULT_PASSWORD, "accept_terms": True},
               headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 201


def test_client_supplied_xff_cannot_dodge_ip_limit(make_client, limiter_on):
    """Our proxy appends the real peer last; a spoofed leading entry is ignored."""
    c = make_client()
    for i in range(2):
        r = c.post("/auth/register", json={"email": f"u{i}@example.com", "password": DEFAULT_PASSWORD, "accept_terms": True},
                   headers={"X-Forwarded-For": f"10.9.9.{i}, 198.51.100.7"})
        assert r.status_code == 201
    r = c.post("/auth/register", json={"email": "u9@example.com", "password": DEFAULT_PASSWORD, "accept_terms": True},
               headers={"X-Forwarded-For": "10.1.1.1, 198.51.100.7"})
    assert r.status_code == 429


def test_limiter_fails_open_when_store_errors(make_client, limiter_on, monkeypatch, db_session):
    c = make_client()
    register_and_login(c, "owner@example.com")

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ratelimit, "_remaining", boom)
    monkeypatch.setattr(ratelimit, "_bump", boom)
    for _ in range(6):
        assert _login(c, "owner@example.com").status_code == 401  # never 429
    assert _login(c, "owner@example.com", DEFAULT_PASSWORD).status_code == 200
    assert c.post("/auth/register", json={"email": "z@example.com", "password": DEFAULT_PASSWORD, "accept_terms": True}).status_code == 201


def test_window_expiry_unlocks(make_client, limiter_on, db_session):
    c = make_client()
    register_and_login(c, "owner@example.com")
    for _ in range(3):
        _login(c, "owner@example.com")
    assert _login(c, "owner@example.com").status_code == 429
    from datetime import timedelta
    for row in db_session.query(models.AuthThrottle).all():
        row.window_start = row.window_start - timedelta(hours=2)
    db_session.commit()
    assert _login(c, "owner@example.com", DEFAULT_PASSWORD).status_code == 200


def _assert_sec_headers(r):
    assert r.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp.split("style-src")[0]  # never for scripts


def test_headers_on_api_response(make_client):
    _assert_sec_headers(make_client().get("/healthz"))
    _assert_sec_headers(make_client().get("/auth/me"))  # a 401 too


def test_headers_on_spa_index(make_client, tmp_path, monkeypatch):
    index = tmp_path / "index.html"
    index.write_text("<!doctype html><title>PicoPaaS</title>")
    monkeypatch.setattr(main, "_INDEX_HTML", index)
    r = make_client().get("/apps/new", headers={"sec-fetch-mode": "navigate", "accept": "text/html"})
    assert "<title>PicoPaaS</title>" in r.text
    _assert_sec_headers(r)


def test_headers_on_static_pages(make_client):
    r = make_client().get("/", headers={"host": f"nosuchapp.{config.APP_DOMAIN}"})
    assert r.status_code == 404
    _assert_sec_headers(r)


def test_domain_suffix_comes_from_config(make_client, monkeypatch):
    from app.routers import admin, apps
    assert not hasattr(apps, "DOMAIN_SUFFIX") and not hasattr(admin, "DOMAIN_SUFFIX")
    assert apps.APP_DOMAIN == config.APP_DOMAIN == admin.APP_DOMAIN
