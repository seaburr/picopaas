"""Kaisen worker and builder: scripted model runs against the real API, fake model / GitHub / runtime."""
import os
import subprocess
import sys
import threading
import types
import time
from datetime import timedelta

import pytest

from app import config, models
from app.kaisen import builder, worker
from app.kaisen.llm import FakeModelClient, ModelError, reply, tool_use
from app.kaisen.models import KaisenBuild, KaisenEvent, KaisenInbox, KaisenRun
from app.main import app as fastapi_app
from app.providers.fake import FakeRegistry
from app.routers.kaisen import get_github_factory, get_model_factory
from tests.conftest import register_and_login
from tests.kaisen_helpers import KEY, TOKEN, FakeRuntime, Fakes, Keep, app_env, db

IMG = "ghcr.io/example/kaisen-runtime:1"
_P = config.KAISEN_PRICES[config.KAISEN_DEFAULT_MODEL]
PER_CALL = (1000 * _P["in"] + 500 * _P["out"]) / 1_000_000  # default fake usage on the default model


@pytest.fixture()
def env(make_client, monkeypatch):
    f = Fakes()
    fastapi_app.dependency_overrides[get_github_factory] = lambda: (lambda token: f.github)
    fastapi_app.dependency_overrides[get_model_factory] = lambda: (lambda key: f.anthropic)
    monkeypatch.setattr(config, "KAISEN_RUNTIME_IMAGE", IMG)
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    a = make_client()
    register_and_login(a, "a@example.com")
    pid = a.post("/projects", json={"name": "Team"}).json()["id"]
    # build first, key second: creating a build with a key queues its first run, these tests start it themselves
    bid = a.post("/kaisen/builds", json={"project_id": pid, "name": "Todo list", "brief": "A tiny todo list"}).json()["id"]
    assert a.put(f"/projects/{pid}/kaisen", json={"anthropic_key": KEY}).status_code == 200
    return type("Env", (), {"admin": admin, "a": a, "pid": pid, "bid": bid, "fakes": f})


def go(model, runtime=None, **kw):
    """One worker tick with scripted model replies; returns whether work was attempted."""
    kw.setdefault("gh_fn", lambda token: Fakes().github)
    return worker.tick(Keep(db()), worker.Lease(), model_fn=lambda key: model,
                       registry_factory=lambda: FakeRegistry(default_size_bytes=1024 * 1024),
                       runtime=runtime or FakeRuntime(), wait_s=1, sleep=lambda s: None, **kw)


def write(path, content):
    return tool_use("write_file", {"path": path, "content": content})


def start(env):
    r = env.a.post(f"/kaisen/builds/{env.bid}/run")
    assert r.status_code == 200, r.text
    return r.json()


def detail(env):
    return env.a.get(f"/kaisen/builds/{env.bid}").json()


# ---- the full path -------------------------------------------------------------------------------

