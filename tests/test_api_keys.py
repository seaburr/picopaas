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
    assert set(row) == {"id", "prefix", "name", "scope", "created_at", "last_used_at", "revoked_at"}
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


def test_name_round_trips_and_is_trimmed(client):
    register_and_login(client, "named@example.com")
    resp = client.post("/api/keys", json={"name": "  github-actions  "})
    assert resp.status_code == 201
    assert resp.json()["name"] == "github-actions"
    assert client.get("/api/keys").json()[0]["name"] == "github-actions"


def test_bodyless_create_has_null_name(client):
    register_and_login(client, "noname@example.com")
    assert client.post("/api/keys").json()["name"] is None
    assert client.post("/api/keys", json={"name": "   "}).json()["name"] is None


def test_bad_names_rejected(client):
    register_and_login(client, "badname@example.com")
    assert client.post("/api/keys", json={"name": "a" * 65}).status_code == 422
    assert client.post("/api/keys", json={"name": "bad\nname"}).status_code == 422
    assert client.post("/api/keys", json={"name": "x", "extra": 1}).status_code == 422


def test_scope_defaults_to_full_and_round_trips(client):
    register_and_login(client, "scopedefault@example.com")
    assert client.post("/api/keys").json()["scope"] == "full"
    assert client.post("/api/keys", json={"scope": "read"}).json()["scope"] == "read"
    scopes = {r["scope"] for r in client.get("/api/keys").json()}
    assert scopes == {"full", "read"}


def test_invalid_scope_rejected(client):
    register_and_login(client, "scopebad@example.com")
    assert client.post("/api/keys", json={"scope": "admin"}).status_code == 422
    assert client.post("/api/keys", json={"scope": "deploy"}).status_code == 422


def test_read_key_can_read_but_not_write(make_client):
    owner = make_client()
    register_and_login(owner, "readkey@example.com")
    read_key = owner.post("/api/keys", json={"scope": "read"}).json()["key"]
    full_key = owner.post("/api/keys", json={"scope": "full"}).json()["key"]

    reader = make_client()
    rhdr = {"Authorization": f"Bearer {read_key}"}
    # Reads work.
    assert reader.get("/auth/me", headers=rhdr).status_code == 200
    assert reader.get("/apps", headers=rhdr).status_code == 200
    # Writes are refused (403), with a message pointing at a full key.
    denied = reader.post("/apps", headers=rhdr, json={"name": "x", "image": "nginx:alpine", "port": 8080})
    assert denied.status_code == 403
    assert "read-only" in denied.json()["detail"].lower()

    # A full key from the same account can write.
    writer = make_client()
    fhdr = {"Authorization": f"Bearer {full_key}"}
    assert writer.post("/apps", headers=fhdr, json={"name": "fw", "image": "nginx:alpine", "port": 8080}).status_code in (200, 201)


def test_api_key_cannot_manage_keys(make_client):
    """A key (any scope) can never mint or revoke keys — that's browser-session only (leaked-key containment)."""
    owner = make_client()
    register_and_login(owner, "nomintkey@example.com")
    full_key = owner.post("/api/keys", json={"scope": "full"}).json()["key"]
    existing_id = owner.get("/api/keys").json()[0]["id"]

    bearer = make_client()
    hdr = {"Authorization": f"Bearer {full_key}"}
    # Full scope passes the read/write gate, but key management still requires a session.
    minted = bearer.post("/api/keys", headers=hdr, json={"scope": "full"})
    assert minted.status_code == 403
    assert "web ui" in minted.json()["detail"].lower()
    # ...and can't revoke either.
    assert bearer.delete(f"/api/keys/{existing_id}", headers=hdr).status_code == 403
    # Listing (a read) is still fine.
    assert bearer.get("/api/keys", headers=hdr).status_code == 200


def test_last_used_at_set_and_throttled(client):
    from datetime import datetime, timedelta, timezone

    from app import models
    from app.db import get_db
    from app.main import app

    register_and_login(client, "used@example.com")
    raw = client.post("/api/keys").json()["key"]
    assert client.get("/api/keys").json()[0]["last_used_at"] is None
    client.cookies.clear()
    hdr = {"Authorization": f"Bearer {raw}"}

    assert client.get("/auth/me", headers=hdr).status_code == 200
    db = next(app.dependency_overrides[get_db]())
    try:
        row = db.query(models.ApiKey).one()
        first = row.last_used_at
        assert first is not None
        # Within the throttle window: no further write.
        assert client.get("/auth/me", headers=hdr).status_code == 200
        db.refresh(row)
        assert row.last_used_at == first
        # Older than the window: updated.
        row.last_used_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.commit()
        old = row.last_used_at
        assert client.get("/auth/me", headers=hdr).status_code == 200
        db.refresh(row)
        assert row.last_used_at > old
    finally:
        db.close()


def test_admin_lists_and_revokes_user_keys(make_client):
    from app import models
    from app.db import get_db
    from app.main import app

    admin = make_client()
    register_and_login(admin, "keyadmin@example.com")
    user = make_client()
    register_and_login(user, "keyuser@example.com")
    other = make_client()
    register_and_login(other, "keyother@example.com")
    created = user.post("/api/keys", json={"name": "ci", "scope": "read"}).json()
    raw, kid = created["key"], created["id"]
    other_kid = other.post("/api/keys").json()["id"]
    uid = next(u["id"] for u in admin.get("/admin/users").json() if u["email"] == "keyuser@example.com")
    users = {u["email"]: u for u in admin.get("/admin/users").json()}
    assert users["keyuser@example.com"]["api_key_count"] == 1

    # last_used_at is visible to the admin after the key is used
    bearer = make_client()
    hdr = {"Authorization": f"Bearer {raw}"}
    assert bearer.get("/auth/me", headers=hdr).status_code == 200
    rows = admin.get(f"/admin/users/{uid}/api-keys").json()
    assert len(rows) == 1 and set(rows[0]) == {"id", "prefix", "name", "scope", "created_at", "last_used_at", "revoked_at"}
    assert rows[0]["scope"] == "read" and rows[0]["last_used_at"] is not None
    assert raw not in str(rows)

    # non-admin: 403; key of another user: 404
    assert user.get(f"/admin/users/{uid}/api-keys").status_code == 403
    assert user.delete(f"/admin/users/{uid}/api-keys/{kid}").status_code == 403
    assert admin.delete(f"/admin/users/{uid}/api-keys/{other_kid}").status_code == 404
    assert admin.get("/admin/users/99999/api-keys").status_code == 404

    # an admin API key cannot revoke (session only)
    araw = admin.post("/api/keys").json()["key"]
    keyed = make_client()
    assert keyed.delete(f"/admin/users/{uid}/api-keys/{kid}", headers={"Authorization": f"Bearer {araw}"}).status_code == 403

    assert admin.delete(f"/admin/users/{uid}/api-keys/{kid}").status_code == 204
    assert admin.delete(f"/admin/users/{uid}/api-keys/{kid}").status_code == 204
    assert bearer.get("/auth/me", headers=hdr).status_code == 401
    assert admin.get(f"/admin/users/{uid}/api-keys").json()[0]["revoked_at"] is not None
    assert user.get("/api/keys").json()[0]["revoked_at"] is not None
    db = next(app.dependency_overrides[get_db]())
    try:
        assert db.query(models.AuditLog).filter(models.AuditLog.action == "apikey.admin_revoke").count() == 1
    finally:
        db.close()
