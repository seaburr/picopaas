"""Signup approval, suspension, admin user/app management, audit log."""
from app import models
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import reconcile_all
from tests.conftest import DEFAULT_PASSWORD, register_and_login

APP = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


def _setup(make_client):
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    user = make_client()
    register_and_login(user, "user@example.com", approve=False)
    uid = next(u["id"] for u in admin.get("/admin/users").json() if u["email"] == "user@example.com")
    return admin, user, uid


def _api_key_client(make_client, raw_key):
    c = make_client()
    c.headers["Authorization"] = f"Bearer {raw_key}"
    return c


def test_first_user_active_others_pending(make_client):
    admin, user, _ = _setup(make_client)
    assert admin.get("/auth/me").json()["status"] == "active"
    assert user.get("/auth/me").json()["status"] == "pending"


def test_pending_blocked_cookie_and_approve_flips(make_client):
    admin, user, uid = _setup(make_client)
    for resp in (user.get("/apps"), user.post("/apps", json=APP), user.get("/api/keys"),
                 user.post("/api/keys"), user.post("/apps/spec", json={"spec": "x"})):
        assert resp.status_code == 403
        assert resp.json()["detail"] .startswith("Your account is waiting for administrator approval")
    assert admin.post(f"/admin/users/{uid}/approve").status_code == 200
    assert user.get("/apps").status_code == 200
    assert user.post("/apps", json=APP).status_code == 201


def test_pending_blocked_with_api_key(make_client):
    admin, user, uid = _setup(make_client)
    admin.post(f"/admin/users/{uid}/approve")
    raw = user.post("/api/keys").json()["key"]
    # back to suspended: key must not work either
    admin.post(f"/admin/users/{uid}/suspend")
    kc = _api_key_client(make_client, raw)
    r = kc.get("/apps")
    assert r.status_code == 403 and r.json()["detail"] .startswith("Your account is suspended")
    assert kc.post("/apps/spec", json={"spec": "x"}).status_code == 403
    admin.post(f"/admin/users/{uid}/unsuspend")
    assert kc.get("/apps").status_code == 200


def test_suspend_scales_apps_to_zero_and_unsuspend_redeploys(make_client, db_session):
    admin, user, uid = _setup(make_client)
    admin.post(f"/admin/users/{uid}/approve")
    app_id = user.post("/apps", json=APP).json()["id"]
    admin.post(f"/admin/users/{uid}/suspend")
    row = db_session.get(models.App, app_id)
    assert row.status == "suspended"
    kube = FakeKubeProvider()
    kw = dict(kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=FakeObjectStore(), settings=None)
    reconcile_all(db_session, **kw)
    deployments = [m for m in kube_applied(kube) if m["kind"] == "Deployment"]
    assert deployments and deployments[-1]["spec"]["replicas"] == 0
    db_session.refresh(row)
    assert row.status == "suspended"
    # owner cannot edit or unsuspend
    r = user.patch(f"/apps/{app_id}", json={"name": "x"})
    assert r.status_code in (403,)
    # user is suspended so cookie is rejected first; check app-level gate via admin unsuspend of user
    admin.post(f"/admin/users/{uid}/unsuspend")
    assert db_session.get(models.App, app_id).status == "pending"


def kube_applied(kube):
    for attr in ("applied", "manifests", "calls"):
        if hasattr(kube, attr):
            v = getattr(kube, attr)
            if isinstance(v, dict):
                v = [m for ms in v.values() for m in (ms if isinstance(ms, list) else [ms])]
            return [m for m in v if isinstance(m, dict)]
    raise AssertionError("fake kube exposes no applied manifests")


def test_owner_cannot_edit_suspended_app(make_client, db_session):
    admin, user, uid = _setup(make_client)
    admin.post(f"/admin/users/{uid}/approve")
    app_id = user.post("/apps", json=APP).json()["id"]
    assert admin.post(f"/admin/apps/{app_id}/suspend").status_code == 200
    r = user.patch(f"/apps/{app_id}", json={"name": "x"})
    assert r.status_code == 403 and r.json()["detail"] .startswith("This app is suspended by an admin")
    assert admin.post(f"/admin/apps/{app_id}/unsuspend").json()["status"] == "pending"
    assert user.patch(f"/apps/{app_id}", json={"name": "x"}).status_code == 200