def test_scripted_build_with_question_resume_finish_and_auto_deploy(env):
    env.a.put(f"/projects/{env.pid}/kaisen", json={"github_token": TOKEN})
    assert env.a.put(f"/kaisen/builds/{env.bid}/output", json={"mode": "github", "repo": "octo/demo"}).status_code == 200
    start(env)
    seen = []
    model = FakeModelClient([
        reply(tool_use("list_files", {}), write("kaisen.json", '{"runtime": "python", "start": "python app.py", "database": "mysql"}'),
              write("app.py", "print('hi')")),
        reply(tool_use("ask_human", {"question": "Which colour?"})),
        lambda msgs: (seen.append(msgs[-1]), reply(write("README.md", "# Todo"), tool_use("deploy_preview", {})))[1],
        reply(tool_use("finish", {"summary": "Built a todo list"})),
    ])
    gh = Fakes().github
    assert go(model, gh_fn=lambda t: gh) is True
    d = detail(env)
    assert d["status"] == "waiting" and d["open_questions"] == 1 and d["app"] is None
    assert [f["path"] for f in d["files"]] == ["app.py", "kaisen.json"]
    q = next(i for i in d["inbox"] if i["kind"] == "question")
    assert q["text"] == "Which colour?"
    assert go(model) is False  # nothing queued while waiting for the human

    ans = env.a.post(f"/kaisen/builds/{env.bid}/inbox/{q['id']}/answer", json={"text": "blue"})
    assert ans.status_code == 200 and ans.json()["status"] == "queued"
    assert env.a.post(f"/kaisen/builds/{env.bid}/inbox/{q['id']}/answer", json={"text": "again"}).status_code == 409
    assert go(model, gh_fn=lambda t: gh) is True

    assert any("blue" in str(b) for b in seen[0]["content"])  # the answer reached the model as a tool_result
    d = detail(env)
    assert d["status"] == "done" and d["runs"][0]["summary"] == "Built a todo list"
    assert d["runs"][0]["status"] == "done" and abs(d["runs"][0]["cost_usd"] - 4 * PER_CALL) < 1e-9
    # auto-deploy: one app, in the build's project, linked both ways, expected spec incl. the MySQL DB
    assert d["app"]["status"] == "running" and d["app"]["hostname"].endswith(config.APP_DOMAIN)
    apps = env.a.get("/apps", params={"project_id": env.pid}).json()
    assert len(apps) == 1 and apps[0]["kaisen_build_id"] == env.bid and apps[0]["want_db"] is True
    assert apps[0]["image"] == IMG and apps[0]["id"] == d["app_id"]
    envs = {e["key"] for e in apps[0]["env"]}
    assert app_env(apps[0]["id"])["KAISEN_BUNDLE_URL"][1] is True
    assert {"KAISEN_BUNDLE_URL", "KAISEN_BUNDLE_SHA256", "DB_HOST", "DATABASE_URL"} <= envs
    kinds = [i["kind"] for i in d["inbox"]]
    assert kinds.count("deploy") == 1 and "push" in kinds  # push on finish reported in the inbox
    assert "Preview deployed: Result: OK" in next(i for i in d["inbox"] if i["kind"] == "deploy")["text"]
    assert gh.pushes[-1]["repo"] == "octo/demo" and "README.md" in gh.pushes[-1]["files"]
    assert db().query(KaisenEvent).filter_by(build_id=env.bid).count() == 4
    assert env.a.get("/kaisen/spend", params={"project_id": env.pid}).json()["today_usd"] == pytest.approx(4 * PER_CALL)
    assert len(env.a.get("/apps", params={"project_id": env.pid}).json()) == 1  # finish did not create a second deploy


def test_finish_without_prior_preview_auto_deploys(env):
    start(env)
    model = FakeModelClient([reply(write("kaisen.json", '{"runtime": "static"}'), write("index.html", "<p>x</p>")),
                             reply(tool_use("finish", {"summary": "done"}))])
    runtime = FakeRuntime()
    go(model, runtime)
    d = detail(env)
    assert d["status"] == "done" and d["app"]["id"] == d["app_id"]
    assert runtime.probes == [f"https://{d['app']['hostname']}"]


def test_app_limit_is_a_plain_message_in_inbox_and_tool_result(env):
    assert env.admin.patch(f"/admin/projects/{env.pid}", json={"app_limit": 1}).status_code == 200
    env.a.post("/apps", json={"name": "taken", "image": "nginx:1", "port": 8080, "project_id": env.pid})
    start(env)
    model = FakeModelClient([reply(write("kaisen.json", "{}"), tool_use("deploy_preview", {})),
                             reply(tool_use("finish", {"summary": "could not preview"}))])
    go(model)
    d = detail(env)
    assert d["status"] == "done" and d["app"] is None
    texts = [i["text"] for i in d["inbox"] if i["kind"] == "deploy"]
    assert any("reached its limit of 1 app" in t for t in texts) and len(texts) == 2  # tool + automatic deploy
    tool_result = model.calls[1][-1]["content"][-1]
    assert tool_result["is_error"] is True and "call finish" in tool_result["content"]
    assert db().query(models.App).filter(models.App.kaisen_build_id.is_not(None)).count() == 0


def test_feedback_run_updates_same_app(env):
    start(env)
    go(FakeModelClient([reply(write("kaisen.json", '{"runtime": "static"}'), write("index.html", "v1")),
                        reply(tool_use("finish", {"summary": "v1 built"}))]))
    app_id = detail(env)["app_id"]
    r = env.a.post(f"/kaisen/builds/{env.bid}/feedback", json={"text": "make it blue"})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    model = FakeModelClient([reply(write("index.html", "v2 blue")), reply(tool_use("finish", {"summary": "now blue"}))])
    go(model)
    first = model.calls[0][0]["content"]
    assert "make it blue" in first and "v1 built" in first and "Current preview: https://" in first
    d = detail(env)
    assert d["app_id"] == app_id and d["runs"][0]["kind"] == "feedback" and d["status"] == "done"
    assert len(env.a.get("/apps", params={"project_id": env.pid}).json()) == 1


