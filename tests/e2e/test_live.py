"""Live end-to-end tests against a deployed PicoPaaS. See tests/e2e/README.md.

Skipped unless E2E_USER_TOKEN and E2E_ADMIN_TOKEN are set. Tests share two module-scoped apps
(hello, probe) to keep deploy time down; each test leaves them running.
"""
import os
import secrets

import pytest

from tests.e2e.harness import (
    IMG, PROBE_ENV, SLOW, app_url, get_json, http_get, poll, rand_name, skip_unless_live,
)

pytestmark = [pytest.mark.e2e, skip_unless_live]

APP_PORT = int(os.environ.get("E2E_APP_PORT", "8080"))  # port the testapps listen on


@pytest.fixture(scope="module")
def hello(user, admin):
    app = user.create_app(rand_name(), IMG["hello"], APP_PORT,
                          env=[{"key": "APP_VERSION", "value": "v1"}])
    try:
        user.wait_status(app["id"])
        yield app
    finally:
        admin.delete_app(app["id"])


@pytest.fixture(scope="module")
def probe(user, admin):
    app = user.create_app(rand_name(), IMG["probe"], APP_PORT, db=True, storage=True, env=PROBE_ENV)
    try:
        user.wait_status(app["id"])
        yield app
    finally:
        admin.delete_app(app["id"])


# 1 ----------------------------------------------------------------------------------------------
def test_01_hello_deploys_and_serves(user, hello):
    assert user.get_app(hello["id"])["status"] == "running"
    body = poll(lambda: get_json(app_url(hello)), 120, desc="hello GET /")
    assert isinstance(body, dict)
    r = http_get(app_url(hello, "/healthz"))
    assert r is not None and r.status_code == 200


# 2 ----------------------------------------------------------------------------------------------
def test_02_probe_db_and_storage(probe):
    db = poll(lambda: get_json(app_url(probe, "/db")), 120, desc="probe /db")
    assert db["ok"] is True
    assert db["tls"] is not None, "DB connection is not using TLS"
    s3 = poll(lambda: get_json(app_url(probe, "/s3")), 120, desc="probe /s3")
    assert s3["ok"] is True


# 3 ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("target,path", [("db", "/db"), ("storage", "/s3")])
def test_03_rotate_credentials(user, probe, target, path):
    user.ok("POST", f"/apps/{probe['id']}/rotate-credentials", json={"target": target})
    user.wait_rollout(probe["id"])
    body = poll(lambda: get_json(app_url(probe, path)), 180, desc=f"probe {path} after rotation")
    assert body["ok"] is True


# 4 ----------------------------------------------------------------------------------------------
def test_04_env_update_rolls_out(user, hello):
    marker = f"v-{secrets.token_hex(3)}"
    user.ok("PATCH", f"/apps/{hello['id']}",
            json={"env": [{"key": "APP_VERSION", "value": marker}]})
    user.wait_rollout(hello["id"])
    poll(lambda: marker in (http_get(app_url(hello)).text if http_get(app_url(hello)) else ""),
         180, desc="new APP_VERSION visible in GET /")


# 5 ----------------------------------------------------------------------------------------------
def test_05_admin_suspend_unsuspend(user, admin, hello):
    admin.ok("POST", f"/admin/apps/{hello['id']}/suspend")
    try:
        def down():
            r = http_get(app_url(hello), timeout=10)
            return r is None or r.status_code != 200
        poll(down, 180, desc="suspended app to stop serving")
        # the owner can't edit a suspended app
        assert user.req("PATCH", f"/apps/{hello['id']}", json={"port": APP_PORT}).status_code == 403
    finally:
        admin.unsuspend(hello["id"])
    user.wait_rollout(hello["id"])
    poll(lambda: get_json(app_url(hello)), 120, desc="unsuspended app serving again")


# 6 ----------------------------------------------------------------------------------------------
def test_06_root_image_rejected(user):
    r = user.req("POST", "/apps", json={"name": rand_name(), "image": "docker.io/library/nginx:latest",
                                        "port": 80})
    assert r.status_code == 422, r.text
    assert "root" in r.text.lower()


def test_06_unknown_image_rejected(user):
    r = user.req("POST", "/apps", json={"name": rand_name(),
                                        "image": "ghcr.io/seaburr/picopaas-does-not-exist:nope",
                                        "port": 8080})
    assert r.status_code == 422


@pytest.mark.skipif(not os.environ.get("E2E_BIG_IMAGE"),
                    reason="set E2E_BIG_IMAGE to a public non-root image >512MB to test the size cap")
def test_06_oversize_image_rejected(user):
    r = user.req("POST", "/apps", json={"name": rand_name(), "image": os.environ["E2E_BIG_IMAGE"],
                                        "port": 8080})
    assert r.status_code == 422, r.text


