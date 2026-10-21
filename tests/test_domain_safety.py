"""Domain-migration safety nets: our own domain can't be claimed as a custom domain; host routing with no legacy hosts."""
import pytest

from app import config, custom_domains as cd, main

PLATFORM, APEX = config.PLATFORM_HOST, config.APEX_HOST


@pytest.mark.parametrize("bad", [
    config.APP_DOMAIN, config.APP_DOMAIN.upper(), f"*.{config.APP_DOMAIN}", f"a.b.{config.APP_DOMAIN}",
    f"https://x.{config.APP_DOMAIN}/path", f"x.{config.APP_DOMAIN}.", config.CUSTOM_DOMAIN_CNAME_TARGET,
    PLATFORM, APEX,
])
def test_custom_domain_cannot_be_our_domain(bad):
    with pytest.raises(cd.CustomDomainError):
        cd.normalize_host(bad)


def test_lookalike_suffix_is_allowed():
    # "evilpicopaas.xyz" is not under our domain: only a dot-boundary suffix is ours.
    assert cd.normalize_host("not" + config.APP_DOMAIN) == "not" + config.APP_DOMAIN


def _get(make_client, host, path="/"):
    return make_client().get(path, headers={"host": host}, follow_redirects=False)


@pytest.fixture()
def no_legacy(monkeypatch):
    monkeypatch.setattr(config, "LEGACY_HOSTS", ())


def test_no_legacy_hosts_platform_and_apex_unaffected(make_client, no_legacy):
    assert _get(make_client, PLATFORM, "/healthz").status_code == 200
    assert _get(make_client, APEX, "/healthz").status_code == 200
    r = _get(make_client, APEX, "/login")
    assert r.status_code == 301 and r.headers["location"] == f"https://{PLATFORM}/login"


def test_no_legacy_hosts_old_name_is_plain_unknown_host(make_client, no_legacy):
    # With nothing in LEGACY_HOSTS, a former platform name under the app domain is just an unknown app host: 404,
    # never a redirect to the platform.
    r = _get(make_client, f"picopaas.{config.APP_DOMAIN}", "/anything")
    assert r.status_code == 404 and "location" not in r.headers


def test_legacy_host_redirects_only_when_configured(make_client, monkeypatch):
    legacy = f"old.{config.APP_DOMAIN}"
    monkeypatch.setattr(config, "LEGACY_HOSTS", (legacy,))
    assert _get(make_client, legacy, "/x").status_code == 301
    monkeypatch.setattr(config, "LEGACY_HOSTS", ())
    assert _get(make_client, legacy, "/x").status_code == 404


def test_host_routing_reads_config_at_request_time():
    # The autouse fixture in test_host_routing relies on main consulting config.LEGACY_HOSTS per request.
    import inspect

    src = inspect.getsource(main.host_routing)
    assert "config.LEGACY_HOSTS" in src