# ---- caps, stops, errors --------------------------------------------------------------------------

def _writes(n):
    return [reply(write(f"f{i}.txt", str(i))) for i in range(n)]


def test_run_cap_stops_then_resume_continues(env):
    env.a.put(f"/projects/{env.pid}/kaisen", json={"cap_run": PER_CALL * 2 - 0.001})
    start(env)
    model = FakeModelClient(_writes(5))
    go(model)
    d = detail(env)
    assert d["status"] == "stopped" and len(model.calls) == 2
    item = next(i for i in d["inbox"] if i["kind"] == "budget")
    assert item["text"].startswith("Run cap $") and "reached" in item["text"]
    assert d["last_error"] == item["text"]
    env.a.put(f"/projects/{env.pid}/kaisen", json={"cap_run": 5})
    assert env.a.post(f"/kaisen/builds/{env.bid}/run").json()["runs"][0]["kind"] == "resume"
    resumed = FakeModelClient([reply(tool_use("finish", {"summary": "ok"}))])
    go(resumed, runtime=FakeRuntime())
    assert resumed.calls[0][0]["role"] == "user" and len(resumed.calls[0]) > 3  # history carried over
    assert detail(env)["status"] == "done"


def test_build_cap_and_daily_cap(env):
    env.a.put(f"/projects/{env.pid}/kaisen", json={"cap_build": PER_CALL * 0.5})
    start(env)
    model = FakeModelClient(_writes(3))
    go(model)
    assert len(model.calls) == 1
    assert "Build cap $" in detail(env)["last_error"]
    env.a.put(f"/projects/{env.pid}/kaisen", json={"cap_build": 100, "cap_day": 0})
    env.a.post(f"/kaisen/builds/{env.bid}/run")
    go(FakeModelClient(_writes(1)))
    assert "Daily cap $" in detail(env)["last_error"]


def test_repeat_detector_stops_identical_calls(env):
    start(env)
    same = [reply(tool_use("list_files", {}, id=f"toolu_{i}")) for i in range(5)]
    model = FakeModelClient(same)
    go(model)
    d = detail(env)
    assert d["status"] == "stopped" and len(model.calls) == 3 and "same call" in d["last_error"]


@pytest.mark.parametrize("stop,kind,status", [("refusal", "refusal", "stopped"), ("max_tokens", "error", "error")])
def test_refusal_and_max_tokens(env, stop, kind, status):
    start(env)
    go(FakeModelClient([reply(stop_reason=stop)]))
    d = detail(env)
    assert d["status"] == status and d["inbox"][0]["kind"] == kind


def test_model_error_becomes_plain_error(env):
    start(env)
    go(FakeModelClient([ModelError("Anthropic rejected the API key (401)")]))
    d = detail(env)
    assert d["status"] == "error" and d["last_error"] == "Anthropic rejected the API key (401)"


def test_unknown_model_refuses_to_run(env):
    start(env)
    s = db()
    from app.kaisen.models import KaisenSettings
    s.get(KaisenSettings, env.pid).model = "claude-mystery"
    s.commit()
    model = FakeModelClient(_writes(1))
    go(model)
    assert model.calls == [] and "Unknown model" in detail(env)["last_error"]


def test_run_without_key_errors_before_calling_the_model(env):
    start(env)
    env.a.put(f"/projects/{env.pid}/kaisen", json={"clear_anthropic_key": True})
    model = FakeModelClient(_writes(1))
    go(model)
    assert model.calls == [] and "No Anthropic API key" in detail(env)["last_error"]


