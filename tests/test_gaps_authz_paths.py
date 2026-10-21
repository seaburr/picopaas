"""Error paths and authz denials that were uncovered: session-token hardening, suspended-app guards on every
mutating route, admin state-transition conflicts, provider failures surfacing as safe errors."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app import config, models, security
from app.config import SESSION_COOKIE_NAME
from app.dependencies import get_kube
from app.main import app as fastapi_app
from app.providers.base import MySQLAdminError
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.routers.apps import get_mysql_admin_factory, get_object_store
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
SPEC = "apiVersion: picopaas/v1\nname: web\nimage: ghcr.io/example/demo:1.0\nport: 8080\n"


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


# --- security.py -------------------------------------------------------------------------------------------

def test_password_hash_roundtrip_and_malformed_hash():
    h = security.hash_password("not-the-password")
    assert security.verify_password("not-the-password", h) and not security.verify_password("other", h)
    assert security.verify_password("x", "not-a-bcrypt-hash") is False  # malformed stored hash must not raise


def test_token_roundtrip_and_rejections():
    assert security.decode_access_token(security.create_access_token(42)) == 42
    assert security.decode_access_token("garbage") is None
    assert security.decode_access_token("") is None
    expired = jwt.encode({"sub": "1", "exp": datetime.now(timezone.utc) - timedelta(seconds=5)},
                         config.SECRET_KEY, algorithm=config.JWT_ALGORITHM)
    assert security.decode_access_token(expired) is None
    wrong_key = jwt.encode({"sub": "1", "exp": datetime.now(timezone.utc) + timedelta(days=1)},
                           "some-other-signing-key-0123456789abcdef", algorithm=config.JWT_ALGORITHM)
    assert security.decode_access_token(wrong_key) is None
    for sub in ("abc", None):
        payload = {"exp": datetime.now(timezone.utc) + timedelta(days=1)}
        if sub is not None:
            payload["sub"] = sub
        assert security.decode_access_token(jwt.encode(payload, config.SECRET_KEY, algorithm=config.JWT_ALGORITHM)) is None


def test_alg_none_token_is_rejected():
    forged = jwt.encode({"sub": "1", "exp": datetime.now(timezone.utc) + timedelta(days=1)}, None, algorithm="none")
    assert security.decode_access_token(forged) is None


def test_forged_cookie_for_existing_user_is_401(admin, make_client):
    c = make_client()
    c.cookies.set(SESSION_COOKIE_NAME, "x.y.z")
    assert c.get("/apps").status_code == 401


def test_session_for_deleted_user_stops_working(admin, user, db_session):
    assert user.get("/apps").status_code == 200
    db_session.query(models.User).filter(models.User.email == "user@example.com").one().status = "deleted"
    db_session.commit()
    assert user.get("/apps").status_code == 401


# --- suspended-app guards on every mutating owner route ------------------------------------------------------

def _suspended(admin, user):
    aid = user.post("/apps", json=APP).json()["id"]
    assert admin.post(f"/admin/apps/{aid}/suspend").status_code == 200
    return aid


def test_suspended_app_blocks_owner_mutations_but_not_reads(admin, user, db_session):
    aid = _suspended(admin, user)
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda s: FakeMySQLAdmin())
    fastapi_app.dependency_overrides[get_object_store] = lambda: FakeObjectStore()
    try:
        assert user.patch(f"/apps/{aid}", json={"port": 9000}).status_code == 403
        assert user.delete(f"/apps/{aid}").status_code == 403
        assert user.post(f"/apps/{aid}/restart").status_code == 409
        assert user.post(f"/apps/{aid}/check-image").status_code == 409
        assert user.post(f"/apps/{aid}/rollback", json={"digest": "sha256:" + "0" * 64}).status_code == 409
        assert user.post(f"/apps/{aid}/rotate-credentials", json={"target": "db"}).status_code == 403
        assert user.put(f"/apps/{aid}/domain", json={"host": "a.example.org"}).status_code == 403
        r = user.post("/apps/spec", json={"spec": SPEC, "secrets": {}})
        assert r.status_code == 403 and "suspended" in r.text
    finally:
        fastapi_app.dependency_overrides.pop(get_mysql_admin_factory, None)
        fastapi_app.dependency_overrides.pop(get_object_store, None)
    db_session.expire_all()
    row = db_session.get(models.App, aid)
    assert row.status == "suspended" and row.port == 8080 and row.deleted_at is None
    assert user.get(f"/apps/{aid}").status_code == 200  # still readable


# --- admin state-transition conflicts ------------------------------------------------------------------------

def test_unsuspend_of_running_app_is_409_and_unknown_is_404(admin, user):
    aid = user.post("/apps", json=APP).json()["id"]
    assert admin.post(f"/admin/apps/{aid}/unsuspend").status_code == 409
    assert admin.post("/admin/apps/99999/unsuspend").status_code == 404
    assert admin.post("/admin/apps/99999/suspend").status_code == 404
    assert admin.delete("/admin/apps/99999").status_code == 404


def test_approve_conflicts(admin, make_client, db_session):
    p = make_client()
    register_and_login(p, "pending@example.com", approve=False)
    uid = db_session.query(models.User).filter(models.User.email == "pending@example.com").one().id
    assert admin.post(f"/admin/users/{uid}/approve").status_code == 200
    r = admin.post(f"/admin/users/{uid}/approve")
    assert r.status_code == 409 and "already approved" in r.json()["detail"]
    row = db_session.get(models.User, uid)
    row.status = "suspended"
    db_session.commit()
    r = admin.post(f"/admin/users/{uid}/approve")
    assert r.status_code == 409 and "suspended" in r.json()["detail"]


def test_patch_unknown_db_server_is_404(admin):
    assert admin.patch("/admin/db-servers/99999", json={"status": "draining"}).status_code == 404


def test_non_admin_denied_on_admin_state_changes(admin, user):
    aid = user.post("/apps", json=APP).json()["id"]
    for method, path in (("post", f"/admin/apps/{aid}/suspend"), ("post", f"/admin/apps/{aid}/unsuspend"),
                         ("delete", f"/admin/apps/{aid}"), ("post", "/admin/users/1/approve"),
                         ("patch", "/admin/db-servers/1"), ("get", "/admin/audit")):
        assert getattr(user, method)(path).status_code == 403, path


# --- provider failure paths ----------------------------------------------------------------------------------

class _ExplodingKube(FakeKubeProvider):
    def pod_logs(self, namespace, tail):
        raise RuntimeError("apiserver says: token=super-secret-value")


def test_logs_failure_returns_a_safe_note_not_the_exception(user):
    aid = user.post("/apps", json=APP).json()["id"]
    fastapi_app.dependency_overrides[get_kube] = lambda: _ExplodingKube()
    try:
        r = user.get(f"/apps/{aid}/logs")
    finally:
        fastapi_app.dependency_overrides.pop(get_kube, None)
    assert r.status_code == 200 and r.json()["lines"] == [] and "super-secret" not in r.text
    assert "Couldn't read the logs" in r.json()["note"]


def test_logs_tail_is_capped_and_validated(user):
    aid = user.post("/apps", json=APP).json()["id"]
    kube = FakeKubeProvider()
    fastapi_app.dependency_overrides[get_kube] = lambda: kube
    try:
        assert user.get(f"/apps/{aid}/logs?tail=0").status_code == 422
        assert user.get(f"/apps/{aid}/logs?tail=99999").status_code == 200
    finally:
        fastapi_app.dependency_overrides.pop(get_kube, None)
    assert kube.log_calls[-1] == (f"app-{aid}", 1000)


def test_logs_other_users_app_is_404_but_admin_can_read(admin, user, make_client):
    aid = user.post("/apps", json=APP).json()["id"]
    stranger = make_client()
    register_and_login(stranger, "stranger@example.com")
    fastapi_app.dependency_overrides[get_kube] = lambda: FakeKubeProvider()
    try:
        assert stranger.get(f"/apps/{aid}/logs").status_code == 404
        assert admin.get(f"/apps/{aid}/logs").status_code == 200
    finally:
        fastapi_app.dependency_overrides.pop(get_kube, None)


def test_db_rotation_failure_restores_old_password_and_is_502(user, db_session):
    from app import crypto

    aid = user.post("/apps", json={**APP, "name": "dbapp", "want_db": True}).json()["id"]
    server = models.DbServer(name="p", host="h", port=3306, admin_user="r", max_databases=5,
                             admin_password_encrypted=crypto.encrypt("x"))
    db_session.add(server)
    db_session.commit()
    db_session.add(models.AppDatabase(app_id=aid, db_server_id=server.id, db_name="d", db_user="u",
                                      password_encrypted=crypto.encrypt("orig-pw")))
    db_session.commit()

    class Failing(FakeMySQLAdmin):
        def set_password(self, app_id, pw):
            raise MySQLAdminError("ALTER USER failed: password=" + pw)

    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda s: Failing())
    fastapi_app.dependency_overrides[get_object_store] = lambda: FakeObjectStore()
    try:
        r = user.post(f"/apps/{aid}/rotate-credentials", json={"target": "db"})
    finally:
        fastapi_app.dependency_overrides.pop(get_mysql_admin_factory, None)
        fastapi_app.dependency_overrides.pop(get_object_store, None)
    assert r.status_code == 502
    db_session.expire_all()
    assert crypto.decrypt(db_session.get(models.App, aid).database.password_encrypted) == "orig-pw"
    assert "password=" not in r.text


def test_rotate_before_db_exists_is_409(user):
    aid = user.post("/apps", json={**APP, "name": "dbapp", "want_db": True}).json()["id"]
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda s: FakeMySQLAdmin())
    fastapi_app.dependency_overrides[get_object_store] = lambda: FakeObjectStore()
    try:
        assert user.post(f"/apps/{aid}/rotate-credentials", json={"target": "db"}).status_code == 409
        assert user.post(f"/apps/{aid}/rotate-credentials", json={"target": "nope"}).status_code == 422
    finally:
        fastapi_app.dependency_overrides.pop(get_mysql_admin_factory, None)
        fastapi_app.dependency_overrides.pop(get_object_store, None)


def test_hostname_reservation_race_is_409(user, monkeypatch):
    """If the unique index rejects the generated hostname at commit, the user gets a retryable 409, not a 500."""
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    real = Session.commit
    state = {"n": 0}

    def flaky(self):
        if self.new and state["n"] == 0:
            state["n"] += 1
            raise IntegrityError("insert", {}, Exception("dup"))
        return real(self)

    monkeypatch.setattr(Session, "commit", flaky)
    r = user.post("/apps", json=APP)
    assert r.status_code == 409 and "Try again" in r.json()["detail"]
