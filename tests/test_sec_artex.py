"""ARTEX regression suite (2026-11-17): keeps PicoPaaS hardened against an autonomous, machine-speed attacker that
maps endpoints by 200-vs-403 differences, replays stolen API keys against admin routes, mass-assigns privilege
fields, and plants persistence (second admin, password reset). These tests INVENTORY the app (every route, every
request body model) instead of listing today's routes, so a route or schema added later is checked automatically.
A failure names the new route and says what to do. See docs/PENTEST.md "ARTEX pass" for what each test guards.

Fixtures never use secret-shaped literals (gitleaks): values are `not-the-...` or generated at run time.
"""
import re
import typing
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app import config, crypto, models
from app.db import get_db
from app.dependencies import get_current_user, get_kube
from app.kaisen.models import KaisenSettings
from app.main import app as fastapi_app
from app.providers import mysql as mysql_provider
from app.providers.fake import FakeKubeProvider, FakeObjectStore
from app.routers.apps import get_object_store
from app.providers import postgres as postgres_provider
from app.routers.kaisen import get_github_factory, get_model_factory
from tests.conftest import DEFAULT_PASSWORD, register_and_login
from tests.kaisen_helpers import KEY, TOKEN, Fakes

ROOT = Path(__file__).resolve().parent.parent
SAFE = {"GET", "HEAD", "OPTIONS"}
GHOST_ID = 987654


# --------------------------------------------------------------------------------------------------- inventory
class Rt(typing.NamedTuple):
    method: str
    path: str
    endpoint: typing.Callable
    dependant: typing.Any
    body_params: tuple

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"


def _calls(dep, acc=None):
    acc = set() if acc is None else acc
    acc.add(dep.call)
    for sub in dep.dependencies:
        _calls(sub, acc)
    return acc


def inventory() -> list[Rt]:
    """Every (method, path) the app serves. FastAPI 0.16x wraps include_router() results in _IncludedRouter objects, so
    `[r for r in app.routes if isinstance(r, APIRoute)]` sees only the 3 routes declared directly on the app; the real
    ones are reached through effective_route_contexts(). The openapi cross-check below fails loudly if a FastAPI
    upgrade changes this and the inventory silently shrinks."""
    out: list[Rt] = []
    for r in fastapi_app.routes:
        if type(r).__name__ == "_IncludedRouter":
            for c in r.effective_route_contexts():
                for m in sorted(c.methods - {"HEAD"}):
                    out.append(Rt(m, c.path, c.endpoint, c.dependant, tuple(c.dependant.body_params)))
        elif hasattr(r, "dependant") and getattr(r, "methods", None):
            for m in sorted(r.methods - {"HEAD"}):
                out.append(Rt(m, r.path, r.endpoint, r.dependant, tuple(r.dependant.body_params)))
    return out


ROUTES = inventory()


def _url(path: str, **ids) -> str:
    return re.sub(r"\{([^}:]+)(?::[^}]*)?\}", lambda m: str(ids.get(m.group(1), 1)), path)


class _Crashed:
    """Stands in for a response when the server raised: the walk reports it instead of aborting."""
    status_code, text = 599, "server raised"


def _req(client, rt: Rt, headers=None, **ids):
    kw = {"headers": headers or {}}
    if rt.method not in ("GET", "DELETE"):
        kw["json"] = {}
    try:
        return client.request(rt.method, _url(rt.path, **ids), **kw)
    except Exception:  # noqa: BLE001
        return _Crashed()


