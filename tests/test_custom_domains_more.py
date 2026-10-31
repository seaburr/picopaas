"""Custom domains: DnsLookup (fake resolver, no network), host normalisation edges, verify() branches,
and the owner endpoints' authz / state-transition / release-on-teardown behaviour."""
import types

import pytest

from app import config, models
from app import custom_domains as cd
from app import k8s_manifests as k8s
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import reconcile_all
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
HOST = "blog.example.com"
TARGET = config.CUSTOM_DOMAIN_CNAME_TARGET


# --- DnsLookup with a fake dnspython resolver -------------------------------------------------------------

class _TxtRdata:
    def __init__(self, *chunks):
        self.strings = list(chunks)


class _ARdata:
    def __init__(self, address):
        self.address = address


@pytest.fixture()
def fake_dns(monkeypatch):
    """Replace dns.resolver.Resolver with a table-driven fake. Entries: (name, rdtype) -> list | Exception."""
    import dns.resolver

    table = {}
    seen = []

    class FakeResolver:
        timeout = lifetime = None

        def resolve(self, name, rdtype):
            seen.append((name, rdtype, self.timeout, self.lifetime))
            result = table.get((name, rdtype), dns.resolver.NXDOMAIN())
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(dns.resolver, "Resolver", FakeResolver)
    return types.SimpleNamespace(table=table, seen=seen)


def test_dns_txt_joins_chunks_and_returns_all_values(fake_dns):
    fake_dns.table[("t.example.com", "TXT")] = [_TxtRdata(b"abc", b"def"), _TxtRdata(b"other")]
    assert cd.DnsLookup(timeout=2.5).txt("t.example.com") == ["abcdef", "other"]
    # The configured timeout is applied to the resolver (a hung resolver must not hang the request).
    assert fake_dns.seen[0][2:] == (2.5, 2.5)


def test_dns_txt_tolerates_invalid_utf8(fake_dns):
    fake_dns.table[("t.example.com", "TXT")] = [_TxtRdata(b"ok\xff")]
    assert cd.DnsLookup().txt("t.example.com") == ["ok�"]


def test_dns_txt_nxdomain_and_noanswer_are_empty(fake_dns):
    import dns.resolver

    assert cd.DnsLookup().txt("missing.example.com") == []  # NXDOMAIN (fake default)
    fake_dns.table[("na.example.com", "TXT")] = dns.resolver.NoAnswer()
    assert cd.DnsLookup().txt("na.example.com") == []


def test_dns_txt_timeout_is_a_user_facing_error(fake_dns):
    import dns.exception

    fake_dns.table[("slow.example.com", "TXT")] = dns.exception.Timeout()
    with pytest.raises(cd.CustomDomainError, match="Couldn't read DNS for slow.example.com"):
        cd.DnsLookup().txt("slow.example.com")


def test_dns_a_unions_a_and_aaaa(fake_dns):
    fake_dns.table[("h.example.com", "A")] = [_ARdata("203.0.113.1"), _ARdata("203.0.113.2")]
    fake_dns.table[("h.example.com", "AAAA")] = [_ARdata("2001:db8::1")]
    assert cd.DnsLookup().a("h.example.com") == {"203.0.113.1", "203.0.113.2", "2001:db8::1"}


def test_dns_a_missing_family_is_skipped_not_fatal(fake_dns):
    import dns.resolver

    fake_dns.table[("h.example.com", "A")] = [_ARdata("203.0.113.1")]
    fake_dns.table[("h.example.com", "AAAA")] = dns.resolver.NoAnswer()
    assert cd.DnsLookup().a("h.example.com") == {"203.0.113.1"}
    assert cd.DnsLookup().a("nothing.example.com") == set()


def test_dns_a_servfail_on_either_family_raises(fake_dns):
    import dns.resolver

    fake_dns.table[("h.example.com", "A")] = [_ARdata("203.0.113.1")]
    fake_dns.table[("h.example.com", "AAAA")] = dns.resolver.NoNameservers()
    with pytest.raises(cd.CustomDomainError, match="try again"):
        cd.DnsLookup().a("h.example.com")


