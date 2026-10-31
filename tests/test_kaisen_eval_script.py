"""Unit tests for scripts/kaisen_eval.py (H6). Pure logic over httpx.MockTransport; nothing touches the network."""
import importlib.util
import io
import json
import tarfile
import sys
from pathlib import Path

import httpx
import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "kaisen_eval.py"
_spec = importlib.util.spec_from_file_location("kaisen_eval", _PATH)
ke = importlib.util.module_from_spec(_spec)
sys.modules["kaisen_eval"] = ke
_spec.loader.exec_module(ke)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ke, "CHECK_RETRY_SLEEP", 0)


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def write_briefs(tmp_path, data):
    p = tmp_path / "b.json"
    p.write_text(json.dumps(data))
    return p


GOOD = {"name": "x", "brief": "do it", "checks": [{"method": "GET", "path": "/", "expect_status": 200}]}


# ---- brief loading
def test_shipped_briefs_are_valid_and_complete():
    briefs = ke.load_briefs()
    assert len(briefs) == 8
    by = {b['name']: b for b in briefs}
    assert by['Small CRM']['database'] == 'postgres' and by['Small CRM']['version'] == 1 and by['Small CRM']['flow']
    assert by['Kanban board']['version'] == 2 and '/api/signup' in by['Kanban board']['brief'] and '/api/login' in by['Kanban board']['brief']
    assert '/api/import' in by['Small CRM']['brief'] and '/api/export.csv' in by['Small CRM']['brief']
    guide = (Path(__file__).resolve().parents[1] / "docs" / "USER_GUIDE.md").read_text()
    for b in briefs[:5]:
        assert b["brief"] in guide  # copied exactly from the user guide
    assert all(len(b["brief"]) <= 8000 for b in briefs)


def test_load_select_by_name_case_insensitive():
    got = ke.load_briefs(names=["habit tracker", "Recipe scaler"])
    assert [b["name"] for b in got] == ["Habit tracker", "Recipe scaler"]


def test_load_unknown_name(tmp_path):
    with pytest.raises(ke.EvalError, match="unknown brief"):
        ke.load_briefs(names=["nope"])


@pytest.mark.parametrize("mutate", [
    lambda b: b.pop("name"),
    lambda b: b.update(brief=""),
    lambda b: b.update(brief="x" * 8001),
    lambda b: b.update(checks=[]),
    lambda b: b.update(checks=[{"method": "FETCH", "path": "/", "expect_status": 200}]),
    lambda b: b.update(checks=[{"method": "GET", "path": "x", "expect_status": 200}]),
    lambda b: b.update(checks=[{"method": "GET", "path": "/"}]),
])
def test_load_rejects_invalid(tmp_path, mutate):
    b = json.loads(json.dumps(GOOD))
    mutate(b)
    with pytest.raises(ke.EvalError):
        ke.load_briefs(write_briefs(tmp_path, [b]))


def test_load_rejects_duplicates_and_bad_json(tmp_path):
    with pytest.raises(ke.EvalError, match="duplicate"):
        ke.load_briefs(write_briefs(tmp_path, [GOOD, GOOD]))
    p = tmp_path / "bad.json"
    p.write_text("{")
    with pytest.raises(ke.EvalError):
        ke.load_briefs(p)
    with pytest.raises(ke.EvalError):
        ke.load_briefs(tmp_path / "missing.json")


# ---- check evaluation
def chk_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_status_and_contains():
    c = chk_client(lambda r: httpx.Response(201, json={"title": "eval note"}))
    base = "https://p.example"
    assert ke.evaluate_check(c, base, {"method": "POST", "path": "/api", "body": {"a": 1},
                                       "expect_status": [200, 201], "expect_contains": "eval note"})[0]
    ok, why = ke.evaluate_check(c, base, {"method": "GET", "path": "/", "expect_status": 200})
    assert not ok and "201" in why
    ok, why = ke.evaluate_check(c, base, {"method": "GET", "path": "/", "expect_status": 201, "expect_contains": "zzz"})
    assert not ok and "lacks" in why


def test_check_sends_json_body_and_retries_5xx():
    seen = []

    def h(req):
        seen.append((req.method, req.url.path, req.content))
        return httpx.Response(503) if len(seen) < 3 else httpx.Response(200, text="ok")
    ok, _ = ke.evaluate_check(chk_client(h), "https://p", {"method": "POST", "path": "/x", "body": {"k": "v"}, "expect_status": 200})
    assert ok and len(seen) == 3 and json.loads(seen[0][2]) == {"k": "v"}


