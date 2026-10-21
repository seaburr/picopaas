"""Host routing edge cases (app/main.py): reserved-subdomain / suspended lookups, cache, header parsing,
domain-migration safety nets, and the opt-in scheduler lifespan."""
import pytest

from app import config, main, models

PLATFORM, APEX, DOMAIN = config.PLATFORM_HOST, config.APEX_HOST, config.APP_DOMAIN


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(config, "LEGACY_HOSTS", ())
    main._suspended_cache.clear()
    yield
    main._suspended_cache.clear()


@pytest.fixture()
def pages(tmp_path, monkeypatch):
    for name, title in (("landing.html", "LANDING"), ("privacy.html", "PRIVACY"), ("404.html", "NOPE"),
                        ("suspended.html", "PAUSED")):
        (tmp_path / name).write_text(f"<title>{title}</title>")
    monkeypatch.setattr(main, "_STATIC_DIR", tmp_path)
    return tmp_path


def get(make_client, host, path="/", method="get", **kw):
    return getattr(make_client(), method)(path, headers={"host": host}, follow_redirects=False, **kw)


def _add_app(db_session, **kw):
    owner = db_session.query(models.User).first()
    if owner is None:
        owner = models.User(email="o@example.com", password_hash="x")
        db_session.add(owner)
        db_session.commit()
    fields = dict(owner_id=owner.id, name="a", hostname="sleepy-abc1234", image="i", port=8080, status="suspended")
    fields.update(kw)
    row = models.App(**fields)
    db_session.add(row)
    db_session.commit()
    return row


# --- suspended / reserved subdomain lookups ----------------------------------------------------------------

def test_suspended_via_generated_host_and_reserved_host_both_503(make_client, db_session, pages):
    _add_app(db_session, platform_subdomain="blog")
    assert get(make_client, f"sleepy-abc1234.{DOMAIN}").status_code == 503
    assert get(make_client, f"blog.{DOMAIN}").status_code == 503


def test_deleted_suspended_app_is_404_for_both_hosts(make_client, db_session, pages):
    from datetime import datetime, timezone

    _add_app(db_session, platform_subdomain="blog", deleted_at=datetime.now(timezone.utc))
    assert get(make_client, f"sleepy-abc1234.{DOMAIN}").status_code == 404
    assert get(make_client, f"blog.{DOMAIN}").status_code == 404


def test_released_reserved_label_is_404_not_503(make_client, db_session, pages):
    row = _add_app(db_session, platform_subdomain="blog")
    assert get(make_client, f"blog.{DOMAIN}").status_code == 503
    row.platform_subdomain = None  # label released at teardown
    db_session.commit()
    main._suspended_cache.clear()
    assert get(make_client, f"blog.{DOMAIN}").status_code == 404


def test_nested_subdomain_of_reserved_host_is_404(make_client, db_session, pages):
    _add_app(db_session, platform_subdomain="blog")
    assert get(make_client, f"x.blog.{DOMAIN}").status_code == 404


def test_host_header_port_and_case_are_normalised(make_client, db_session, pages):
    _add_app(db_session, platform_subdomain="blog")
    assert get(make_client, f"BLOG.{DOMAIN.upper()}:8443").status_code == 503
    assert get(make_client, f"{APEX.upper()}:443", "/").status_code == 200  # apex landing, case-insensitive


def test_suspended_lookup_is_cached_then_expires(make_client, db_session, pages, monkeypatch):
    row = _add_app(db_session, platform_subdomain="blog")
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    host = f"blog.{DOMAIN}"
    assert get(make_client, host).status_code == 503
    row.status = "running"
    db_session.commit()
    now[0] += main._SUSPENDED_TTL - 1
    assert get(make_client, host).status_code == 503  # served from cache
    now[0] += 2
    assert get(make_client, host).status_code == 404  # TTL elapsed: re-read


def test_suspended_cache_is_bounded(make_client, db_session, pages, monkeypatch):
    for i in range(1001):
        main._suspended_cache[f"h{i}.{DOMAIN}"] = (1e12, False)
    get(make_client, f"fresh.{DOMAIN}")
    assert len(main._suspended_cache) <= 2  # cleared when over 1000, then only the new entry


