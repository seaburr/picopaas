"""Kaisen F8e reviewer pass and F8f generated-test results: scripted fake model, real API, boot.py run for real."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import config
from app.kaisen import reviewing, testresults
from app.kaisen.llm import FakeModelClient, reply, tool_use
from app.kaisen.models import KaisenBuild, KaisenEvent
from tests.kaisen_helpers import FakeRuntime, db
from tests.test_kaisen_worker import PER_CALL, detail, env, go, start, write  # noqa: F401 (env is a fixture)

BOOT = Path(__file__).resolve().parent.parent / "images" / "kaisen-runtime" / "boot.py"
_H = config.KAISEN_PRICES[config.KAISEN_REVIEWER_DEFAULT_MODEL]
REVIEW_CALL = (1000 * _H["in"] + 500 * _H["out"]) / 1_000_000


def _set(env, **kw):
    r = env.a.put(f"/projects/{env.pid}/kaisen", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def _finish(msg="Built it"):
    return reply(tool_use("finish", {"summary": msg}))


def _verdict(verdict, findings=()):
    return reply(tool_use("submit_review", {"verdict": verdict, "findings": list(findings)}))


# ---- settings ------------------------------------------------------------------------------------

def test_reviewer_defaults_off_and_is_settable_and_validated(env):
    s = env.a.get(f"/projects/{env.pid}/kaisen").json()
    assert s["brief_max"] == config.KAISEN_BRIEF_MAX and s["feedback_max"] == config.KAISEN_FEEDBACK_MAX  # the UI counter reads these
    assert s["reviewer"] == "off" and s["reviewer_model"] == config.KAISEN_REVIEWER_DEFAULT_MODEL
    assert s["reviewer_modes"] == list(config.KAISEN_REVIEWER_MODES)
    out = _set(env, reviewer="on", reviewer_model="claude-sonnet-5-5")
    assert out["reviewer"] == "on" and out["reviewer_model"] == "claude-sonnet-5-5"
    assert env.a.put(f"/projects/{env.pid}/kaisen", json={"reviewer": "maybe"}).status_code == 422
    assert env.a.put(f"/projects/{env.pid}/kaisen", json={"reviewer_model": "gpt-9"}).status_code == 422
    assert env.a.get(f"/projects/{env.pid}/kaisen").json()["reviewer"] == "on"


def test_reviewer_settings_are_owner_only(env, make_client):
    from tests.conftest import register_and_login
    other = make_client()
    register_and_login(other, "z@example.com")
    assert other.put(f"/projects/{env.pid}/kaisen", json={"reviewer": "on"}).status_code == 404


def test_reviewer_off_makes_no_reviewer_call(env):
    start(env)
    model = FakeModelClient([_finish()])
    go(model)
    assert model.roles == ["builder"] and detail(env)["runs"][0]["review"] is None


# ---- the review ----------------------------------------------------------------------------------

def test_reviewer_approves(env):
    _set(env, reviewer="on")
    start(env)
    model = FakeModelClient([
        reply(write("app.py", "print(1)")), _finish(),
        reply(tool_use("list_files", {}), tool_use("read_file", {"path": "app.py"})),
        _verdict("approve"),
    ])
    go(model)
    assert model.roles == ["builder", "builder", "reviewer", "reviewer"]
    assert model.models[2] == config.KAISEN_REVIEWER_DEFAULT_MODEL
    d = detail(env)
    run = d["runs"][0]
    assert d["status"] == "done" and run["status"] == "done"
    assert run["review"] == {"verdict": "approve", "model": config.KAISEN_REVIEWER_DEFAULT_MODEL, "findings": [], "note": None}
    assert any(t["text"] == f"{reviewing.MARK} approve" for t in run["transcript"])
    # the reviewer saw the file through its read-only tools
    assert "print(1)" in json.dumps(model.calls[3])
    # cost counted: two builder calls on the project model plus two reviewer calls on the reviewer model
    assert run["cost_usd"] == pytest.approx(2 * PER_CALL + 2 * REVIEW_CALL)
    ev = db().query(KaisenEvent).filter_by(build_id=env.bid).all()
    assert len(ev) == 4


def test_findings_give_exactly_one_fix_round_then_finish(env):
    _set(env, reviewer="on")
    start(env)
    model = FakeModelClient([
        reply(write("app.py", "print(1)")), _finish("first"),
        _verdict("changes", ["app.py: handle empty input", "README is missing"]),
        reply(write("app.py", "print(2)")), _finish("second"),
        _verdict("approve"),  # must never be consumed: no second review
    ])
    go(model)
    assert model.roles == ["builder", "builder", "reviewer", "builder", "builder"]
    assert len(model.script) == 1
    note = json.dumps(model.calls[3])
    assert "handle empty input" in note and "only fix round" in note
    d = detail(env)
    run = d["runs"][0]
    assert d["status"] == "done" and run["summary"] == "second"
    assert run["review"]["verdict"] == "changes" and run["review"]["findings"] == ["app.py: handle empty input", "README is missing"]
    assert any("handle empty input" in t["text"] for t in run["transcript"])


def _fix_round(env, second, *, response_in=None):
    _set(env, reviewer="on")
    start(env)
    model = FakeModelClient([
        reply(write("app.py", "print(1)")), _finish("first"),
        _verdict("changes", ["app.py: handle empty input"]),
        *second,
    ])
    go(model)
    return model, detail(env)["runs"][0]


def test_fix_round_finish_without_edits_or_response_is_refused_once_then_accepted(env):
    model, run = _fix_round(env, [_finish("second"), _finish("third")])
    assert model.roles == ["builder", "builder", "reviewer", "builder", "builder"]
    refusal = model.calls[4][-1]["content"][0]
    assert refusal["is_error"] and refusal["content"].startswith(reviewing.RESPONSE_MARK)
    assert run["status"] == "done" and run["summary"] == "third"
    assert run["review"]["response"] == [] and run["review"]["files_changed"] is False


def test_fix_round_finish_with_response_is_accepted_and_exposed(env):
    resp = ["fixed: n/a", "disagree: input is validated upstream", "  multi\nline  "]
    model, run = _fix_round(env, [reply(tool_use("finish", {"summary": "second", "review_response": resp}))])
    assert run["status"] == "done" and run["summary"] == "second" and model.roles.count("builder") == 3
    assert run["review"]["verdict"] == "changes"
    assert run["review"]["response"] == ["fixed: n/a", "disagree: input is validated upstream", "multi line"]
    assert run["review"]["files_changed"] is False


def test_fix_round_finish_after_an_edit_needs_no_response_and_flags_files_changed(env):
    model, run = _fix_round(env, [reply(write("app.py", "print(2)")), _finish("second")])
    assert run["status"] == "done" and run["review"]["response"] == [] and run["review"]["files_changed"] is True


def test_fix_round_plan_only_edit_does_not_count_as_a_change(env):
    model, run = _fix_round(env, [reply(write("PLAN.md", "- [x] a\n")), _finish("second"), _finish("third")])
    assert model.calls[5][-1]["content"][0]["is_error"] and run["summary"] == "third"
    assert run["review"]["files_changed"] is False


def test_review_response_is_capped_and_ignores_junk():
    n, c = config.KAISEN_REVIEW_RESPONSE_ITEMS, config.KAISEN_REVIEW_RESPONSE_CHARS
    out = reviewing.clean_response({"review_response": ["x" * (c + 50)] * (n + 5) + [3, "", None]})
    assert len(out) == n and all(len(x) == c for x in out)
    assert reviewing.clean_response({"review_response": 5}) == [] and reviewing.clean_response(None) == []


def test_response_is_masked_in_the_view(env):
    secret = "not-the-password-value"
    run = type("R", (), {"review": {"verdict": "changes", "model": "m", "findings": [], "note": None,
                                    "response": [f"fixed: used {secret}"], "files_changed": True}})()
    assert secret not in json.dumps(reviewing.view(run, [secret]))


def test_review_view_without_a_fix_round_has_no_response_fields(env):
    run = type("R", (), {"review": {"verdict": "approve", "model": "m", "findings": [], "note": None}})()
    assert "response" not in reviewing.view(run, [])


def test_findings_are_capped_and_empty_changes_count_as_approve(env):
    verdict, findings = reviewing.parse_verdict({"verdict": "changes", "findings": [f"f{i}" for i in range(20)]})
    assert verdict == "changes" and len(findings) == config.KAISEN_REVIEWER_MAX_FINDINGS
    assert reviewing.parse_verdict({"verdict": "changes", "findings": []}) == ("approve", [])
    assert reviewing.parse_verdict("junk") == ("approve", [])
    long = reviewing.parse_verdict({"verdict": "changes", "findings": ["x" * 5000]})[1][0]
    assert len(long) <= config.KAISEN_REVIEWER_FINDING_CHARS


def test_cap_reached_mid_review_skips_the_review_but_not_the_run(env):
    _set(env, reviewer="on", cap_run=PER_CALL * 2 + REVIEW_CALL * 0.5)
    start(env)
    model = FakeModelClient([reply(write("app.py", "x")), _finish(), reply(tool_use("list_files", {})), _verdict("approve")])
    go(model)
    assert model.roles == ["builder", "builder", "reviewer"]  # second reviewer turn refused by the cap
    d = detail(env)
    assert d["status"] == "done"
    rv = d["runs"][0]["review"]
    assert rv["verdict"] == "skipped" and "cap" in rv["note"].lower()


def test_member_limit_counts_reviewer_spend(env):
    _set(env, reviewer="on")
    start(env)
    model = FakeModelClient([_finish(), _verdict("approve")])
    go(model)
    events = db().query(KaisenEvent).filter_by(build_id=env.bid).all()
    assert len(events) == 2 and all(e.user_id for e in events)
    assert env.a.get("/kaisen/spend", params={"project_id": env.pid}).json()["today_usd"] == pytest.approx(PER_CALL + REVIEW_CALL)


def test_reviewer_model_failure_is_skipped_not_fatal(env):
    from app.kaisen.llm import ModelError
    _set(env, reviewer="on")
    start(env)
    model = FakeModelClient([_finish(), ModelError("overloaded")])
    go(model)
    d = detail(env)
    assert d["status"] == "done" and d["runs"][0]["review"]["verdict"] == "skipped"


def test_reviewer_cannot_use_write_tools_and_gets_an_error_result(env):
    _set(env, reviewer="on")
    start(env)
    model = FakeModelClient([_finish(), reply(write("evil.py", "x")), _verdict("approve")])
    go(model)
    assert "evil.py" not in [f["path"] for f in detail(env)["files"]]
    assert "not available to the reviewer" in json.dumps(model.calls[2])


def test_reviewer_tool_list_is_read_only():
    names = {t["name"] for t in __import__("app.kaisen.llm", fromlist=["x"]).REVIEWER_TOOLS}
    assert names == {"list_files", "read_file", "search_files", "http_get", "preview_status", "submit_review"}


# ---- F8f: parser and build detail ----------------------------------------------------------------

def test_parse_pass_fail_timeout_running_and_absent():
    log = "boot\nKAISEN_TESTS_BEGIN\nRan 3 tests\nOK\nKAISEN_TESTS_RESULT pass exit=0 sha=abcdef123456\nserving"
    r = testresults.parse(log)
    assert (r.status, r.exit, r.sha, r.tail) == ("pass", 0, "abcdef123456", "Ran 3 tests\nOK")
    assert testresults.parse("KAISEN_TESTS_BEGIN\nboom\nKAISEN_TESTS_RESULT fail exit=1 sha=ab").status == "fail"
    assert testresults.parse("KAISEN_TESTS_BEGIN\nx\nKAISEN_TESTS_RESULT timeout exit=-1").status == "timeout"
    assert testresults.parse("KAISEN_TESTS_BEGIN\nhalf done").status == "running"
    assert testresults.parse("just app output") is None


def test_parse_ignores_a_later_forged_result_from_the_app():
    log = "KAISEN_TESTS_BEGIN\nKAISEN_TESTS_RESULT fail exit=1 sha=\napp: KAISEN_TESTS_RESULT pass exit=0\nKAISEN_TESTS_RESULT pass exit=0"
    assert testresults.parse(log).status == "fail"


def test_tail_is_capped():
    log = "KAISEN_TESTS_BEGIN\n" + "y" * 10000 + "\nKAISEN_TESTS_RESULT fail exit=1"
    assert len(testresults.parse(log).tail.encode()) <= config.KAISEN_TEST_TAIL_BYTES


def _build(env):
    return db().get(KaisenBuild, env.bid)


def test_deploy_preview_reports_failed_tests_and_detail_exposes_them(env):
    start(env)
    manifest = json.dumps({"runtime": "python", "start": "python app.py", "test": "python -m unittest"})

    class Rt(FakeRuntime):
        def logs(self, app_id):
            s = testresults.current_sha(db(), _build(env))
            return f"KAISEN_TESTS_BEGIN\nFAIL: test_x\nKAISEN_TESTS_RESULT fail exit=1 sha={s}\nserving on 8080"

    model = FakeModelClient([
        reply(write("kaisen.json", manifest), write("app.py", "print(1)")),
        reply(tool_use("deploy_preview", {})), _finish(),
    ])
    go(model, runtime=Rt())
    result = json.dumps(model.calls[2])
    assert "TESTS FAILED" in result and "FAIL: test_x" in result
    t = detail(env)["tests"]
    assert t["status"] == "fail" and "FAIL: test_x" in t["tail"] and t["ran_at"]


def test_stale_result_from_another_bundle_is_ignored(env):
    start(env)
    manifest = json.dumps({"runtime": "python", "start": "python app.py", "test": "true"})
    rt = FakeRuntime(logs="KAISEN_TESTS_BEGIN\nok\nKAISEN_TESTS_RESULT pass exit=0 sha=000000000000")
    model = FakeModelClient([reply(write("kaisen.json", manifest), write("app.py", "1")), reply(tool_use("deploy_preview", {})), _finish()])
    go(model, runtime=rt)
    assert detail(env)["tests"] is None
    assert "no result in the log yet" in json.dumps(model.calls[2])
    assert "call deploy_preview again" not in json.dumps(model.calls[2]).lower().replace("don't call deploy_preview again", "")


def test_static_site_without_tests_is_told_that_is_fine(env):
    # no kaisen.json means static; the hint must not push the builder into adding a server (demo 2026-10-30)
    start(env)
    model = FakeModelClient([reply(write("index.html", "<p>hi</p>")), reply(tool_use("deploy_preview", {})), _finish()])
    go(model)
    result = json.dumps(model.calls[2])
    assert "node --test" in result and "fails on current Node" in result and "do not change the runtime" in result.lower()
    assert detail(env)["tests"] is None


def test_server_without_declared_test_gets_an_optional_hint(env):
    start(env)
    manifest = json.dumps({"runtime": "python", "start": "python app.py"})
    model = FakeModelClient([reply(write("kaisen.json", manifest), write("app.py", "1")), reply(tool_use("deploy_preview", {})), _finish()])
    go(model)
    result = json.dumps(model.calls[2])
    assert "none declared. Optional" in result and "Never change the runtime" in result
    assert detail(env)["tests"] is None


def test_tests_anonymous_and_other_user_cannot_see_build(env, make_client):
    from tests.conftest import register_and_login
    assert make_client().get(f"/kaisen/builds/{env.bid}").status_code == 401
    other = make_client()
    register_and_login(other, "z@example.com")
    assert other.get(f"/kaisen/builds/{env.bid}").status_code == 404


def test_deploy_rejects_a_bad_test_field(env):
    start(env)
    model = FakeModelClient([reply(write("kaisen.json", json.dumps({"runtime": "python", "start": "x", "test": "t" * (config.KAISEN_TEST_COMMAND_MAX + 1)})),
                                   write("app.py", "1")), reply(tool_use("deploy_preview", {})), _finish()])
    go(model)
    assert "test" in json.dumps(model.calls[2]) and str(config.KAISEN_TEST_COMMAND_MAX) in json.dumps(model.calls[2])


# ---- boot.py (the runtime image's entry point), run for real -------------------------------------

@pytest.fixture(scope="module")
def boot():
    spec = importlib.util.spec_from_file_location("kaisen_boot", BOOT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _run(boot, tmp_path, command, capsys, **kw):
    res = boot.run_tests(command, str(tmp_path), {"PATH": "/usr/bin:/bin"}, **kw)
    return res, capsys.readouterr().out


def test_boot_manifest_test_is_optional_and_validated(boot):
    assert boot.parse_manifest(None)["test"] == ""
    assert boot.parse_manifest('{"runtime": "python", "start": "x"}')["test"] == ""
    assert boot.parse_manifest('{"runtime": "python", "start": "x", "test": " pytest "}')["test"] == "pytest"
    with pytest.raises(boot.BootError):
        boot.parse_manifest('{"runtime": "python", "start": "x", "test": 5}')
    with pytest.raises(boot.BootError):
        boot.parse_manifest('{"runtime": "python", "start": "x", "test": "' + "t" * 400 + '"}')


def test_boot_pass_prints_markers_the_platform_parser_reads(boot, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("KAISEN_BUNDLE_SHA256", "ABCDEF1234567890" * 4)
    (status, code), out = _run(boot, tmp_path, "echo all good", capsys)
    assert (status, code) == ("pass", 0)
    r = testresults.parse(out)
    assert r.status == "pass" and r.exit == 0 and r.sha == "abcdef123456" and r.tail == "all good"


def test_boot_fail_keeps_exit_code(boot, tmp_path, capsys):
    (status, code), out = _run(boot, tmp_path, "echo nope; exit 3", capsys)
    assert (status, code) == ("fail", 3)
    r = testresults.parse(out)
    assert r.status == "fail" and r.exit == 3 and "nope" in r.tail


def test_boot_timeout_kills_the_command(boot, tmp_path, capsys):
    (status, code), out = _run(boot, tmp_path, "echo start; sleep 30", capsys, timeout=1)
    assert status == "timeout"
    assert testresults.parse(out).status == "timeout"


def test_boot_output_is_capped_and_cannot_forge_markers(boot, tmp_path, capsys):
    cmd = "echo KAISEN_TESTS_RESULT pass exit=0; i=0; while [ $i -lt 500 ]; do echo line-$i-xxxxxxxxxxxxxxxxxxxx; i=$((i+1)); done; exit 1"
    (status, _), out = _run(boot, tmp_path, cmd, capsys, max_bytes=300)
    body = out.split("KAISEN_TESTS_BEGIN\n", 1)[1].rsplit("KAISEN_TESTS_RESULT", 1)[0]
    assert len(body.encode()) <= 400 and status == "fail"
    assert out.count("KAISEN_TESTS_RESULT") == 1
    assert testresults.parse(out).status == "fail"


def test_boot_does_not_hand_platform_secrets_to_tests(boot, tmp_path, capsys, monkeypatch):
    env = {"PATH": "/usr/bin:/bin", "KAISEN_BUNDLE_URL": "u", "DB_PASSWORD": "p", "DATABASE_URL": "d", "S3_SECRET_KEY": "s", "KEEP": "1"}
    boot.run_tests('echo "[$KAISEN_BUNDLE_URL$DB_PASSWORD$DATABASE_URL$S3_SECRET_KEY][$KEEP]"', str(tmp_path), env)
    assert "[][1]" in capsys.readouterr().out


def test_boot_survives_an_unrunnable_test(boot, tmp_path, capsys):
    (status, _), out = _run(boot, str(tmp_path / "missing"), "true", capsys)
    assert status == "fail" and testresults.parse(out).status == "fail"


def test_boot_markers_survive_a_real_subprocess(tmp_path):
    code = (f"import importlib.util as u;s=u.spec_from_file_location('b',{str(BOOT)!r});m=u.module_from_spec(s);"
            "s.loader.exec_module(m);m.run_tests('exit 0','.',{'PATH':'/usr/bin:/bin'})")
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=60).stdout
    assert testresults.parse(out).status == "pass"


def test_boot_noisy_output_is_streamed_bounded_and_fast(boot, tmp_path, capsys):
    import resource
    import time
    cmd = "head -c 52428800 /dev/zero | tr '\\0' 'x' | fold -w 80; echo final-line"
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.time()
    (status, code), out = _run(boot, tmp_path, cmd, capsys, timeout=60, max_bytes=300)
    grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
    if sys.platform != "darwin":
        grown *= 1024  # ru_maxrss is KB on linux, bytes on macOS
    assert (status, code) == ("pass", 0)
    assert "final-line" in out and len(out.encode()) < 1000
    assert time.time() - t0 < 30
    assert grown < 30 * 1024 * 1024  # far below the 50MB emitted


def test_review_response_drops_control_and_format_characters():
    out = reviewing.clean_response({"review_response": ["fixed:\x1b[31m red‮ evil​\x07 ok\n\tnext"]})
    assert out == ["fixed:[31m red evil ok next"]
