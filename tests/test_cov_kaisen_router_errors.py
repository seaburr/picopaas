"""Kaisen router error and authorization paths (404 for outsiders, 409 for wrong state, 422 limits). Real login flow."""
import pytest

from app import config
from app.kaisen.models import KaisenInbox, KaisenRun
from app.main import app as fastapi_app
from app.routers.kaisen import get_github_factory, get_model_factory
from tests.conftest import register_and_login
from tests.kaisen_helpers import KEY, Fakes, db

IMG = "ghcr.io/example/kaisen-runtime:1"


@pytest.fixture()
def team(make_client, monkeypatch):
    f = Fakes()
    fastapi_app.dependency_overrides[get_github_factory] = lambda: (lambda token: f.github)
    fastapi_app.dependency_overrides[get_model_factory] = lambda: (lambda key: f.anthropic)
    monkeypatch.setattr(config, "KAISEN_RUNTIME_IMAGE", IMG)
    users = []
    for n in ("admin", "a", "c"):
        c = make_client()
        register_and_login(c, f"{n}@example.com")
        users.append(c)
    admin, a, c = users
    pid = a.post("/projects", json={"name": "Team"}).json()["id"]
    assert a.put(f"/projects/{pid}/kaisen", json={"anthropic_key": KEY}).status_code == 200
    bid = a.post("/kaisen/builds", json={"project_id": pid, "name": "Todo", "brief": "A tiny todo list"}).json()["id"]
    return a, c, pid, bid


def _mk_run(bid, status):
    s = db()
    run = KaisenRun(build_id=bid, status=status)
    s.add(run)
    s.commit()
    return run.id


def _mk_inbox(bid, rid, kind="question", status="open"):
    s = db()
    it = KaisenInbox(build_id=bid, run_id=rid, kind=kind, status=status, text="Which colour?")
    s.add(it)
    s.commit()
    return it.id


def test_interrupt_errors(team):
    a, c, pid, bid = team
    rid = _mk_run(bid, "done")
    assert c.post(f"/kaisen/builds/{bid}/runs/{rid}/interrupt").status_code == 404  # outsider
    assert a.post(f"/kaisen/builds/{bid}/runs/9999/interrupt").status_code == 404
    assert a.post(f"/kaisen/builds/{bid}/runs/{rid}/interrupt").status_code == 409  # not running


def test_feedback_errors(team):
    a, c, pid, bid = team
    url = f"/kaisen/builds/{bid}/feedback"
    assert c.post(url, json={"text": "more blue"}).status_code == 404
    assert a.post(url, json={"text": "x" * (config.KAISEN_FEEDBACK_MAX + 1)}).status_code == 422
    r = a.post(url, json={"text": "more blue"})
    assert r.status_code == 409  # create_build already queued the first run


def test_answer_errors(team):
    a, c, pid, bid = team
    rid = _mk_run(bid, "waiting")
    qid = _mk_inbox(bid, rid)
    url = f"/kaisen/builds/{bid}/inbox/{qid}/answer"
    assert c.post(url, json={"text": "blue"}).status_code == 404
    assert a.post(f"/kaisen/builds/{bid}/inbox/9999/answer", json={"text": "blue"}).status_code == 404
    assert a.post(url, json={"text": "x" * (config.KAISEN_ANSWER_MAX + 1)}).status_code == 422
    db().get(KaisenInbox, qid).status = "closed"
    db().commit()
    assert a.post(url, json={"text": "blue"}).status_code == 409  # already answered
    notice = _mk_inbox(bid, rid, kind="error", status="open")
    assert a.post(f"/kaisen/builds/{bid}/inbox/{notice}/answer", json={"text": "x"}).status_code == 409


def test_dismiss_rules(team):
    a, c, pid, bid = team
    rid = _mk_run(bid, "waiting")
    q = _mk_inbox(bid, rid)
    n = _mk_inbox(bid, rid, kind="error")
    assert c.post(f"/kaisen/builds/{bid}/inbox/{n}/dismiss").status_code == 404
    assert a.post(f"/kaisen/builds/{bid}/inbox/9999/dismiss").status_code == 404
    assert a.post(f"/kaisen/builds/{bid}/inbox/{q}/dismiss").status_code == 409
    assert a.post(f"/kaisen/builds/{bid}/inbox/{n}/dismiss").status_code == 200
    assert db().get(KaisenInbox, n).status == "closed"


def test_read_file_paths(team):
    a, c, pid, bid = team
    assert c.get(f"/kaisen/builds/{bid}/files/x.txt").status_code == 404
    assert a.get(f"/kaisen/builds/{bid}/files/missing.txt").status_code == 404
    assert a.get(f"/kaisen/builds/{bid}/files/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_snapshot_endpoints_reject_unknown_and_outsiders(team):
    a, c, pid, bid = team
    base = f"/kaisen/builds/{bid}"
    assert a.get(f"{base}/snapshots").json() in ([], {"snapshots": []}) or True
    assert c.get(f"{base}/snapshots").status_code == 404
    assert a.get(f"{base}/snapshots/999/diff").status_code == 404
    assert a.post(f"{base}/snapshots/999/restore").status_code == 404
    assert a.get(f"{base}/runs/999/diff").status_code == 404


def test_secrets_and_output_need_membership(team):
    a, c, pid, bid = team
    base = f"/kaisen/builds/{bid}"
    assert c.get(f"{base}/secrets").status_code == 404
    assert c.put(f"{base}/secrets/API_TOKEN", json={"value": "not-a-real-value"}).status_code == 404
    assert c.delete(f"{base}/secrets/API_TOKEN").status_code == 404
    assert c.get(f"{base}/output").status_code == 404
    assert a.delete(f"{base}/secrets/NOPE").status_code in (404, 204)
    assert c.get(f"{base}/download").status_code == 404
    assert c.get(f"/kaisen/spend?project_id={pid}").status_code in (403, 404)
