"""Per-pod outbound bandwidth cap: pod annotation, admin override, migration."""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect

from app import models, resources
from app import k8s_manifests as k8s
from tests.conftest import register_and_login

APP = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
ANN = "kubernetes.io/egress-bandwidth"


def _ann(rendered):
    return rendered["deployment"]["spec"]["template"]["metadata"]["annotations"]


def test_default_annotation():
    r = k8s.render_app_manifests(1, "h", "img", 8080, {}, None)
    assert _ann(r)[ANN] == "20M"


def test_override_annotation():
    r = k8s.render_app_manifests(1, "h", "img", 8080, {}, None, resources={**resources.effective(None), "egress_mbit": 160})
    assert _ann(r)[ANN] == "160M"


def test_suspended_still_annotated():
    r = k8s.render_app_manifests(1, "h", "img", 8080, {}, None, replicas=0, include_ingress=False)
    assert r["deployment"]["spec"]["replicas"] == 0
    assert _ann(r)[ANN] == "20M"


def test_env_override_default(monkeypatch):
    monkeypatch.setattr("app.config.EGRESS_BANDWIDTH_MBIT", 40)
    assert resources.effective(None)["egress_mbit"] == 40


def test_admin_patch(make_client, db_session):
    c = make_client()
    register_and_login(c, "admin@example.com")
    aid = c.post("/apps", json=APP).json()["id"]
    for v in (0, 161, -5):
        assert c.patch(f"/admin/apps/{aid}/resources", json={"egress_mbit": v}).status_code == 422
    r = c.patch(f"/admin/apps/{aid}/resources", json={"egress_mbit": 160})
    assert r.status_code == 200
    d = r.json()
    assert d["restarts_app"] is True and d["resources"]["egress_mbit"] == 160
    assert d["resources"]["overridden"] and d["resources"]["defaults"]["egress_mbit"] == 20
    assert db_session.get(models.App, aid).status == "pending"
    r = c.patch(f"/admin/apps/{aid}/resources", json={"egress_mbit": 160})
    assert r.json()["restarts_app"] is False
    r = c.patch(f"/admin/apps/{aid}/resources", json={"egress_mbit": None})
    assert r.json()["restarts_app"] is True and r.json()["resources"]["egress_mbit"] == 20
    assert c.get(f"/apps/{aid}").json()["resources"]["egress_mbit"] == 20


def test_migration_round_trip(tmp_path):
    url = f"sqlite:///{tmp_path}/m.db"
    root = os.path.join(os.path.dirname(__file__), "..")
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True,
                              capture_output=True, text=True).stdout

    cols = lambda: {c["name"] for c in inspect(create_engine(url)).get_columns("apps")}
    alembic("upgrade", "head")
    assert "egress_mbit" in cols()
    assert len(alembic("heads").strip().splitlines()) == 1
    alembic("downgrade", "d4a7b9c1e3f5")
    assert "egress_mbit" not in cols() and "image_size_mb" in cols()
    alembic("upgrade", "head")
    assert "egress_mbit" in cols()
