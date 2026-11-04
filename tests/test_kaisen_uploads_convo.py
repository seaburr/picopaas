"""Uploads in the conversation: idle (draft) builds, run claiming, run_id in the list, prompt marker, K91 push filter."""
import pytest

from app import config
from app.kaisen import uploads as ku
from app.kaisen.models import KaisenAsset, KaisenBuild, KaisenFile, KaisenRun
from app.main import app as fastapi_app
from app.providers.fake import FakeImageProcessor
from app.routers.kaisen import get_github_factory, get_model_factory
from app.routers.kaisen_uploads import get_image_processor
from tests import imgfix
from tests.conftest import register_and_login
from tests.kaisen_helpers import KEY, TOKEN, Fakes, db
from tests.test_kaisen_uploads_api import up


@pytest.fixture()
def w(make_client):
    fastapi_app.dependency_overrides[get_image_processor] = lambda: FakeImageProcessor()
    f = Fakes()
    fastapi_app.dependency_overrides[get_github_factory] = lambda: (lambda token: f.github)
    fastapi_app.dependency_overrides[get_model_factory] = lambda: (lambda key: f.anthropic)
    admin, a, b = make_client(), make_client(), make_client()
    for c, e in ((admin, "admin@example.com"), (a, "a@example.com"), (b, "b@example.com")):
        register_and_login(c, e)
    pid = next(p["id"] for p in a.get("/projects").json() if p["is_personal"])
    admin.put(f"/admin/projects/{pid}/features", json={"kaisen_uploads": True})
    a.put(f"/projects/{pid}/kaisen", json={"anthropic_key": KEY, "github_token": TOKEN})
    return type("W", (), {"a": a, "b": b, "admin": admin, "pid": pid, "f": f})


def _draft(w, **extra):
    return w.a.post("/kaisen/builds", json={"project_id": w.pid, "name": "Draft", "brief": "A site", "start": False, **extra})


def test_start_false_creates_an_idle_build_with_no_run(w):
    r = _draft(w)
    assert r.status_code == 201 and r.json()["start_error"] is None and r.json()["status"] == "idle"
    s = db()
    assert s.query(KaisenRun).count() == 0 and s.get(KaisenBuild, r.json()["id"]).status == "idle"


def test_start_false_works_without_an_anthropic_key_and_default_still_starts(w):
    w.a.put(f"/projects/{w.pid}/kaisen", json={"clear_anthropic_key": True})
    assert _draft(w).json()["start_error"] is None
    assert w.a.post("/kaisen/builds", json={"project_id": w.pid, "name": "X", "brief": "y"}).json()["start_error"]
    assert db().query(KaisenRun).count() == 0


def test_draft_builds_count_against_the_build_limit(w, monkeypatch):
    monkeypatch.setattr(config, "KAISEN_MAX_BUILDS_PER_PROJECT", 2)
    assert _draft(w).status_code == 201 and _draft(w).status_code == 201
    r = _draft(w)
    assert r.status_code == 403 and "most allowed" in r.json()["detail"]


def test_draft_create_authz_and_validation(w, make_client):
    assert make_client().post("/kaisen/builds", json={"name": "a", "brief": "b", "start": False}).status_code == 401
    assert w.b.post("/kaisen/builds", json={"project_id": w.pid, "name": "a", "brief": "b", "start": False}).status_code == 404
    assert _draft(w, name="").status_code == 422
    assert w.a.post("/kaisen/builds", json={"project_id": w.pid, "name": "a", "brief": "b", "start": "maybe"}).status_code == 422


def test_upload_on_a_draft_then_start_claims_it_and_marks_it(w):
    bid = _draft(w).json()["id"]
    r = up(w.a, bid, imgfix.png(10, 10), filename="logo.png")
    assert r.status_code == 201, r.text
    listing = w.a.get(f"/kaisen/builds/{bid}/uploads").json()["uploads"]
    assert [u["run_id"] for u in listing] == [None]
    started = w.a.post(f"/kaisen/builds/{bid}/run")
    assert started.status_code == 200
    run = db().query(KaisenRun).one()
    assert run.kind == "build" and "A site" in str(run.messages[0]["content"])
    assert "[attached to this message]" in str(run.messages[0]["content"])
    assert [u["run_id"] for u in w.a.get(f"/kaisen/builds/{bid}/uploads").json()["uploads"]] == [run.id]