def test_check_connection_error():
    def h(req):
        raise httpx.ConnectError("boom", request=req)
    ok, why = ke.evaluate_check(chk_client(h), "https://p", {"method": "GET", "path": "/", "expect_status": 200})
    assert not ok and "request failed" in why


def test_preview_base():
    assert ke.preview_base({"app_hostname": "a.picopaas.xyz"}) == "https://a.picopaas.xyz"
    assert ke.preview_base({"app": {"hostname": "https://b.x/"}}) == "https://b.x"
    assert ke.preview_base({}) is None


# ---- fake platform
class Platform:
    """Scripted Kaisen API. `script` is a list of detail dicts returned by successive GET /kaisen/builds/{id}."""

    def __init__(self, script, create=None):
        self.script = list(script)
        self.calls = []
        self.create = create or {"id": 7, "status": "queued", "start_error": None}
        self.last = None

    def __call__(self, req):
        self.calls.append((req.method, req.url.path, req.url.params.get("delete_app")))
        path = req.url.path
        if req.method == "POST" and path == "/kaisen/builds":
            self.created_body = json.loads(req.content)
            return httpx.Response(201, json=self.create)
        if req.method == "GET" and path == "/kaisen/builds/7":
            if len(self.script) > 1:
                self.last = self.script.pop(0)
            else:
                self.last = self.script[0]
            return httpx.Response(200, json=self.last)
        if req.method == "POST" and path.endswith("/answer"):
            self.answer = json.loads(req.content)
            return httpx.Response(200, json={})
        if req.method == "POST" and path.endswith("/run"):
            return httpx.Response(200, json={})
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "nope"})

    def names(self):
        return [(m, p) for m, p, _ in self.calls]


def D(status, **kw):
    d = {"id": 7, "status": status, "runs": [], "inbox": [], "cost_usd": 0.0, "tests": None, "app_hostname": None}
    d.update(kw)
    return d


def make_eval(platform, check_handler=None, **kw):
    clock = Clock()
    api = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(platform))
    chk = chk_client(check_handler or (lambda r: httpx.Response(200, text="hi")))
    kw.setdefault("max_usd", 1.0)
    ev = ke.Evaluator(api, chk, project_id=5, sleep=clock.sleep, clock=clock.now, poll_seconds=10,
                      timeout_seconds=1200, log=lambda m: None, **kw)
    return ev, clock


# ---- poll loop
def test_happy_path_records_everything_and_deletes():
    runs = [{"id": 2, "step": 3, "review": {"verdict": "approve"}}, {"id": 1, "step": 5, "review": None}]
    done = D("done", runs=runs, cost_usd=0.042, tests={"status": "pass"}, app_hostname="p.picopaas.xyz")
    p = Platform([D("queued"), D("running"), done])
    ev, clock = make_eval(p)
    row = ev.run_brief(GOOD)
    assert row["status"] == "done" and row["runs"] == 2 and row["steps"] == 8
    assert row["cost"] == pytest.approx(0.042) and row["tests"] == "pass" and row["review"] == "approve"
    assert row["checks_passed"] == 1 and row["checks_total"] == 1
    assert row["minutes"] == pytest.approx(20 / 60, abs=0.1)
    assert ("DELETE", "/kaisen/builds/7") in p.names()
    assert p.calls[-1][2] == "true"  # delete_app=true
    assert p.created_body["project_id"] == 5 and p.created_body["brief"] == "do it"


def test_idle_create_triggers_run():
    p = Platform([D("done", app_hostname="h")], create={"id": 7, "status": "idle", "start_error": None})
    ev, _ = make_eval(p)
    ev.run_brief(GOOD)
    assert ("POST", "/kaisen/builds/7/run") in p.names()


def test_start_error_recorded_and_still_cleaned():
    p = Platform([], create={"id": 7, "status": "idle", "start_error": "no key"})
    ev, _ = make_eval(p)
    row = ev.run_brief(GOOD)
    assert row["status"] == "error" and any("no key" in n for n in row["notes"])
    assert ("DELETE", "/kaisen/builds/7") in p.names()


def test_waiting_answers_once_then_done():
    q = {"id": 11, "kind": "question", "status": "open", "text": "which?"}
    p = Platform([D("waiting", inbox=[q]), D("waiting", inbox=[dict(q, status="closed")]),
                  D("running"), D("done", app_hostname="h")])
    ev, _ = make_eval(p)
    row = ev.run_brief(GOOD)
    assert row["status"] == "done" and "answered ask_human" in row["notes"]
    assert p.answer == {"text": "Decide yourself and note the assumption."}
    assert sum(1 for m, pa in p.names() if pa.endswith("/answer")) == 1