def test_path_rules_and_limits_are_errors_the_model_can_read(env, monkeypatch):
    monkeypatch.setattr(config, "KAISEN_MAX_FILES", 1)
    start(env)
    model = FakeModelClient([
        reply(write("../escape", "x"), write("/abs", "x"), write(".github/workflows/x.yml", "x"), write("ok.txt", "x"),
              write("second.txt", "x")),
        reply(tool_use("finish", {"summary": "done"})),
    ])
    go(model)
    results = model.calls[1][-1]["content"]
    errs = [r["content"] for r in results if r.get("is_error")]
    assert len(errs) == 4 and any("'..'" in e for e in errs) and any(".github" in e for e in errs) and any("too many files" in e for e in errs)
    assert [f["path"] for f in detail(env)["files"]] == ["ok.txt"]


def test_deleted_build_run_is_stopped_not_executed(env):
    start(env)
    env.a.delete(f"/kaisen/builds/{env.bid}")
    # delete stopped the active run; nothing left to pick up
    assert go(FakeModelClient(_writes(1))) is False


def test_delete_during_model_call_discards_the_response(env):
    """Delete lands while the model is streaming: nothing is written back, no app is deployed."""
    start(env)

    def delete_then_reply(messages):
        assert env.a.delete(f"/kaisen/builds/{env.bid}").status_code == 204
        return reply(write("zombie.txt", "x"), tool_use("finish", {"summary": "done"}))

    go(FakeModelClient([delete_then_reply]))
    s = db()
    s.expire_all()
    from app.kaisen.models import KaisenFile
    assert s.query(KaisenFile).filter_by(build_id=env.bid).count() == 0
    assert s.query(models.App).filter(models.App.kaisen_build_id == env.bid).count() == 0
    assert s.query(KaisenRun).filter_by(build_id=env.bid).one().status == "stopped"


# ---- redaction --------------------------------------------------------------------------------------

def test_transcript_and_files_are_redacted_on_read(env):
    start(env)
    leak = f"my key is {KEY} and token {TOKEN}"
    go(FakeModelClient([reply({"type": "text", "text": leak}, write("notes.txt", leak)),
                        reply(tool_use("finish", {"summary": f"summary with {KEY}"}))]))
    env.a.put(f"/projects/{env.pid}/kaisen", json={"github_token": TOKEN})
    d = env.a.get(f"/kaisen/builds/{env.bid}")
    assert KEY not in d.text and TOKEN not in d.text and "[REDACTED]" in d.text
    f = env.a.get(f"/kaisen/builds/{env.bid}/files/notes.txt").json()
    assert KEY not in f["content"] and "[REDACTED]" in f["content"]


def test_deploy_logs_are_sanitized_for_the_model(env):
    start(env)
    noisy = "z" * 9000 + f"\n\x1b[31mred\x1b[0m line\r\nbundle https://x/kaisen/bundles/{'A' * 43}.tar.gz key {KEY}\n"
    model = FakeModelClient([reply(write("kaisen.json", "{}"), tool_use("deploy_preview", {})),
                             reply(tool_use("finish", {"summary": "ok"}))])
    go(model, FakeRuntime(logs=noisy))
    text = model.calls[1][-1]["content"][-1]["content"]
    assert "\x1b" not in text and "A" * 43 not in text and KEY not in text and "[REDACTED]" in text
    assert len(text.encode()) <= config.KAISEN_PREVIEW_RESULT_BYTES and "[truncated]" in text


def test_unhealthy_preview_is_reported_not_ok(env):
    start(env)
    model = FakeModelClient([reply(write("kaisen.json", "{}"), tool_use("deploy_preview", {})),
                             reply(tool_use("finish", {"summary": "ok"}))])
    go(model, FakeRuntime(code=500, stay=None))
    result = model.calls[1][-1]["content"][-1]
    assert result["is_error"] is True and "NOT OK" in result["content"] and "http: 500" in result["content"]


# ---- worker plumbing ----------------------------------------------------------------------------------

def test_heartbeat_file_is_touched(tmp_path, monkeypatch, db_session):
    hb = tmp_path / "heartbeat"
    monkeypatch.setattr(config, "KAISEN_HEARTBEAT_FILE", str(hb))
    worker.touch_heartbeat()
    assert hb.exists()
    os.utime(hb, (1, 1))
    w = worker.Worker(S=Keep(db_session))
    w.run_once()  # idle iteration still touches
    assert hb.stat().st_mtime > 1000


