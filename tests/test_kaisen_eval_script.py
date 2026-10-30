"""Unit tests for scripts/kaisen_eval.py (H6). Pure logic over httpx.MockTransport; nothing touches the network."""
import importlib.util
import json
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
    assert len(briefs) == 7
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
    assert "| A | done | 2 | 11 | $0.061 | 11.0 min | pass | 2/2 | approve | resumed once |" in t
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
