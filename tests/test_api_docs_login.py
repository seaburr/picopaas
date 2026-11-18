"""/docs, /redoc and /openapi.json answer only to a signed-in active user; admin and root routes are never in the schema."""
from fastapi.openapi.utils import get_openapi

from app.main import app as fastapi_app
from tests.conftest import register_and_login

PATHS = ("/docs", "/redoc", "/openapi.json")
NOT_FOUND = {"detail": "Not Found"}


def test_anonymous_and_bogus_bearer_get_the_unknown_path_404(client):
    for p in PATHS:
        r = client.get(p)
        assert (r.status_code, r.json()) == (404, NOT_FOUND), p
        r = client.get(p, headers={"Authorization": "Bearer pp_sk_" + "0" * 64})
        assert (r.status_code, r.json()) == (404, NOT_FOUND), p
        r = client.get(p, headers={"Authorization": "Bearer not-a-key"})
        assert r.status_code == 404, p


def test_session_and_api_keys_get_200(client, make_client):
    register_and_login(client, "owner@example.com")
    full = client.post("/api/keys").json()["key"]
    read = client.post("/api/keys", json={"scope": "read"}).json()["key"]
    anon = make_client()
    for p in PATHS:
        assert client.get(p).status_code == 200, p
        for k in (full, read):
            assert anon.get(p, headers={"Authorization": f"Bearer {k}"}).status_code == 200, p
    r = client.get("/openapi.json")
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["info"]["title"] == "PicoPaaS API"
    assert "swagger" in client.get("/docs").text.lower()
    assert "redoc" in client.get("/redoc").text.lower()


def test_pending_user_does_not_get_the_docs(client):
    register_and_login(client, "admin@example.com")  # first user is the admin
    other = client.__class__(fastapi_app)
    register_and_login(other, "pending@example.com", approve=False)
    assert other.get("/openapi.json").status_code == 404


def test_served_schema_has_no_admin_or_root_paths(client):
    register_and_login(client, "owner@example.com")
    schema = client.get("/openapi.json").json()
    assert schema["paths"], "schema is empty"
    assert not [p for p in schema["paths"] if p.startswith(("/admin", "/root"))]
    assert "/apps" in schema["paths"] or any(p.startswith("/apps") for p in schema["paths"])
    text = client.get("/openapi.json").text
    assert "admin-reset" not in text


def test_every_current_admin_route_is_in_the_unfiltered_schema_but_not_the_served_one(client):
    register_and_login(client, "owner@example.com")
    full = get_openapi(title="t", version="0", routes=fastapi_app.routes)["paths"]
    admin = {p for p in full if p.startswith(("/admin", "/root"))}
    assert len(admin) > 20
    served = set(client.get("/openapi.json").json()["paths"])
    assert not admin & served


def test_a_new_admin_router_is_covered_automatically(client):
    from fastapi import APIRouter

    r = APIRouter(prefix="/admin/newthing")

    @r.get("/x")
    def _x():
        return {}

    before = list(fastapi_app.router.routes)
    fastapi_app.include_router(r)
    fastapi_app.openapi_schema = None
    try:
        register_and_login(client, "owner@example.com")
        assert not [p for p in client.get("/openapi.json").json()["paths"] if p.startswith("/admin")]
    finally:
        fastapi_app.router.routes[:] = before
        fastapi_app.openapi_schema = None


def test_docs_csp_exception_is_only_for_docs_and_redoc(client):
    register_and_login(client, "owner@example.com")
    assert "content-security-policy" not in client.get("/docs").headers
    assert "content-security-policy" in client.get("/openapi.json").headers