def test_heartbeat_thread_runs_while_main_loop_is_busy(tmp_path, monkeypatch, db_session):
    hb = tmp_path / "heartbeat"
    monkeypatch.setattr(config, "KAISEN_HEARTBEAT_FILE", str(hb))
    monkeypatch.setattr(config, "KAISEN_HEARTBEAT_SECONDS", 0.05)
    w = worker.Worker(S=Keep(db_session))
    t = threading.Thread(target=w.heartbeat_loop, daemon=True)
    t.start()
    time.sleep(0.2)
    os.utime(hb, (1, 1))  # as if a long model stream had starved the loop
    time.sleep(0.3)
    w.stop.set()
    assert hb.stat().st_mtime > 1000


def test_run_once_survives_a_failing_tick(tmp_path, monkeypatch, db_session):
    monkeypatch.setattr(config, "KAISEN_HEARTBEAT_FILE", str(tmp_path / "hb"))

    class Boom(worker.Lease):
        def acquire(self):
            raise RuntimeError("db down")

    assert worker.Worker(S=Keep(db_session), lease=Boom()).run_once() is False


def test_sqlite_lease_is_always_leader():
    lease = worker.Lease()
    assert lease.acquire() and lease.check()


def test_worker_is_not_imported_by_the_api():
    root = os.path.dirname(os.path.dirname(__file__))
    code = f"import sys; sys.path.insert(0, {root!r}); import app.main; assert 'app.kaisen.worker' not in sys.modules, 'worker imported'"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "DATABASE_URL": "sqlite:///:memory:"})
    assert r.returncode == 0, r.stderr


def test_retention_trims_old_transcripts_and_events(env):
    start(env)
    go(FakeModelClient([reply(write("a.txt", "1")), reply(tool_use("finish", {"summary": "ok"}))]))
    s = db()
    run = s.query(KaisenRun).one()
    run.finished_at = run.finished_at - timedelta(days=config.KAISEN_TRANSCRIPT_DAYS + 1)
    for e in s.query(KaisenEvent).all():
        e.at = e.at - timedelta(days=config.KAISEN_EVENT_DAYS + 1)
    s.commit()
    assert worker.retention(Keep(s)) == {"transcripts": 1, "events": 2}
    run = detail(env)["runs"][0]
    assert run["transcript"] is None and run["transcript_trimmed"] is True
    # a trimmed run cannot be resumed from; a fresh build run starts instead
    assert builder.start_build(s, s.get(KaisenBuild, env.bid))[0].kind == "build"


def test_push_on_finish_uses_the_build_token_and_respects_the_flag(env):
    build_token = "not-the-build-github-token"
    r = env.a.put(f"/kaisen/builds/{env.bid}/output", json={
        "mode": "github", "repo": "https://github.com/octo/mine.git", "auth": "build", "github_token": build_token,
        "push_on_finish": True})
    assert r.status_code == 200, r.text
    start(env)
    seen, gh = [], Fakes().github
    model = FakeModelClient([reply(write("app.py", "x = 1"), tool_use("finish", {"summary": "done"}))])
    assert go(model, gh_fn=lambda t: (seen.append(t), gh)[1]) is True
    assert seen == [build_token] and gh.pushes[-1]["repo"] == "octo/mine"
    d = detail(env)
    assert any(i["kind"] == "push" and "Pushed to" in i["text"] for i in d["inbox"]) and build_token not in str(d)

    env.a.put(f"/kaisen/builds/{env.bid}/output", json={"push_on_finish": False})
    start(env)
    seen.clear()
    assert go(FakeModelClient([reply(tool_use("finish", {"summary": "again"}))]), gh_fn=lambda t: (seen.append(t), gh)[1]) is True
    assert seen == []


# ---- remaining run_step / tick branches ----------------------------------------------------------

def test_unexpected_model_failure_and_no_text_nudge_and_turn_limit(env, monkeypatch):
    start(env)
    go(FakeModelClient([RuntimeError("boom")]))
    assert detail(env)["status"] == "error"
    start(env)
    # a reply with no tool call is nudged; MAX_TURNS then stops the step
    monkeypatch.setattr(config, "KAISEN_MAX_TURNS", 2)
    go(FakeModelClient([reply({"type": "text", "text": "hmm"}), reply({"type": "text", "text": "hmm"})]))
    assert detail(env)["status"] == "stopped"


