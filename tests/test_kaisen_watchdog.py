"""Kaisen hang watchdog: bounded model calls, stale-run sweep, retries, fencing of abandoned steps, worker liveness,
manual interrupt endpoint. Real register/login, fake model/runtime, no real sleeps beyond tens of milliseconds."""
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from app import config, models
from app.kaisen import builder, watchdog, worker
from app.kaisen.llm import FakeModelClient, reply, tool_use
from app.kaisen.models import KaisenInbox, KaisenRun
from tests.kaisen_helpers import db
from tests.test_kaisen_api import _audit, _new_build, fakes, team  # noqa: F401 (fixtures)
from tests.test_kaisen_worker import go

FINISH = reply(tool_use("finish", {"summary": "All done"}))


@pytest.fixture()
def S(make_client):
    """Real, separate sessions over the test database (as the worker's SessionLocal would be)."""
    return sessionmaker(bind=db().get_bind(), autoflush=False)


@pytest.fixture(autouse=True)
def _alive():
    watchdog.bump()
    watchdog.set_phase("idle")
    yield
    watchdog.bump()
    watchdog.set_phase("idle")


def _join_steps():
    for th in threading.enumerate():
        if th.name.startswith("kaisen-step-"):
            th.join(10)


def _run(bid):
    s = db()
    s.expire_all()
    return builder.latest_run(s, bid)


def _make_running(bid, idle_s=None, retries=0):
    s = db()
    run = builder.latest_run(s, bid)
    builder.set_status(s, run, "running")
    run.hang_retries = retries
    if idle_s is not None:
        run.last_progress_at = builder._now() - timedelta(seconds=idle_s)
    s.commit()
    return run.id


def _notes(rid, kind="hang"):
    s = db()
    s.expire_all()
    return [i.text for i in s.query(KaisenInbox).filter_by(run_id=rid, kind=kind).order_by(KaisenInbox.id)]


# ---- bounded model call ------------------------------------------------------------------------

def test_hung_model_call_times_out_and_the_run_retries(team, monkeypatch):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    monkeypatch.setattr(config, "KAISEN_MODEL_CALL_MAX_SECONDS", 0.05)
    release = threading.Event()

    def hang(_messages):
        release.wait(5)
        return FINISH

    model = FakeModelClient([hang, FINISH])
    try:
        assert go(model) is True
        run = _run(bid)
        assert run.status == "queued" and run.attempt == 1 and run.hang_retries == 1  # timeout is a hang, not an error
        assert run.error is None and run.error_reason is None
        assert _notes(run.id) == ["Model call timed out; interrupted, retrying (1/2)."]
        assert go(model) is True  # the retry resumes from the persisted messages
        assert _run(bid).status == "done"
    finally:
        release.set()
    assert db().query(models.AuditLog).filter_by(action="kaisen.interrupt").count() == 0  # automatic, not a user action


def test_model_timeout_exhausts_retries_then_errors_as_hung(team, monkeypatch):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    monkeypatch.setattr(config, "KAISEN_MODEL_CALL_MAX_SECONDS", 0.03)
    release = threading.Event()
    model = FakeModelClient([lambda m: release.wait(5)] * (config.KAISEN_HANG_RETRIES + 1))
    try:
        for _ in range(config.KAISEN_HANG_RETRIES + 1):
            go(model)
    finally:
        release.set()
    run = _run(bid)
    assert run.status == "error" and run.error_reason == "hung"
    assert run.error == f"Hung {config.KAISEN_HANG_RETRIES + 1} times; Resume to retry."
    assert _notes(run.id, "error") == [run.error]


# ---- sweep -------------------------------------------------------------------------------------

def test_stale_running_run_is_requeued_with_event_text(team, S):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _make_running(bid, idle_s=config.KAISEN_HANG_SECONDS + 60)
    before = list(db().get(KaisenRun, rid).messages)
    assert watchdog.sweep(S) == [(rid, "requeued")]
    run = _run(bid)
    assert run.status == "queued" and run.attempt == 1 and run.hang_retries == 1 and run.last_progress_at is None
    assert run.messages == before  # resumes from what was persisted
    mins = round((config.KAISEN_HANG_SECONDS + 60) / 60)
    assert _notes(rid) == [f"No progress for {mins} min; interrupted, retrying (1/{config.KAISEN_HANG_RETRIES})."]
    assert watchdog.sweep(S) == []  # queued now: nothing left to do


def test_fresh_progress_is_not_a_hang(team, S):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _make_running(bid, idle_s=config.KAISEN_HANG_SECONDS - 60)
    assert watchdog.sweep(S) == []
    assert db().get(KaisenRun, rid).status == "running"


