"""Kaisen H5: restore points and per-run diff. Real login flow; runs are queued through the API and finished in the DB."""
import pytest

from app import config
from app.kaisen import snapshots
from app.kaisen.models import KaisenBuild, KaisenFile, KaisenRun, KaisenSnapshot
from tests.kaisen_helpers import db
from tests.test_kaisen_api import _add_files, _audit, _join, _new_build, fakes, team  # noqa: F401 (fixtures)

V1 = {"kaisen.json": '{"runtime": "static"}', "index.html": "<h1>one</h1>\n"}
V2 = {"kaisen.json": '{"runtime": "static"}', "index.html": "<h1>two</h1>\n", "style.css": "body {}\n"}


def _set_files(bid, files):
    s = db()
    s.query(KaisenFile).filter_by(build_id=bid).delete()
    s.commit()
    _add_files(bid, files)


def _files(bid):
    s = db()
    s.expire_all()
    return {r.path: r.content for r in s.query(KaisenFile).filter_by(build_id=bid)}


def _finish_runs(bid, status="done"):
    s = db()
    for r in s.query(KaisenRun).filter_by(build_id=bid):
        r.status = status
    b = s.get(KaisenBuild, bid)
    b.status = status
    s.commit()


def _snaps(bid):
    s = db()
    s.expire_all()
    return s.query(KaisenSnapshot).filter_by(build_id=bid).order_by(KaisenSnapshot.id).all()


