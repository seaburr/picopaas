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


# 9 (slow) quota enforcement --------------------------------------------------------------------
# The platform polls DB size every 60s and storage every 5 min (plus a pod rollout after a key swap), so the
# timeouts below are generous. Each quota test gets its own probe app so a failed test can't poison the next.
SLOW_ONLY = pytest.mark.skipif(not SLOW, reason="set E2E_SLOW=1 (quota polls: DB 60s, storage 5 min)")
DB_POLL_TIMEOUT = 420    # 60s poll + slack for a slow/failed first attempt and the next tick
STORAGE_POLL_TIMEOUT = 1200   # 5 min poll + key swap + rollout, twice over for slack


@pytest.fixture()
def quota_probe(user, admin):
    app = user.create_app(rand_name("e2e-q"), IMG["probe"], APP_PORT, db=True, storage=True, env=PROBE_ENV)
    try:
        user.wait_status(app["id"])
        poll(lambda: get_json(app_url(app, "/db")), 120, desc="probe /db")
        yield app
    finally:
        admin.delete_app(app["id"])


def _writes_fail(app, kind):
    def check():
        r = get_json(app_url(app, f"/{kind}/fill?mb=1"), ok_only=False)
        return r is not None and r["ok"] is False
    return check


def _writes_work(app, kind):
    def check():
        r = get_json(app_url(app, f"/{kind}/fill?mb=1"), ok_only=False)
        return r is not None and r["ok"] is True
    return check


@SLOW_ONLY
def test_09_db_quota_revokes_writes_keeps_reads_and_recovers(user, quota_probe):
    app = quota_probe
    get_json(app_url(app, "/db/fill?mb=60"), ok_only=False)  # may itself fail once the cap is enforced mid-way
    poll(_writes_fail(app, "db"), DB_POLL_TIMEOUT, 10, desc="DB writes to be revoked after exceeding the cap")
    assert get_json(app_url(app, "/s3"))["ok"] is True, "storage unaffected by DB quota"
    # Reads still work, and the platform reports the state.
    assert poll(lambda: get_json(app_url(app, "/db/read")), 60, desc="DB reads while over cap")["ok"] is True
    q = user.get_app(app["id"])["quota"]["db"]
    assert q["over_quota"] is True and q["size_bytes"] > q["limit_bytes"]
    assert user.ok("GET", f"/apps/{app['id']}/database/usage")["over_quota"] is True
    # Cleaning up is allowed while over: DELETE succeeds, but InnoDB keeps the pages, so reclaim with TRUNCATE.
    assert get_json(app_url(app, "/db/clear?mode=delete"), ok_only=False)["ok"] is True
    assert get_json(app_url(app, "/db/clear?mode=truncate"), ok_only=False)["ok"] is True
    poll(_writes_work(app, "db"), DB_POLL_TIMEOUT, 10, desc="DB writes to be restored after freeing space")
    assert user.get_app(app["id"])["quota"]["db"]["over_quota"] is False


@SLOW_ONLY
def test_09_db_quota_drop_table_also_recovers(quota_probe):
    app = quota_probe
    get_json(app_url(app, "/db/fill?mb=60"), ok_only=False)
    poll(_writes_fail(app, "db"), DB_POLL_TIMEOUT, 10, desc="DB writes to be revoked")
    assert get_json(app_url(app, "/db/clear?mode=drop"), ok_only=False)["ok"] is True
    poll(_writes_work(app, "db"), DB_POLL_TIMEOUT, 10, desc="DB writes to be restored after DROP TABLE")


@SLOW_ONLY
def test_09_db_quota_long_single_connection_fill_is_cut_off(quota_probe):
    """One connection writing 300MB used to finish untouched (prod overshoot: 291MB under a 50MB cap). The
    platform must end the session, so the request fails/ends early and the DB stays far under 300MB."""
    app = quota_probe
    r = http_get(app_url(app, "/db/fill?mb=300"), timeout=900)
    ended_early = r is None or r.status_code != 200 or r.json().get("ok") is False
    size = poll(lambda: get_json(app_url(app, "/db/size")), 120, desc="/db/size")["mb"]
    assert ended_early, f"a 300MB single-connection fill completed (db is {size} MB)"
    assert size < 150, f"db grew to {size} MB under a 50 MB cap: open sessions were not stopped"


@SLOW_ONLY
def test_09_storage_quota_reads_survive_and_delete_endpoint_recovers(user, quota_probe):
    app = quota_probe
    get_json(app_url(app, "/s3/fill?mb=110"), ok_only=False)
    poll(_writes_fail(app, "s3"), STORAGE_POLL_TIMEOUT, 15, desc="S3 writes to be revoked after exceeding the cap")
    # Reads must keep working on the read-only key (the old key is only revoked after the rollout).
    assert poll(lambda: get_json(app_url(app, "/s3/read")), 120, desc="S3 reads while over cap")["objects"] > 0
    u = user.ok("GET", f"/apps/{app['id']}/storage/usage")
    assert u["over_quota"] is True and u["objects"] > 0 and u["limit_bytes"] == 100 * 1024 * 1024
    assert user.get_app(app["id"])["quota"]["storage"]["over_quota"] is True
    # The app can't delete with a read-only key; the platform does it.
    cleared = get_json(app_url(app, "/s3/clear"), ok_only=False)
    assert cleared is not None and cleared["ok"] is False, "a read-only key must not be able to delete"
    res = user.ok("POST", f"/apps/{app['id']}/storage/delete", json={"prefix": "probe-fill/"})
    assert res["deleted"] > 0
    while res["truncated"]:
        res = user.ok("POST", f"/apps/{app['id']}/storage/delete", json={"all": True})
    assert res["over_quota"] is False
    user.wait_rollout(app["id"])  # the restored read-write key rolls out immediately, not at the next poll
    poll(_writes_work(app, "s3"), 600, 15, desc="S3 writes to be restored after the platform delete")
    assert user.get_app(app["id"])["quota"]["storage"]["over_quota"] is False
