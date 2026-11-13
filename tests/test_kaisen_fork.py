"""POST /kaisen/builds/{id}/fork: copy brief and files into a new idle build; nothing else comes along."""
import pytest

from app import config, models
from app.kaisen.models import (KaisenAsset, KaisenBuild, KaisenEvent, KaisenFile, KaisenInbox, KaisenRun,
                               KaisenSecret, KaisenSnapshot)
from tests.kaisen_helpers import TOKEN, db
from tests.test_kaisen_api import _add_files, _audit, _new_build as _queued_build, fakes, team  # noqa: F401 (fixtures)

V1 = {"kaisen.json": '{"runtime": "static"}', "index.html": "<h1>one</h1>\n"}
V2 = {"kaisen.json": '{"runtime": "static"}', "index.html": "<h1>two</h1>\n", "style.css": "body {}\n"}
SECRET_VALUE = "not-the-build-secret"


def _fork(c, bid, **body):
    return c.post(f"/kaisen/builds/{bid}/fork", json=body)


def _files(bid):
    s = db()
    s.expire_all()
    return {r.path: r.content for r in s.query(KaisenFile).filter_by(build_id=bid)}


def _new_build(c, pid, **kw):
    """_new_build leaves run 1 queued; a fork of current files needs the source idle, so finish it."""
    bid = _queued_build(c, pid, **kw)
    _set_run_status(bid, "done")
    return bid


def _set_run_status(bid, status):
    s = db()
    for r in s.query(KaisenRun).filter_by(build_id=bid):
        r.status = status
    s.get(KaisenBuild, bid).status = status
    s.commit()


@pytest.fixture()
def src(team):
    """A build with V1 files and a finished first run, plus a restore point of V1 (taken by a feedback run)."""
    _, a, b, c, pid = team
    bid = _new_build(a, pid, files=V1)
    _set_run_status(bid, "done")
    assert a.post(f"/kaisen/builds/{bid}/feedback", json={"text": "change"}).status_code == 200
    _set_run_status(bid, "done")
    s = db()
    for path in list(V1):
        s.query(KaisenFile).filter_by(build_id=bid, path=path).delete()
    s.commit()
    _add_files(bid, V2)  # the restore point holds V1, the current files are V2
    sid = s.query(KaisenSnapshot).filter_by(build_id=bid).one().id
    return a, b, c, pid, bid, sid


def test_fork_current_files(src):
    a, _, _, pid, bid, _ = src
    r = _fork(a, bid)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["id"] != bid and d["name"] == "Todo list fork" and d["brief"] == "A tiny todo list"
    assert d["status"] == "idle" and d["project_id"] == pid and d["runs"] == [] and d["app"] is None
    assert {f["path"] for f in d["files"]} == set(V2) and _files(d["id"]) == V2
    assert d["forked_from"] == {"id": bid, "name": "Todo list", "snapshot_id": None, "secret_names": []}
    assert d["forked_from_id"] == bid
    assert a.get(f"/kaisen/builds/{d['id']}").json()["forked_from"]["id"] == bid
    assert [x for x in a.get("/kaisen/builds", params={"project_id": pid}).json() if x["id"] == d["id"]][0]["forked_from_id"] == bid
    assert _files(bid) == V2  # the source is untouched


def test_fork_from_a_restore_point(src):
    a, _, _, _, bid, sid = src
    d = _fork(a, bid, snapshot_id=sid, name="  Try V1  ").json()
    assert d["name"] == "Try V1" and _files(d["id"]) == V1
    assert d["forked_from"]["snapshot_id"] == sid
    row = db().get(KaisenBuild, d["id"])
    assert (row.forked_from_id, row.forked_from_snapshot_id) == (bid, sid)


def test_default_name_is_trimmed_to_fit(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid, name="n" * config.KAISEN_NAME_MAX)
    name = _fork(a, bid).json()["name"]
    assert len(name) == config.KAISEN_NAME_MAX and name.endswith(" fork")


def test_snapshot_of_another_build_is_404(src):
    a, _, _, pid, bid, sid = src
    other = _new_build(a, pid, name="Other", files=V1)
    r = _fork(a, other, snapshot_id=sid)
    assert r.status_code == 404
    assert _fork(a, bid, snapshot_id=999999).status_code == 404
    assert db().query(KaisenBuild).filter(KaisenBuild.forked_from_id.is_not(None)).count() == 0