def test_wall_clock_limit_and_lease_loss_and_skipped_run(env, monkeypatch):
    start(env)
    rid = db().query(KaisenRun).filter_by(build_id=env.bid).one().id
    ticks = iter([0, 10_000, 10_000, 10_000])
    assert builder.run_step(Keep(db()), rid, FakeModelClient(), worker.Lease(), clock=lambda: next(ticks)) == "stopped"
    assert "wall-clock" in detail(env)["inbox"][0]["text"]
    assert builder.run_step(Keep(db()), rid, FakeModelClient(), worker.Lease()) == "skipped"
    assert builder.run_step(Keep(db()), 99999, FakeModelClient(), worker.Lease()) == "skipped"

    class Lost:
        def check(self):
            return False
    start(env)
    rid = max(r.id for r in db().query(KaisenRun).filter_by(build_id=env.bid))
    assert builder.run_step(Keep(db()), rid, FakeModelClient([reply(tool_use("list_files", {}))]), Lost()) == "lease_lost"


def test_metering_failure_stops_the_run(env, monkeypatch):
    start(env)
    def bad(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(builder, "record_usage", bad)
    go(FakeModelClient([reply(tool_use("list_files", {}))]))
    assert detail(env)["status"] == "error"


def test_tool_errors_are_results_and_only_one_question_and_no_previews(env):
    start(env)
    go(FakeModelClient([
        reply(tool_use("ask_human", {"question": "a?"}, id="q1"), tool_use("ask_human", {"question": "b?"}, id="q2"),
              tool_use("deploy_preview", {}, id="d1"), tool_use("ask_human", {}, id="q3")),
    ]), runtime=FakeRuntime())
    assert detail(env)["open_questions"] == 1
    run = db().query(KaisenRun).filter_by(build_id=env.bid).one()
    blob = str(run.messages) + str(run.pending)
    assert "Only one question" in blob


def test_tick_edge_cases(env, monkeypatch):
    # deployer missing: tool error text
    start(env)
    ws_s = db()
    run = ws_s.query(KaisenRun).filter_by(build_id=env.bid).one()
    out = builder._handle_response(ws_s, run, types.SimpleNamespace(
        stop_reason=None, content=[tool_use("deploy_preview", {}, id="x")]), [], None)
    assert out == "continue" and "not available" in str(run.messages)
    ws_s.rollback()
    # model factory rejects the key
    def bad_model(key):
        raise ModelError("bad key")
    assert worker.tick(Keep(db()), worker.Lease(), model_fn=bad_model, runtime=FakeRuntime(),
                       registry_factory=lambda: FakeRegistry()) is True
    assert detail(env)["status"] == "error"
    # deleted build: its queued run is stopped
    start(env)
    assert env.a.delete(f"/kaisen/builds/{env.bid}").status_code in (200, 204)
    go(FakeModelClient())
    assert db().query(KaisenRun).filter(KaisenRun.status.in_(("queued", "running"))).count() == 0


def test_worker_factories_and_main_loop(env, monkeypatch, tmp_path):
    from app.kaisen.github import RealGitHub
    from app.kaisen.llm import AnthropicClient
    assert isinstance(worker.model_factory(KEY), AnthropicClient)
    assert isinstance(worker.github_factory(TOKEN), RealGitHub)
    assert worker.session_factory() is not None
    monkeypatch.setattr(config, "KAISEN_HEARTBEAT_FILE", str(tmp_path / "hb"))
    w = worker.Worker(S=Keep(db()), lease=worker.Lease(), runtime=FakeRuntime())
    calls = []
    def once():
        calls.append(1)
        w.stop.set()
        return False
    monkeypatch.setattr(w, "run_once", once)
    w.run()
    assert calls == [1]


# ---- suggestions, web access, pause_turn ------------------------------------------------------------------------

def _web_blocks(query="fastapi docs", url="https://docs.example.com/x"):
    return [{"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": query}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
             "content": [{"type": "web_search_result", "url": url, "title": "PAGE-BODY-SECRET-TEXT", "encrypted_content": "enc"}]}]


def _usage(searches=0):
    return {"input_tokens": 1000, "output_tokens": 500, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "web_search_requests": searches}


