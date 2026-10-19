"""Shared helpers for the live-environment tests (tests/e2e, tests/pentest).

Nothing here talks to the network at import time. All fixtures create uniquely named
apps (e2e-<random>) and delete them (admin DELETE /admin/apps/{id}) even on failure.
"""
import json
import os
import secrets
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import httpx
import pytest

PICOPAAS_URL = os.environ.get("PICOPAAS_URL", "https://platform.raisehellpraisedale.xyz").rstrip("/")
APP_DOMAIN = os.environ.get("APP_DOMAIN", "raisehellpraisedale.xyz")
USER_TOKEN = os.environ.get("E2E_USER_TOKEN", "")
ADMIN_TOKEN = os.environ.get("E2E_ADMIN_TOKEN", "")
IMAGE_TAG = os.environ.get("E2E_IMAGE_TAG", "latest")
SLOW = os.environ.get("E2E_SLOW") == "1"
ENABLED = bool(USER_TOKEN and ADMIN_TOKEN)

SKIP_REASON = "set E2E_USER_TOKEN and E2E_ADMIN_TOKEN (and PICOPAAS_URL) to run live tests"
skip_unless_live = pytest.mark.skipif(not ENABLED, reason=SKIP_REASON)

IMG = {n: f"ghcr.io/seaburr/picopaas-testapp-{n}:{IMAGE_TAG}" for n in ("hello", "probe", "toolbox", "webpage")}
DEPLOY_TIMEOUT = 300

PROBE_ENV = [
    {"key": "DB_HOST", "value": "${{ db.host }}"},
    {"key": "DB_PORT", "value": "${{ db.port }}"},
    {"key": "DB_NAME", "value": "${{ db.name }}"},
    {"key": "DB_USER", "value": "${{ db.user }}"},
    {"key": "DB_PASSWORD", "value": "${{ db.password }}"},
    {"key": "DB_CA_CERT", "value": "${{ db.ca_cert }}"},
    {"key": "S3_ENDPOINT", "value": "${{ storage.endpoint }}"},
    {"key": "S3_REGION", "value": "${{ storage.region }}"},
    {"key": "S3_BUCKET", "value": "${{ storage.bucket }}"},
    {"key": "S3_ACCESS_KEY", "value": "${{ storage.access_key }}"},
    {"key": "S3_SECRET_KEY", "value": "${{ storage.secret_key }}"},
]


def rand_name(prefix: str = "e2e") -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