def test_delete_user_cleans_apps_and_keys(make_client, db_session):
    admin, user, uid = _setup(make_client)
    admin.post(f"/admin/users/{uid}/approve")
    app_id = user.post("/apps", json=APP).json()["id"]
    raw = user.post("/api/keys").json()["key"]
    assert admin.delete(f"/admin/users/{uid}").status_code == 204
    assert db_session.get(models.App, app_id).deleted_at is not None
    assert all(k.revoked_at is not None for k in db_session.query(models.ApiKey).all())
    assert _api_key_client(make_client, raw).get("/apps").status_code == 401
    assert user.get("/auth/me").status_code == 401
    assert make_client().post("/auth/login", json={"email": "user@example.com", "password": DEFAULT_PASSWORD}).status_code == 401
    assert uid not in [u["id"] for u in admin.get("/admin/users").json()]


def test_admin_routes_forbidden_for_non_admin(make_client):
    admin, user, uid = _setup(make_client)
    admin.post(f"/admin/users/{uid}/approve")
    for method, path in [("get", "/admin/users"), ("post", f"/admin/users/{uid}/approve"),
                         ("post", f"/admin/users/{uid}/suspend"), ("post", f"/admin/users/{uid}/unsuspend"),
                         ("delete", f"/admin/users/{uid}"), ("get", "/admin/apps"),
                         ("post", "/admin/apps/1/suspend"), ("post", "/admin/apps/1/unsuspend"),
                         ("delete", "/admin/apps/1"), ("get", "/admin/audit")]:
        assert getattr(user, method)(path).status_code == 403, path


def test_platform_admin_cannot_be_suspended_or_deleted(make_client):
    admin, _, _ = _setup(make_client)
    aid = next(u["id"] for u in admin.get("/admin/users").json() if u["role"] == "platform_admin")
    assert admin.post(f"/admin/users/{aid}/suspend").status_code == 409  # last active admin
    assert admin.delete(f"/admin/users/{aid}").status_code == 409
    assert admin.get("/auth/me").json()["status"] == "active"


def test_admin_apps_and_delete(make_client):
    admin, user, uid = _setup(make_client)
    admin.post(f"/admin/users/{uid}/approve")
    app_id = user.post("/apps", json=APP).json()["id"]
    rows = admin.get("/admin/apps").json()
    assert rows[0]["owner_email"] == "user@example.com" and rows[0]["id"] == app_id
    assert set(rows[0]) == {"id", "name", "hostname", "status", "owner_id", "owner_email", "image", "created_at",
                           "resources", "exceeds_platform_caps", "replicas", "volume", "want_db", "want_storage", "db_engine",
                            "platform_host", "custom_domain", "restarts_app"}
    assert admin.delete(f"/admin/apps/{app_id}").status_code == 204
    assert admin.get("/admin/apps").json() == []


def test_audit_entries_and_no_secrets(make_client):
    admin, user, uid = _setup(make_client)
    make_client().post("/auth/login", json={"email": "user@example.com", "password": "not-the-password"})
    admin.post(f"/admin/users/{uid}/approve")
    app = dict(APP, env=[{"key": "TOK", "value": "super-secret-value", "is_secret": True}])
    app_id = user.post("/apps", json=app).json()["id"]
    key = user.post("/api/keys").json()
    admin.post(f"/admin/apps/{app_id}/suspend")
    entries = admin.get("/admin/audit?limit=100").json()
    actions = [e["action"] for e in entries]
    for a in ("user.register", "login.failed", "user.approve", "app.create", "apikey.create", "app.suspend"):
        assert a in actions, a
    assert entries[0]["id"] > entries[-1]["id"]
    assert len(admin.get("/admin/audit?limit=2").json()) == 2
    blob = str(entries)
    for secret in ("not-the-password", DEFAULT_PASSWORD, "super-secret-value", key["key"]):
        assert secret not in blob


def test_audit_never_raises(db_session):
    from app import audit

    class Boom:
        def add(self, *_):
            raise RuntimeError("db down")

        def rollback(self):
            pass

    audit.record(Boom(), None, "x")  # must not raise