def test_verify_uses_real_lookup_when_none_injected(fake_dns):
    """verify() with no injected lookup goes through DnsLookup (here the fake resolver): full happy path."""
    fake_dns.table[(cd.challenge_record(HOST), "TXT")] = [_TxtRdata(b"tok")]
    fake_dns.table[(HOST, "A")] = [_ARdata("203.0.113.9")]
    fake_dns.table[(TARGET, "A")] = [_ARdata("203.0.113.9")]
    cd.verify(HOST, "tok")


# --- normalize_host edge cases -----------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("HTTP://Blog.Example.com", "blog.example.com"),                 # scheme is case-insensitive (lowercased first)
    ("  blog.example.com  ", "blog.example.com"),
    ("https://blog.example.com/a/b?x=1", "blog.example.com"),        # path stripped
    ("blog.example.com...", "blog.example.com"),                     # all trailing dots
    ("a-b.c-d.example.co.uk", "a-b.c-d.example.co.uk"),
    ("x" * 63 + ".example.com", "x" * 63 + ".example.com"),
    ("xn--bcher-kva.example.com", "xn--bcher-kva.example.com"),      # A-label (punycode) is fine for a user's own domain
])
def test_normalize_host_accepts(raw, expected):
    assert cd.normalize_host(raw) == expected


@pytest.mark.parametrize("bad", [
    None, "", "   ", "/", "https://", "localhost", "1.2.3.4", "[::1]", "example.com:8080", "user@example.com",
    "a_b.example.com", "bad host.example.com", "x" * 64 + ".example.com", "*.example.com",
    "exa mple.com", "example.c", "example.123", "blog.example.com:443/x", "http://", "-a.example.com",
    "a-.example.com", ("a." * 130) + "com",
    "ünï.example.com",
])
def test_normalize_host_rejects(bad):
    with pytest.raises(cd.CustomDomainError):
        cd.normalize_host(bad)


def test_normalize_host_error_messages_are_user_facing():
    with pytest.raises(cd.CustomDomainError, match="valid domain"):
        cd.normalize_host("nope")
    with pytest.raises(cd.CustomDomainError, match="must be your own domain"):
        cd.normalize_host(f"x.{config.APP_DOMAIN}")
    with pytest.raises(cd.CustomDomainError, match="platform's own hostname|must be your own domain"):
        cd.normalize_host(TARGET)


def test_normalize_host_rejects_reserved_platform_subdomain_hosts():
    """A reserved platform subdomain is under APP_DOMAIN, so it can never also be claimed as a custom domain."""
    for label in ("blog", "anything"):
        with pytest.raises(cd.CustomDomainError):
            cd.normalize_host(f"{label}.{config.APP_DOMAIN}")


def test_challenge_record_and_token():
    assert cd.challenge_record("a.example.com") == f"{config.CUSTOM_DOMAIN_CHALLENGE_LABEL}.a.example.com"
    t1, t2 = cd.generate_token(), cd.generate_token()
    assert t1 != t2 and len(t1) == 32 and int(t1, 16) >= 0


def test_intersects():
    assert cd.intersects(["a", "b"], {"b"}) and not cd.intersects([], ["a"]) and not cd.intersects(["a"], ["b"])


# --- verify() branches -------------------------------------------------------------------------------------

class FakeLookup:
    def __init__(self, txt=None, a=None):
        self._txt, self._a = txt or {}, a or {}

    def txt(self, name):
        return list(self._txt.get(name, []))

    def a(self, name):
        return set(self._a.get(name, set()))


def test_verify_token_among_several_txt_values():
    lk = FakeLookup(txt={cd.challenge_record(HOST): ["v=spf1 -all", "tok", "other"]},
                    a={HOST: {"203.0.113.1"}, TARGET: {"203.0.113.1"}})
    cd.verify(HOST, "tok", lookup=lk)


