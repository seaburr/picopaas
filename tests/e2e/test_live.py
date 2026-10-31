"""Live end-to-end tests against a deployed PicoPaaS. See tests/e2e/README.md.

Skipped unless E2E_USER_TOKEN and E2E_ADMIN_TOKEN are set. Tests share two module-scoped apps
(hello, probe) to keep deploy time down; each test leaves them running.
"""
import os
import secrets
import time

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
                                        "port": 8080})
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


# 10 (slow) an admin raising a limit restores an over-limit app -----------------------------------------
# Lower the app's own limit, push it over, then put the limit back (null = platform default) and check the app
# recovers. Raising a quota rechecks that app at once, so recovery shouldn't wait for the next poll.
def _set_limits(admin, app, **fields):
    return admin.ok("PATCH", f"/admin/apps/{app['id']}/resources", json=fields)


@SLOW_ONLY
def test_10_db_quota_raise_restores_writes(admin, user, quota_probe):
    app = quota_probe
    _set_limits(admin, app, db_quota_mb=5)
    get_json(app_url(app, "/db/fill?mb=10"), ok_only=False)
    poll(_writes_fail(app, "db"), DB_POLL_TIMEOUT, 10, desc="DB writes to be revoked over a 5 MB override")
    out = _set_limits(admin, app, db_quota_mb=None)
    assert out["restarts_app"] is False and out["resources"]["db_quota_mb"] == 50
    poll(_writes_work(app, "db"), 90, 5, desc="DB writes to be restored right after the quota was raised")
    assert user.get_app(app["id"])["quota"]["db"]["over_quota"] is False


@SLOW_ONLY
def test_10_storage_quota_raise_restores_writes(admin, user, quota_probe):
    app = quota_probe
    _set_limits(admin, app, storage_quota_mb=2)
    get_json(app_url(app, "/s3/fill?mb=4"), ok_only=False)
    poll(_writes_fail(app, "s3"), STORAGE_POLL_TIMEOUT, 15, desc="S3 writes to be revoked over a 2 MB override")
    _set_limits(admin, app, storage_quota_mb=None)
    user.wait_rollout(app["id"])  # the read-write key is restored by the PATCH and rolled out
    poll(_writes_work(app, "s3"), 600, 15, desc="S3 writes to be restored after the quota was raised")
    assert user.get_app(app["id"])["quota"]["storage"]["over_quota"] is False


@SLOW_ONLY
def test_10_scratch_disk_raise_lets_the_app_write_more(admin, user, quota_probe):
    app = quota_probe
    out = _set_limits(admin, app, ephemeral_limit_mi=64)
    assert out["restarts_app"] is True
    user.wait_rollout(app["id"])
    get_json(app_url(app, "/disk/fill?mb=100"), ok_only=False)  # over 64 Mi: the pod is evicted and replaced
    out = _set_limits(admin, app, ephemeral_limit_mi=None)
    assert out["restarts_app"] is True and out["resources"]["ephemeral_limit_mi"] == 250
    user.wait_rollout(app["id"])
    r = poll(lambda: get_json(app_url(app, "/disk/fill?mb=100")), 300, 10, desc="100 MB scratch write after raise")
    assert r["written_mb"] == 100


# 11 project and user suspension (throwaway team project + apps; fast, no builds) -----------------
def _serving(app) -> bool:
    r = http_get(app_url(app), timeout=10)
    return r is not None and r.status_code == 200


def _all_down(*apps):
    return lambda: all(not _serving(a) for a in apps)


def _all_up(*apps):
    return lambda: all(_serving(a) for a in apps)


@pytest.fixture(scope="module")
def team(user, admin):
    """A throwaway team project with two hello apps, removed (apps, then project) even on failure."""
    proj = user.ok("POST", "/projects", json={"name": f"e2e suspend {int(time.time())}"})
    apps = []
    try:
        for _ in range(2):
            r = user.req("POST", "/apps", json={"name": rand_name(), "image": IMG["hello"], "port": APP_PORT,
                                                "project_id": proj["id"]})
            assert r.status_code == 201, r.text
            apps.append(r.json())
        for a in apps:
            user.wait_status(a["id"])
        poll(_all_up(*apps), 120, desc="both team apps serving")
        yield proj, apps
    finally:
        admin.req("POST", f"/admin/projects/{proj['id']}/unsuspend")  # 409 when not suspended: fine
        for a in apps:
            admin.delete_app(a["id"])
        if user.req("DELETE", f"/projects/{proj['id']}").status_code != 204:
            time.sleep(10)
            user.req("DELETE", f"/projects/{proj['id']}")