def _feedback(c, bid, text="change it"):
    r = c.post(f"/kaisen/builds/{bid}/feedback", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def built(team):
    """A build with files V1 whose first run is done."""
    admin, a, b, c, pid = team
    bid = _new_build(a, pid, files=V1)
    _finish_runs(bid)
    return a, b, c, pid, bid


# ---- taking snapshots ------------------------------------------------------------------------------

def test_empty_file_set_is_not_snapshotted(team):
    _, a, _, _, pid = team
    bid = a.post("/kaisen/builds", json={"project_id": pid, "name": "x", "brief": "y"}).json()["id"]  # queues run 1, no files
    assert _snaps(bid) == [] and a.get(f"/kaisen/builds/{bid}/snapshots").json() == []


def test_feedback_snapshots_current_files_and_links_the_run(built):
    a, _, _, _, bid = built
    d = _feedback(a, bid)
    snaps = _snaps(bid)
    assert len(snaps) == 1 and snaps[0].kind == "run_start" and snaps[0].run_id == d["runs"][0]["id"]
    assert snaps[0].files_count == 2 and snaps[0].bytes == sum(len(v.encode()) for v in V1.values())
    assert snapshots.load(snaps[0]) == V1
    assert d["runs"][0]["has_diff"] is True and d["runs"][1]["has_diff"] is False
    rows = a.get(f"/kaisen/builds/{bid}/snapshots").json()
    assert rows == [{"id": snaps[0].id, "run_id": snaps[0].run_id, "run_number": 2, "kind": "run_start",
                     "created_at": rows[0]["created_at"], "files_count": 2, "bytes": snaps[0].bytes}]


def test_resume_snapshots_too(built):
    a, _, _, _, bid = built
    _finish_runs(bid, "stopped")
    r = a.post(f"/kaisen/builds/{bid}/run")
    assert r.status_code == 200 and r.json()["runs"][0]["kind"] == "resume"
    assert len(_snaps(bid)) == 1 and _snaps(bid)[0].run_id == r.json()["runs"][0]["id"]


def test_unchanged_files_are_not_snapshotted_again(built):
    a, _, _, _, bid = built
    _feedback(a, bid)
    _finish_runs(bid)
    d = _feedback(a, bid)
    assert len(_snaps(bid)) == 1  # same sha: nothing new, the second run links nothing
    assert d["runs"][0]["has_diff"] is False
    assert a.get(f"/kaisen/builds/{bid}/runs/{d['runs'][0]['id']}/diff").status_code == 404


def test_pruned_to_the_configured_count_oldest_first(built, monkeypatch):
    a, _, _, _, bid = built
    monkeypatch.setattr(config, "KAISEN_SNAPSHOTS_KEPT", 3)
    for i in range(5):
        _set_files(bid, {"a.txt": f"v{i}"})
        _feedback(a, bid)
        _finish_runs(bid)
    kept = [snapshots.load(x)["a.txt"] for x in _snaps(bid)]
    assert kept == ["v2", "v3", "v4"]


def test_oversized_snapshot_is_skipped(built, monkeypatch):
    a, _, _, _, bid = built
    monkeypatch.setattr(config, "KAISEN_SNAPSHOT_MAX_BYTES", 10)
    _set_files(bid, {"a.txt": "x" * 5000})
    _feedback(a, bid)
    assert _snaps(bid) == []


def test_binary_blob_roundtrips_unicode(built):
    a, _, _, _, bid = built
    _set_files(bid, {"é.txt": "héllo ✓\n"})
    _feedback(a, bid)
    assert snapshots.load(_snaps(bid)[0]) == {"é.txt": "héllo ✓\n"}


# ---- restore -----------------------------------------------------------------------------------------

def test_restore_replaces_files_takes_before_restore_and_is_undoable(built):
    a, b, _, _, bid = built
    _feedback(a, bid)
    _finish_runs(bid)
    first = _snaps(bid)[0]
    _set_files(bid, V2)  # what the run produced
    r = b.post(f"/kaisen/builds/{bid}/snapshots/{first.id}/restore")  # a member may restore
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored"] is True and body["files_count"] == 2
    assert _files(bid) == V1
    snaps = _snaps(bid)
    before = next(x for x in snaps if x.id == body["before_restore_snapshot_id"])
    assert before.kind == "before_restore" and before.run_id is None and snapshots.load(before) == V2
    assert [e.action for e in _audit("kaisen.restore")] == ["kaisen.restore"]
    # undo: restore the before_restore snapshot
    r2 = a.post(f"/kaisen/builds/{bid}/snapshots/{before.id}/restore")
    assert r2.status_code == 200 and _files(bid) == V2
    assert snapshots.load(next(x for x in _snaps(bid) if x.id == r2.json()["before_restore_snapshot_id"])) == V1


def test_restore_when_files_are_unchanged_reuses_newest_snapshot(built):
    a, _, _, _, bid = built
    _feedback(a, bid)
    _finish_runs(bid)
    only = _snaps(bid)[0]
    r = a.post(f"/kaisen/builds/{bid}/snapshots/{only.id}/restore")
    assert r.status_code == 200 and r.json()["before_restore_snapshot_id"] == only.id
    assert len(_snaps(bid)) == 1


def test_restore_refused_while_a_run_is_active(built):
    a, _, _, _, bid = built
    _feedback(a, bid)  # queued
    _set_files(bid, V2)
    sid = _snaps(bid)[0].id
    for status in ("queued", "running", "waiting"):
        s = db()
        s.query(KaisenRun).filter_by(build_id=bid).order_by(KaisenRun.id.desc()).first().status = status
        s.commit()
        r = a.post(f"/kaisen/builds/{bid}/snapshots/{sid}/restore")
        assert r.status_code == 409 and r.json()["detail"] == "A run is in progress; stop it before restoring."
    assert _files(bid) == V2 and len(_snaps(bid)) == 1 and _audit("kaisen.restore") == []


def test_restore_marks_preview_stale_and_next_deploy_builds_a_new_bundle(built):
    from app.kaisen import httptool
    from app.kaisen.models import KaisenBundle
    a, _, _, _, bid = built
    assert a.post(f"/kaisen/builds/{bid}/deploy").status_code == 200
    s = db()
    b = s.get(KaisenBuild, bid)
    assert httptool.files_changed(s, b) is False
    _feedback(a, bid)
    _finish_runs(bid)
    first = _snaps(bid)[0]
    _set_files(bid, V2)
    assert a.post(f"/kaisen/builds/{bid}/deploy").status_code == 200
    h2 = s.query(KaisenBundle).filter_by(build_id=bid).one().sha256
    assert a.post(f"/kaisen/builds/{bid}/snapshots/{first.id}/restore").status_code == 200
    s.expire_all()
    assert httptool.files_changed(s, s.get(KaisenBuild, bid)) is True  # restore does not deploy
    assert s.query(KaisenBundle).filter_by(build_id=bid).one().sha256 == h2
    assert a.post(f"/kaisen/builds/{bid}/deploy").status_code == 200
    s.expire_all()
    assert s.query(KaisenBundle).filter_by(build_id=bid).one().sha256 != h2
    assert httptool.files_changed(s, s.get(KaisenBuild, bid)) is False


# ---- permissions ----------------------------------------------------------------------------------------

def test_anonymous_gets_401(built, make_client):
    _, _, _, _, bid = built
    anon = make_client()
    for method, url in (("get", f"/kaisen/builds/{bid}/snapshots"), ("get", f"/kaisen/builds/{bid}/snapshots/1/diff"),
                        ("get", f"/kaisen/builds/{bid}/runs/1/diff"), ("post", f"/kaisen/builds/{bid}/snapshots/1/restore")):
        assert getattr(anon, method)(url).status_code == 401


def test_non_member_gets_404_everywhere(built):
    a, _, c, _, bid = built
    _feedback(a, bid)
    snap = _snaps(bid)[0]
    run_id = snap.run_id
    assert c.get(f"/kaisen/builds/{bid}/snapshots").status_code == 404
    assert c.get(f"/kaisen/builds/{bid}/snapshots/{snap.id}/diff").status_code == 404
    assert c.get(f"/kaisen/builds/{bid}/runs/{run_id}/diff").status_code == 404
    _finish_runs(bid)
    _set_files(bid, V2)
    assert c.post(f"/kaisen/builds/{bid}/snapshots/{snap.id}/restore").status_code == 404
    assert _files(bid) == V2


def test_snapshot_of_another_build_is_404(team):
    _, a, _, _, pid = team
    b1 = _new_build(a, pid, "one", V1)
    b2 = _new_build(a, pid, "two", V2)
    _finish_runs(b1), _finish_runs(b2)
    _feedback(a, b1)
    sid = _snaps(b1)[0].id
    assert a.get(f"/kaisen/builds/{b2}/snapshots/{sid}/diff").status_code == 404
    assert a.post(f"/kaisen/builds/{b2}/snapshots/{sid}/restore").status_code == 404
    assert a.post(f"/kaisen/builds/{b1}/snapshots/99999/restore").status_code == 404


def test_read_scope_key_can_get_but_not_restore(built, make_client):
    a, _, _, _, bid = built
    _feedback(a, bid)
    _finish_runs(bid)
    sid = _snaps(bid)[0].id
    _set_files(bid, V2)
    key = a.post("/api/keys", json={"scope": "read"}).json()
    ro = make_client()
    ro.headers["Authorization"] = f"Bearer {key['key']}"
    assert ro.get(f"/kaisen/builds/{bid}/snapshots").status_code == 200
    assert ro.get(f"/kaisen/builds/{bid}/snapshots/{sid}/diff").status_code == 200
    assert ro.post(f"/kaisen/builds/{bid}/snapshots/{sid}/restore").status_code == 403
    assert _files(bid) == V2


# ---- diff ---------------------------------------------------------------------------------------------

def test_snapshot_diff_shape_against_current_files(built):
    a, _, _, _, bid = built
    _feedback(a, bid)
    sid = _snaps(bid)[0].id
    _set_files(bid, {"kaisen.json": V1["kaisen.json"], "index.html": "<h1>two</h1>\n", "style.css": "body {}\n"})
    r = a.get(f"/kaisen/builds/{bid}/snapshots/{sid}/diff")
    assert r.status_code == 200
    d = r.json()
    assert d["from"] == "snapshot" and d["to"] == "current" and d["truncated"] is False
    assert d["added"] == ["style.css"] and d["removed"] == []
    assert [c["path"] for c in d["changed"]] == ["index.html"]
    assert "-<h1>one</h1>" in d["changed"][0]["diff"] and "+<h1>two</h1>" in d["changed"][0]["diff"]


def test_diff_unit_added_removed_and_missing_trailing_newline():
    d = snapshots.diff({"a": "x", "gone": "1"}, {"a": "y", "new": "2"})
    assert d["added"] == ["new"] and d["removed"] == ["gone"] and d["changed"][0]["path"] == "a"
    assert "No newline" in d["changed"][0]["diff"]


def test_per_file_and_total_caps(monkeypatch):
    monkeypatch.setattr(config, "KAISEN_DIFF_FILE_MAX_BYTES", 100)
    monkeypatch.setattr(config, "KAISEN_DIFF_MAX_BYTES", 250)
    old = {f"f{i}.txt": "a\n" * 200 for i in range(5)}
    new = {k: "b\n" * 200 for k in old}
    d = snapshots.diff(old, new)
    assert d["truncated"] is True and [c["path"] for c in d["changed"]] == sorted(old)
    assert d["changed"][0]["diff"].rstrip().endswith("... diff truncated")
    with_diff = [c for c in d["changed"] if c["diff"]]
    assert 1 <= len(with_diff) < 5 and all(c["diff"] == "" for c in d["changed"][len(with_diff):])
    assert sum(len(c["diff"]) for c in with_diff) < 250 + 150


def test_diff_redacts_build_secrets(built):
    a, _, _, _, bid = built
    secret = "not-the-secret-value"
    assert a.put(f"/kaisen/builds/{bid}/secrets/MY_TOKEN", json={"value": secret}).status_code == 200
    _set_files(bid, {"app.py": f"TOKEN = '{secret}'\nold = 1\n"})
    _feedback(a, bid)
    sid = _snaps(bid)[0].id
    _set_files(bid, {"app.py": f"TOKEN = '{secret}'\nold = 2\n", f"{'n'}.txt": "x"})
    r = a.get(f"/kaisen/builds/{bid}/snapshots/{sid}/diff")
    assert secret not in r.text and "[REDACTED]" in r.json()["changed"][0]["diff"]


def test_run_diff_between_start_and_next_snapshot_then_current(built):
    a, _, _, _, bid = built
    d2 = _feedback(a, bid)  # run 2 starts on V1
    run2 = d2["runs"][0]["id"]
    _set_files(bid, V2)  # run 2 produced V2
    _finish_runs(bid)
    r = a.get(f"/kaisen/builds/{bid}/runs/{run2}/diff")  # latest run: against the current files
    assert r.status_code == 200
    d = r.json()
    assert d["from"] == "before run #2" and d["to"] == "after run #2"
    assert d["added"] == ["style.css"] and [c["path"] for c in d["changed"]] == ["index.html"]
    d3 = _feedback(a, bid)  # run 3 starts on V2 -> snapshot 2
    _set_files(bid, {"only.txt": "z"})
    r = a.get(f"/kaisen/builds/{bid}/runs/{run2}/diff").json()  # now bounded by run 3's start, not by current files
    assert r["added"] == ["style.css"] and r["removed"] == []
    r3 = a.get(f"/kaisen/builds/{bid}/runs/{d3['runs'][0]['id']}/diff").json()
    assert r3["removed"] == sorted(V2) and r3["added"] == ["only.txt"]


def test_run_diff_404_without_snapshot_and_for_unknown_run(built):
    a, _, _, _, bid = built
    run1 = a.get(f"/kaisen/builds/{bid}").json()["runs"][0]
    assert run1["has_diff"] is False
    assert a.get(f"/kaisen/builds/{bid}/runs/{run1['id']}/diff").status_code == 404
    assert a.get(f"/kaisen/builds/{bid}/runs/99999/diff").status_code == 404


def test_run_numbers_are_one_based_per_build(built):
    a, _, _, _, bid = built
    _feedback(a, bid)
    _finish_runs(bid)
    _set_files(bid, V2)
    _feedback(a, bid)
    assert [x["run_number"] for x in a.get(f"/kaisen/builds/{bid}/snapshots").json()] == [3, 2]


def test_before_restore_snapshot_has_null_run_number(built):
    a, _, _, _, bid = built
    _feedback(a, bid)
    _finish_runs(bid)
    sid = _snaps(bid)[0].id
    _set_files(bid, V2)
    a.post(f"/kaisen/builds/{bid}/snapshots/{sid}/restore")
    top = a.get(f"/kaisen/builds/{bid}/snapshots").json()[0]
    assert top["kind"] == "before_restore" and top["run_id"] is None and top["run_number"] is None


# ---- lifecycle ---------------------------------------------------------------------------------------------

def test_deleting_the_build_removes_its_snapshots(built):
    a, _, _, _, bid = built
    _feedback(a, bid)
    assert len(_snaps(bid)) == 1
    assert a.delete(f"/kaisen/builds/{bid}").status_code == 204
    assert _snaps(bid) == []


def test_snapshot_fk_cascades_on_build_row_delete():
    fk = [c for c in KaisenSnapshot.__table__.foreign_keys if c.column.table.name == "kaisen_builds"]
    assert fk and fk[0].ondelete == "CASCADE"


# ---- security review hardening (K28-K31) -------------------------------------------------------------

def _forge(bid, files_json: bytes):
    import zlib
    s = db()
    s.add(KaisenSnapshot(build_id=bid, run_id=None, kind="run_start", files_count=1, bytes=1, sha256="0" * 64,
                         blob=zlib.compress(files_json)))
    s.commit()
    return _snaps(bid)[-1].id


def test_decompression_bomb_is_refused_without_inflating(built):
    import json as _json
    a, _, _, _, bid = built
    big = _json.dumps({"index.html": "a" * (40 * 1024 * 1024)}).encode()  # ~40 MB of output from ~40 KB
    sid = _forge(bid, big)
    before = _files(bid)
    assert a.post(f"/kaisen/builds/{bid}/snapshots/{sid}/restore").status_code == 409
    assert a.get(f"/kaisen/builds/{bid}/snapshots/{sid}/diff").status_code == 409
    assert _files(bid) == before and len(_snaps(bid)) == 1  # no before_restore snapshot for a bad blob either


@pytest.mark.parametrize("payload", [
    b'{"../etc/passwd": "x"}', b'{"/abs": "x"}', b'{".git/config": "x"}', b'{"a//b": "x"}', b'{"a": 5}', b'[1]',
    b'not json',
])
def test_restore_revalidates_paths_and_shapes(built, payload):
    a, _, _, _, bid = built
    sid = _forge(bid, payload)
    before = _files(bid)
    assert a.post(f"/kaisen/builds/{bid}/snapshots/{sid}/restore").status_code == 409
    assert _files(bid) == before


def test_restore_enforces_size_and_count_limits(built, monkeypatch):
    a, _, _, _, bid = built
    import json as _json
    monkeypatch.setattr(config, "KAISEN_MAX_FILE_BYTES", 10)
    sid = _forge(bid, _json.dumps({"a.txt": "x" * 11}).encode())
    assert a.post(f"/kaisen/builds/{bid}/snapshots/{sid}/restore").status_code == 409
    monkeypatch.setattr(config, "KAISEN_MAX_FILE_BYTES", 200 * 1024)
    monkeypatch.setattr(config, "KAISEN_MAX_FILES", 1)
    sid = _forge(bid, _json.dumps({"a.txt": "x", "b.txt": "y"}).encode())
    assert a.post(f"/kaisen/builds/{bid}/snapshots/{sid}/restore").status_code == 409


def test_diff_is_bounded_on_pathological_line_counts(monkeypatch):
    import random
    import time
    rnd = random.Random(1)
    old = {"big.txt": "".join(f"{rnd.randint(0, 150)}\n" for _ in range(100_000))}
    new = {"big.txt": "".join(f"{rnd.randint(0, 150)}\n" for _ in range(100_000))}
    t0 = time.monotonic()
    d = snapshots.diff(old, new)  # unbounded difflib takes ~17 s here
    assert time.monotonic() - t0 < 2
    assert d["truncated"] is True and "too many lines" in d["changed"][0]["diff"]


def test_diff_time_budget_lists_remaining_files_without_diff(monkeypatch):
    monkeypatch.setattr(config, "KAISEN_DIFF_BUDGET_SECONDS", -1)
    d = snapshots.diff({"a": "1\n", "b": "1\n"}, {"a": "2\n", "b": "2\n"})
    assert d["truncated"] is True and all(c["diff"] == "" for c in d["changed"])


def test_snapshot_failure_does_not_block_starting_a_run(built, monkeypatch):
    a, _, _, _, bid = built
    def boom(*args, **kw):
        raise RuntimeError("packet too large")
    monkeypatch.setattr(snapshots, "take", boom)
    assert a.post(f"/kaisen/builds/{bid}/feedback", json={"text": "change it"}).status_code == 200


def test_build_detail_says_how_many_restore_points_are_kept(built):
    a, _, _, _, bid = built
    assert a.get(f"/kaisen/builds/{bid}").json()["snapshots_kept"] == config.KAISEN_SNAPSHOTS_KEPT
