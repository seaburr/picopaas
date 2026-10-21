"""Admin reserved-subdomain API: API-key scopes, body validation, serializer fields, and routing of the new host."""
import pytest

from app import config, models
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def user(make_client, admin):
    c = make_client()
    register_and_login(c, "user@example.com")
    return c


def _put(c, aid, body, **kw):
    return c.put(f"/admin/apps/{aid}/platform-host", json=body, **kw)


@pytest.mark.parametrize("body", [
    {"subdomain": None}, {"subdomain": 123}, {"subdomain": ["blog"]}, {"subdomain": "blog", "extra": 1},
    {"label": "blog"}, {"subdomain": "x" * 64}, {"subdomain": "   "}, {"subdomain": "xn--abc"},
])
def test_bad_bodies_are_422_and_change_nothing(admin, user, body):
    aid = user.post("/apps", json=APP).json()["id"]
    assert _put(admin, aid, body).status_code == 422
    assert user.get(f"/apps/{aid}").json()["platform_subdomain"] is None


def test_422_detail_is_plain_english(admin, user):
    aid = user.post("/apps", json=APP).json()["id"]
    r = _put(admin, aid, {"subdomain": "WWW"})
    assert r.status_code == 422 and "single lowercase label" in r.json()["detail"]
    r = _put(admin, aid, {"subdomain": "www"})
    assert "reserved" in r.json()["detail"]
    r = _put(admin, aid, {"subdomain": "xn--abc"})
    assert "xn--" in r.json()["detail"]


def test_label_padded_with_whitespace_is_stored_trimmed(admin, user):
    aid = user.post("/apps", json=APP).json()["id"]
    r = _put(admin, aid, {"subdomain": "  blog "})
    assert r.status_code == 200 and r.json()["platform_subdomain"] == "blog"


def test_conflict_message_does_not_name_the_other_app(admin, user):
    a = user.post("/apps", json=APP).json()["id"]
    b = admin.post("/apps", json=APP).json()["id"]
    _put(admin, a, {"subdomain": "blog"})
    r = _put(admin, b, {"subdomain": "blog"})
    assert r.status_code == 409 and str(a) not in r.json()["detail"] and "user@example.com" not in r.text


def test_changing_label_replaces_it_and_frees_the_old_one(admin, user):
    a = user.post("/apps", json=APP).json()["id"]
    b = admin.post("/apps", json=APP).json()["id"]
    _put(admin, a, {"subdomain": "old"})
    assert _put(admin, a, {"subdomain": "new"}).json()["platform_host"] == f"new.{config.APP_DOMAIN}"
    assert _put(admin, b, {"subdomain": "old"}).status_code == 200  # old label is free immediately (no Ingress rule left)


# --- API-key authz: the admin endpoints honour key scope and the key owner's role ---------------------------

def _key(c, scope):
    return {"Authorization": "Bearer " + c.post("/api/keys", json={"scope": scope}).json()["key"]}


def test_admin_read_key_cannot_set_or_clear_but_full_key_can(admin, user, make_client):
    aid = user.post("/apps", json=APP).json()["id"]
    ro, rw = _key(admin, "read"), _key(admin, "full")
    api = make_client()
    assert _put(api, aid, {"subdomain": "blog"}, headers=ro).status_code == 403
    assert api.delete(f"/admin/apps/{aid}/platform-host", headers=ro).status_code == 403
    assert user.get(f"/apps/{aid}").json()["platform_subdomain"] is None
    assert _put(api, aid, {"subdomain": "blog"}, headers=rw).status_code == 200
    assert api.delete(f"/admin/apps/{aid}/platform-host", headers=ro).status_code == 403  # still set: read key can't clear
    assert user.get(f"/apps/{aid}").json()["platform_subdomain"] == "blog"
    assert api.delete(f"/admin/apps/{aid}/platform-host", headers=rw).status_code == 200


def test_non_admin_full_key_is_still_forbidden(admin, user, make_client):
    aid = user.post("/apps", json=APP).json()["id"]
    api = make_client()
    hdr = _key(user, "full")
    assert _put(api, aid, {"subdomain": "blog"}, headers=hdr).status_code == 403
    assert api.delete(f"/admin/apps/{aid}/platform-host", headers=hdr).status_code == 403


def test_garbage_bearer_token_is_401(admin, user, make_client):
    aid = user.post("/apps", json=APP).json()["id"]
    assert _put(make_client(), aid, {"subdomain": "blog"}, headers={"Authorization": "Bearer pp_sk_nope"}).status_code == 401


# --- serializers -------------------------------------------------------------------------------------------

def test_hostname_field_is_unchanged_by_reserved_subdomain(admin, user):
    aid = user.post("/apps", json=APP).json()["id"]
    before = user.get(f"/apps/{aid}").json()
    _put(admin, aid, {"subdomain": "blog"})
    after = user.get(f"/apps/{aid}").json()
    assert after["hostname"] == before["hostname"]  # the generated host is always still served
    assert after["platform_host"] == f"blog.{config.APP_DOMAIN}"
    assert any(a["platform_host"] == f"blog.{config.APP_DOMAIN}" for a in user.get("/apps").json())
    assert admin.get("/admin/apps").json()[0]["hostname"].endswith(config.APP_DOMAIN)
