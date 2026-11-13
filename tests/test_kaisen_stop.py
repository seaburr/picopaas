"""Kaisen: a user can Stop a run that is waiting on a question. Real login flow; the waiting state is set in the DB."""
from app.kaisen import builder, snapshots
from app.kaisen.models import KaisenBuild, KaisenInbox, KaisenRun
from tests.kaisen_helpers import db
from tests.test_kaisen_api import _audit, _new_build, fakes, team  # noqa: F401 (fixtures)

ASK = "toolu_ask_1"


def _wait(bid, status="waiting"):
    """Put the build's run into the state the worker leaves it in when Kaisen asks a question."""
    s = db()
    run = s.query(KaisenRun).filter_by(build_id=bid).one()
    run.status = status
    run.messages = [{"role": "user", "content": "brief"},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": ASK, "name": "ask_user",
                                                       "input": {"question": "Which colour?"}}]}]
    run.pending = {"results": [], "ask_id": ASK} if status == "waiting" else None
    s.get(KaisenBuild, bid).status = status
    item = KaisenInbox(build_id=bid, run_id=run.id, kind="question", text="Which colour?")
    s.add(item)
    s.commit()
    return run.id, item.id


def _stop(c, bid, rid):
    return c.post(f"/kaisen/builds/{bid}/runs/{rid}/stop")


def test_stop_waiting_run(team):
    _, a, b, _, pid = team
    bid = _new_build(a, pid)
    rid, _ = _wait(bid)
    r = _stop(b, bid, rid)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "stopped"
    s = db()
    s.expire_all()
    run = s.get(KaisenRun, rid)
    assert run.status == "stopped" and run.error == "Stopped by b@example.com while waiting for an answer."
    assert run.pending is None and run.finished_at is not None
    notice = s.query(KaisenInbox).filter_by(run_id=rid, kind="stopped").one()
    assert notice.text == run.error


def test_stop_closes_the_question(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid, item = _wait(bid)
    assert _stop(a, bid, rid).status_code == 200
    s = db()
    s.expire_all()
    assert s.get(KaisenInbox, item).status == "closed"
    assert not s.query(KaisenInbox).filter_by(run_id=rid, kind="question", status="open").count()


def test_stop_audit_row(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid, _ = _wait(bid)
    _stop(a, bid, rid)
    rows = _audit("kaisen.stop")
    assert len(rows) == 1 and rows[0].detail == f"run={rid}" and rows[0].target_id == str(bid)


def test_stop_refuses_running_and_done(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid, _ = _wait(bid, status="running")
    for status in ("running", "queued", "done"):
        s = db()
        s.get(KaisenRun, rid).status = status
        s.commit()
        r = _stop(a, bid, rid)
        assert r.status_code == 409 and r.json()["detail"] == "That run is not waiting on a question."
        s.expire_all()
        assert s.get(KaisenRun, rid).status == status
    assert not _audit("kaisen.stop")


def test_stop_run_of_another_build_is_404(team):
    _, a, _, _, pid = team
    one, two = _new_build(a, pid, name="one"), _new_build(a, pid, name="two")
    rid, _ = _wait(one)
    assert _stop(a, two, rid).status_code == 404
    assert _stop(a, one, 9999).status_code == 404
    assert db().get(KaisenRun, rid).status == "waiting"


def test_stop_anonymous_and_non_member(team, client):
    _, a, _, c, pid = team
    bid = _new_build(a, pid)
    rid, _ = _wait(bid)
    assert _stop(c, bid, rid).status_code == 404
    anon = client
    anon.cookies.clear()
    assert _stop(anon, bid, rid).status_code == 401
    assert db().get(KaisenRun, rid).status == "waiting"


def test_stop_refused_for_read_only_key(team, make_client):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid, _ = _wait(bid)
    key = a.post("/api/keys", json={"scope": "read"}).json()
    ro = make_client()
    ro.headers["Authorization"] = f"Bearer {key['key']}"
    assert _stop(ro, bid, rid).status_code == 403
    assert db().get(KaisenRun, rid).status == "waiting"


def test_build_is_usable_after_stop(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid, _ = _wait(bid)
    _stop(a, bid, rid)
    r = a.post(f"/kaisen/builds/{bid}/feedback", json={"text": "make it blue"})
    assert r.status_code == 200 and r.json()["runs"][0]["kind"] == "feedback"


def test_restore_allowed_after_stop(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid, _ = _wait(bid)
    s = db()
    sid = snapshots.take(s, bid, "run_start", rid)[0].id
    assert a.post(f"/kaisen/builds/{bid}/snapshots/{sid}/restore").status_code == 409
    _stop(a, bid, rid)
    assert a.post(f"/kaisen/builds/{bid}/snapshots/{sid}/restore").status_code == 200


def test_resume_after_stop_has_a_valid_history(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid, _ = _wait(bid)
    _stop(a, bid, rid)
    s = db()
    s.expire_all()
    stopped = s.get(KaisenRun, rid)
    last = stopped.messages[-1]
    assert last["role"] == "user" and last["content"][-1]["tool_use_id"] == ASK
    assert last["content"][-1]["content"] == builder.STOPPED_ANSWER
    r = a.post(f"/kaisen/builds/{bid}/run")
    assert r.status_code == 200 and r.json()["runs"][0]["kind"] == "resume"
    s.expire_all()
    resumed = s.query(KaisenRun).filter_by(build_id=bid, kind="resume").one()
    assert resumed.messages[-1] == last


def test_answer_after_stop_is_409(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid, item = _wait(bid)
    _stop(a, bid, rid)
    r = a.post(f"/kaisen/builds/{bid}/inbox/{item}/answer", json={"text": "blue"})
    assert r.status_code == 409
    s = db()
    s.expire_all()
    assert s.get(KaisenRun, rid).status == "stopped"


def test_stop_after_answer_is_409(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid, item = _wait(bid)
    assert a.post(f"/kaisen/builds/{bid}/inbox/{item}/answer", json={"text": "blue"}).status_code == 200
    assert _stop(a, bid, rid).status_code == 409
    s = db()
    s.expire_all()
    assert s.get(KaisenRun, rid).status == "queued"
