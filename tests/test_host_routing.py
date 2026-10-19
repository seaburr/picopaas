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
