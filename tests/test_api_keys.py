"""Owner-scoped API key endpoints (app/routers/api_keys.py)."""
from app.config import API_KEY_PREFIX
from tests.conftest import register_and_login


def test_create_api_key_returns_plaintext_once(client):
    register_and_login(client, "keys@example.com")
    resp = client.post("/api/keys")
    assert resp.status_code == 201
    body = resp.json()
    assert body["key"].startswith(API_KEY_PREFIX)
    assert body["prefix"] in body["key"]
    assert "id" in body and "created_at" in body


def test_list_shows_prefix_and_created_revoked_only(client):
    register_and_login(client, "listkeys@example.com")
    created = client.post("/api/keys")
    raw_key = created.json()["key"]

    listed = client.get("/api/keys")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"id", "prefix", "created_at", "revoked_at"}
    assert row["revoked_at"] is None
    assert raw_key not in listed.text


def test_revoke_api_key(client):
    register_and_login(client, "revoke@example.com")
    created = client.post("/api/keys")
    key_id = created.json()["id"]

    resp = client.delete(f"/api/keys/{key_id}")
    assert resp.status_code == 204

    listed = client.get("/api/keys").json()
    assert listed[0]["revoked_at"] is not None


def test_cross_user_revoke_returns_404(make_client):
    owner = make_client()
    register_and_login(owner, "keyowner@example.com")
    created = owner.post("/api/keys")
    key_id = created.json()["id"]

    intruder = make_client()
    register_and_login(intruder, "keyintruder@example.com")

    resp = intruder.delete(f"/api/keys/{key_id}")
    assert resp.status_code == 404

    # Unaffected for the real owner.
    assert owner.get("/api/keys").json()[0]["revoked_at"] is None


def test_revoked_key_cannot_authenticate(make_client):
    c = make_client()
    register_and_login(c, "revokedauth@example.com")
    created = c.post("/api/keys")
    raw_key = created.json()["key"]
    key_id = created.json()["id"]

    # A fresh client (no session cookie) using the raw key as a Bearer token works.
    bearer_client = make_client()
    ok = bearer_client.get("/auth/me", headers={"Authorization": f"Bearer {raw_key}"})
    assert ok.status_code == 200

    c.delete(f"/api/keys/{key_id}")

    denied = bearer_client.get("/auth/me", headers={"Authorization": f"Bearer {raw_key}"})
    assert denied.status_code == 401
