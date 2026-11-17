"""Kaisen "confirm the plan before building": flag + project default, seed paragraph, tool gate, plan_confirm in the inbox."""
import os
import sqlite3
import subprocess
import sys


from app import config
from app.kaisen import builder
from app.kaisen.llm import FakeModelClient, reply, tool_use
from app.kaisen.models import KaisenBuild, KaisenFile, KaisenRun
from tests.kaisen_helpers import db
from tests.test_kaisen_api import _audit, fakes, team  # noqa: F401 (fixtures)
from tests.test_kaisen_worker import env, go, write  # noqa: F401 (fixtures)

PLAN = "# Plan\n- [ ] list todos\n"
OFF_SEED = ("Project name: Todo list\n\n<brief>\nA tiny todo list\n</brief>\n\n"
            "Build this project. Use list_files first to see any existing work.")


def _build(c, pid, **kw):
    r = c.post("/kaisen/builds", json={"project_id": pid, "name": "Todo list", "brief": "A tiny todo list", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _plan_env(env, **kw):
    """A fresh build on the env project, created idle (start=False); returns its id."""
    return _build(env.a, env.pid, start=False, **kw)["id"]


def _run(env, bid):
    r = env.a.post(f"/kaisen/builds/{bid}/run")
    assert r.status_code == 200, r.text


def _last(bid):
    s = db()
    s.expire_all()
    return builder.latest_run(s, bid)


def _results(run):
    out = {}
    for m in run.messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for blk in m["content"]:
                if blk.get("type") == "tool_result":
                    out[blk["tool_use_id"]] = (blk["content"], bool(blk.get("is_error")))
    return out


def _paths(bid):
    s = db()
    s.expire_all()
    return sorted(f.path for f in s.query(KaisenFile).filter_by(build_id=bid))


# ---- default off ---------------------------------------------------------------------------------

def test_default_off_seed_unchanged(env):
    bid = _plan_env(env)
    d = env.a.get(f"/kaisen/builds/{bid}").json()
    assert d["plan_first"] is False
    b = db().get(KaisenBuild, bid)
    assert builder.seed_message(b)["content"] == OFF_SEED
    _run(env, bid)
    assert _last(bid).messages[0]["content"] == OFF_SEED
    assert env.a.get(f"/projects/{env.pid}/kaisen").json()["plan_first"] is False
    assert not _audit("kaisen.build_create")[-1].detail.endswith("plan_first=1")


def test_off_build_writes_freely(env):
    bid = _plan_env(env)
    _run(env, bid)
    go(FakeModelClient([reply(write("app.py", "print('hi')"), tool_use("deploy_preview", {}, id="dp"))]))
    assert _results(_last(bid))["dp"][0] != f"Error: {builder.PLAN_FIRST_REFUSAL}"
    assert "app.py" in _paths(bid)


# ---- flag, default and override --------------------------------------------------------------------

def test_override_on_with_default_off(env):
    out = _build(env.a, env.pid, start=False, plan_first=True)
    assert out["plan_first"] is True
    assert _audit("kaisen.build_create")[-1].detail == f"project={env.pid} plan_first=1"
    assert env.a.get(f"/kaisen/builds/{out['id']}").json()["plan_first"] is True
    listed = env.a.get(f"/kaisen/builds?project_id={env.pid}").json()
    assert next(x for x in listed if x["id"] == out["id"])["plan_first"] is True


def test_project_default_on_and_override_off(env):
    assert env.a.put(f"/projects/{env.pid}/kaisen", json={"plan_first": True}).status_code == 200
    assert _build(env.a, env.pid, start=False)["plan_first"] is True
    assert _build(env.a, env.pid, start=False, plan_first=None)["plan_first"] is True
    off = _build(env.a, env.pid, start=False, plan_first=False)
    assert off["plan_first"] is False
    assert not _audit("kaisen.build_create")[-1].detail.endswith("plan_first=1")


def test_build_validation_rejects_non_bool(env):
    r = env.a.post("/kaisen/builds", json={"project_id": env.pid, "name": "x", "brief": "y", "plan_first": "maybe"})
    assert r.status_code == 422


def test_settings_round_trip_and_member_cannot_change(team):
    _, a, b, c, pid = team
    assert a.get(f"/projects/{pid}/kaisen").json()["plan_first"] is False
    assert a.put(f"/projects/{pid}/kaisen", json={"plan_first": True}).status_code == 200
    assert b.get(f"/projects/{pid}/kaisen").json()["plan_first"] is True
    assert a.put(f"/projects/{pid}/kaisen", json={"effort": "low"}).status_code == 200  # omitted keeps it
    assert a.get(f"/projects/{pid}/kaisen").json()["plan_first"] is True
    assert b.put(f"/projects/{pid}/kaisen", json={"plan_first": False}).status_code == 403
    assert c.put(f"/projects/{pid}/kaisen", json={"plan_first": False}).status_code == 404
    assert a.get(f"/projects/{pid}/kaisen").json()["plan_first"] is True
    assert a.put(f"/projects/{pid}/kaisen", json={"plan_first": "maybe"}).status_code == 422
    assert a.put(f"/projects/{pid}/kaisen", json={"plan_first": False}).status_code == 200
    assert a.get(f"/projects/{pid}/kaisen").json()["plan_first"] is False


def test_anonymous_and_outsider_build_routes(team, client):
    _, a, b, c, pid = team
    assert client.post("/kaisen/builds", json={"name": "x", "brief": "y", "plan_first": True}).status_code == 401
    assert c.post("/kaisen/builds", json={"project_id": pid, "name": "x", "brief": "y", "plan_first": True}).status_code == 404


# ---- seed paragraph ------------------------------------------------------------------------------

def test_seed_paragraph_only_in_the_build_run(env):
    bid = _plan_env(env, plan_first=True)
    _run(env, bid)
    first = _last(bid).messages[0]["content"]
    assert first.startswith(OFF_SEED) and builder.PLAN_FIRST_NOTE in first
    assert "ask_human exactly once" in first and "PLAN.md" in first
    go(FakeModelClient([reply(tool_use("ask_human", {"question": "plan?"}))]))
    q = next(i for i in env.a.get(f"/kaisen/builds/{bid}").json()["inbox"] if i["kind"] == "question")
    env.a.post(f"/kaisen/builds/{bid}/inbox/{q['id']}/answer", json={"text": "go"})
    go(FakeModelClient([reply(write("PLAN.md", "# Plan\n- [x] done\n"), tool_use("finish", {"summary": "ok"}))]))
    assert _last(bid).status == "done"
    r = env.a.post(f"/kaisen/builds/{bid}/feedback", json={"text": "make it blue"})
    assert r.status_code == 200, r.text
    fb = _last(bid)
    assert fb.kind == "feedback" and builder.PLAN_FIRST_NOTE not in str(fb.messages[0]["content"])


# ---- the gate --------------------------------------------------------------------------------------

def test_gate_refuses_other_work_then_answer_releases_it(env):
    bid = _plan_env(env, plan_first=True)
    _run(env, bid)
    model = FakeModelClient([
        reply(tool_use("list_files", {}, id="ls"), write("app.py", "print('hi')"),
              tool_use("edit_file", {"path": "x.py", "old_str": "a", "new_str": "b"}, id="ed"),
              tool_use("delete_file", {"path": "x.py"}, id="rm"),
              tool_use("deploy_preview", {}, id="dp"), tool_use("finish", {"summary": "done"}, id="fin"),
              write("PLAN.md", PLAN)),
        reply(tool_use("ask_human", {"question": "Plan: " + PLAN + "\nReply go."})),
    ])
    assert go(model) is True
    run = _last(bid)
    res = _results(run)
    for tid in ("ed", "rm", "dp", "fin"):
        assert res[tid] == (f"Error: {builder.PLAN_FIRST_REFUSAL}", True), tid
    assert res["ls"][1] is False
    assert res["ls"][0] != f"Error: {builder.PLAN_FIRST_REFUSAL}"
    assert _paths(bid) == ["PLAN.md"]  # the write of app.py was refused, PLAN.md went through
    assert run.status == "waiting"
    d = env.a.get(f"/kaisen/builds/{bid}").json()
    q = [i for i in d["inbox"] if i["kind"] == "question"]
    assert len(q) == 1 and q[0]["plan_confirm"] is True and q[0]["text"].startswith("Plan: ")
    assert env.a.post(f"/kaisen/builds/{bid}/inbox/{q[0]['id']}/answer", json={"text": "go"}).status_code == 200
    model2 = FakeModelClient([
        reply(write("app.py", "print('hi')"), tool_use("deploy_preview", {}, id="dp2")),
        reply(tool_use("ask_human", {"question": "Blue or green?"})),
    ])
    go(model2)
    assert "app.py" in _paths(bid)
    assert _results(_last(bid))["dp2"][0] != f"Error: {builder.PLAN_FIRST_REFUSAL}"
    inbox = env.a.get(f"/kaisen/builds/{bid}").json()["inbox"]
    later = next(i for i in inbox if i["text"] == "Blue or green?")
    assert later["plan_confirm"] is False
    assert next(i for i in inbox if i["id"] == q[0]["id"])["plan_confirm"] is True


def test_plan_md_edit_allowed_before_the_answer(env):
    bid = _plan_env(env, plan_first=True)
    _run(env, bid)
    go(FakeModelClient([reply(write("PLAN.md", PLAN)),
                        reply(tool_use("edit_file", {"path": "PLAN.md", "old_str": "list", "new_str": "show"}, id="e")),
                        reply(tool_use("ask_human", {"question": "plan"}))]))
    assert _results(_last(bid))["e"][1] is False


def test_long_plan_question_fits(env):
    bid = _plan_env(env, plan_first=True)
    _run(env, bid)
    big = "- [ ] item\n" * 400  # about 4400 characters
    go(FakeModelClient([reply(tool_use("ask_human", {"question": big}))]))
    q = next(i for i in env.a.get(f"/kaisen/builds/{bid}").json()["inbox"] if i["kind"] == "question")
    assert q["text"] == big and len(big) > 4000


def test_stop_while_waiting_then_resume_is_not_gated(env):
    bid = _plan_env(env, plan_first=True)
    _run(env, bid)
    go(FakeModelClient([reply(tool_use("ask_human", {"question": "plan"}))]))
    run = _last(bid)
    assert builder.plan_pending(db().get(KaisenBuild, bid), run) is True
    assert env.a.post(f"/kaisen/builds/{bid}/runs/{run.id}/stop").status_code == 200
    s = db()
    s.expire_all()
    assert builder.plan_pending(s.get(KaisenBuild, bid), s.get(KaisenRun, run.id)) is False
    _run(env, bid)  # Resume
    resumed = _last(bid)
    assert resumed.kind == "resume"
    assert builder.plan_pending(db().get(KaisenBuild, bid), resumed) is False
    go(FakeModelClient([reply(write("app.py", "print('hi')"))]))
    assert "app.py" in _paths(bid)


def test_resume_before_any_ask_is_still_gated(env):
    bid = _plan_env(env, plan_first=True)
    _run(env, bid)
    s = db()
    run = _last(bid)
    run.status = "stopped"
    s.get(KaisenBuild, bid).status = "stopped"
    s.commit()
    _run(env, bid)
    resumed = _last(bid)
    assert resumed.kind == "resume" and builder.plan_pending(db().get(KaisenBuild, bid), resumed) is True


def test_feedback_run_is_never_gated(env):
    bid = _plan_env(env, plan_first=True)
    s = db()
    b = s.get(KaisenBuild, bid)
    fb = KaisenRun(build_id=bid, kind="feedback", status="queued",
                   messages=[builder.feedback_message(s, b, None, "change it", None)])
    s.add(fb)
    s.commit()
    assert builder.plan_pending(b, fb) is False
    fb2 = KaisenRun(build_id=bid, kind="resume", status="queued", messages=list(fb.messages))
    assert builder.plan_pending(b, fb2) is False  # a resumed feedback run keeps its feedback history


def test_gate_does_not_dead_end_a_model_that_never_asks(env, monkeypatch):
    monkeypatch.setattr(config, "KAISEN_MAX_TURNS", 3)
    monkeypatch.setattr(config, "KAISEN_MAX_STEPS_PER_RUN", 1)
    bid = _plan_env(env, plan_first=True)
    _run(env, bid)
    go(FakeModelClient([reply(write(f"f{i}.py", str(i))) for i in range(5)]))
    assert _last(bid).status == "stopped"  # the normal turn limit ends it
    assert _paths(bid) == []


# ---- fork ------------------------------------------------------------------------------------------

def test_fork_does_not_copy_the_flag(team):
    _, a, b, c, pid = team
    src = _build(a, pid, start=False, plan_first=True)["id"]
    assert a.get(f"/kaisen/builds/{src}").json()["plan_first"] is True
    r = a.post(f"/kaisen/builds/{src}/fork", json={})
    assert r.status_code in (200, 201), r.text
    assert r.json()["plan_first"] is False
    assert db().get(KaisenBuild, r.json()["id"]).plan_first is False


# ---- migration -------------------------------------------------------------------------------------

def test_migration_up_down_up(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dbfile = tmp_path / "m.db"
    env_ = {**os.environ, "DATABASE_URL": f"sqlite:///{dbfile}", "ENVIRONMENT": "dev"}

    def alembic(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env_, check=True, capture_output=True, text=True)

    def cols(table):
        con = sqlite3.connect(dbfile)
        try:
            return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        finally:
            con.close()

    alembic("upgrade", "f1a7c3d9e2b5")
    assert "plan_first" not in cols("kaisen_builds")
    con = sqlite3.connect(dbfile)
    con.execute("INSERT INTO kaisen_builds (project_id, created_by, name, slug, brief, status, created_at) VALUES"
                " (1,1,'b','b','x','idle',CURRENT_TIMESTAMP)")
    con.commit()
    con.close()
    alembic("upgrade", "head")
    assert "plan_first" in cols("kaisen_builds") and "plan_first" in cols("kaisen_settings")
    con = sqlite3.connect(dbfile)
    assert con.execute("SELECT plan_first FROM kaisen_builds").fetchone() == (0,)  # existing rows stay off
    con.close()
    alembic("downgrade", "f1a7c3d9e2b5")
    assert "plan_first" not in cols("kaisen_builds") and "plan_first" not in cols("kaisen_settings")
    alembic("upgrade", "head")
    assert "plan_first" in cols("kaisen_settings")


def test_run_again_after_a_finished_run_does_not_ask_for_the_plan_again(team):
    from app.kaisen import builder
    from app.kaisen.models import KaisenBuild, KaisenRun
    from tests.kaisen_helpers import db
    _, a, _, _, pid = team
    r = a.post("/kaisen/builds", json={"project_id": pid, "name": "Again", "brief": "A tiny todo list", "plan_first": True})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    s = db()
    first = s.query(KaisenRun).filter_by(build_id=bid).one()
    assert builder.PLAN_MARK in first.messages[0]["content"] and builder.plan_pending(s.get(KaisenBuild, bid), first)
    first.status = "done"
    s.get(KaisenBuild, bid).status = "done"
    s.commit()
    run, _msg = builder.start_build(s, s.get(KaisenBuild, bid), first.started_by)
    s.commit()
    assert run is not None and run.kind == "build" and builder.PLAN_MARK not in run.messages[0]["content"]
    assert not builder.plan_pending(s.get(KaisenBuild, bid), run)