def test_active_run_blocks_current_files_but_not_a_restore_point(src):
    a, _, _, _, bid, sid = src
    _set_run_status(bid, "running")
    r = _fork(a, bid)
    assert r.status_code == 409 and r.json()["detail"] == (
        "A run is in progress. Fork from a restore point, or wait for it to finish.")
    ok = _fork(a, bid, snapshot_id=sid)
    assert ok.status_code == 201 and _files(ok.json()["id"]) == V1


def test_build_cap_uses_create_builds_message(team, monkeypatch):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    monkeypatch.setattr(config, "KAISEN_MAX_BUILDS_PER_PROJECT", 1)
    r = _fork(a, bid)
    create = a.post("/kaisen/builds", json={"project_id": pid, "name": "x", "brief": "y"})
    assert r.status_code == create.status_code == 403 and r.json()["detail"] == create.json()["detail"]
    assert db().query(KaisenBuild).filter(KaisenBuild.forked_from_id.is_not(None)).count() == 0


def test_member_can_fork_non_member_and_anonymous_cannot(team, make_client):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    assert _fork(b, bid).status_code == 201
    assert _fork(c, bid).status_code == 404
    assert _fork(make_client(), bid).status_code == 401
    assert _fork(a, 999999).status_code == 404


def test_validation_errors(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    assert _fork(a, bid, name="").status_code == 422
    assert _fork(a, bid, name="   ").status_code == 422
    assert _fork(a, bid, name="x" * (config.KAISEN_NAME_MAX + 1)).status_code == 422
    assert _fork(a, bid, snapshot_id="nope").status_code == 422
    assert _fork(a, bid, bogus=1).status_code == 422


def test_read_only_key_is_refused(team, make_client):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    key = a.post("/api/keys", json={"scope": "read"}).json()
    ro = make_client()
    ro.headers["Authorization"] = f"Bearer {key['key']}"
    assert _fork(ro, bid).status_code == 403
    assert db().query(KaisenBuild).filter(KaisenBuild.forked_from_id.is_not(None)).count() == 0


def test_secret_values_are_not_copied_and_names_are_listed(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    assert a.put(f"/kaisen/builds/{bid}/secrets/API_TOKEN", json={"value": SECRET_VALUE}).status_code == 200
    r = _fork(a, bid)
    assert r.status_code == 201 and SECRET_VALUE not in r.text
    d = r.json()
    assert d["forked_from"]["secret_names"] == ["API_TOKEN"] and d["secrets"] == []
    assert db().query(KaisenSecret).filter_by(build_id=d["id"]).count() == 0
    assert SECRET_VALUE not in a.get(f"/kaisen/builds/{d['id']}").text
    a.delete(f"/kaisen/builds/{bid}/secrets/API_TOKEN")  # names are read at the time of reading
    assert a.get(f"/kaisen/builds/{d['id']}").json()["forked_from"]["secret_names"] == []


def test_uploads_are_not_copied(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    s = db()
    s.add(KaisenAsset(build_id=bid, kind="asset", path="uploads/x.png", mime="image/png", ext="png", size=1,
                      sha256="a" * 64, orig_sha256="b" * 64, data=b"x"))
    s.commit()
    d = _fork(a, bid).json()
    assert db().query(KaisenAsset).filter_by(build_id=d["id"]).count() == 0
    assert db().query(KaisenAsset).filter_by(build_id=bid).count() == 1


def test_github_output_and_token_are_not_copied(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    assert a.put(f"/kaisen/builds/{bid}/output", json={"mode": "github", "repo": "octo/demo", "branch": "dev",
                                                       "auth": "build", "github_token": TOKEN}).status_code == 200
    assert db().get(KaisenBuild, bid).github_token_enc
    d = _fork(a, bid).json()
    row = db().get(KaisenBuild, d["id"])
    assert (row.output_mode, row.github_repo, row.github_branch, row.github_auth, row.github_token_enc) == (
        "download", "", "", "project", None)
    assert d["output"]["mode"] == "download" and TOKEN not in str(d)


def test_no_runs_events_app_inbox_snapshots_or_test_results(src):
    a, _, _, _, bid, _ = src
    assert a.post(f"/kaisen/builds/{bid}/deploy").status_code == 200  # the source has an app
    s = db()
    sb = s.get(KaisenBuild, bid)
    sb.test_status, sb.test_tail = "pass", "ok"
    run = s.query(KaisenRun).filter_by(build_id=bid).first()
    s.add(KaisenEvent(run_id=run.id, build_id=bid, project_id=sb.project_id, user_id=sb.created_by, model="m", usd=0.5))
    s.commit()
    before = (s.query(KaisenEvent).count(), s.query(models.App).count())
    d = _fork(a, bid).json()
    s.expire_all()
    fid = d["id"]
    assert s.query(KaisenRun).filter_by(build_id=fid).count() == 0
    assert s.query(KaisenEvent).filter_by(build_id=fid).count() == 0
    assert s.query(KaisenInbox).filter_by(build_id=fid).count() == 0
    assert s.query(KaisenSnapshot).filter_by(build_id=fid).count() == 0
    assert (s.query(KaisenEvent).count(), s.query(models.App).count()) == before  # no spend, no app
    assert d["app"] is None and d["spend"]["build_usd"] == 0 and d["cost_usd"] == 0 and d["tests"] is None
    assert s.get(KaisenBuild, fid).app_id is None


def test_forked_from_name_is_null_after_the_source_is_deleted(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    a.put(f"/kaisen/builds/{bid}/secrets/API_TOKEN", json={"value": SECRET_VALUE})
    fid = _fork(a, bid).json()["id"]
    assert a.delete(f"/kaisen/builds/{bid}").status_code == 204
    d = a.get(f"/kaisen/builds/{fid}").json()
    assert d["forked_from"] == {"id": bid, "name": None, "snapshot_id": None, "secret_names": []}
    assert _files(fid)  # the copy keeps its own files


def test_non_fork_has_null_forked_from(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    assert a.get(f"/kaisen/builds/{bid}").json()["forked_from"] is None


def test_fork_is_audited(src):
    a, _, _, _, bid, sid = src
    d1 = _fork(a, bid).json()
    d2 = _fork(a, bid, snapshot_id=sid).json()
    rows = {r.target_id: r.detail for r in _audit("kaisen.fork")}
    assert rows[str(d1["id"])] == f"from={bid} snapshot=-"
    assert rows[str(d2["id"])] == f"from={bid} snapshot={sid}"


def test_feedback_on_a_fork_works_on_the_copied_files(team, fakes):
    _, a, _, _, pid = team
    bid = _new_build(a, pid, files=V1)
    fid = _fork(a, bid).json()["id"]
    r = a.post(f"/kaisen/builds/{fid}/feedback", json={"text": "make it blue"})
    assert r.status_code == 200, r.text
    run = db().query(KaisenRun).filter_by(build_id=fid).one()
    assert run.kind == "feedback" and "index.html" in run.messages[0]["content"] and "make it blue" in run.messages[0]["content"]
    assert db().query(KaisenSnapshot).filter_by(build_id=fid).count() == 1  # a restore point of the copied files


def test_feedback_on_a_build_without_files_still_says_run_first(team):
    _, a, _, _, pid = team
    empty = a.post("/kaisen/builds", json={"project_id": pid, "name": "e", "brief": "b", "start": False}).json()["id"]
    r = a.post(f"/kaisen/builds/{empty}/feedback", json={"text": "x"})
    assert r.status_code == 409 and r.json()["detail"] == "Run the build first, then ask for changes."
    fork = _fork(a, empty).json()["id"]  # a fork of nothing has nothing to change either
    assert a.post(f"/kaisen/builds/{fork}/feedback", json={"text": "x"}).json()["detail"] == "Run the build first, then ask for changes."


def test_build_run_on_a_fork_is_run_one_with_the_files_in_place(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid, files=V1)
    fid = _fork(a, bid).json()["id"]
    r = a.post(f"/kaisen/builds/{fid}/run")
    assert r.status_code == 200, r.text
    run = db().query(KaisenRun).filter_by(build_id=fid).one()
    assert run.kind == "build" and "copy of another build" in run.messages[0]["content"]
    assert _files(fid) == V1  # still in the workspace the model reads


def test_deploy_of_a_fork_without_runs_creates_its_own_app(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid, files=V1)
    first = a.post(f"/kaisen/builds/{bid}/deploy").json()["app"]
    fid = _fork(a, bid, name="Todo list fork").json()["id"]
    r = a.post(f"/kaisen/builds/{fid}/deploy")
    assert r.status_code == 200, r.text
    app = r.json()["app"]
    assert app["id"] != first["id"] and app["hostname"] != first["hostname"]