def test_finish_summary_next_lines_become_suggestions(env):
    start(env)
    model = FakeModelClient([reply(write("kaisen.json", '{"runtime": "static"}'), write("index.html", "<p>x</p>")),
                             reply(tool_use("finish", {"summary": "Built it.\nNext: add dark mode\nNext: add export"}))])
    go(model)
    run = detail(env)["runs"][0]
    assert run["summary"] == "Built it." and run["suggestions"] == ["add dark mode", "add export"]
    assert db().get(KaisenRun, run["id"]).suggestions == ["add dark mode", "add export"]


def test_web_access_is_off_by_default_and_passes_nothing_to_the_model(env):
    start(env)
    model = FakeModelClient([reply(tool_use("finish", {"summary": "ok"}))])
    go(model)
    assert model.webs == [None]


def test_web_access_on_passes_the_allow_list_and_counts_search_cost(env):
    r = env.a.put(f"/projects/{env.pid}/kaisen", json={"web_access": True, "web_allowed_domains": ["Docs.Example.com"]})
    assert r.status_code == 200 and r.json()["web_access"] is True and r.json()["web_allowed_domains"] == ["docs.example.com"]
    start(env)
    model = FakeModelClient([
        reply(*_web_blocks(), tool_use("list_files", {}), usage=_usage(searches=2)),
        reply(tool_use("finish", {"summary": "ok"})),
    ])
    go(model)
    assert model.webs == [{"allowed_domains": ["docs.example.com"]}] * 2
    d = detail(env)
    extra = 2 * config.KAISEN_WEB_SEARCH_USD
    assert d["runs"][0]["cost_usd"] == pytest.approx(2 * PER_CALL + extra)
    ev = db().query(KaisenEvent).filter_by(build_id=env.bid).order_by(KaisenEvent.id).all()
    assert [e.web_searches for e in ev] == [2, 0] and ev[0].usd == pytest.approx(PER_CALL + extra)
    assert env.a.get("/kaisen/spend", params={"project_id": env.pid}).json()["today_usd"] == pytest.approx(2 * PER_CALL + extra)


def test_web_tools_omitted_for_a_model_without_support(env, monkeypatch):
    monkeypatch.setattr(config, "KAISEN_WEB_MODELS", ("claude-opus-5-5",))
    env.a.put(f"/projects/{env.pid}/kaisen", json={"web_access": True})
    start(env)
    model = FakeModelClient([reply(tool_use("finish", {"summary": "ok"}))])
    go(model)
    assert model.webs == [None]


@pytest.mark.parametrize("domains,expect", [([], None), (["docs.example.com"], {"allowed_domains": ["docs.example.com"]})])
def test_build_with_secrets_gets_web_only_with_an_allow_list(env, domains, expect):
    # pentest K16: injected page text + build secrets + auto-deploy is an exfiltration chain
    env.a.put(f"/projects/{env.pid}/kaisen", json={"web_access": True, "web_allowed_domains": domains})
    assert env.a.put(f"/kaisen/builds/{env.bid}/secrets/WEATHER_TOKEN", json={"value": "not-the-weather-token"}).status_code == 200
    start(env)
    model = FakeModelClient([reply(tool_use("finish", {"summary": "ok"}))])
    go(model)
    assert model.webs == [expect]


def test_pause_turn_resends_the_paused_message_and_the_run_continues(env):
    env.a.put(f"/projects/{env.pid}/kaisen", json={"web_access": True})
    start(env)
    model = FakeModelClient([
        reply(*_web_blocks(), stop_reason="pause_turn"),
        lambda msgs: reply(tool_use("finish", {"summary": "after pause"})),
    ])
    go(model)
    assert detail(env)["status"] == "done" and len(model.calls) == 2
    assert model.calls[1][-1]["role"] == "assistant" and model.calls[1][-1]["content"][0]["type"] == "server_tool_use"
    assert db().get(KaisenRun, detail(env)["runs"][0]["id"]).messages[1]["content"][1]["content"][0]["encrypted_content"] == "enc"


def test_transcript_shows_what_was_searched_never_page_bodies(env):
    env.a.put(f"/projects/{env.pid}/kaisen", json={"web_access": True})
    start(env)
    model = FakeModelClient([reply(*_web_blocks(), tool_use("list_files", {})), reply(tool_use("finish", {"summary": "ok"}))])
    go(model)
    text = " ".join(t["text"] for t in detail(env)["runs"][0]["transcript"])
    assert "[web] searched for fastapi docs" in text and "PAGE-BODY-SECRET-TEXT" not in text