def _project(user, pid):
    return next(p for p in user.ok("GET", "/projects") if p["id"] == pid)


def test_11_project_suspend_stops_apps_blocks_writes_and_restores(user, admin, team):
    proj, (a1, a2) = team
    reason = "e2e abuse check"
    t0 = time.time()
    admin.ok("POST", f"/admin/projects/{proj['id']}/suspend", json={"reason": reason})
    try:
        poll(_all_down(a1, a2), 180, 3, desc="both suspended-project apps to stop serving")
        print(f"\n[timing] project suspend: apps down after {time.time() - t0:.0f}s")
        p = _project(user, proj["id"])
        assert p["status"] == "suspended" and reason in (p["suspended_reason"] or "")
        r = user.req("PATCH", f"/apps/{a1['id']}", json={"port": APP_PORT})
        assert r.status_code == 403 and reason in r.text, r.text
        r = user.req("POST", "/apps", json={"name": rand_name(), "image": IMG["hello"], "port": APP_PORT,
                                            "project_id": proj["id"]})
        assert r.status_code == 403, r.text
        assert user.req("GET", f"/apps/{a1['id']}").status_code == 200  # reads stay open
        r = user.req("POST", "/kaisen/builds", json={"name": "e2e", "brief": "x", "project_id": proj["id"]})
        assert r.status_code == 403, r.text
    finally:
        t1 = time.time()
        admin.req("POST", f"/admin/projects/{proj['id']}/unsuspend")
    poll(_all_up(a1, a2), 240, 3, desc="both apps serving again after project unsuspend")
    print(f"\n[timing] project unsuspend: apps up after {time.time() - t1:.0f}s")
    assert _project(user, proj["id"])["status"] == "active"


def test_11_project_suspend_is_precise(user, admin, team):
    """An app suspended on its own stays suspended through a project suspend + unsuspend (K46)."""
    proj, (a1, a2) = team
    admin.ok("POST", f"/admin/apps/{a1['id']}/suspend")
    try:
        poll(_all_down(a1), 180, 3, desc="app 1 to stop")
        admin.ok("POST", f"/admin/projects/{proj['id']}/suspend", json={"reason": "e2e precision"})
        poll(_all_down(a2), 180, 3, desc="app 2 to stop")
        # K46: no app restarts on its own while its project is suspended: neither the one the project put to sleep
        # (app 2) nor the one suspended on its own first (app 1; this was a 200 until the fix found by this test).
        for a in (a2, a1):
            r = admin.req("POST", f"/admin/apps/{a['id']}/unsuspend")
            assert r.status_code == 409, r.text
        admin.ok("POST", f"/admin/projects/{proj['id']}/unsuspend")
        poll(_all_up(a2), 240, 3, desc="app 2 back after project unsuspend")
        assert not _serving(a1), "individually suspended app must stay suspended"
        assert user.get_app(a1["id"])["status"] == "suspended"
    finally:
        admin.req("POST", f"/admin/projects/{proj['id']}/unsuspend")
        admin.req("POST", f"/admin/apps/{a1['id']}/unsuspend")
    poll(_all_up(a1, a2), 240, 3, desc="app 1 back after its own unsuspend")


def test_11_user_suspend_stops_personal_apps_only(user, admin, team, hello):
    proj, (a1, a2) = team
    uid = user.me["id"]
    t0 = time.time()
    admin.ok("POST", f"/admin/users/{uid}/suspend", json={"reason": "e2e user suspend"})
    try:
        poll(_all_down(hello), 180, 3, desc="personal app to stop serving")
        print(f"\n[timing] user suspend: personal app down after {time.time() - t0:.0f}s")
        # /auth/me stays readable for a suspended account (by design); the gated routes refuse it
        assert user.req("GET", "/apps").status_code in (401, 403)
        assert _serving(a1) and _serving(a2), "the team project must keep running"
    finally:
        t1 = time.time()
        admin.req("POST", f"/admin/users/{uid}/unsuspend")  # always lift it, a stuck test user breaks the suite
    poll(_all_up(hello), 240, 3, desc="personal app serving after user unsuspend")
    print(f"\n[timing] user unsuspend: personal app up after {time.time() - t1:.0f}s")
    assert user.req("GET", "/apps").status_code == 200
