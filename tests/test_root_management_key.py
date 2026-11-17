"""ROOT_MANAGEMENT_KEY: optional break-glass key that resets a platform admin's password (app/routers/root.py).
Real register/login flow; the key is patched into app.config for the test."""
import importlib

import pytest

from app import config, models, ratelimit
from tests.conftest import DEFAULT_PASSWORD, register_and_login

ROOT = "root-management-fixture-value-0123456789"
URL = "/root/admin-reset"


@pytest.fixture()
def root_key(monkeypatch):
    monkeypatch.setattr(config, "ROOT_MANAGEMENT_KEY", ROOT)
    return {"Authorization": f"Bearer {ROOT}"}


def _audit(db, action):
    return db.query(models.AuditLog).filter_by(action=action).all()


def _login(c, email, password):
    return c.post("/auth/login", json={"email": email, "password": password})


def test_off_by_default_is_404(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    assert config.ROOT_MANAGEMENT_KEY == ""
    for headers in ({}, {"Authorization": f"Bearer {ROOT}"}, {"Authorization": "Bearer "}):
        r = make_client().post(URL, headers=headers)
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer wrong-key-wrong-key-wrong-key-wrong-key"}, {"Authorization": ROOT},
    {"Authorization": f"Basic {ROOT}"}, {"Authorization": f"Bearer {ROOT}x"}, {"Authorization": f"Bearer {ROOT[:-1]}"},
    {"Authorization": "Bearer "}, {"X-Root-Key": ROOT},
])
def test_wrong_or_missing_key_is_401_and_changes_nothing(make_client, db_session, root_key, headers):
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    r = make_client().post(URL, headers=headers, json={"email": "admin@example.com"})
    assert r.status_code == 401 and "temporary_password" not in r.text and ROOT not in r.text
    assert _login(make_client(), "admin@example.com", DEFAULT_PASSWORD).status_code == 200
    rows = _audit(db_session, "root.auth_failed")
    assert len(rows) == 1 and rows[0].actor_email == "root-management-key" and ROOT not in (rows[0].detail or "")


def test_admin_session_or_api_key_is_not_the_root_key(make_client, root_key):
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    assert admin.post(URL).status_code == 401  # a logged-in platform admin without the key
    key = admin.post("/api/keys", json={"name": "k"}).json()
    token = next(v for v in key.values() if isinstance(v, str) and v.startswith(config.API_KEY_PREFIX) and len(v) > 20)
    assert make_client().post(URL, headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_resets_the_only_admin(make_client, db_session, root_key):
    admin, user = make_client(), make_client()
    register_and_login(admin, "admin@example.com")
    register_and_login(user, "user@example.com")
    r = make_client().post(URL, headers=root_key)  # no body: exactly one admin
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    out = r.json()
    assert out["email"] == "admin@example.com" and len(out["temporary_password"]) >= 24
    assert _login(make_client(), "admin@example.com", DEFAULT_PASSWORD).status_code == 401
    c = make_client()
    assert _login(c, "admin@example.com", out["temporary_password"]).status_code == 200
    me = c.get("/auth/me").json()
    assert me["must_change_password"] is True and me["role"] == "platform_admin"
    assert _login(make_client(), "user@example.com", DEFAULT_PASSWORD).status_code == 200  # others untouched
    rows = _audit(db_session, "user.reset_password")
    assert len(rows) == 1 and rows[0].actor_email == "root-management-key" and rows[0].actor_id is None
    assert "via=root-key" in rows[0].detail and out["temporary_password"] not in rows[0].detail


def test_only_active_platform_admins_can_be_targeted(make_client, db_session, root_key):
    admin, user = make_client(), make_client()
    register_and_login(admin, "admin@example.com")
    register_and_login(user, "user@example.com")
    for email in ("user@example.com", "nobody@example.com"):  # same answer: no account probing beyond admins
        r = make_client().post(URL, headers=root_key, json={"email": email})
        assert r.status_code == 404 and r.json()["detail"] == "No active platform administrator has that email."
    assert _login(make_client(), "user@example.com", DEFAULT_PASSWORD).status_code == 200
    u = db_session.query(models.User).filter_by(email="user@example.com").one()
    assert u.role == "user" and u.must_change_password is False
    assert make_client().post(URL, headers=root_key, json={"email": "admin@example.com", "role": "x"}).status_code == 422


def test_several_admins_need_an_email(make_client, root_key):
    admin, second = make_client(), make_client()
    register_and_login(admin, "admin@example.com")
    register_and_login(second, "second@example.com")
    uid = next(u["id"] for u in admin.get("/admin/users").json() if u["email"] == "second@example.com")
    assert admin.post(f"/admin/users/{uid}/promote").status_code == 200
    r = make_client().post(URL, headers=root_key)
    assert r.status_code == 409 and "admin@example.com" in r.json()["detail"] and "second@example.com" in r.json()["detail"]
    r = make_client().post(URL, headers=root_key, json={"email": " Second@Example.com "})
    assert r.status_code == 200 and r.json()["email"] == "second@example.com"
    assert _login(make_client(), "admin@example.com", DEFAULT_PASSWORD).status_code == 200


def test_no_active_admin_is_409(make_client, db_session, root_key):
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    db_session.query(models.User).update({"status": "suspended"})
    db_session.commit()
    r = make_client().post(URL, headers=root_key, json={})
    assert r.status_code == 409 and "app.manage" in r.json()["detail"]


def test_reset_clears_the_login_lockout_and_wrong_keys_never_block_the_right_one(make_client, db_session, root_key, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "ROOT_KEY_MAX_FAILS", 3)
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    c = make_client()
    for _ in range(config.LOGIN_EMAIL_MAX_FAILS):
        assert _login(c, "admin@example.com", "wrong-password-here").status_code == 401
    assert _login(c, "admin@example.com", DEFAULT_PASSWORD).status_code == 429  # locked out
    bad = {"Authorization": "Bearer " + "n" * 40}
    codes = [make_client().post(URL, headers=bad).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    assert len(_audit(db_session, "root.auth_failed")) == 3  # the flood stops writing audit rows
    r = make_client().post(URL, headers=root_key)
    assert r.status_code == 200, r.text
    assert db_session.get(models.AuthThrottle, ratelimit.email_key("admin@example.com")) is None
    assert _login(make_client(), "admin@example.com", r.json()["temporary_password"]).status_code == 200


def test_short_key_refused_at_startup(monkeypatch):
    monkeypatch.setenv("ROOT_MANAGEMENT_KEY", "too-short")
    with pytest.raises(RuntimeError, match="ROOT_MANAGEMENT_KEY"):
        importlib.reload(config)
    monkeypatch.delenv("ROOT_MANAGEMENT_KEY")
    importlib.reload(config)
    assert config.ROOT_MANAGEMENT_KEY == ""


def test_bad_body_is_404_when_off_and_never_422(make_client, monkeypatch):
    c = make_client()
    assert c.post(URL, json={"email": 5}).status_code == 404  # off: nothing reveals the route
    monkeypatch.setattr(config, "ROOT_MANAGEMENT_KEY", ROOT)
    r = c.post(URL, json={"email": 5})
    assert r.status_code == 401 and "5" not in r.text  # wrong key beats body validation, nothing echoed