def test_verify_token_is_exact_match_not_substring():
    lk = FakeLookup(txt={cd.challenge_record(HOST): ["tok-extra"]}, a={HOST: {"1.1.1.1"}, TARGET: {"1.1.1.1"}})
    with pytest.raises(cd.CustomDomainError, match="doesn't match"):
        cd.verify(HOST, "tok", lookup=lk)


def test_verify_txt_is_checked_before_pointing():
    lk = FakeLookup()  # nothing resolves at all: the ownership message comes first
    with pytest.raises(cd.CustomDomainError, match="ownership record"):
        cd.verify(HOST, "tok", lookup=lk)


def test_verify_overlapping_address_set_passes():
    lk = FakeLookup(txt={cd.challenge_record(HOST): ["tok"]},
                    a={HOST: {"198.51.100.1", "203.0.113.1"}, TARGET: {"203.0.113.1", "203.0.113.2"}})
    cd.verify(HOST, "tok", lookup=lk)


def test_verify_messages_name_the_record_and_target():
    lk = FakeLookup()
    with pytest.raises(cd.CustomDomainError) as ei:
        cd.verify(HOST, "tok", lookup=lk)
    assert cd.challenge_record(HOST) in str(ei.value)
    lk = FakeLookup(txt={cd.challenge_record(HOST): ["tok"]}, a={TARGET: {"203.0.113.1"}})
    with pytest.raises(cd.CustomDomainError) as ei:
        cd.verify(HOST, "tok", lookup=lk)
    assert HOST in str(ei.value) and TARGET in str(ei.value)


# --- endpoints: authz, feature flag, state transitions ------------------------------------------------------

@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def owner(make_client, admin):
    c = make_client()
    register_and_login(c, "owner@example.com")
    return c


def _mk(c):
    return c.post("/apps", json=APP).json()["id"]


def _row(db_session, aid):
    db_session.expire_all()
    return db_session.get(models.App, aid)


def _activate(db_session, aid, host=HOST):
    row = _row(db_session, aid)
    row.custom_domain, row.custom_domain_status, row.custom_domain_token = host, "active", "tok"
    row.status = "running"
    db_session.commit()


def test_endpoints_require_login(owner, make_client):
    aid = _mk(owner)
    anon = make_client()
    assert anon.put(f"/apps/{aid}/domain", json={"host": HOST}).status_code == 401
    assert anon.post(f"/apps/{aid}/domain/verify").status_code == 401
    assert anon.delete(f"/apps/{aid}/domain").status_code == 401


def test_cross_user_cannot_verify_or_clear(owner, make_client, db_session):
    aid = _mk(owner)
    owner.put(f"/apps/{aid}/domain", json={"host": HOST})
    intruder = make_client()
    register_and_login(intruder, "intruder@example.com")
    assert intruder.post(f"/apps/{aid}/domain/verify").status_code == 404
    assert intruder.delete(f"/apps/{aid}/domain").status_code == 404
    assert _row(db_session, aid).custom_domain == HOST  # untouched


def test_pending_user_cannot_use_domain_endpoints(make_client, admin):
    pending = make_client()
    register_and_login(pending, "pending@example.com", approve=False)
    assert pending.put("/apps/1/domain", json={"host": HOST}).status_code == 403


def test_read_scope_api_key_cannot_mutate_domain_but_full_can(owner, make_client):
    aid = _mk(owner)
    read_key = owner.post("/api/keys", json={"scope": "read"}).json()["key"]
    full_key = owner.post("/api/keys", json={"scope": "full"}).json()["key"]
    api = make_client()
    ro, rw = {"Authorization": f"Bearer {read_key}"}, {"Authorization": f"Bearer {full_key}"}
    assert api.put(f"/apps/{aid}/domain", json={"host": HOST}, headers=ro).status_code == 403
    assert api.delete(f"/apps/{aid}/domain", headers=ro).status_code == 403
    assert api.post(f"/apps/{aid}/domain/verify", headers=ro).status_code == 403
    assert api.put(f"/apps/{aid}/domain", json={"host": HOST}, headers=rw).status_code == 200
    assert api.get(f"/apps/{aid}", headers=ro).json()["custom_domain"]["host"] == HOST


