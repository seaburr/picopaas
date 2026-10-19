"""Host-based routing: legacy -> platform redirect, apex landing page, /privacy on the platform host."""
import pytest

from app import config, main
from app.naming import RESERVED_HOSTNAMES, generate_unique_hostname

PLATFORM, APEX, LEGACY = config.PLATFORM_HOST, config.APEX_HOST, config.LEGACY_HOSTS[0]


@pytest.fixture
def pages(tmp_path, monkeypatch):
    (tmp_path / "landing.html").write_text("<title>LANDING</title>")
    (tmp_path / "privacy.html").write_text("<title>PRIVACY</title>")
    monkeypatch.setattr(main, "_STATIC_DIR", tmp_path)
    return tmp_path


def get(make_client, host, path, **kw):
    return make_client().get(path, headers={"host": host}, follow_redirects=False, **kw)


def test_legacy_host_redirects_with_path_and_query(make_client):
    r = get(make_client, LEGACY, "/apps/3?x=1")
    assert r.status_code == 301 and r.headers["location"] == f"https://{PLATFORM}/apps/3?x=1"


def test_apex_serves_landing_and_privacy(make_client, pages):
    assert "LANDING" in get(make_client, APEX, "/").text
    assert "PRIVACY" in get(make_client, APEX, "/privacy").text


def test_apex_other_paths_redirect_but_healthz_passes(make_client, pages):
    r = get(make_client, APEX, "/login?next=/x")
    assert r.status_code == 301 and r.headers["location"] == f"https://{PLATFORM}/login?next=/x"
    assert get(make_client, APEX, "/healthz").json() == {"status": "ok"}


def test_platform_serves_privacy_and_healthz(make_client, pages):
    assert "PRIVACY" in get(make_client, PLATFORM, "/privacy").text
    assert get(make_client, PLATFORM, "/healthz").json() == {"status": "ok"}


def test_reserved_hostnames(db_session=None):
    assert {"platform", "picopaas", "lb", "www", "api"} <= RESERVED_HOSTNAMES


def test_generator_skips_reserved(monkeypatch):
    from app import naming

    seq = iter(["platform", "www", "ok-name-12345"])
    monkeypatch.setattr(naming, "_random_hostname", lambda: next(seq))

    class Q:
        def filter(self, *_): return self
        def first(self): return None

    class DB:
        def query(self, *_): return Q()

    assert generate_unique_hostname(DB()) == "ok-name-12345"


def test_hostname_label_length_and_wordlists():
    from app import naming

    longest = max(map(len, naming.ADJECTIVES)) + max(map(len, naming.ANIMALS)) + naming._SUFFIX_LEN + 2
    assert longest < 63
    assert len(naming.ADJECTIVES) >= 150 and len(naming.ANIMALS) >= 150
    assert all(w.isascii() and w.isalpha() and w.islower() for w in naming.ADJECTIVES + naming.ANIMALS)
    assert not (set(naming.ADJECTIVES + naming.ANIMALS) & naming.RESERVED_HOSTNAMES)


def test_unknown_app_subdomain_gets_custom_404(make_client, pages):
    (pages / "404.html").write_text("<title>NOPE</title>")
    r = get(make_client, f"ghost-abc1234.{config.APP_DOMAIN}", "/anything")
    assert r.status_code == 404 and "NOPE" in r.text
    assert r.headers["cache-control"] == "no-store" and "noindex" in r.headers["x-robots-tag"]


def test_unknown_app_subdomain_inline_fallback(make_client, pages):
    r = get(make_client, f"ghost.{config.APP_DOMAIN}", "/")
    assert r.status_code == 404 and "404" in r.text


def test_known_hosts_not_404(make_client, pages):
    assert get(make_client, PLATFORM, "/healthz").status_code == 200
    assert get(make_client, APEX, "/healthz").status_code == 200
    assert get(make_client, LEGACY, "/").status_code == 301


def test_robots_txt_on_every_host(make_client, pages):
    for host in (PLATFORM, APEX, LEGACY, f"ghost.{config.APP_DOMAIN}"):
        r = get(make_client, host, "/robots.txt")
        assert r.status_code == 200 and r.text == "User-agent: *\nDisallow: /\n"
        assert r.headers["content-type"].startswith("text/plain")
        assert r.headers["x-robots-tag"] == "noindex, nofollow"


def test_noindex_header_on_platform_responses(make_client, pages):
    assert get(make_client, PLATFORM, "/healthz").headers["x-robots-tag"] == "noindex, nofollow"
    assert get(make_client, APEX, "/").headers["x-robots-tag"] == "noindex, nofollow"


def test_suspended_host_gets_503_and_unknown_gets_404(make_client, db_session, pages):
    from app import models
    main._suspended_cache.clear()
    (pages / "suspended.html").write_text("<title>PAUSED</title>")
    owner = models.User(email="o@example.com", password_hash="x")
    db_session.add(owner)
    db_session.commit()
    db_session.add_all([
        models.App(owner_id=owner.id, name="a", hostname="sleepy-abc1234", image="i", port=80, status="suspended"),
        models.App(owner_id=owner.id, name="b", hostname="live-abc1234", image="i", port=80, status="running"),
    ])
    db_session.commit()
    r = get(make_client, f"sleepy-abc1234.{config.APP_DOMAIN}", "/")
    assert r.status_code == 503 and "PAUSED" in r.text and "retry-after" not in r.headers
    assert r.headers["cache-control"] == "no-store" and "noindex" in r.headers["x-robots-tag"]
    assert get(make_client, f"nobody-abc1234.{config.APP_DOMAIN}", "/").status_code == 404
    assert get(make_client, f"live-abc1234.{config.APP_DOMAIN}", "/").status_code == 404
    main._suspended_cache.clear()
