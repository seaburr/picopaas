"""Port validation across create / PATCH / spec for odd JSON types and state preservation."""
import pytest

from app import models
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
SPEC = "apiVersion: picopaas/v1\nname: demo\nimage: ghcr.io/example/demo:1.0\nport: {port}\n"


@pytest.fixture()
def c(make_client):
    cl = make_client()
    register_and_login(cl, "admin@example.com")
    return cl


@pytest.mark.parametrize("port", [True, False, None, "", "abc", [8080], {"a": 1}, 8080.5, "80"])
def test_create_rejects_non_integer_or_out_of_range(c, port):
    assert c.post("/apps", json={**APP, "port": port}).status_code == 422


def test_create_without_port_is_422(c):
    body = {k: v for k, v in APP.items() if k != "port"}
    assert c.post("/apps", json=body).status_code == 422


@pytest.mark.parametrize("port,expected", [("8080", 8080), (9090.0, 9090)])
def test_create_accepts_lax_numeric_forms_as_the_integer(c, port, expected):
    # Pydantic lax mode coerces these; the stored value is always a validated in-range int.
    r = c.post("/apps", json={**APP, "port": port})
    assert r.status_code in (200, 201) and r.json()["port"] == expected


def test_patch_null_port_means_unchanged(c):
    aid = c.post("/apps", json=APP).json()["id"]
    r = c.patch(f"/apps/{aid}", json={"port": None, "image": APP["image"]})
    assert r.status_code == 200 and r.json()["port"] == 8080


@pytest.mark.parametrize("port", [True, "x", 1023, 65536])
def test_patch_rejects_bad_port_and_keeps_state(c, db_session, port):
    aid = c.post("/apps", json=APP).json()["id"]
    row = db_session.get(models.App, aid)
    row.status = "running"
    db_session.commit()
    assert c.patch(f"/apps/{aid}", json={"port": port}).status_code == 422
    db_session.expire_all()
    row = db_session.get(models.App, aid)
    assert row.port == 8080 and row.status == "running"  # a rejected change must not requeue/redeploy


def test_patch_port_change_requeues(c, db_session):
    aid = c.post("/apps", json=APP).json()["id"]
    row = db_session.get(models.App, aid)
    row.status = "running"
    db_session.commit()
    assert c.patch(f"/apps/{aid}", json={"port": 9000}).status_code == 200
    db_session.expire_all()
    assert db_session.get(models.App, aid).status == "pending"


def test_spec_update_of_existing_app_rejects_bad_port_and_keeps_old(c, db_session):
    assert c.post("/apps/spec", json={"spec": SPEC.format(port=8080), "secrets": {}}).status_code in (200, 201)
    r = c.post("/apps/spec", json={"spec": SPEC.format(port=80), "secrets": {}})
    assert r.status_code == 422 and "1024-65535" in r.text
    row = db_session.query(models.App).one()
    assert row.port == 8080


def test_spec_update_changes_port_and_requeues(c, db_session):
    c.post("/apps/spec", json={"spec": SPEC.format(port=8080), "secrets": {}})
    row = db_session.query(models.App).one()
    row.status = "running"
    db_session.commit()
    assert c.post("/apps/spec", json={"spec": SPEC.format(port=9090), "secrets": {}}).status_code in (200, 201)
    db_session.expire_all()
    row = db_session.query(models.App).one()
    assert row.port == 9090 and row.status == "pending"


def test_port_reaches_service_and_ingress_manifests():
    from app import k8s_manifests as k8s

    r = k8s.render_app_manifests(1, "h", "img", 1024, {}, None, custom_host="a.example.org")
    assert r["service"]["spec"]["ports"][0]["port"] == 1024
    assert r["ingress"]["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"] == {"number": 1024}
    assert r["custom_ingress"]["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"] == {"number": 1024}