def test_owner_cannot_delete_suspended_app_admin_can(make_client, db_session):
    admin, user, uid = _setup(make_client)
    admin.post(f"/admin/users/{uid}/approve")
    app_id = user.post("/apps", json=APP).json()["id"]
    admin.post(f"/admin/apps/{app_id}/suspend")
    r = user.delete(f"/apps/{app_id}")
    assert r.status_code == 403 and r.json()["detail"] .startswith("This app is suspended by an admin")
    assert admin.delete(f"/admin/apps/{app_id}").status_code == 204


def test_user_unsuspend_keeps_individually_suspended_apps(make_client, db_session):
    admin, user, uid = _setup(make_client)
    admin.post(f"/admin/users/{uid}/approve")
    db_session.get(models.User, uid).app_limit = 5
    db_session.commit()
    bad = user.post("/apps", json=APP).json()["id"]
    good = user.post("/apps", json=dict(APP, name="other")).json()["id"]
    admin.post(f"/admin/apps/{bad}/suspend")
    admin.post(f"/admin/users/{uid}/suspend")
    admin.post(f"/admin/users/{uid}/unsuspend")
    db_session.expire_all()
    assert db_session.get(models.App, bad).status == "suspended"
    assert db_session.get(models.App, good).status == "pending"
    assert admin.post(f"/admin/apps/{bad}/unsuspend").json()["status"] == "pending"


def test_first_user_race_only_one_platform_admin(db_session):
    from app.routers.auth import create_user
    # Both registrants saw an empty users table; only one may claim the platform_init slot.
    a = create_user(db_session, "a@example.com", "Password123!x", first_hint=True)
    b = create_user(db_session, "b@example.com", "Password123!x", first_hint=True)
    assert (a.role, a.status) == ("platform_admin", "active")
    assert (b.role, b.status) == ("user", "pending")
    assert db_session.query(models.User).filter_by(role="platform_admin").count() == 1


def _cert_pem():
    import datetime as dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_admin_user_patch_and_dbserver_audited(make_client):
    admin, user, uid = _setup(make_client)
    assert admin.patch(f"/admin/users/{uid}", json={"app_limit": 5}).status_code == 200
    body = {"name": "second", "host": "h", "admin_user": "root", "admin_password": "adm-pw-xyz",
            "max_databases": 5, "ca_cert": _cert_pem()}
    r = admin.post("/admin/db-servers", json=body)
    assert r.status_code == 201
    sid = r.json()["id"]
    assert admin.patch(f"/admin/db-servers/{sid}", json={"status": "draining"}).status_code == 200
    entries = admin.get("/admin/audit?limit=100").json()
    actions = [e["action"] for e in entries]
    for a in ("user.update", "dbserver.create", "dbserver.update"):
        assert a in actions, a
    blob = str(entries)
    assert "adm-pw-xyz" not in blob and "BEGIN CERTIFICATE" not in blob


def test_patch_deleted_user_404(make_client):
    admin, user, uid = _setup(make_client)
    assert admin.delete(f"/admin/users/{uid}").status_code == 204
    assert admin.patch(f"/admin/users/{uid}", json={"app_limit": 3}).status_code == 404


def test_dbserver_ca_cert_validation(make_client, db_session):
    admin, user, uid = _setup(make_client)
    body = {"name": "s2", "host": "h", "admin_user": "root", "admin_password": "x", "max_databases": 5}
    assert admin.post("/admin/db-servers", json={**body, "ca_cert": "not a cert"}).status_code == 422
    pem = _cert_pem()
    sid = admin.post("/admin/db-servers", json={**body, "ca_cert": pem}).json()["id"]
    assert db_session.get(models.DbServer, sid).ca_cert == pem
    assert admin.patch(f"/admin/db-servers/{sid}", json={"ca_cert": "junk"}).status_code == 422
    admin.patch(f"/admin/db-servers/{sid}", json={"ca_cert": ""})
    db_session.expire_all()
    assert db_session.get(models.DbServer, sid).ca_cert is None
    admin.patch(f"/admin/db-servers/{sid}", json={"ca_cert": pem})
    db_session.expire_all()
    assert db_session.get(models.DbServer, sid).ca_cert == pem