def test_feedback_claims_only_new_uploads_and_marks_them(w):
    bid = _draft(w).json()["id"]
    up(w.a, bid, imgfix.png(10, 10), filename="one.png")
    w.a.post(f"/kaisen/builds/{bid}/run")
    s = db()
    first = s.query(KaisenRun).one()
    first.status = "done"
    s.get(KaisenBuild, bid).status = "done"
    s.commit()
    up(w.a, bid, imgfix.png(12, 12), filename="two.png")
    assert w.a.post(f"/kaisen/builds/{bid}/feedback", json={"text": "use the new picture"}).status_code == 200
    s.expire_all()
    second = s.query(KaisenRun).filter(KaisenRun.id != first.id).one()
    by_path = {r.path: r.run_id for r in s.query(KaisenAsset)}
    assert sorted(by_path.values()) == sorted([first.id, second.id])
    text = str(second.messages[0]["content"])
    assert text.count(ku.ATTACHED) == 1 and "uploads/two.png  png 12x12 80 B" + ku.ATTACHED in text


def test_resume_claims_uploads_too(w):
    bid = _draft(w).json()["id"]
    w.a.post(f"/kaisen/builds/{bid}/run")
    s = db()
    first = s.query(KaisenRun).one()
    first.status = "error"
    s.get(KaisenBuild, bid).status = "error"
    s.commit()
    up(w.a, bid, imgfix.png(10, 10), filename="late.png")
    w.a.post(f"/kaisen/builds/{bid}/run")
    s.expire_all()
    resume = s.query(KaisenRun).filter(KaisenRun.id != first.id).one()
    assert resume.kind == "resume" and s.query(KaisenAsset).one().run_id == resume.id


def test_upload_on_a_draft_keeps_authz_and_flag(w, make_client):
    bid = _draft(w).json()["id"]
    assert up(w.b, bid, imgfix.png(10, 10)).status_code == 404
    assert up(make_client(), bid, imgfix.png(10, 10)).status_code == 401
    w.admin.put(f"/admin/projects/{w.pid}/features", json={"kaisen_uploads": False})
    assert up(w.a, bid, imgfix.png(10, 10)).status_code == 403


def test_prompt_note_without_unbound_files_has_no_marker(w):
    bid = _draft(w).json()["id"]
    up(w.a, bid, imgfix.png(10, 10), filename="a.png")
    s = db()
    ku.claim(s, bid, 7)
    assert "attached to this message" not in ku.prompt_note(s, bid)
    assert ku.claim(s, bid, 8) == 0


def test_migration_up_and_down(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect
    url = f"sqlite:///{tmp_path}/m.db"
    monkeypatch.setattr(config, "DATABASE_URL", url)  # alembic/env.py reads it at run time
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    assert "run_id" in {c["name"] for c in inspect(create_engine(url)).get_columns("kaisen_assets")}
    command.downgrade(cfg, "c5a2e8d4f7b1")
    assert "run_id" not in {c["name"] for c in inspect(create_engine(url)).get_columns("kaisen_assets")}


# ---- K91: legacy secret files never reach GitHub ---------------------------------------------------------------------

def test_push_skips_secret_files_and_names_them(w):
    bid = _draft(w).json()["id"]
    s = db()
    for path in ("public/index.html", ".env", "config/.env.local", "keys/server.pem", "id_rsa"):
        s.add(KaisenFile(build_id=bid, path=path, content="x=1", size=3))
    s.commit()
    w.a.put(f"/kaisen/builds/{bid}/output", json={"mode": "github", "repo": "octo/site"})
    j = w.a.post(f"/kaisen/builds/{bid}/push").json()
    assert j["status"] == "done"
    assert set(w.f.github.pushes[-1]["files"]) == {"public/index.html"}
    assert "Not pushed because they look like secrets: .env, config/.env.local, id_rsa, keys/server.pem" in j["message"]
    assert "x=1" not in j["message"]


def test_push_with_only_secret_files_pushes_nothing(w):
    bid = _draft(w).json()["id"]
    s = db()
    s.add(KaisenFile(build_id=bid, path=".env", content="x=1", size=3))
    s.commit()
    w.a.put(f"/kaisen/builds/{bid}/output", json={"mode": "github", "repo": "octo/site"})
    j = w.a.post(f"/kaisen/builds/{bid}/push").json()
    assert j["message"] == "Nothing to push yet." and w.f.github.pushes == []
