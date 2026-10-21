"""Custom-domain verification (app/custom_domains.py) and the owner endpoints (app/routers/apps.py)."""
import pytest

from app import custom_domains as cd
from app import k8s_manifests as k8s
from app.config import APP_DOMAIN, CUSTOM_DOMAIN_CNAME_TARGET
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
HOST = "blog.example.com"
CHALLENGE = f"_picopaas-challenge.{HOST}"
LB_IP = "203.0.113.10"


class FakeLookup:
    def __init__(self, txt=None, a=None):
        self._txt = txt or {}
        self._a = a or {}

    def txt(self, name):
        return list(self._txt.get(name, []))

    def a(self, name):
        return set(self._a.get(name, set()))


# --- host validation ---

@pytest.mark.parametrize("raw,expected", [
    ("Blog.Example.COM", "blog.example.com"),
    ("https://blog.example.com/", "blog.example.com"),
    ("blog.example.com.", "blog.example.com"),
])
def test_normalize_host_ok(raw, expected):
    assert cd.normalize_host(raw) == expected


@pytest.mark.parametrize("bad", ["", "not a host", "example", "-bad.example.com", "a..b.com", f"x.{APP_DOMAIN}", APP_DOMAIN])
def test_normalize_host_rejected(bad):
    with pytest.raises(cd.CustomDomainError):
        cd.normalize_host(bad)


# --- verify() ---

def test_verify_ok():
    lk = FakeLookup(txt={CHALLENGE: ["tok123"]}, a={HOST: {LB_IP}, CUSTOM_DOMAIN_CNAME_TARGET: {LB_IP}})
    cd.verify(HOST, "tok123", lookup=lk)  # no raise


def test_verify_missing_txt():
    lk = FakeLookup(a={HOST: {LB_IP}, CUSTOM_DOMAIN_CNAME_TARGET: {LB_IP}})
    with pytest.raises(cd.CustomDomainError, match="ownership record"):
        cd.verify(HOST, "tok123", lookup=lk)


def test_verify_wrong_txt():
    lk = FakeLookup(txt={CHALLENGE: ["nope"]}, a={HOST: {LB_IP}, CUSTOM_DOMAIN_CNAME_TARGET: {LB_IP}})
    with pytest.raises(cd.CustomDomainError, match="doesn't match"):
        cd.verify(HOST, "tok123", lookup=lk)


def test_verify_not_resolving():
    lk = FakeLookup(txt={CHALLENGE: ["tok123"]}, a={CUSTOM_DOMAIN_CNAME_TARGET: {LB_IP}})
    with pytest.raises(cd.CustomDomainError, match="doesn't resolve"):
        cd.verify(HOST, "tok123", lookup=lk)


def test_verify_points_elsewhere():
    lk = FakeLookup(txt={CHALLENGE: ["tok123"]}, a={HOST: {"198.51.100.5"}, CUSTOM_DOMAIN_CNAME_TARGET: {LB_IP}})
    with pytest.raises(cd.CustomDomainError, match="not to the platform"):
        cd.verify(HOST, "tok123", lookup=lk)


# --- manifests ---

def test_custom_ingress_manifest_shape():
    m = k8s.custom_domain_ingress_manifest(42, HOST, 8080)
    assert m["metadata"]["name"] == "app-42-custom"
    assert m["metadata"]["annotations"]["cert-manager.io/cluster-issuer"]
    assert m["spec"]["tls"] == [{"hosts": [HOST], "secretName": "app-42-custom-tls"}]
    assert m["spec"]["rules"][0]["host"] == HOST
    assert m["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"] == "app-42"