class Api:
    """Thin authenticated client (Bearer pp_sk_ key)."""

    def __init__(self, token: str, label: str = ""):
        self.label = label
        self.c = httpx.Client(
            base_url=PICOPAAS_URL, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
        self.created: List[int] = []

    def req(self, method: str, path: str, **kw) -> httpx.Response:
        return self.c.request(method, path, **kw)

    def ok(self, method: str, path: str, **kw) -> Any:
        r = self.req(method, path, **kw)
        assert r.status_code < 400, f"{method} {path} -> {r.status_code}: {r.text[:300]}"
        return r.json() if r.content else None

    @property
    def me(self) -> dict:
        return self.ok("GET", "/auth/me")

    def create_app(self, name: str, image: str, port: int, db=False, storage=False,
                   env: Optional[list] = None) -> dict:
        r = self.req("POST", "/apps", json={
            "name": name, "image": image, "port": port,
            "want_db": db, "want_storage": storage, "env": env or [],
        })
        assert r.status_code == 201, f"create {name}: {r.status_code} {r.text[:300]}"
        app = r.json()
        self.created.append(app["id"])
        return app

    def get_app(self, app_id: int) -> dict:
        return self.ok("GET", f"/apps/{app_id}")

    def wait_status(self, app_id: int, want: str = "running", timeout: int = DEPLOY_TIMEOUT) -> dict:
        end = time.time() + timeout
        last: dict = {}
        while time.time() < end:
            last = self.get_app(app_id)
            if last["status"] == want:
                return last
            if last["status"] == "error" and want != "error":
                raise AssertionError(f"app {app_id} errored: {last.get('status_detail')}")
            time.sleep(5)
        raise AssertionError(f"app {app_id} not {want} after {timeout}s (status={last.get('status')}, "
                             f"detail={last.get('status_detail')})")

    def wait_rollout(self, app_id: int, timeout: int = DEPLOY_TIMEOUT) -> dict:
        """After a change that sets status=pending: wait for it to leave 'running' (best effort,
        60s) and then to come back to 'running'."""
        end = time.time() + 60
        while time.time() < end and self.get_app(app_id)["status"] == "running":
            time.sleep(3)
        return self.wait_status(app_id, "running", timeout)


class Admin(Api):
    def delete_app(self, app_id: int) -> None:
        self.req("DELETE", f"/admin/apps/{app_id}")

    def suspend(self, app_id: int) -> httpx.Response:
        return self.req("POST", f"/admin/apps/{app_id}/suspend")

    def unsuspend(self, app_id: int) -> httpx.Response:
        return self.req("POST", f"/admin/apps/{app_id}/unsuspend")


def app_url(app: dict, path: str = "/") -> str:
    """The API returns hostname with the platform suffix baked in; honour APP_DOMAIN if it differs."""
    host = app["hostname"]
    sub = host.split(".", 1)[0]
    return f"https://{sub}.{APP_DOMAIN}{path}"


def http_get(url: str, headers: Optional[dict] = None, timeout: int = 20, **kw) -> Optional[httpx.Response]:
    try:
        return httpx.get(url, headers=headers, timeout=timeout, follow_redirects=False, **kw)
    except httpx.HTTPError:
        return None


def poll(fn, timeout: int = 180, interval: int = 5, desc: str = "condition"):
    """Call fn until it returns a truthy value; return it. Raises AssertionError on timeout."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
        except Exception as exc:  # network blips while rolling
            last = exc
        if last and not isinstance(last, Exception):
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {desc} (last={last!r})")


def get_json(url: str, headers: Optional[dict] = None, ok_only=True):
    r = http_get(url, headers=headers)
    if r is None or (ok_only and r.status_code != 200):
        return None
    try:
        return r.json()
    except ValueError:
        return None


# --- fixtures (re-exported by tests/e2e/conftest.py and tests/pentest/conftest.py) ---------------

@pytest.fixture(scope="session")
def admin():
    a = Admin(ADMIN_TOKEN, "admin")
    assert a.me["role"] == "uberadmin", "E2E_ADMIN_TOKEN must belong to the uberadmin"
    return a


@pytest.fixture(scope="session")
def user(admin):
    """The test customer. Its app_limit is raised for the run and restored afterwards."""
    u = Api(USER_TOKEN, "user")
    me = u.me
    orig = next(x for x in admin.ok("GET", "/admin/users") if x["id"] == me["id"])["app_limit"]
    admin.ok("PATCH", f"/admin/users/{me['id']}", json={"app_limit": max(orig, 6)})
    yield u
    for app_id in u.created:
        admin.delete_app(app_id)
    admin.req("PATCH", f"/admin/users/{me['id']}", json={"app_limit": orig})


@pytest.fixture(scope="session")
def second_user(admin):
    """A freshly registered, admin-approved user with its own API key; deleted at the end."""
    email = f"e2e-{secrets.token_hex(5)}@picopaas-e2e.dev"
    password = secrets.token_urlsafe(18)
    with httpx.Client(base_url=PICOPAAS_URL, timeout=30) as c:
        r = c.post("/auth/register", json={"email": email, "password": password, "accept_terms": True})
        assert r.status_code == 201, f"register: {r.status_code} {r.text[:200]}"
        uid = r.json()["id"]
        try:
            admin.ok("POST", f"/admin/users/{uid}/approve")
            admin.ok("PATCH", f"/admin/users/{uid}", json={"app_limit": 4})
            key = c.post("/api/keys")  # session cookie from register
            assert key.status_code == 201, f"create key: {key.status_code} {key.text[:200]}"
            u = Api(key.json()["key"], "second_user")
            u.id = uid
        except Exception:
            admin.req("DELETE", f"/admin/users/{uid}")
            raise
    yield u
    for app_id in u.created:
        admin.delete_app(app_id)
    admin.req("DELETE", f"/admin/users/{uid}")


@contextmanager
def tracked_app(api: Api, admin: Admin, **kw):
    """Create an app; always remove it (admin delete works even when suspended)."""
    app = api.create_app(**kw)
    try:
        yield app
    finally:
        admin.delete_app(app["id"])


def dump(obj) -> str:
    return json.dumps(obj, sort_keys=True)
