"""Projects BE-1/BE-2: personal project per user, project-membership ownership check, backfill migration."""
from __future__ import annotations

import os
import sqlite3

import subprocess
import sys

from app import models
from app.db import get_db
from app.main import app as fastapi_app
from tests.conftest import register_and_login

_APP = {"name": "demo", "image": "nginx:1", "port": 8080}


def _db():
    return next(fastapi_app.dependency_overrides[get_db]())


def test_register_creates_personal_project(make_client):
    c = make_client()
    register_and_login(c, "a@example.com")
    db = _db()
    user = db.query(models.User).filter_by(email="a@example.com").one()
    proj = db.query(models.Project).filter_by(created_by=user.id).one()
    assert proj.is_personal
    member = db.query(models.ProjectMember).filter_by(project_id=proj.id, user_id=user.id).one()
    assert member.role == "owner"


def test_created_app_lands_in_personal_project(make_client):
    c = make_client()
    register_and_login(c, "a@example.com")
    r = c.post("/apps", json=_APP)
    assert r.status_code == 201, r.text
    db = _db()
    row = db.get(models.App, r.json()["id"])
    assert row.project_id is not None and db.get(models.Project, row.project_id).is_personal


def test_other_user_gets_404_and_empty_list(make_client):
    a = make_client()
    register_and_login(a, "a@example.com")
    app_id = a.post("/apps", json=_APP).json()["id"]
    b = make_client()
    register_and_login(b, "b@example.com")
    assert b.get(f"/apps/{app_id}").status_code == 404
    assert b.delete(f"/apps/{app_id}").status_code == 404
    assert b.get("/apps").json() == []


def test_second_project_member_can_reach_app(make_client):
    a = make_client()
    register_and_login(a, "a@example.com")
    app_id = a.post("/apps", json=_APP).json()["id"]
    b = make_client()
    register_and_login(b, "b@example.com")
    db = _db()
    uid = db.query(models.User).filter_by(email="b@example.com").one().id
    pid = db.get(models.App, app_id).project_id
    db.add(models.ProjectMember(project_id=pid, user_id=uid, role="member"))
    db.commit()
    assert b.get(f"/apps/{app_id}").status_code == 200
    assert [x["id"] for x in b.get("/apps").json()] == [app_id]


def test_migration_backfills_personal_projects(tmp_path, monkeypatch):
    db_path = tmp_path / "m.db"
    root = os.path.join(os.path.dirname(__file__), "..")
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"}

    def alembic(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True, capture_output=True)

    alembic("upgrade", "b8d4f1a6c3e2")
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO users (id,email,password_hash,role,app_limit,status,created_at) VALUES (1,'x@example.com','h','user',1,'active','2026-01-01')")
    cols = [r[1] for r in con.execute("PRAGMA table_info(apps)") if r[3] and r[4] is None and r[1] != "id"]
    vals = {"owner_id": 1, "name": "n", "hostname": "h1", "image": "i", "port": 80, "want_db": 0, "want_storage": 0,
            "created_at": "2026-01-01"}
    cols = [c for c in cols if c not in vals]
    sql_cols = list(vals) + cols
    con.execute(f"INSERT INTO apps ({','.join(sql_cols)}) VALUES ({','.join('?' * len(sql_cols))})",
                list(vals.values()) + [0 if c.startswith(("want", "health", "auto")) else "x" for c in cols])
    con.commit()
    con.close()
    alembic("upgrade", "head")
    con = sqlite3.connect(db_path)
    pid, personal = con.execute("SELECT id,is_personal FROM projects WHERE created_by=1").fetchone()
    assert personal == 1
    assert con.execute("SELECT role FROM project_members WHERE project_id=? AND user_id=1", (pid,)).fetchone() == ("owner",)
    assert con.execute("SELECT project_id FROM apps WHERE owner_id=1").fetchone() == (pid,)