def test_feature_flag_off_hides_all_three_endpoints(owner, monkeypatch):
    aid = _mk(owner)
    monkeypatch.setattr(config, "CUSTOM_DOMAINS_ENABLED", False)
    assert owner.put(f"/apps/{aid}/domain", json={"host": HOST}).status_code == 404
    assert owner.post(f"/apps/{aid}/domain/verify").status_code == 404
    assert owner.delete(f"/apps/{aid}/domain").status_code == 404


def test_unknown_app_404(owner):
    assert owner.put("/apps/99999/domain", json={"host": HOST}).status_code == 404
    assert owner.post("/apps/99999/domain/verify").status_code == 404
    assert owner.delete("/apps/99999/domain").status_code == 404


def test_verify_without_domain_is_400(owner):
    aid = _mk(owner)
    r = owner.post(f"/apps/{aid}/domain/verify")
    assert r.status_code == 400 and "no custom domain" in r.json()["detail"]


def test_set_domain_on_suspended_app_is_forbidden(owner, admin, db_session):
    aid = _mk(owner)
    admin.post(f"/admin/apps/{aid}/suspend")
    assert owner.put(f"/apps/{aid}/domain", json={"host": HOST}).status_code == 403
    assert _row(db_session, aid).custom_domain is None


def test_set_domain_rejects_our_own_domain_and_empty(owner):
    aid = _mk(owner)
    for bad in (f"x.{config.APP_DOMAIN}", config.APP_DOMAIN, TARGET, "", "   "):
        assert owner.put(f"/apps/{aid}/domain", json={"host": bad}).status_code == 422, bad
    assert owner.put(f"/apps/{aid}/domain", json={}).status_code == 422


def test_uniqueness_is_case_and_scheme_insensitive(owner, admin):
    a, b = _mk(owner), admin.post("/apps", json=APP).json()["id"]
    assert owner.put(f"/apps/{a}/domain", json={"host": "Blog.Example.com"}).status_code == 200
    assert admin.put(f"/apps/{b}/domain", json={"host": "https://BLOG.example.com/"}).status_code == 409


def test_reput_same_domain_on_same_app_is_ok_and_rotates_token(owner):
    aid = _mk(owner)
    t1 = owner.put(f"/apps/{aid}/domain", json={"host": HOST}).json()["custom_domain"]["challenge_token"]
    r = owner.put(f"/apps/{aid}/domain", json={"host": HOST})
    assert r.status_code == 200 and r.json()["custom_domain"]["challenge_token"] != t1


def test_replacing_active_domain_resets_to_pending_and_requeues(owner, db_session):
    aid = _mk(owner)
    _activate(db_session, aid)
    r = owner.put(f"/apps/{aid}/domain", json={"host": "new.example.com"})
    assert r.status_code == 200
    row = _row(db_session, aid)
    assert row.custom_domain == "new.example.com" and row.custom_domain_status == "pending"
    assert row.custom_domain_verified_at is None and row.status == "pending"


def test_replacing_pending_domain_does_not_requeue(owner, db_session):
    aid = _mk(owner)
    owner.put(f"/apps/{aid}/domain", json={"host": HOST})
    row = _row(db_session, aid)
    row.status = "running"
    db_session.commit()
    owner.put(f"/apps/{aid}/domain", json={"host": "other.example.com"})
    assert _row(db_session, aid).status == "running"  # nothing was served on the pending host: no re-render needed


