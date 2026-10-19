from tests.conftest import DEFAULT_PASSWORD, register_and_login


def test_first_user_becomes_uberadmin(client):
    resp = register_and_login(client, "first@example.com")
    body = resp.json()
    assert body["role"] == "uberadmin"
    assert body["app_limit"] == 1


def test_second_user_is_plain_user(make_client):
    register_and_login(make_client(), "first@example.com")
    resp = register_and_login(make_client(), "second@example.com")
    body = resp.json()
    assert body["role"] == "user"


def test_register_rejects_duplicate_email(client):
    register_and_login(client, "dupe@example.com")
    resp = client.post(
        "/auth/register", json={"email": "dupe@example.com", "password": DEFAULT_PASSWORD}
    )
    assert resp.status_code == 409


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
