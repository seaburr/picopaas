from tests.conftest import DEFAULT_PASSWORD, register_and_login


def test_first_user_becomes_platform_admin(client):
    resp = register_and_login(client, "first@example.com")
    body = resp.json()
    assert body["role"] == "platform_admin"
    assert body["app_limit"] == 1


def test_second_user_is_plain_user(make_client):
    register_and_login(make_client(), "first@example.com")
    resp = register_and_login(make_client(), "second@example.com")
    body = resp.json()
    assert body["role"] == "user"


def test_register_rejects_duplicate_email(client):
    register_and_login(client, "dupe@example.com")
    resp = client.post(
        "/auth/register", json={"email": "dupe@example.com", "password": DEFAULT_PASSWORD, "accept_terms": True}
    )
    # Enumeration-safe: looks exactly like a fresh pending signup (see tests/test_ux_backend.py).
    assert resp.status_code == 201 and resp.json()["status"] == "pending"


def test_login_real_flow_sets_session_cookie(make_client):
    register_client = make_client()
    register_and_login(register_client, "login@example.com")

    fresh_client = make_client()
    resp = fresh_client.post(
        "/auth/login", json={"email": "login@example.com", "password": DEFAULT_PASSWORD}
    )
    assert resp.status_code == 200
    assert "session" in resp.cookies

    me = fresh_client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "login@example.com"


def test_login_rejects_wrong_password(client):
    register_and_login(client, "wrongpass@example.com")
    resp = client.post(
        "/auth/login", json={"email": "wrongpass@example.com", "password": "not-the-password"}
    )
    assert resp.status_code == 401


def test_me_requires_authentication(client):
    resp = client.get("/auth/me")
    assert resp.status_code == 401


def test_logout_clears_session(client):
    register_and_login(client, "logout@example.com")
    assert client.get("/auth/me").status_code == 200

    resp = client.post("/auth/logout")
    assert resp.status_code == 204

    assert client.get("/auth/me").status_code == 401


def test_register_requires_accept_terms(client):
    base = {"email": "t@example.com", "password": DEFAULT_PASSWORD}
    assert client.post("/auth/register", json=base).status_code == 422
    r = client.post("/auth/register", json={**base, "accept_terms": False})
    assert r.status_code == 422 and "Accept the Privacy Policy" in r.text
    assert client.post("/auth/register", json={**base, "accept_terms": True, "x": 1}).status_code == 422


def test_terms_accepted_at_stored_and_listed_for_admin(make_client):
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    register_and_login(make_client(), "u@example.com")
    users = admin.get("/admin/users").json()
    assert all(u["terms_accepted_at"] for u in users)


def test_existing_users_have_null_terms(make_client, db_session):
    from app import models
    from app.routers.auth import create_user
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    create_user(db_session, "old@example.com", "Password123!x", first_hint=False)
    old = next(u for u in admin.get("/admin/users").json() if u["email"] == "old@example.com")
    assert old["terms_accepted_at"] is None