def _hdr(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _is_admin_path(path: str) -> bool:
    return path.startswith(("/admin", "/root"))


def test_inventory_is_complete():
    """Guards the guard: the inventory must match the OpenAPI schema, so no test below runs over a shrunken route list."""
    assert len(ROUTES) > 120, f"route inventory only found {len(ROUTES)} routes; FastAPI internals changed, fix inventory()"
    seen = {(r.method.lower(), re.sub(r"\{([^}:]+):[^}]*\}", r"{\1}", r.path)) for r in ROUTES}
    # The served schema hides /admin and /root, so cross-check against the UNFILTERED route list.
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(title="t", version="0", routes=fastapi_app.routes)["paths"]
    assert any(p.startswith("/admin") for p in schema), "unfiltered schema lost the admin routes"
    missing = [f"{m.upper()} {p}" for p, ops in schema.items() for m in ops if (m, p) not in seen]
    assert not missing, f"routes in OpenAPI but missing from the test inventory: {missing}"


# ------------------------------------------------------------------------------------------------------ fixtures
@pytest.fixture()
def world(make_client):
    """admin (first user), owner, outsider; sessions and keys. Returns a namespace-like dict. The cluster client is faked,
    so routes that read pods behave the same with or without a local kube config (CI has none)."""
    kube = FakeKubeProvider()
    fastapi_app.dependency_overrides[get_kube] = lambda: kube
    admin, owner, outsider = make_client(), make_client(), make_client()
    register_and_login(admin, "admin@example.com")
    register_and_login(owner, "owner@example.com")
    register_and_login(outsider, "outsider@example.com")
    keys = {n: c.post("/api/keys").json()["key"] for n, c in (("admin", admin), ("owner", owner), ("outsider", outsider))}
    read = {n: c.post("/api/keys", json={"scope": "read"}).json()["key"]
            for n, c in (("admin", admin), ("owner", owner))}
    return {"admin": admin, "owner": owner, "outsider": outsider, "keys": keys, "read": read,
            "anon": make_client(), "make_client": make_client}


# a. ------------------------------------------------------------------------------------ unauthenticated access
# Routes that do NOT depend on get_current_user, each public on purpose. Adding a route here is a security decision.
PUBLIC_ROUTES = {
    "POST /auth/register",  # signup (rate limited per IP; new accounts start pending approval)
    "POST /auth/login",  # login (per-email and per-IP lockout)
    "POST /auth/logout",  # clears the cookie; nothing to protect
    "GET /api/banner",  # the platform notice every visitor sees, including the login page
    "GET /kaisen/bundles/{token}.tar.gz",  # unguessable expiring token is the credential (download link)
    "POST /root/admin-reset",  # 404 unless ROOT_MANAGEMENT_KEY is set; then it checks that key itself (K145)
    "GET /openapi.json",  # 404 unless a signed-in active user (browser or key); checked in tests/test_api_docs_login.py
    "GET /docs",  # same
    "GET /redoc",  # same
    "GET /healthz",  # liveness probe, no data
    "GET /metrics",  # 404 on public hosts; probe/auth rules live in app/main.py
    "GET /{full_path:path}",  # SPA shell and static files
}


def test_public_routes_are_exactly_the_allowlist():
    public = {r.label for r in ROUTES if get_current_user not in _calls(r.dependant)}
    new = sorted(public - PUBLIC_ROUTES)
    assert not new, (f"New route(s) with NO authentication dependency: {new}. If they must be public, add them to "
                     "PUBLIC_ROUTES with a comment saying why; otherwise add Depends(get_active_user) or similar.")
    stale = sorted(PUBLIC_ROUTES - public)
    assert not stale, f"PUBLIC_ROUTES lists routes that no longer exist or now need auth: {stale}; remove them."


def test_every_non_public_route_refuses_anonymous_and_bogus_bearer(world):
    anon = world["anon"]
    bad = []
    for r in ROUTES:
        if r.label in PUBLIC_ROUTES:
            continue
        for hdrs in (None, _hdr("pp_sk_" + "0" * 64)):
            got = _req(anon, r, hdrs).status_code
            if got not in (401, 404):  # never 2xx, never 5xx, never a 403/422 that tells an anonymous caller more
                bad.append(f"{r.label} -> {got}")
    assert not bad, (f"Routes answering something other than 401/404 without valid credentials: {bad}. "
                     "Authenticate the route before it validates input or touches data.")


# b. ---------------------------------------------------------------------------------------- admin routes + keys
# Admin writes that still accept a platform admin's API key (CLI/e2e operational use). Everything else under /admin
# that changes state needs a browser session (require_admin_action / require_admin_session). Adding here = decision.
ADMIN_KEY_WRITE_ALLOWLIST = {
    "POST /admin/backups/{run_id}/restore",  # CLI: picopaas backup restore (cli/picopaas.py)
    "POST /admin/apps/{app_id}/suspend",  # abuse response from the CLI/e2e harness; reversible, no identity effect
    "POST /admin/apps/{app_id}/unsuspend",  # reverse of the above
    "DELETE /admin/apps/{app_id}",  # e2e harness cleanup of throwaway apps; removes an app, grants nothing
    "PATCH /admin/apps/{app_id}/resources",  # operational resource override on one app
    "PUT /admin/apps/{app_id}/replicas",  # operational scaling of one app
    "PATCH /admin/users/{user_id}",  # only app_limit, capped by the platform maximum; used by the e2e harness
    "PUT /admin/apps/{app_id}/backups",  # abuse control on one app's backups (lock/ceiling); K149: consider web-only
    "DELETE /admin/kaisen/builds/{build_id}/uploads/{upload_id}",  # takedown: removes content, grants nothing
}
# Admin routes that need no platform-admin dependency at all but live under /admin or /root (none today).
NO_ADMIN_DEP_OK = {"POST /root/admin-reset"}


def _admin_routes():
    return [r for r in ROUTES if _is_admin_path(r.path) and r.label not in NO_ADMIN_DEP_OK]


def test_admin_allowlist_has_no_stale_entries():
    labels = {r.label for r in ROUTES}
    stale = sorted(ADMIN_KEY_WRITE_ALLOWLIST - labels)
    assert not stale, f"ADMIN_KEY_WRITE_ALLOWLIST names routes that no longer exist: {stale}"
    not_admin = sorted(x for x in ADMIN_KEY_WRITE_ALLOWLIST if not x.split(" ", 1)[1].startswith("/admin"))
    assert not not_admin, f"allowlist entries outside /admin: {not_admin}"


def test_admin_routes_refuse_normal_users_session_and_key(world):
    bad = []
    for r in _admin_routes():
        for who, hdrs in (("session", None), ("key", _hdr(world["keys"]["owner"]))):
            client = world["owner"] if who == "session" else world["anon"]
            got = _req(client, r, hdrs).status_code
            if got not in (403, 404):
                bad.append(f"{r.label} as normal user {who} -> {got}")
    assert not bad, f"Admin routes reachable by a non-admin: {bad}. Depend on require_platform_admin (router level)."


def test_admin_writes_refuse_a_platform_admins_api_key_unless_allowlisted(world):
    bad = []
    for r in _admin_routes():
        if r.method in SAFE or r.label in ADMIN_KEY_WRITE_ALLOWLIST:
            continue
        resp = _req(world["anon"], r, _hdr(world["keys"]["admin"]))
        if resp.status_code != 403:
            bad.append(f"{r.label} -> {resp.status_code}")
    assert not bad, (f"Admin write routes that accept a platform admin's API key: {bad}. A stolen admin key must not "
                     "change identity, privilege or policy: use Depends(require_admin_action) on the route, or, if it "
                     "is an operational call the CLI needs, add it to ADMIN_KEY_WRITE_ALLOWLIST with the reason.")


def test_web_login_only_admin_actions_say_so_and_work_in_the_browser(world):
    admin, owner = world["admin"], world["owner"]
    uid = owner.get("/auth/me").json()["id"]
    r = world["anon"].post(f"/admin/users/{uid}/reset-password", headers=_hdr(world["keys"]["admin"]))
    assert r.status_code == 403 and r.json()["detail"] == "This admin action needs a web login, not an API key."
    r = world["anon"].post(f"/admin/users/{uid}/promote", headers=_hdr(world["keys"]["admin"]))
    assert r.status_code == 403
    assert not _is_admin_user(uid)  # the refused promote changed nothing
    assert admin.post(f"/admin/users/{uid}/reset-password").status_code == 200  # the browser path still works


def _is_admin_user(uid: int) -> bool:
    s = next(fastapi_app.dependency_overrides[get_db]())
    return bool(s.get(models.User, uid).is_platform_admin)


# c. ----------------------------------------------------------------------------------------- read-only keys
READ_KEY_EXEMPT = {
    "POST /apps/{app_id}/storage/presign",  # a GET presign is a pure read; the route refuses PUT presigns itself
    "POST /auth/register", "POST /auth/login", "POST /auth/logout",  # public, no key involved
    "POST /root/admin-reset",  # public, guarded by its own management key
}


def test_unsafe_routes_refuse_a_read_scope_key(world):
    bad = []
    for r in ROUTES:
        if r.method in SAFE or r.label in READ_KEY_EXEMPT:
            continue
        key = world["read"]["admin"] if _is_admin_path(r.path) else world["read"]["owner"]
        got = _req(world["anon"], r, _hdr(key)).status_code
        if got != 403:
            bad.append(f"{r.label} -> {got}")
    assert not bad, (f"Unsafe-method routes a read-scope key can reach: {bad}. They must authenticate through "
                     "get_current_user (which enforces scope) or be added to READ_KEY_EXEMPT with a reason.")


# d. ------------------------------------------------------------------------------------------ mass assignment
FORBIDDEN_FIELDS = {"role", "is_admin", "is_platform_admin", "status", "app_limit", "owner_id", "user_id", "created_by"}
# (model name, field) pairs allowed on purpose, each with the reason.
PRIVILEGED_FIELD_OK = {
    ("AdminUserPatch", "app_limit"),  # admin-only route; the value is capped by the platform maximum
    ("InviteCreate", "role"),  # project invite role (member/owner), project owner via browser session only
    ("RolePatch", "role"),  # project role change, project owner via browser session only
    ("AdminProjectPatch", "app_limit"),  # admin route, web login only, capped by the per-account ceiling
    ("DbServerPatch", "status"),  # admin route: enable/disable a DB server for new placements
}
# Request models allowed to accept unknown keys (reason).
EXTRA_OK = {
    "Body": "FastAPI wrapper for a bare dict body; not a model",
}


def _models_in(annotation, seen=None) -> list[type]:
    seen = set() if seen is None else seen
    found = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return []
        seen.add(annotation)
        found.append(annotation)
        for f in annotation.model_fields.values():
            found.extend(_models_in(f.annotation, seen))
    else:
        for a in typing.get_args(annotation):
            found.extend(_models_in(a, seen))
    return found


def _request_models() -> dict[type, set[str]]:
    out: dict[type, set[str]] = {}
    for r in ROUTES:
        if r.method in SAFE:
            continue
        for p in r.body_params:
            for m in _models_in(p.field_info.annotation):
                out.setdefault(m, set()).add(r.label)
    return out


def test_request_models_forbid_unknown_fields():
    bad = [f"{m.__module__}.{m.__name__} (used by {sorted(rts)[0]})"
           for m, rts in _request_models().items()
           if m.model_config.get("extra") != "forbid" and m.__name__ not in EXTRA_OK]
    assert not bad, (f"Request body models that accept unknown fields: {bad}. Add "
                     "model_config = ConfigDict(extra='forbid') so unexpected keys are a 422, never silently passed on.")


def test_request_models_do_not_expose_privileged_fields():
    bad = []
    for m, rts in _request_models().items():
        for name in m.model_fields:
            if name in FORBIDDEN_FIELDS and (m.__name__, name) not in PRIVILEGED_FIELD_OK:
                bad.append(f"{m.__name__}.{name} (used by {sorted(rts)[0]})")
    assert not bad, (f"Request models with privilege/ownership fields: {bad}. A client must never set these. Remove "
                     "the field, or add (model, field) to PRIVILEGED_FIELD_OK with the reason if the route is "
                     "admin- or owner-gated by design.")


def test_registration_cannot_mass_assign_privilege(world):
    c = world["anon"]
    r = c.post("/auth/register", json={"email": "sneaky@example.com", "password": DEFAULT_PASSWORD,
                                       "accept_terms": True, "is_platform_admin": True, "status": "active"})
    assert r.status_code == 422
    r = c.post("/auth/register", json={"email": "sneaky@example.com", "password": DEFAULT_PASSWORD, "accept_terms": True})
    assert r.status_code == 201 and r.json()["status"] == "pending" and not _is_admin_user(r.json()["id"])


# e. ---------------------------------------------------------------------------- existence oracle on others' objects
OBJECT_PARAMS = {"app_id", "project_id", "build_id"}


@pytest.fixture()
def fakes(monkeypatch):
    f = Fakes()
    fastapi_app.dependency_overrides[get_github_factory] = lambda: (lambda token: (f.tokens.append(token), f.github)[1])
    fastapi_app.dependency_overrides[get_model_factory] = lambda: (lambda key: f.anthropic)
    monkeypatch.setattr(config, "KAISEN_RUNTIME_IMAGE", "ghcr.io/example/kaisen-runtime:1")
    fastapi_app.dependency_overrides[get_object_store] = lambda: FakeObjectStore()
    return f


APP_SECRET = "not-the-app-secret-value"
BUILD_TOKEN = "not-the-build-github-token"


@pytest.fixture()
def seeded(world, fakes):
    """The owner has an app (with a secret env var), a project with Kaisen key/token, and a build with its own token."""
    owner = world["owner"]
    r = owner.post("/apps", json={"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "want_db": False,
                                  "want_storage": False,
                                  "env": [{"key": "API_TOKEN", "value": APP_SECRET, "is_secret": True}]})
    assert r.status_code == 201, r.text
    pid = owner.post("/projects", json={"name": "Team"}).json()["id"]
    assert owner.put(f"/projects/{pid}/kaisen", json={"anthropic_key": KEY, "github_token": TOKEN}).status_code == 200
    b = owner.post("/kaisen/builds", json={"project_id": pid, "name": "Todo", "brief": "A tiny todo list"})
    assert b.status_code == 201, b.text
    ids = {"app_id": r.json()["id"], "project_id": pid, "build_id": b.json()["id"]}
    out = owner.put(f"/kaisen/builds/{ids['build_id']}/output",
                    json={"mode": "github", "repo": "octo/demo", "auth": "build", "github_token": BUILD_TOKEN})
    assert out.status_code == 200, out.text
    return {**world, "ids": ids}


def _norm(text: str) -> str:
    return re.sub(r"\d+", "N", text)


def test_no_existence_oracle_for_other_tenants_objects(seeded):
    outsider, ids = seeded["outsider"], seeded["ids"]
    checked, bad = 0, []
    for r in ROUTES:
        names = set(re.findall(r"\{([^}:]+)", r.path))
        if not names & OBJECT_PARAMS or r.label in PUBLIC_ROUTES or _is_admin_path(r.path):
            continue
        # a route nested under the object (backups, env, events...) must not tell an outsider more than a ghost id
        real = _req(outsider, r, **ids)
        ghost = _req(outsider, r, **{k: GHOST_ID for k in ids})
        checked += 1
        if (real.status_code, _norm(real.text)) != (ghost.status_code, _norm(ghost.text)):
            bad.append(f"{r.label}: existing -> {real.status_code}, nonexistent -> {ghost.status_code}")
    assert checked > 30, f"only {checked} object routes compared; inventory regression?"
    assert not bad, (f"Responses differ between another tenant's object and a nonexistent id: {bad}. Return the same "
                     "404 (same body) for both; never 403 for 'exists but not yours'.")
    for p in ("/apps/{app_id}", "/projects/{project_id}", "/kaisen/builds/{build_id}", "/apps/{app_id}/backups"):
        assert outsider.get(p.format(**ids)).status_code == 404, p


# f. ----------------------------------------------------------------------------------- no internals in errors
def test_unhandled_error_hides_sql_paths_and_exception_text(world):
    from sqlalchemy.exc import OperationalError

    secret_sql = "SELECT users.password_hash FROM users WHERE users.email = 'x' /etc/picopaas/not-a-real-path.py"

    def boom():
        raise OperationalError(secret_sql, {}, Exception("connection to 10.0.0.5:3306 refused, traceback marker"))
        yield  # pragma: no cover

    fastapi_app.dependency_overrides[get_db] = boom
    try:
        c = TestClient(fastapi_app, raise_server_exceptions=False)
        resp = c.get("/projects", headers=_hdr(world["keys"]["owner"]))
    finally:
        pass  # make_client's teardown clears dependency_overrides
    assert resp.status_code == 500
    body = resp.text
    for marker in ("Traceback", "SELECT", "password_hash", "/etc/", ".py", "OperationalError", "10.0.0.5", "refused",
                   'File "', "sqlalchemy"):
        assert marker not in body, f"500 body leaks {marker!r}: {body[:200]!r}"


# g. ---------------------------------------------------------------------------------- tenant DB isolation
GLOBAL_GRANT_MARKERS = ("*.*", "SUPER", "FILE", "PROCESS", "SYSTEM_VARIABLES_ADMIN", "SUPERUSER", "CREATEROLE",
                        "ALTER SYSTEM", "PG_READ_SERVER_FILES", "PG_EXECUTE_SERVER_PROGRAM", "PG_WRITE_SERVER_FILES",
                        "WITH GRANT OPTION", "GRANT OPTION", "SET GLOBAL", "GENERAL_LOG", "SECURE_FILE")


class _Rec:
    """Recording stand-in for a pymysql/psycopg connection+cursor: keeps every statement, answers with nothing."""

    def __init__(self, log):
        self.log = log

    def _text(self, q):
        try:
            return q.as_string(None) if hasattr(q, "as_string") else str(q)
        except Exception:  # noqa: BLE001
            return repr(q)

    def execute(self, q, params=None):
        self.log.append(self._text(q))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def cursor(self, *a, **k):
        return self

    def commit(self): ...
    def close(self): ...
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Server:
    host, port, admin_user, ca_cert, flavor = "db.example.internal", 3306, "admin", "", "self-managed"
    admin_password_encrypted = ""


def _server():
    s = _Server()
    s.admin_password_encrypted = crypto.encrypt("not-the-db-admin-password")
    return s


def _assert_no_global_grants(statements, who):
    assert statements, f"{who}: recorded no SQL, the recording fake is not wired to the provider"
    for st in statements:
        up = st.upper()
        for marker in GLOBAL_GRANT_MARKERS:
            hit = marker in up if marker == "*.*" else re.search(rf"\b{re.escape(marker.upper())}\b", up)  # not PROCESSLIST
            assert not hit, f"{who} sends a global/privileged statement ({marker}): {st[:160]}"


def test_mysql_provisioning_sql_has_no_global_grants(monkeypatch):
    log: list[str] = []
    monkeypatch.setattr(mysql_provider.pymysql, "connect", lambda **k: _Rec(log))
    monkeypatch.setattr(config, "MYSQL_ADMIN_USE_PROCEDURES", False)
    admin = mysql_provider.MySQLAdminClient(_server())
    for call in (lambda: admin.create_db_user(7), lambda: admin.set_user_connection_limit(7, 5),
                 lambda: admin.set_password(7, "not-the-new-password"), lambda: admin.set_read_only(7, True),
                 lambda: admin.set_read_only(7, False), lambda: admin.drop(7), lambda: admin.db_size_bytes(7)):
        try:
            call()
        except Exception:  # noqa: BLE001  (the fake returns no rows; only the SQL matters here)
            pass
    grants = [s for s in log if s.upper().startswith(("GRANT", "REVOKE"))]
    assert grants and all(re.fullmatch(r"(GRANT ALL|GRANT INSERT, UPDATE, CREATE, ALTER|REVOKE .*) ON `app_7`\.\* (TO|FROM) 'app_7'@'%'",
                                       g) for g in grants), grants
    _assert_no_global_grants(log, "MySQL provider")


def test_mysql_stored_procedures_only_grant_on_the_tenant_database():
    text = (ROOT / "deploy/mysql/statefulset.yaml").read_text()
    glob = [ln.strip() for ln in text.splitlines() if "*.*" in ln and "GRANT" in ln.upper() and not ln.strip().startswith("--")]
    # the only global grant is the read-only backup account (SELECT etc., no write, no SUPER/FILE)
    assert len(glob) == 1 and "'picopaas_backup'" in glob[0] and "SUPER" not in glob[0].upper() \
        and "FILE" not in glob[0].upper(), glob
    for ln in text.splitlines():
        if "CONCAT('GRANT" in ln:
            assert "`', v, '`.*" in ln, f"procedure grants outside the tenant database: {ln.strip()}"


def test_postgres_provisioning_sql_has_no_global_grants(monkeypatch):
    log: list[str] = []
    monkeypatch.setattr(postgres_provider.psycopg, "connect", lambda **k: _Rec(log))
    for flavor in ("do-managed", "self-managed"):
        s = _server()
        s.flavor = flavor
        admin = postgres_provider.PostgresAdmin(s)
        for call in (admin.init_server, lambda: admin.create_db_user(7), lambda: admin.set_user_connection_limit(7, 5),
                     lambda: admin.set_password(7, "not-the-new-password"), lambda: admin.set_read_only(7, True),
                     lambda: admin.set_read_only(7, True, hard=True), lambda: admin.set_read_only(7, False),
                     lambda: admin.drop(7), lambda: admin.db_size_bytes(7)):
            try:
                call()
            except Exception:  # noqa: BLE001
                pass
    assert any("CREATE ROLE" in s for s in log)
    _assert_no_global_grants([s for s in log if not s.startswith("SELECT")], "PostgreSQL provider")


# h. -------------------------------------------------------------------------------------------- tenant egress
MUST_EXCLUDE = {"169.254.0.0/16": "cloud metadata", "10.0.0.0/8": "VPC/private", "172.16.0.0/12": "private",
                "192.168.0.0/16": "private", "100.64.0.0/10": "carrier-grade NAT", "127.0.0.0/8": "loopback"}


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def test_tenant_network_policy_blocks_metadata_private_and_loopback_ranges():
    from app import k8s_manifests as k8s

    policies = k8s.network_policies(42, 8080, "10.1.2.3/32", 3306, storage_ips=["10.1.2.4"], custom_domain=True,
                                    backup_ips=["10.1.2.5"])
    open_world = [b["ipBlock"] for p in policies for b in _walk(p) if "ipBlock" in b
                  and b["ipBlock"]["cidr"] == "0.0.0.0/0"]
    assert open_world, "no internet egress rule found; test needs updating"
    for blk in open_world:
        missing = [f"{c} ({why})" for c, why in MUST_EXCLUDE.items() if c not in blk.get("except", [])]
        assert not missing, f"0.0.0.0/0 egress rule does not exclude {missing}"


def _platform_policies():
    for f in sorted((ROOT / "deploy/platform").glob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text()):
            if doc and doc.get("kind") == "NetworkPolicy":
                yield f.name, doc


def test_platform_pod_network_policies_exclude_metadata_everywhere():
    seen = 0
    for fname, doc in _platform_policies():
        for blk in (b["ipBlock"] for b in _walk(doc) if "ipBlock" in b):
            if blk["cidr"] == "0.0.0.0/0":
                seen += 1
                assert "169.254.0.0/16" in blk.get("except", []), \
                    f"{fname} {doc['metadata']['name']}: 0.0.0.0/0 egress without excluding 169.254.0.0/16"
    assert seen >= 2, "expected worker and backup egress rules; deploy/platform layout changed?"


# i. ------------------------------------------------------------------------------ secrets never echoed
def test_secrets_never_appear_in_any_get_response(seeded):
    ids = seeded["ids"]
    plain_keys = list(seeded["keys"].values()) + list(seeded["read"].values())
    secrets = {"kaisen key": KEY, "github token": TOKEN, "build token": BUILD_TOKEN, "app secret": APP_SECRET}
    secrets.update({f"api key {i}": k for i, k in enumerate(plain_keys)})
    walked = 0
    for who in ("owner", "admin"):
        client = seeded[who]
        for r in ROUTES:
            if r.method != "GET" or r.label in PUBLIC_ROUTES and not r.path.startswith("/api"):
                continue
            if any(x in r.path for x in ("/stream", "/download", "/export", "/logs")):
                continue  # file/stream bodies: covered by their own tests; they never carry settings values
            resp = _req(client, r, **ids)
            walked += 1
            assert resp.status_code < 500, f"GET {r.path} -> {resp.status_code} for {who}"
            for name, val in secrets.items():
                assert val not in resp.text, f"{name} appears in GET {r.path} (as {who})"
    assert walked > 100, f"walked only {walked} GET routes"
    # the dedicated reads, with the secret fields present as set-flags only
    assert seeded["owner"].get(f"/projects/{ids['project_id']}/kaisen").json()["anthropic_key"] == \
        {"set": True, "last4": KEY[-4:]}
    listed = seeded["owner"].get("/api/keys").text
    assert all(k not in listed for k in plain_keys), "API key plaintext is readable after creation"
    # at rest: encrypted, not the plaintext
    row = next(fastapi_app.dependency_overrides[get_db]()).get(KaisenSettings, ids["project_id"])
    assert KEY not in repr(vars(row))


# j. ---------------------------------------------------------------------------------------------- brute force
def test_login_lockout_still_engages(world, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "LOGIN_EMAIL_MAX_FAILS", 3)
    monkeypatch.setattr(config, "LOGIN_IP_MAX_FAILS", 100)
    codes = [world["anon"].post("/auth/login", json={"email": "owner@example.com", "password": "not-the-password"}).status_code
             for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429], codes
    ok = world["anon"].post("/auth/login", json={"email": "owner@example.com", "password": DEFAULT_PASSWORD})
    assert ok.status_code == 429, "the right password must not bypass an active lockout"


# API-key validity timing (second half of j) was assessed and skipped: keys are looked up by a SHA-256 of the full
# value (constant work per candidate), so there is no prefix oracle to measure; a wall-clock assertion would be flaky.
def test_api_keys_are_looked_up_by_hash_not_by_prefix():
    src = (ROOT / "app/dependencies.py").read_text()
    assert "ApiKey.hashed == key_hash" in src and "ApiKey.prefix ==" not in src