def test_netpol_allows_acme_solver_only_with_custom_domain():
    base = k8s.network_policies(1, 8080, None)
    assert not any(p["metadata"]["name"] == "allow-acme-solver" for p in base)
    cdp = k8s.network_policies(1, 8080, None, custom_domain=True)
    solver = next(p for p in cdp if p["metadata"]["name"] == "allow-acme-solver")
    assert solver["spec"]["podSelector"]["matchLabels"] == {"acme.cert-manager.io/http01-solver": "true"}
    assert solver["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8089}]
    # Rendering with a custom host wires it through.
    pols = k8s.render_app_manifests(1, "h", "img", 80, {}, None, custom_host="blog.example.com")["network_policies"]
    assert any(p["metadata"]["name"] == "allow-acme-solver" for p in pols)


def test_quota_gives_solver_headroom_only_with_custom_domain():
    # Default: one service, two pods, no nodeports.
    base = k8s.resource_quota_manifest(1)["spec"]["hard"]
    assert base["services"] == "1" and base["pods"] == "2" and base["services.nodeports"] == "0"
    # Custom domain: +1 service and +1 pod for the ACME HTTP-01 solver; still no nodeports/loadbalancers.
    cdq = k8s.resource_quota_manifest(1, custom_domain=True)["spec"]["hard"]
    assert cdq["services"] == "2" and cdq["pods"] == "3"
    assert cdq["services.nodeports"] == "0" and cdq["services.loadbalancers"] == "0"
    # Rendering with a custom host wires it through.
    rq = k8s.render_app_manifests(1, "h", "img", 80, {}, None, custom_host="blog.example.com")["resource_quota"]
    assert rq["spec"]["hard"]["services"] == "2"


def test_render_includes_custom_ingress_only_when_host_and_served():
    base = dict(app_id=1, hostname="h", image="img", port=80, env={}, mysql_cidr=None)
    assert k8s.render_app_manifests(**base)["custom_ingress"] is None
    assert k8s.render_app_manifests(**base, custom_host=HOST)["custom_ingress"]["metadata"]["name"] == "app-1-custom"
    # Suspended (no ingress) => no custom ingress even with a host.
    assert k8s.render_app_manifests(**base, custom_host=HOST, include_ingress=False)["custom_ingress"] is None
    flat_names = [m["metadata"]["name"] for m in k8s.flatten_manifests(k8s.render_app_manifests(**base, custom_host=HOST))]
    assert "app-1-custom" in flat_names


# --- endpoints ---

def _make_app(client):
    register_and_login(client, "domains@example.com")
    return client.post("/apps", json=APP).json()["id"]


def test_set_domain_returns_instructions_pending(client):
    aid = _make_app(client)
    r = client.put(f"/apps/{aid}/domain", json={"host": "Blog.Example.com"})
    assert r.status_code == 200, r.text
    cdom = r.json()["custom_domain"]
    assert cdom["host"] == "blog.example.com"
    assert cdom["status"] == "pending"
    assert cdom["challenge_host"] == CHALLENGE
    assert cdom["challenge_token"] and cdom["cname_target"] == CUSTOM_DOMAIN_CNAME_TARGET


def test_set_domain_bad_host_422(client):
    aid = _make_app(client)
    assert client.put(f"/apps/{aid}/domain", json={"host": "not a domain"}).status_code == 422
    assert client.put(f"/apps/{aid}/domain", json={"host": "blog.example.com", "extra": 1}).status_code == 422


def test_domain_uniqueness_409(make_client):
    a = make_client()
    register_and_login(a, "owner-a@example.com")
    aid = a.post("/apps", json=APP).json()["id"]
    assert a.put(f"/apps/{aid}/domain", json={"host": HOST}).status_code == 200

    b = make_client()
    register_and_login(b, "owner-b@example.com")
    bid = b.post("/apps", json=APP).json()["id"]
    assert b.put(f"/apps/{bid}/domain", json={"host": HOST}).status_code == 409


def test_verify_success_marks_active_and_requeues(client, monkeypatch):
    aid = _make_app(client)
    client.put(f"/apps/{aid}/domain", json={"host": HOST})
    monkeypatch.setattr("app.routers.apps.custom_domains.verify", lambda *a, **k: None)
    r = client.post(f"/apps/{aid}/domain/verify")
    assert r.status_code == 200
    cdom = r.json()["custom_domain"]
    assert cdom["status"] == "active" and cdom["verified_at"] and cdom["error"] is None
    assert r.json()["status"] == "pending"  # requeued for the reconciler


def test_verify_failure_stores_error(client, monkeypatch):
    aid = _make_app(client)
    client.put(f"/apps/{aid}/domain", json={"host": HOST})

    def _boom(*a, **k):
        raise cd.CustomDomainError("The ownership record isn't visible yet.")

    monkeypatch.setattr("app.routers.apps.custom_domains.verify", _boom)
    r = client.post(f"/apps/{aid}/domain/verify")
    assert r.status_code == 200
    cdom = r.json()["custom_domain"]
    assert cdom["status"] == "pending" and "ownership record" in cdom["error"]


def test_clear_domain(client):
    aid = _make_app(client)
    client.put(f"/apps/{aid}/domain", json={"host": HOST})
    r = client.delete(f"/apps/{aid}/domain")
    assert r.status_code == 200
    assert r.json()["custom_domain"] is None


def test_cross_user_cannot_set_domain(make_client):
    owner = make_client()
    register_and_login(owner, "cd-owner@example.com")
    aid = owner.post("/apps", json=APP).json()["id"]
    intruder = make_client()
    register_and_login(intruder, "cd-intruder@example.com")
    assert intruder.put(f"/apps/{aid}/domain", json={"host": HOST}).status_code == 404