def test_verify_failure_does_not_requeue_and_clears_on_success(owner, db_session, monkeypatch):
    aid = _mk(owner)
    owner.put(f"/apps/{aid}/domain", json={"host": HOST})
    row = _row(db_session, aid)
    row.status = "running"
    db_session.commit()

    def boom(*a, **k):
        raise cd.CustomDomainError("not yet")

    monkeypatch.setattr("app.routers.apps.custom_domains.verify", boom)
    r = owner.post(f"/apps/{aid}/domain/verify")
    assert r.json()["custom_domain"]["error"] == "not yet" and r.json()["status"] == "running"
    monkeypatch.setattr("app.routers.apps.custom_domains.verify", lambda *a, **k: None)
    r = owner.post(f"/apps/{aid}/domain/verify")
    assert r.json()["custom_domain"]["error"] is None and r.json()["status"] == "pending"


def test_verify_passes_the_stored_token_and_host(owner, monkeypatch):
    aid = _mk(owner)
    token = owner.put(f"/apps/{aid}/domain", json={"host": HOST}).json()["custom_domain"]["challenge_token"]
    seen = {}
    monkeypatch.setattr("app.routers.apps.custom_domains.verify", lambda host, tok, **k: seen.update(h=host, t=tok))
    owner.post(f"/apps/{aid}/domain/verify")
    assert seen == {"h": HOST, "t": token}


def test_clear_pending_does_not_requeue_but_clear_active_does(owner, db_session):
    aid = _mk(owner)
    owner.put(f"/apps/{aid}/domain", json={"host": HOST})
    row = _row(db_session, aid)
    row.status = "running"
    db_session.commit()
    assert owner.delete(f"/apps/{aid}/domain").json()["custom_domain"] is None
    assert _row(db_session, aid).status == "running"
    _activate(db_session, aid)
    assert owner.delete(f"/apps/{aid}/domain").status_code == 200
    row = _row(db_session, aid)
    assert row.status == "pending"
    assert (row.custom_domain, row.custom_domain_token, row.custom_domain_status) == (None, None, None)


def test_clear_when_nothing_attached_is_a_noop(owner, db_session):
    aid = _mk(owner)
    r = owner.delete(f"/apps/{aid}/domain")
    assert r.status_code == 200 and r.json()["custom_domain"] is None
    assert _row(db_session, aid).status == "pending"  # unchanged from creation


def test_domain_actions_are_audited(owner, admin, monkeypatch):
    aid = _mk(owner)
    owner.put(f"/apps/{aid}/domain", json={"host": HOST})
    monkeypatch.setattr("app.routers.apps.custom_domains.verify", lambda *a, **k: None)
    owner.post(f"/apps/{aid}/domain/verify")
    owner.delete(f"/apps/{aid}/domain")
    actions = {e["action"]: e for e in admin.get("/admin/audit?limit=100").json() if e["action"].startswith("app.domain")}
    assert set(actions) == {"app.domain.set", "app.domain.verify", "app.domain.clear"}
    assert all(HOST in e["detail"] for e in actions.values())


def test_challenge_token_is_never_listed_for_other_users(owner, make_client):
    aid = _mk(owner)
    token = owner.put(f"/apps/{aid}/domain", json={"host": HOST}).json()["custom_domain"]["challenge_token"]
    other = make_client()
    register_and_login(other, "other@example.com")
    assert token not in other.get("/apps").text
    assert other.get(f"/apps/{aid}").status_code == 404


# --- hosts are held until teardown (custom domain side of release-on-delete) -------------------------------

def test_custom_domain_held_by_deleted_app_until_teardown(owner, admin, db_session):
    a = _mk(owner)
    owner.put(f"/apps/{a}/domain", json={"host": HOST})
    b = admin.post("/apps", json=APP).json()["id"]
    assert owner.delete(f"/apps/{a}").status_code == 204
    # Soft-deleted, namespace/Ingress still present: the host may not be re-attached elsewhere yet.
    assert admin.put(f"/apps/{b}/domain", json={"host": HOST}).status_code == 409
    assert _row(db_session, b).custom_domain is None
    kube, mysql, store = FakeKubeProvider(), FakeMySQLAdmin(), FakeObjectStore()
    reconcile_all(db_session, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store, settings={})
    assert admin.put(f"/apps/{b}/domain", json={"host": HOST}).status_code == 200