def test_second_question_is_left_waiting():
    q1 = {"id": 11, "kind": "question", "status": "open"}
    q2 = {"id": 12, "kind": "question", "status": "open"}
    p = Platform([D("waiting", inbox=[q1]), D("waiting", inbox=[q2])])
    ev, _ = make_eval(p)
    row = ev.run_brief(GOOD)
    assert row["status"] == "waiting"
    assert sum(1 for m, pa in p.names() if pa.endswith("/answer")) == 1
    assert row["checks_passed"] == 0


def test_stopped_resumes_once_then_gives_up():
    p = Platform([D("stopped", last_error="Repeat detector"), D("running"), D("stopped", last_error="again")])
    ev, _ = make_eval(p)
    row = ev.run_brief(GOOD)
    assert row["status"] == "stopped" and "resumed once" in row["notes"]
    assert sum(1 for m, pa in p.names() if pa == "/kaisen/builds/7/run") == 1


def test_stopped_then_resumed_done():
    p = Platform([D("stopped", last_error="Step turn limit (40) reached."), D("running"), D("done", app_hostname="h")])
    ev, _ = make_eval(p)
    row = ev.run_brief(GOOD)
    assert row["status"] == "done" and row["checks_passed"] == 1


def test_cap_stop_is_not_resumed():
    p = Platform([D("stopped", last_error="Run cap reached")])
    ev, _ = make_eval(p)
    row = ev.run_brief(GOOD)
    assert row["status"] == "stopped" and "resumed once" not in row["notes"]
    assert not any(pa.endswith("/run") for m, pa in p.names())


def test_timeout():
    p = Platform([D("running")])
    ev, clock = make_eval(p)
    row = ev.run_brief(GOOD)
    assert row["status"] == "timeout" and clock.t >= 1200


def test_keep_skips_delete():
    p = Platform([D("done", app_hostname="h")])
    ev, _ = make_eval(p, keep=True)
    ev.run_brief(GOOD)
    assert not any(m == "DELETE" for m, _ in p.names())


def test_failed_check_noted_and_api_error_surfaces():
    p = Platform([D("done", app_hostname="h")])
    ev, _ = make_eval(p, check_handler=lambda r: httpx.Response(404))
    row = ev.run_brief(GOOD)
    assert row["checks_passed"] == 0 and any("404" in n for n in row["notes"])

    def broken(req):
        return httpx.Response(500, json={"detail": "x"})
    ev2 = ke.Evaluator(httpx.Client(base_url="https://a", transport=httpx.MockTransport(broken)), chk_client(lambda r: httpx.Response(200)),
                       project_id=1, max_usd=1, log=lambda m: None)
    row2 = ev2.run_brief(GOOD)
    assert row2["status"] == "error" and "500" in row2["notes"][0]


# ---- spend guard
def test_spend_guard_skips_remaining():
    class Multi(Platform):
        def __call__(self, req):
            if req.method == "GET":
                return httpx.Response(200, json=D("done", cost_usd=0.6, app_hostname="h"))
            return super().__call__(req)
    p = Multi([])
    ev, _ = make_eval(p, max_usd=1.0)
    briefs = [dict(GOOD, name=n) for n in "abcd"]
    rows, spent = ev.run_all(briefs)
    assert [r["status"] for r in rows] == ["done", "done", "skipped", "skipped"]
    assert spent == pytest.approx(1.2)
    assert "spend guard" in rows[2]["notes"][0]
    assert sum(1 for m, pa in p.names() if pa == "/kaisen/builds" and m == "POST") == 2


# ---- table
def test_render_table_and_totals():
    rows = [
        {"name": "A", "status": "done", "runs": 2, "steps": 11, "cost": 0.061, "minutes": 11.0, "tests": "pass",
         "checks_passed": 2, "checks_total": 2, "review": "approve", "notes": ["resumed once"]},
        {"name": "B", "status": "skipped", "runs": 0, "steps": 0, "cost": 0.0, "minutes": 0.0, "tests": "-",
         "checks_passed": 0, "checks_total": 3, "review": "-", "notes": ["a|b"]},
    ]
    t = ke.render_table(rows, date="2026-10-30")
    assert "| A | - | done | 2 | 11 | $0.061 | 11.0 min | pass | 2/2 | approve | resumed once |" in t
    assert "a/b" in t  # pipes escaped
    assert "Totals: 1/2 done, cost $0.061, 11.0 min, 11 steps, checks 2/2." in t