def test_db_error_in_suspended_lookup_degrades_to_404(make_client, pages):
    class BrokenSession:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    def broken():
        yield BrokenSession()

    from app.db import get_db

    c = make_client()
    main.app.dependency_overrides[get_db] = broken
    try:
        assert c.get("/", headers={"host": f"anything.{DOMAIN}"}, follow_redirects=False).status_code == 404
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_suspended_page_inline_fallback_when_file_missing(make_client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_STATIC_DIR", tmp_path)  # empty dir: no suspended.html / 404.html
    _add_app(db_session, platform_subdomain="blog")
    r = get(make_client, f"blog.{DOMAIN}")
    assert r.status_code == 503 and "suspended" in r.text.lower()
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method", ["get", "head", "post", "put", "delete"])
def test_unknown_app_host_404_for_every_method(make_client, pages, method):
    assert get(make_client, f"ghost-abc1234.{DOMAIN}", "/x", method=method).status_code == 404


def test_unknown_host_never_reaches_the_api(make_client, pages):
    """An unknown app host must not expose the platform API (e.g. /auth/me, /admin/*) under that name."""
    for path in ("/auth/me", "/admin/users", "/apps", "/healthz"):
        assert get(make_client, f"ghost-abc1234.{DOMAIN}", path).status_code == 404


# --- domain-migration safety nets --------------------------------------------------------------------------

def test_empty_legacy_hosts_never_redirects_or_crashes(make_client, pages):
    for host in (f"picopaas.{DOMAIN}", f"old.{DOMAIN}", "raisehellpraisedale.xyz", "", "localhost", "testserver"):
        r = get(make_client, host, "/healthz")
        assert r.status_code in (200, 404), (host, r.status_code)
        assert "location" not in r.headers


def test_foreign_host_is_not_treated_as_unknown_app_host(make_client, pages):
    # A custom domain (not under APP_DOMAIN) that reaches the platform pod is routed normally, not 404'd.
    assert get(make_client, "blog.example.com", "/healthz").status_code == 200
    # ...and a lookalike suffix is not "under" our domain.
    assert get(make_client, f"evil{DOMAIN}", "/healthz").status_code == 200


def test_platform_host_and_apex_are_never_unknown_app_hosts():
    assert not main._is_unknown_app_host(PLATFORM) and not main._is_unknown_app_host(APEX)
    assert not main._is_unknown_app_host(DOMAIN)  # the bare domain has no label under it
    assert main._is_unknown_app_host(f"x.{DOMAIN}") and main._is_unknown_app_host(f"a.b.{DOMAIN}")


def test_legacy_host_beats_unknown_app_host(make_client, pages, monkeypatch):
    legacy = f"old.{DOMAIN}"
    monkeypatch.setattr(config, "LEGACY_HOSTS", (legacy,))
    r = get(make_client, legacy, "/x?y=1")
    assert r.status_code == 301 and r.headers["location"] == f"https://{PLATFORM}/x?y=1"


def test_apex_non_get_redirects_and_landing_missing_falls_through(make_client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_STATIC_DIR", tmp_path)  # no landing.html
    assert get(make_client, APEX, "/", method="post").status_code == 301
    assert get(make_client, APEX, "/").status_code < 500  # no landing page built: falls through, never a 500


def test_platform_privacy_post_is_not_served_as_page(make_client, pages):
    assert "PRIVACY" in get(make_client, PLATFORM, "/privacy").text
    assert "PRIVACY" not in get(make_client, PLATFORM, "/privacy", method="post").text


# --- lifespan (RECONCILER_ENABLED) -------------------------------------------------------------------------

def test_lifespan_is_inert_without_the_flag(monkeypatch):
    from fastapi.testclient import TestClient

    from app import scheduler

    monkeypatch.delenv("RECONCILER_ENABLED", raising=False)
    called = []
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: called.append("start"))
    with TestClient(main.app) as c:
        assert c.get("/healthz").status_code == 200
    assert called == []


def test_lifespan_starts_and_stops_scheduler_when_enabled(monkeypatch):
    from fastapi.testclient import TestClient

    from app import scheduler

    events = []

    class FakeSched:
        def shutdown(self, wait=True):
            events.append(("shutdown", wait))

    monkeypatch.setenv("RECONCILER_ENABLED", "1")
    monkeypatch.setattr(scheduler, "rotate_encrypted_data", lambda: events.append("rotate"))
    monkeypatch.setattr(scheduler, "bootstrap_db_server", lambda: events.append("bootstrap"))
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: (events.append("start"), FakeSched())[1])
    with TestClient(main.app) as c:
        assert c.get("/healthz").status_code == 200
        assert events == ["rotate", "bootstrap", "start"]  # data rotation + bootstrap happen before jobs start
    assert events[-1] == ("shutdown", False)