def test_retries_exhausted_stops_the_run_as_hung(team, S):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _make_running(bid, idle_s=config.KAISEN_HANG_SECONDS + 1, retries=config.KAISEN_HANG_RETRIES)
    assert watchdog.sweep(S) == [(rid, "error")]
    run = _run(bid)
    n = config.KAISEN_HANG_RETRIES + 1
    assert run.status == "error" and run.error_reason == "hung" and run.error == f"Hung {n} times; Resume to retry."
    assert run.finished_at is not None
    assert _notes(rid, "error") == [run.error]
    assert watchdog.sweep(S) == []  # no auto-retry loop beyond the limit
    detail = a.get(f"/kaisen/builds/{bid}").json()["runs"][0]
    assert detail["error_reason"] == "hung" and detail["hang_retries"] == n
    assert a.post(f"/kaisen/builds/{bid}/run").status_code == 200  # Resume works afterwards
    assert _run(bid).hang_retries == 0


def test_two_detectors_count_one_hang(team, S):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _make_running(bid, idle_s=config.KAISEN_HANG_SECONDS + 1)
    attempt = db().get(KaisenRun, rid).attempt
    assert watchdog.interrupt(S, rid, attempt, "x") == "requeued"
    assert watchdog.interrupt(S, rid, attempt, "x") == "skipped"  # compare-and-swap on the attempt
    assert _run(bid).hang_retries == 1