# ---- CLI
def test_import_has_no_side_effects_and_cli_requires_token(monkeypatch, capsys):
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    assert ke.main(["--project-id", "1", "--token-env", "NOPE_TOKEN"]) == 2
    assert "NOPE_TOKEN" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        ke.parse_args([])  # --project-id required


def test_main_end_to_end_with_out_file_never_prints_token(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MY_TOK", "not-the-token-value")
    p = Platform([D("done", cost_usd=0.01, app_hostname="h")])
    real = httpx.Client

    def fake_client(*a, **kw):
        kw["transport"] = httpx.MockTransport(p)
        return real(*a, **kw)
    monkeypatch.setattr(ke.httpx, "Client", fake_client)
    out = tmp_path / "r.md"
    out.write_text("# existing\n")
    rc = ke.main(["--project-id", "5", "--briefs", "Habit tracker", "--token-env", "MY_TOK", "--out", str(out)])
    text = out.read_text()
    assert rc == 0 and text.startswith("# existing") and "Habit tracker" in text
    cap = capsys.readouterr()
    assert "not-the-token-value" not in cap.out + cap.err + text


# ---- data collection, metrics, settings
def call(name, **inp):
    return {"role": "assistant", "text": f"[call] {name} {json.dumps(inp)}"}


def res(text):
    return {"role": "user", "text": "[result] " + text}


def make_tar(files, top="slug"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            raw = data.encode()
            ti = tarfile.TarInfo(f"{top}/{path}")
            ti.size = len(raw)
            tf.addfile(ti, io.BytesIO(raw))
    return buf.getvalue()


TAR = make_tar({"Dockerfile": "FROM python\nUSER app\nCMD x\n", "PLAN.md": "- [x] a\n- [X] b\n- [ ] c\n",
                "kaisen.json": '{"port": 8000}', "README.md": "hi", "tests/test_a.py": "x", "app.py": "print(1)",
                "CONVERSATION.md": "long"})


def test_analyze_transcripts_counts_and_pairs_results():
    runs = [
        {"id": 2, "context_resets": 1, "error": "Step turn limit (40) reached.", "transcript": [
            call("write_file", path="a.py"), res("wrote a.py\nWARNING: syntax error line 3\nWARNING: x"),
            call("deploy_preview"), res("Result: NOT OK\nstatus: crash"),
            call("http_request", path="/"), res("HTTP 500\nboom"),
            call("mystery_tool"), res("whatever"),
        ]},
        {"id": 1, "context_resets": 0, "error": "Repeat detector: the same call was made 3 times in a row.", "transcript": [
            call("deploy_preview"), res("Result: OK\nstatus: running"),
            call("http_request", path="/"), res("HTTP 200\nok"),
            call("http_request", path="/x"), res("HTTP 200\nok"),
            call("read_file"), res("Repeat detector: identical call 3 times in a row; run stopped."),
            call("finish"),
        ]},
        {"id": 3, "transcript": None, "error": "Model hit max_tokens; step stopped. Resume to retry."},
    ]
    m = ke.analyze_transcripts(runs)
    t = m["tool_calls"]
    assert (t["write_file"], t["deploy_preview"], t["http_request"], t["finish"], t["read_file"]) == (1, 2, 3, 1, 1)
    assert m["other_tool_calls"] == 1
    assert m["write_warnings"] == 2
    assert (m["deploy_not_ok"], m["deploy_ok"]) == (1, 1)
    assert m["http_status"] == {"500": 1, "200": 2}
    assert m["repeat_stops"] == 1  # result line and run.error are the same event
    assert m["max_tokens_stops"] == 1 and m["turn_limit_stops"] == 1
    assert m["context_resets"] == 1 and m["transcripts_missing"] == 1


def test_analyze_transcripts_tolerates_garbage():
    m = ke.analyze_transcripts([{"id": 1, "transcript": ["x", None, {"role": "user"}, {"text": 5}]}, {}])
    assert sum(m["tool_calls"].values()) == 0


def test_analyze_tarball():
    c = ke.analyze_tarball(TAR)
    assert c["files"] == 6 and c["has_tests"] and c["has_readme"]  # CONVERSATION.md not counted
    assert c["dockerfile_user"] == "USER app" and c["kaisen_json"] == {"port": 8000}
    assert (c["plan_ticked"], c["plan_unticked"]) == (2, 1)
    assert c["bytes"] > 0
    assert ke.analyze_tarball(b"not a tar")["error"]
    bare = ke.analyze_tarball(make_tar({"app.py": "x"}))
    assert not bare["has_tests"] and not bare["has_readme"] and bare["dockerfile_user"] is None and bare["plan_ticked"] is None


def test_run_seconds_and_slug():
    assert ke.run_seconds({"started_at": "2026-10-30T10:00:00", "finished_at": "2026-10-30T10:02:30"}) == 150
    assert ke.run_seconds({"started_at": "2026-10-30T10:00:00", "finished_at": None}) is None
    assert ke.slugify("Habit tracker!") == "habit-tracker"


def test_briefs_accept_slugs_in_given_order():
    got = ke.load_briefs(names=["recipe-scaler", "Habit tracker"])
    assert [b["name"] for b in got] == ["Recipe scaler", "Habit tracker"]


class DataPlatform(Platform):
    """Adds download, diff and project settings on top of the scripted platform."""

    def __init__(self, *a, diff_status=200, **kw):
        super().__init__(*a, **kw)
        self.settings = {"model": "old-model", "effort": "low"}
        self.puts = []
        self.diff_status = diff_status
        self.auth_seen = []

    def __call__(self, req):
        path = req.url.path
        if path == "/projects/5/kaisen":
            if req.method == "PUT":
                body = json.loads(req.content)
                self.puts.append(body)
                self.settings.update(body)
            return httpx.Response(200, json=dict(self.settings))
        if path == "/kaisen/builds/7/download":
            return httpx.Response(200, content=TAR)
        if path.endswith("/diff"):
            return httpx.Response(self.diff_status, json={"files": [{"path": "a"}]} if self.diff_status == 200 else {"detail": "no"})
        return super().__call__(req)


RUN = {"id": 9, "step": 4, "has_diff": True, "last_tool": "finish", "cost_usd": 0.02, "context_resets": 0,
       "started_at": "2026-10-30T10:00:00", "finished_at": "2026-10-30T10:01:00", "review": {"verdict": "approve"},
       "transcript": [call("deploy_preview"), res("Result: OK\nstatus: running"), call("finish")]}


def test_data_dir_files_and_metrics(tmp_path):
    p = DataPlatform([D("running", runs=[dict(RUN, transcript=[])]), D("done", runs=[RUN], cost_usd=0.02, app_hostname="h")])
    ev, _ = make_eval(p, data_dir=tmp_path / "d", model="m1", scrub=["not-the-token-value"])
    row = ev.run_brief(GOOD)
    d = tmp_path / "d" / "x"
    assert row["model"] == "m1" and row["status"] == "done"
    assert json.loads((d / "detail.json").read_text())["status"] == "done"
    assert (d / "code.tar.gz").read_bytes() == TAR
    assert json.loads((d / "run-9-diff.json").read_text())["files"]
    checks = json.loads((d / "checks.json").read_text())
    assert checks[0]["passed"] and checks[0]["status"] == 200 and checks[0]["attempts"] == 1
    polls = [json.loads(x) for x in (d / "poll.jsonl").read_text().splitlines()]
    assert len(polls) == 2 and polls[0]["status"] == "running" and polls[1]["last_tool"] == "finish"
    assert row["metrics"]["tool_calls"]["deploy_preview"] == 1
    assert row["first_deploy_ok_min"] == 0.2  # 10 s, seen on the second poll, rounded to 0.1 min
    assert row["run_seconds"] == [60.0] and row["code"]["files"] == 6
    assert ("DELETE", "/kaisen/builds/7") in p.names()  # deleted after the data was saved


def test_check_body_snippet_is_capped(tmp_path):
    p = DataPlatform([D("done", app_hostname="h")])
    ev, _ = make_eval(p, data_dir=tmp_path, check_handler=lambda r: httpx.Response(200, text="z" * 5000))
    ev.run_brief(GOOD)
    rec = json.loads((tmp_path / "x" / "checks.json").read_text())[0]
    assert len(rec["body_snippet"]) == 2048


def test_diff_404_is_tolerated(tmp_path):
    p = DataPlatform([D("done", runs=[RUN], app_hostname="h")], diff_status=404)
    ev, _ = make_eval(p, data_dir=tmp_path)
    row = ev.run_brief(GOOD)
    assert row["status"] == "done" and not list((tmp_path / "x").glob("run-*"))
    assert (tmp_path / "x" / "detail.json").exists()


def test_keep_still_saves_data_and_does_not_delete(tmp_path):
    p = DataPlatform([D("done", app_hostname="h")])
    ev, _ = make_eval(p, data_dir=tmp_path, keep=True)
    ev.run_brief(GOOD)
    assert (tmp_path / "x" / "code.tar.gz").exists()
    assert not any(m == "DELETE" for m, _ in p.names())


def test_writer_scrubs_secret_and_never_writes_headers(tmp_path):
    secret = "not-the-token-value"
    p = DataPlatform([D("done", runs=[dict(RUN, summary=f"leaked {secret}")], app_hostname="h")])
    api = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(p),
                       headers={"Authorization": f"Bearer {secret}"})
    ev = ke.Evaluator(api, chk_client(lambda r: httpx.Response(200)), project_id=5, max_usd=1, log=lambda m: None,
                      data_dir=tmp_path, scrub=[secret], sleep=lambda s: None)
    ev.run_brief(GOOD)
    blob = b"".join(f.read_bytes() for f in tmp_path.rglob("*") if f.is_file() and f.name != "code.tar.gz")
    assert secret.encode() not in blob and b"Authorization" not in blob and b"[redacted]" in blob


def test_settings_applied_and_restored():
    p = DataPlatform([D("done")])
    ev, _ = make_eval(p, model="new-model", effort="high")
    ev.apply_settings()
    assert p.settings == {"model": "new-model", "effort": "high"} and ev.model_used == "new-model"
    assert ev.restore_settings()
    assert p.settings == {"model": "old-model", "effort": "low"}
    assert ev.restore_settings()  # nothing left to restore
    assert len(p.puts) == 2


def test_no_flags_leaves_settings_alone():
    p = DataPlatform([D("done")])
    ev, _ = make_eval(p)
    ev.apply_settings()
    ev.restore_settings()
    assert p.puts == [] and ev.model_used == "old-model"


def test_main_restores_settings_after_failure_and_writes_summary(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MY_TOK", "not-the-token-value")
    p = DataPlatform([D("stopped", last_error="Run cap reached", runs=[RUN])])
    real = httpx.Client
    monkeypatch.setattr(ke.httpx, "Client", lambda *a, **kw: real(*a, **dict(kw, transport=httpx.MockTransport(p))))
    dd = tmp_path / "data"
    rc = ke.main(["--project-id", "5", "--briefs", "recipe-scaler,Habit tracker", "--token-env", "MY_TOK",
                  "--model", "new-model", "--data-dir", str(dd)])
    assert rc == 1
    assert p.settings["model"] == "old-model"  # restored in finally
    summ = json.loads((dd / "summary.json").read_text())
    assert [r["name"] for r in summ["rows"]] == ["Recipe scaler", "Habit tracker"]
    assert summ["model"] == "new-model" and summ["settings_restored"]
    md = (dd / "summary.md").read_text()
    assert "#### Behaviour" in md and "new-model" in md
    cap = capsys.readouterr()
    assert "not-the-token-value" not in cap.out + cap.err + md + json.dumps(summ)


def test_default_data_dir_under_scratch_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOK", "not-the-token-value")
    p = DataPlatform([D("done", app_hostname="h")])
    real = httpx.Client
    monkeypatch.setattr(ke.httpx, "Client", lambda *a, **kw: real(*a, **dict(kw, transport=httpx.MockTransport(p))))
    ke.main(["--project-id", "5", "--briefs", "Habit tracker", "--token-env", "MY_TOK", "--scratch-dir", str(tmp_path)])
    runs = list((tmp_path / "eval-data").iterdir())
    assert len(runs) == 1 and (runs[0] / "summary.json").exists()


def test_behaviour_table_renders():
    row = ke.new_row(GOOD, "m")
    row["status"] = "done"
    row["metrics"] = ke.analyze_transcripts([{"id": 1, "transcript": [call("write_file"), res("ok")]}])
    row["code"] = ke.analyze_tarball(TAR)
    row["first_deploy_ok_min"] = 1.5
    row["run_seconds"] = [60.0, None]
    t = ke.render_table([row])
    assert "write_file:1" in t and "1.5 min" in t and "60,?" in t and "USER app" in t and "2/1" in t


# ---- settle after done
def test_settle_waits_for_app_running_and_final_test():
    host = "p.picopaas.xyz"
    p = Platform([D("done", app={"status": "pending"}, tests={"status": "running"}, app_hostname=host),
                  D("done", app={"status": "running"}, tests={"status": "running"}, app_hostname=host),
                  D("done", app={"status": "running"}, tests={"status": "pass"}, app_hostname=host)])
    ev, _ = make_eval(p)
    row = ev.run_brief(GOOD)
    assert row["settle"] == {"settled": True, "seconds": 20.0} and row["tests"] == "pass" and row["checks_passed"] == 1
    assert not any("not settled" in n for n in row["notes"])


def test_settle_timeout_still_runs_checks_and_notes():
    p = Platform([D("done", app={"status": "pending"}, tests={"status": "running"}, app_hostname="h")])
    ev, _ = make_eval(p, settle_seconds=30)
    row = ev.run_brief(GOOD)
    assert row["settle"]["settled"] is False and row["checks_passed"] == 1
    assert any("not settled" in n and "app pending" in n and "test running" in n for n in row["notes"])
    assert "not settled" in ke.render_table([dict(row, metrics=ke.analyze_transcripts([]), code={})])


def test_settle_declared_test_in_tarball_waits_for_result():
    class P(DataPlatform):
        downloads = 0

        def __call__(self, req):
            self.downloads += req.url.path.endswith("/download")
            return super().__call__(req)
    global TAR
    old = TAR
    TAR = make_tar({"kaisen.json": '{"port": 8000, "test": "python -m unittest"}'})
    try:
        p = P([D("done", app={"status": "running"}, tests=None, app_hostname="h"),
               D("done", app={"status": "running"}, tests={"status": "fail"}, app_hostname="h")])
        ev, _ = make_eval(p)
        row = ev.run_brief(GOOD)
        assert row["settle"] == {"settled": True, "seconds": 10.0} and row["tests"] == "fail"
        assert p.downloads == 1  # cached, not fetched again
    finally:
        TAR = old


def test_settle_no_declared_test_does_not_wait():
    p = DataPlatform([D("done", app={"status": "running"}, tests=None, app_hostname="h")])  # TAR has no test key
    ev, _ = make_eval(p)
    row = ev.run_brief(GOOD)
    assert row["settle"] == {"settled": True, "seconds": 0.0} and row["tests"] == "none"


# ---- flows with a cookie jar
def crm_site():
    users, state = {}, {"contacts": [], "companies": []}

    def who(req):
        return req.headers.get("cookie", "").replace("sid=", "") or None

    def h(req):
        path, m = req.url.path, req.method
        if path == "/api/signup":
            b = json.loads(req.content)
            users[b["name"]] = b["password"]
            return httpx.Response(201, json={"ok": 1})
        if path == "/api/login":
            b = json.loads(req.content)
            if users.get(b["name"]) != b["password"]:
                return httpx.Response(401)
            return httpx.Response(200, json={}, headers={"set-cookie": f"sid={b['name']}; Path=/"})
        if path in ("/", "/health"):
            return httpx.Response(200, text="ok")
        if not who(req):
            return httpx.Response(401)
        if path == "/api/companies" and m == "POST":
            c = {"id": 41, **json.loads(req.content)}
            state["companies"].append(c)
            return httpx.Response(201, json=c)
        if path == "/api/companies":
            return httpx.Response(200, json=state["companies"])
        if path == "/api/contacts" and m == "POST":
            c = json.loads(req.content)
            assert c["company_id"] == 41  # captured id is substituted with its own type
            state["contacts"].append(c)
            return httpx.Response(201, json=c)
        if path == "/api/contacts":
            q = req.url.params.get("q", "")
            return httpx.Response(200, json=[c for c in state["contacts"] if q in c["name"]])
        if path == "/api/import":
            assert req.headers["content-type"] == "text/csv"
            for line in req.content.decode().splitlines()[1:]:
                state["contacts"].append({"name": line.split(",")[0]})
            return httpx.Response(200, json={"imported": 2})
        if path == "/api/export.csv":
            return httpx.Response(200, text="\n".join(c["name"] for c in state["contacts"]), headers={"content-type": "text/csv"})
        return httpx.Response(200, text="hi")
    return h, users


def test_crm_flow_runs_with_cookie_jar_and_fresh_client_gets_401():
    crm = ke.load_briefs(names=["Small CRM"])[0]
    h, users = crm_site()
    p = DataPlatform([D("done", app_hostname="h")])
    ev, _ = make_eval(p, check_handler=h)
    row = ev.run_brief(crm)
    assert row["checks_total"] == 12 and row["checks_passed"] == 12, row["notes"]
    (u,) = users
    assert u.startswith("eval-") and len(u) > 9
    assert ev.check_client.cookies.get("sid") is None  # jar cleared afterwards


def test_flow_failure_is_noted_and_variables_substitute():
    assert ke.subst({"a": ["x-{u}", "{n}"]}, {"u": "1", "n": 5}) == {"a": ["x-1", 5]}
    step = {"method": "GET", "path": "/need", "expect_status": 200}
    b = dict(GOOD, flow=[step])
    p = Platform([D("done", app_hostname="h")])
    ev, _ = make_eval(p, check_handler=lambda r: httpx.Response(401))
    row = ev.run_brief(b)
    assert row["checks_total"] == 2 and row["checks_passed"] == 0 and any(n.startswith("flow GET /need") for n in row["notes"])


def test_flow_post_is_not_retried_on_5xx():
    seen = []
    ev, _ = make_eval(Platform([D("done", app_hostname="h")]), check_handler=lambda r: (seen.append(r.url.path), httpx.Response(500))[1])
    ev.run_brief(dict(GOOD, checks=GOOD["checks"], flow=[{"method": "POST", "path": "/api/signup", "body": {}, "expect_status": 201}]))
    assert seen.count("/api/signup") == 1


def test_load_rejects_bad_flow_and_version(tmp_path):
    for extra in ({"flow": "x"}, {"flow": [{"method": "GET", "path": "/"}]}, {"version": "2"}):
        with pytest.raises(ke.EvalError):
            ke.load_briefs(write_briefs(tmp_path, [dict(GOOD, **extra)]))


# ---- feature verification and PLAN gate metrics
def test_feature_verification_and_plan_gate_metrics():
    run = {"id": 1, "transcript": [
        call("http_request", method="GET", path="/api/items?x=1"), res("HTTP 401\n"),
        call("http_request", method="get", path="/api/items"), res("HTTP 200\n"),
        call("http_request", method="POST", path="/api/login", body="{}"), res("HTTP 200\nSet-Cookie: sid=1"),
        call("http_request", method="GET", path="/api/me"), res("HTTP 200\n"),
        call("deploy_preview"), res("Result: OK"),
        {"role": "user", "text": "[result] PLAN.md still has 2 open items"},
        {"role": "user", "text": "PLAN.md still has 1 open item"},
    ]}
    m = ke.analyze_transcripts([run])
    assert m["http_paths"] == {"GET": 2, "POST": 1}
    assert m["set_cookie_seen"] == 1 and m["http_authed"] == 1  # only the request after the cookie
    assert m["plan_gate"] == 2
    run2 = {"id": 2, "transcript": [call("http_request", method="GET", path="/a", headers={"Cookie": "sid=1"}), res("HTTP 200")]}
    assert ke.analyze_transcripts([run2])["http_authed"] == 1
    out = ke.render_table([dict(ke.new_row(GOOD), metrics=m, code={}, settle={"settled": True, "seconds": 20})])
    assert "GET:2 POST:1" in out and "20s" in out


def test_settle_seconds_cli_flag():
    assert ke.parse_args(["--project-id", "1"]).settle_seconds == 300
    assert ke.parse_args(["--project-id", "1", "--settle-seconds", "5"]).settle_seconds == 5


def test_web_settings_applied_and_restored():
    p = DataPlatform([D("done")])
    p.settings.update(web_access=False, web_allowed_domains=[])
    ev, _ = make_eval(p, web=True, web_domains=["open-meteo.com"])
    ev.apply_settings()
    assert p.settings["web_access"] is True and p.settings["web_allowed_domains"] == ["open-meteo.com"]
    assert p.settings["model"] == "old-model"  # untouched without --model
    assert ev.restore_settings()
    assert p.settings["web_access"] is False and p.settings["web_allowed_domains"] == []


def test_web_lookups_counted():
    runs = [{"id": 1, "transcript": [{"text": "[web] searched for open-meteo forecast api"},
                                     {"text": "[web] read https://open-meteo.com/en/docs"},
                                     {"text": "[web] read https://open-meteo.com/en/docs/geocoding-api"}]}]
    m = ke.analyze_transcripts(runs)
    assert m["web_searches"] == 1 and m["web_fetches"] == 2


def test_main_briefs_file_and_web_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOK", "not-the-token-value")
    p = DataPlatform([D("done", app_hostname="h")])
    p.settings.update(web_access=False, web_allowed_domains=[])
    real = httpx.Client
    monkeypatch.setattr(ke.httpx, "Client", lambda *a, **kw: real(*a, **dict(kw, transport=httpx.MockTransport(p))))
    bf = tmp_path / "briefs.json"
    bf.write_text(json.dumps([{"name": "Weather", "brief": "Show the weather.", "checks": [{"method": "GET", "path": "/", "expect_status": 200}]}]))
    dd = tmp_path / "data"
    ke.main(["--project-id", "5", "--briefs-file", str(bf), "--token-env", "MY_TOK", "--web", "on",
             "--web-domains", "open-meteo.com, api.open-meteo.com", "--data-dir", str(dd)])
    assert {"web_access": True, "web_allowed_domains": ["open-meteo.com", "api.open-meteo.com"]} in p.puts
    assert p.settings["web_access"] is False  # restored
    summ = json.loads((dd / "summary.json").read_text())
    assert [r["name"] for r in summ["rows"]] == ["Weather"] and summ["web"] == "on"