# 7 ----------------------------------------------------------------------------------------------
def test_07_spec_round_trip(user, admin):
    name = rand_name()
    spec = (
        "apiVersion: picopaas/v1\n"
        f"name: {name}\n"
        f"image: {IMG['hello']}\n"
        f"port: {APP_PORT}\n"
        "resources: { db: false, storage: false }\n"
        "env:\n  APP_VERSION: s1\n"
        "secrets:\n  - API_TOKEN\n"
    )
    r1 = user.ok("POST", "/apps/spec", json={"spec": spec, "secrets": {"API_TOKEN": "s3cret-" + name}})
    user.created.append(r1["id"])
    try:
        assert r1["created"] is True and r1["name"] == name
        env = {e["key"]: e for e in r1["env"]}
        assert env["APP_VERSION"]["value"] == "s1"
        assert env["API_TOKEN"]["is_secret"] and "s3cret" not in env["API_TOKEN"]["value"]
        # re-apply with a changed env and no secret value: secret keeps its stored value
        r2 = user.ok("POST", "/apps/spec", json={"spec": spec.replace("s1", "s2"), "secrets": {}})
        assert r2["created"] is False and r2["id"] == r1["id"]
        assert {e["key"]: e["value"] for e in r2["env"]}["APP_VERSION"] == "s2"
        assert any(a["id"] == r1["id"] for a in user.ok("GET", "/apps"))
        # unknown keys rejected
        bad = user.req("POST", "/apps/spec", json={"spec": spec + "privileged: true\n", "secrets": {}})
        assert bad.status_code == 422
    finally:
        admin.delete_app(r1["id"])


# 8 ----------------------------------------------------------------------------------------------
def test_08_idor(user, admin, second_user, hello):
    admin_app = admin.create_app(rand_name(), IMG["hello"], APP_PORT)
    other_app = second_user.create_app(rand_name(), IMG["hello"], APP_PORT)
    try:
        def assert_cannot_touch(actor, app_id):
            assert actor.req("GET", f"/apps/{app_id}").status_code == 404
            assert actor.req("PATCH", f"/apps/{app_id}", json={"port": 9999}).status_code == 404
            assert actor.req("POST", f"/apps/{app_id}/rotate-credentials",
                             json={"target": "db"}).status_code == 404
            assert actor.req("DELETE", f"/apps/{app_id}").status_code == 404
            assert all(a["id"] != app_id for a in actor.ok("GET", "/apps"))

        assert_cannot_touch(user, admin_app["id"])
        assert_cannot_touch(user, other_app["id"])
        assert_cannot_touch(second_user, hello["id"])
        # the apps are intact
        assert admin.get_app(admin_app["id"])["port"] == APP_PORT
        assert second_user.get_app(other_app["id"])["port"] == APP_PORT
        # non-admins can't use admin routes
        for who in (user, second_user):
            assert who.req("GET", "/admin/users").status_code == 403
            assert who.req("POST", f"/admin/apps/{hello['id']}/suspend").status_code == 403
        # unauthenticated / garbage key
        import httpx
        from tests.e2e.harness import PICOPAAS_URL
        assert httpx.get(f"{PICOPAAS_URL}/apps", timeout=20).status_code == 401
        assert httpx.get(f"{PICOPAAS_URL}/apps", headers={"Authorization": "Bearer pp_sk_bogus"},
                         timeout=20).status_code == 401
    finally:
        admin.delete_app(admin_app["id"])
        admin.delete_app(other_app["id"])


# 9 (slow) ---------------------------------------------------------------------------------------
@pytest.mark.skipif(not SLOW, reason="set E2E_SLOW=1 (quota poll runs every 5 min)")
def test_09_db_quota_makes_writes_fail(probe):
    get_json(app_url(probe, "/db/fill?mb=110"))  # may itself report an error once over the cap

    def writes_fail():
        r = get_json(app_url(probe, "/db/fill?mb=1"), ok_only=False)
        return r is not None and r["ok"] is False
    poll(writes_fail, 900, 20, desc="DB writes to be revoked after exceeding 100MB")
    assert get_json(app_url(probe, "/s3"))["ok"] is True, "storage unaffected by DB quota"


@pytest.mark.skipif(not SLOW, reason="set E2E_SLOW=1 (quota poll runs every 5 min)")
def test_09_storage_quota_makes_writes_fail(probe):
    get_json(app_url(probe, "/s3/fill?mb=260"))

    def writes_fail():
        r = get_json(app_url(probe, "/s3/fill?mb=1"), ok_only=False)
        return r is not None and r["ok"] is False
    poll(writes_fail, 900, 20, desc="S3 writes to be revoked after exceeding 250MB")