def test_reconciler_sweeps_stale_runs(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _make_running(bid, idle_s=config.KAISEN_HANG_SECONDS + 1)
    assert watchdog.sweep_session(db()) == [(rid, "requeued")]
    assert _run(bid).status == "queued"


# ---- fencing of abandoned steps ----------------------------------------------------------------

def test_late_model_response_from_an_abandoned_attempt_is_discarded(team, S):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _run(bid).id
    before = list(_run(bid).messages)

    def late(_messages):  # the watchdog interrupts while the model is still "thinking"
        assert watchdog.interrupt(S, rid, 0, "No progress for 11 min") == "requeued"
        return reply(tool_use("write_file", {"path": "late.txt", "content": "late"}))

    assert builder.run_step(S, rid, FakeModelClient([late]), worker.Lease()) == "stopped"
    run = _run(bid)
    assert run.status == "queued" and run.attempt == 1 and run.messages == before and run.cost_usd == 0
    from app.kaisen.models import KaisenFile
    assert db().query(KaisenFile).filter_by(build_id=bid, path="late.txt").count() == 0


def test_late_model_error_does_not_stop_the_requeued_run(team, S):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _run(bid).id

    def late(_messages):
        watchdog.interrupt(S, rid, 0, "No progress for 11 min")
        raise RuntimeError("boom")

    assert builder.run_step(S, rid, FakeModelClient([late]), worker.Lease()) == "error"
    run = _run(bid)
    assert run.status == "queued" and run.error is None  # stop_run is fenced by the attempt


def test_late_tool_result_from_an_abandoned_attempt_is_discarded(team, S):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _run(bid).id
    before = list(_run(bid).messages)

    def slow_deployer(s, run):  # the tool hung; the watchdog gave up on it; then it returned
        watchdog.interrupt(S, rid, 0, "A tool call timed out")
        return True, "late preview ok"

    model = FakeModelClient([reply(tool_use("deploy_preview", {}))])
    assert builder.run_step(S, rid, model, worker.Lease(), deployer=slow_deployer) == "stopped"
    run = _run(bid)
    assert run.status == "queued" and run.messages == before


def test_threaded_waiter_abandons_a_hung_tool_and_requeues(team, S, monkeypatch):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _run(bid).id
    monkeypatch.setattr(config, "KAISEN_TOOL_SECONDS", 0.05)
    release = threading.Event()

    def hung_deployer(s, run):
        release.wait(5)
        return True, "late"

    model = FakeModelClient([reply(tool_use("deploy_preview", {}))])
    try:
        out = worker.guarded_step(S, rid, model, worker.Lease(), hung_deployer, None, True, poll_s=0.02)
        assert out == "hung"
        run = _run(bid)
        assert run.status == "queued" and run.attempt == 1 and run.hang_retries == 1
        assert _notes(rid) == [f"A tool call timed out; interrupted, retrying (1/{config.KAISEN_HANG_RETRIES})."]
    finally:
        release.set()
        _join_steps()  # the abandoned thread finishes; it must write nothing
    assert _run(bid).status == "queued" and _run(bid).messages[-1]["role"] == "user"


def test_threaded_waiter_abandons_when_interrupted_elsewhere(team, S):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _run(bid).id
    release = threading.Event()

    def hung_deployer(s, run):
        watchdog.interrupt(S, rid, 0, "Interrupted by someone", manual=True)  # e.g. the API pod
        release.wait(5)
        return True, "late"

    model = FakeModelClient([reply(tool_use("deploy_preview", {}))])
    try:
        assert worker.guarded_step(S, rid, model, worker.Lease(), hung_deployer, None, True, poll_s=0.02) == "interrupted"
    finally:
        release.set()
        _join_steps()
    assert _run(bid).status == "queued" and _run(bid).hang_retries == 0


# ---- worker liveness ---------------------------------------------------------------------------

def test_heartbeat_touched_only_while_the_loop_is_alive(S, tmp_path, monkeypatch):
    hb = tmp_path / "hb"
    monkeypatch.setattr(config, "KAISEN_HEARTBEAT_FILE", str(hb))
    w = worker.Worker(S=S)
    watchdog.bump()
    assert w.heartbeat_once() is True and hb.exists()
    hb.unlink()
    watchdog._alive = time.monotonic() - (config.KAISEN_HANG_SECONDS + config.KAISEN_HANG_MARGIN_SECONDS + 1)  # wedged
    assert w.heartbeat_once() is False and not hb.exists()
    watchdog.bump()
    assert w.heartbeat_once() is True and hb.exists()  # recovers when the loop moves again


def test_main_loop_sweeps_hung_runs_without_starting_threads(team, S, tmp_path, monkeypatch):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    monkeypatch.setattr(config, "KAISEN_HEARTBEAT_FILE", str(tmp_path / "hb"))
    _make_running(bid, idle_s=config.KAISEN_HANG_SECONDS + 5)
    w = worker.Worker(S=S)
    w.sweep_if_due()
    assert _run(bid).status == "queued"
    w.sweep_if_due()  # throttled: not due again yet
    assert _run(bid).hang_retries == 1


def test_startup_recovery_counts_a_retry(team, S):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _make_running(bid, idle_s=config.KAISEN_HANG_SECONDS + 30)
    assert worker.Worker(S=S).recover() == [(rid, "requeued")]
    run = _run(bid)
    assert run.status == "queued" and run.hang_retries == 1


def test_progress_is_stamped_during_a_step(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    assert _run(bid).last_progress_at is None
    go(FakeModelClient([FINISH]))
    assert _run(bid).last_progress_at is not None


# ---- manual interrupt endpoint -----------------------------------------------------------------

def _url(bid, rid):
    return f"/kaisen/builds/{bid}/runs/{rid}/interrupt"


def test_interrupt_requires_login(team, make_client):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    assert make_client().post(_url(bid, _run(bid).id)).status_code == 401


def test_owner_interrupts_a_running_step_and_it_is_requeued_and_audited(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _make_running(bid, idle_s=5)
    r = a.post(_url(bid, rid))
    assert r.status_code == 200, r.text
    run = _run(bid)
    assert run.status == "queued" and run.attempt == 1 and run.hang_retries == 0  # not counted as a hang
    assert _notes(rid) == ["Interrupted by a@example.com; retrying."]
    body = r.json()["runs"][0]
    assert body["notes"][0]["text"] == "Interrupted by a@example.com; retrying." and body["status"] == "queued"
    rows = _audit("kaisen.interrupt")
    assert len(rows) == 1 and rows[0].target_id == str(bid) and f"run={rid}" in rows[0].detail


def test_member_can_interrupt_like_they_can_run(team):
    _, _, b, _, pid = team
    bid = _new_build(team[1], pid)
    rid = _make_running(bid)
    assert b.post(_url(bid, rid)).status_code == 200


def test_outsider_gets_404_and_nothing_changes(team):
    _, a, _, c, pid = team
    bid = _new_build(a, pid)
    rid = _make_running(bid)
    assert c.post(_url(bid, rid)).status_code == 404
    assert _run(bid).status == "running" and _run(bid).attempt == 0
    assert _audit("kaisen.interrupt") == []


def test_run_of_another_build_is_404(team):
    _, a, _, _, pid = team
    b1, b2 = _new_build(a, pid, "One"), _new_build(a, pid, "Two")
    rid = _make_running(b2)
    assert a.post(_url(b1, rid)).status_code == 404
    assert a.post(_url(b1, 99999)).status_code == 404


def test_interrupt_validation_non_running_run_is_409(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _run(bid).id  # queued: nothing in flight
    assert a.post(_url(bid, rid)).status_code == 409
    assert a.post(f"/kaisen/builds/{bid}/runs/not-a-number/interrupt").status_code == 422
    assert _audit("kaisen.interrupt") == [] and _run(bid).attempt == 0


def test_config_rejects_a_hang_limit_below_the_call_bounds():
    import os
    import subprocess
    import sys
    env = {**os.environ, "KAISEN_HANG_SECONDS": "100"}
    out = subprocess.run([sys.executable, "-c", "import app.config"], env=env, capture_output=True, text=True)
    assert out.returncode != 0 and "KAISEN_HANG_SECONDS must exceed" in out.stderr
    env["KAISEN_HANG_SECONDS"] = "soon"
    out = subprocess.run([sys.executable, "-c", "import app.config"], env=env, capture_output=True, text=True)
    assert out.returncode != 0 and "whole number of seconds" in out.stderr


def test_manual_interrupt_helper_requeues_without_counting(team, S):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _make_running(bid)
    assert watchdog.manual_interrupt(S, rid, "a@example.com") == "requeued"
    assert _run(bid).hang_retries == 0 and _run(bid).attempt == 1
    assert watchdog.manual_interrupt(S, rid, "a@example.com") == "skipped"  # no longer running
    assert watchdog.manual_interrupt(S, 99999, "a@example.com") == "skipped"


def test_unthreaded_guarded_step_is_plain_run_step(team, S):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _run(bid).id
    assert worker.guarded_step(S, rid, FakeModelClient([FINISH]), worker.Lease(), None, None, False) == "done"


def test_reviewer_model_hang_is_a_hang_too(team, S, monkeypatch):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    assert a.put(f"/projects/{pid}/kaisen", json={"reviewer": "on"}).status_code == 200
    rid = _run(bid).id
    monkeypatch.setattr(config, "KAISEN_MODEL_CALL_MAX_SECONDS", 0.05)
    release = threading.Event()
    model = FakeModelClient([FINISH, lambda m: release.wait(5)])
    try:
        assert builder.run_step(S, rid, model, worker.Lease()) == "hung"
    finally:
        release.set()
    run = _run(bid)
    assert run.status == "queued" and run.attempt == 1
    assert _notes(rid) == [f"Review model call timed out; interrupted, retrying (1/{config.KAISEN_HANG_RETRIES})."]


# ---- security review hardening (K69-K72) -------------------------------------------------------

def test_manual_interrupts_are_rate_limited_per_build(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    for _ in range(watchdog.MANUAL_INTERRUPTS_PER_HOUR):
        rid = _make_running(bid, idle_s=5)
        assert a.post(_url(bid, rid)).status_code == 200
    rid = _make_running(bid, idle_s=5)
    r = a.post(_url(bid, rid))
    assert r.status_code == 429 and _run(bid).status == "running" and _run(bid).attempt == watchdog.MANUAL_INTERRUPTS_PER_HOUR


def test_abandoned_thread_cannot_refresh_liveness_or_phase():
    watchdog.set_phase("tool")
    box = {}

    def _go():
        threading.current_thread().abandoned = True
        watchdog.set_phase("idle")
        watchdog.bump()
        box["done"] = True
    th = threading.Thread(target=_go)
    th.start()
    th.join(5)
    assert box["done"] and watchdog.phase_age()[0] == "tool"


def test_abandoned_model_thread_issues_no_further_requests(monkeypatch):
    from app.kaisen.llm import AnthropicClient, ModelTimeout

    class _A:
        class RateLimitError(Exception): ...
        class APIStatusError(Exception): ...
        class APITimeoutError(Exception): ...
        class APIConnectionError(Exception): ...

    c = AnthropicClient.__new__(AnthropicClient)
    c._a, c.sleep = _A, lambda s: None
    calls = []
    c._call = lambda *a: calls.append(1)
    out = {}

    def _go():
        threading.current_thread().abandoned = True
        try:
            c.create(model="m", messages=[], effort="low")
        except ModelTimeout:
            out["t"] = True
    th = threading.Thread(target=_go)
    th.start()
    th.join(5)
    assert out.get("t") and not calls


def test_abandoned_deploy_does_not_persist_its_writes(team, S):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid = _make_running(bid, idle_s=5)
    s = S()
    run = s.get(KaisenRun, rid)
    assert run.attempt == 0
    watchdog.interrupt(S, rid, 0, "Interrupted by someone", manual=True)  # the retry was queued; we are the late attempt
    dep = worker.make_deployer(S, lambda: None, None, 1)
    with pytest.raises(builder.ToolError):
        dep(s, run)
    s.close()
    assert _run(bid).status == "queued" and _run(bid).last_progress_at is None