def test_custom_ingress_removed_when_domain_detached(owner, db_session):
    aid = _mk(owner)
    kube, mysql, store = FakeKubeProvider(), FakeMySQLAdmin(), FakeObjectStore()

    def sweep():
        reconcile_all(db_session, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store, settings={})
        db_session.expire_all()

    def custom_ingresses():
        return [m for m in kube.applied[f"app-{aid}"] if m["kind"] == "Ingress" and m["metadata"]["name"].endswith("-custom")]

    owner.put(f"/apps/{aid}/domain", json={"host": HOST})
    sweep()
    assert custom_ingresses() == []  # pending: not served
    _activate(db_session, aid)
    row = _row(db_session, aid)
    row.status = "pending"
    db_session.commit()
    sweep()
    assert len(custom_ingresses()) == 1
    owner.delete(f"/apps/{aid}/domain")
    sweep()
    assert custom_ingresses() == []


# --- manifest rendering ------------------------------------------------------------------------------------

def test_custom_ingress_is_separate_from_primary_and_cert_is_per_host():
    r = k8s.render_app_manifests(7, "gen-abc1234", "img", 8080, {}, None, custom_host=HOST, platform_subdomain="blog")
    prim, cust = r["ingress"], r["custom_ingress"]
    assert prim["metadata"]["name"] == "app-7" and cust["metadata"]["name"] == "app-7-custom"
    # Custom host is only on the custom Ingress (own cert); platform hosts only on the wildcard-TLS primary.
    assert HOST not in [x["host"] for x in prim["spec"]["rules"]]
    assert [x["host"] for x in cust["spec"]["rules"]] == [HOST]
    assert "secretName" not in prim["spec"]["tls"][0] and "annotations" not in prim["metadata"]
    assert cust["spec"]["tls"][0]["secretName"] == "app-7-custom-tls"
    assert cust["metadata"]["annotations"]["cert-manager.io/cluster-issuer"] == k8s.CUSTOM_DOMAIN_ISSUER
    assert cust["spec"]["ingressClassName"] == prim["spec"]["ingressClassName"]
    # Both route to the same Service/port in the app's own namespace.
    assert cust["metadata"]["namespace"] == prim["metadata"]["namespace"] == "app-7"
    assert cust["spec"]["rules"][0]["http"] == prim["spec"]["rules"][0]["http"]
    flat_kinds = [(m["kind"], m["metadata"]["name"]) for m in k8s.flatten_manifests(r)]
    assert flat_kinds.count(("Ingress", "app-7")) == 1 and flat_kinds.count(("Ingress", "app-7-custom")) == 1


def test_custom_ingress_follows_port():
    m = k8s.custom_domain_ingress_manifest(1, HOST, 9090)
    assert m["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"] == {"number": 9090}
    assert k8s.custom_ingress_name(1) == "app-1-custom"
    assert k8s.ingress_name(1) == "app-1"


def test_egress_ports_exclude_exactly_the_smtp_ports(monkeypatch):
    def covered(ranges):
        s = set()
        for p in ranges:
            if p["protocol"] == "TCP":
                s.update(range(p["port"], p.get("endPort", p["port"]) + 1))
        return s

    tcp = covered(k8s._internet_egress_ports())
    extra = {x for lo, hi in k8s.ABUSE_TCP_RANGES for x in range(lo, hi + 1)}
    assert tcp == set(range(1, 65536)) - set(k8s.SMTP_PORTS) - extra
    assert any(p["protocol"] == "UDP" for p in k8s._internet_egress_ports())
    # Adjacent / leading blocked ports (exercises the range-skipping branches).
    monkeypatch.setattr(k8s, "SMTP_PORTS", (1, 2, 25, 26, 65535))
    monkeypatch.setattr(k8s, "ABUSE_TCP_RANGES", ())
    assert covered(k8s._internet_egress_ports()) == set(range(1, 65536)) - {1, 2, 25, 26, 65535}
