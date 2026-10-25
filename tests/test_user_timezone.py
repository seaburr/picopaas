from tests.conftest import register_and_login


def _login(make_client):
    c = make_client()
    register_and_login(c, "tz@example.com")
    return c


def test_timezone_default_null(make_client):
    c = _login(make_client)
    assert c.get("/auth/me").json()["timezone"] is None


def test_set_valid_and_reflected(make_client):
    c = _login(make_client)
    r = c.patch("/auth/me", json={"timezone": "America/New_York"})
    assert r.status_code == 200, r.text
    assert r.json()["timezone"] == "America/New_York"
    assert "features" in r.json()
    assert c.get("/auth/me").json()["timezone"] == "America/New_York"
    assert c.patch("/auth/me", json={"timezone": "UTC"}).json()["timezone"] == "UTC"


def test_set_null(make_client):
    c = _login(make_client)
    c.patch("/auth/me", json={"timezone": "Europe/Paris"})
    r = c.patch("/auth/me", json={"timezone": None})
    assert r.status_code == 200 and r.json()["timezone"] is None
    assert c.get("/auth/me").json()["timezone"] is None


def test_invalid_422(make_client):
    c = _login(make_client)
    for bad in ("Mars/Olympus", "", "america/new_york"):
        r = c.patch("/auth/me", json={"timezone": bad})
        assert r.status_code == 422, bad
        assert "Unknown timezone" in r.text
    assert c.get("/auth/me").json()["timezone"] is None


def test_unauthenticated_401(client):
    assert client.patch("/auth/me", json={"timezone": "UTC"}).status_code == 401
