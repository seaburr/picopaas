"""Per-app HTTP health checks: path validation, API, spec, manifests, migration."""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect

from app import k8s_manifests as k8s
from app.health_path import validate_health_path
from app.spec import SpecError, parse_spec
from tests.conftest import register_and_login

@pytest.fixture(autouse=True)
def _login(request):
    if "client" in request.fixturenames:
        register_and_login(request.getfixturevalue("client"), "admin@example.com")


GOOD = ["/", "/healthz", "/a/b-c_d.e", "/health?x"[:7], "/x:y@z", "/a%20b", "/" + "a" * 254]
BAD = ["", "healthz", "http://x", "/../etc", "/a/../b", "/a/..", "/a b", "/a\tb", "/a\nb", "/a\n",
       "/a'b", '/a"b', "/a\x00b", "/a/%2e%2e/b", "/" + "a" * 255, "/a?q=1", "/a#f"]


@pytest.mark.parametrize("p", GOOD)
def test_good_paths(p):
    assert validate_health_path(p) == p


@pytest.mark.parametrize("p", BAD)
def test_bad_paths(p):
    with pytest.raises(ValueError):
        validate_health_path(p)


def _create(client, **kw):
    payload = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080}
    payload.update(kw)
    return client.post("/apps", json=payload)


def test_api_defaults_and_round_trip(client):
    j = _create(client).json()
    assert j["health_check_enabled"] is False and j["health_check_path"] == "/healthz"
    j = _create(client, name="b", health_check_enabled=True, health_check_path="/ready").json()
    assert (j["health_check_enabled"], j["health_check_path"]) == (True, "/ready")
    r = client.patch(f"/apps/{j['id']}", json={"health_check_path": "/up"})
    assert r.status_code == 200 and r.json()["health_check_path"] == "/up"
    assert r.json()["health_check_enabled"] is True and r.json()["status"] == "pending"
    r = client.patch(f"/apps/{j['id']}", json={"health_check_enabled": False})
    assert r.json()["health_check_enabled"] is False
    assert client.get(f"/apps/{j['id']}").json()["health_check_path"] == "/up"


@pytest.mark.parametrize("p", ["", "http://x", "/a/../b", "/a b", "/a\nb", "/a'b"])
def test_api_rejects_bad_paths(client, p):
    assert _create(client, health_check_enabled=True, health_check_path=p).status_code == 422
    app_id = _create(client, name="ok").json()["id"]
    assert client.patch(f"/apps/{app_id}", json={"health_check_path": p}).status_code == 422


def test_update_marks_pending_for_reconcile(client, db_session):
    from app import models
    app_id = _create(client).json()["id"]
    a = db_session.get(models.App, app_id)
    a.status = "running"
    db_session.commit()
    client.patch(f"/apps/{app_id}", json={"health_check_enabled": True})
    db_session.expire_all()
    assert db_session.get(models.App, app_id).status == "pending"


SPEC = "apiVersion: picopaas/v1\nname: x\nimage: i\nport: 8080\n"


def test_spec_parsing():
    s = parse_spec(SPEC)
    assert (s.healthCheck.enabled, s.healthCheck.path) == (False, "/healthz")
    s = parse_spec(SPEC + "healthCheck: {enabled: true}\n")
    assert (s.healthCheck.enabled, s.healthCheck.path) == (True, "/healthz")
    s = parse_spec(SPEC + "healthCheck: {enabled: true, path: /up}\n")
    assert s.healthCheck.path == "/up"
    for bad in ("healthCheck: {enabled: true, path: /a/../b}", "healthCheck: {path: 'x y'}",
                "healthCheck: {bogus: 1}", "healthCheck: {enabled: 'yes'}"):
        with pytest.raises(SpecError):
            parse_spec(SPEC + bad + "\n")


def test_spec_endpoint_applies(client):
    spec = SPEC + "healthCheck: {enabled: true, path: /up}\n"
    r = client.post("/apps/spec", json={"spec": spec, "secrets": {}})
    assert r.status_code == 200, r.text
    assert (r.json()["health_check_enabled"], r.json()["health_check_path"]) == (True, "/up")
    r = client.post("/apps/spec", json={"spec": SPEC, "secrets": {}})
    assert r.json()["health_check_enabled"] is False and r.json()["status"] == "pending"


def _container(**kw):
    d = k8s.deployment_manifest(1, "img", 8080, **kw)
    return d["spec"]["template"]["spec"]["containers"][0]


def test_manifest_disabled_keeps_tcp_probe():
    c = _container()
    assert c["readinessProbe"] == {"tcpSocket": {"port": 8080}, "initialDelaySeconds": 2, "periodSeconds": 5}
    assert "livenessProbe" not in c


def test_manifest_enabled_http_probes():
    c = _container(health_check_enabled=True, health_check_path="/up")
    assert c["readinessProbe"] == {"httpGet": {"path": "/up", "port": 8080}, "initialDelaySeconds": 5,
                                   "periodSeconds": 10, "timeoutSeconds": 3, "failureThreshold": 3}
    assert c["livenessProbe"] == {"httpGet": {"path": "/up", "port": 8080}, "initialDelaySeconds": 30,
                                  "periodSeconds": 20, "timeoutSeconds": 5, "failureThreshold": 3}


def test_port_env_injected_unless_user_sets_it():
    assert _container()["env"] == [{"name": "PORT", "value": "8080"}]
    assert _container(user_env={"A": "b"})["env"] == [{"name": "PORT", "value": "8080"}]
    assert "env" not in _container(user_env={"PORT": "9000"})
    r = k8s.render_app_manifests(1, "h", "img", 8080, {"PORT": "9000"}, None)
    assert "env" not in r["deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert r["secret"]["stringData"]["PORT"] == "9000"
    r = k8s.render_app_manifests(1, "h", "img", 8080, {}, None, health_check_enabled=True, health_check_path="/z")
    c = r["deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert c["env"][0]["value"] == "8080" and c["livenessProbe"]["httpGet"]["path"] == "/z"


def test_migration_round_trip(tmp_path):
    url = f"sqlite:///{tmp_path}/m.db"
    root = os.path.join(os.path.dirname(__file__), "..")
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True, capture_output=True)

    def cols():
        return {c["name"] for c in inspect(create_engine(url)).get_columns("apps")}
    alembic("upgrade", "head")
    assert {"health_check_enabled", "health_check_path"} <= cols()
    alembic("downgrade", "f6c3d9e2a4b8")  # past health_check and rendered_hash
    assert not ({"health_check_enabled", "health_check_path"} & cols())
    alembic("upgrade", "head")
    assert {"health_check_enabled", "health_check_path"} <= cols()
