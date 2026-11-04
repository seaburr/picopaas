"""GitHub push as an async job (202 + poll), GitHub error mapping, and the per-project web search / uploads flags."""
from datetime import timedelta

import httpx
import pytest

from app import config, models
from app.kaisen import pushing
from app.kaisen.github import GitHubError, RealGitHub
from app.kaisen.models import KaisenPush
from tests.kaisen_helpers import KEY, TOKEN, db
from tests.test_kaisen_api import _new_build, _out, _user, fakes, team  # noqa: F401


def _ready(team):
    _, a, _b, _c, pid = team
    bid = _new_build(a, pid)
    a.put(f"/projects/{pid}/kaisen", json={"github_token": TOKEN})
    assert _out(a, bid, mode="github", repo="octo/guitar-tuner").status_code == 200
    return a, bid


def test_push_returns_202_and_the_job_view(team, fakes):
    a, bid = _ready(team)
    r = a.post(f"/kaisen/builds/{bid}/push")
    assert r.status_code == 202
    j = r.json()
    assert j["status"] == "done" and j["url"] == "https://github.com/octo/guitar-tuner/tree/kaisen/todo-list"
    assert j["error_code"] is None and j["finished_at"] and fakes.github.pushes[-1]["repo"] == "octo/guitar-tuner"
    got = a.get(f"/kaisen/builds/{bid}/push/{j['id']}")
    assert got.status_code == 200 and got.json() == j
    assert db().get(KaisenPush, j["id"]).status == "done"
    assert a.get(f"/kaisen/builds/{bid}").json()["push"]["id"] == j["id"]


def test_push_anonymous_and_outsider(team, fakes, make_client):
    a, bid = _ready(team)
    _, _, _, outsider, _ = team
    job = a.post(f"/kaisen/builds/{bid}/push").json()["id"]
    anon = make_client()
    assert anon.post(f"/kaisen/builds/{bid}/push").status_code == 401
    assert anon.get(f"/kaisen/builds/{bid}/push/{job}").status_code == 401
    assert outsider.post(f"/kaisen/builds/{bid}/push").status_code == 404
    assert outsider.get(f"/kaisen/builds/{bid}/push/{job}").status_code == 404


def test_push_status_of_another_builds_job_is_404(team, fakes):
    a, bid = _ready(team)
    other = _new_build(a, team[4], name="Other")
    job = a.post(f"/kaisen/builds/{bid}/push").json()["id"]
    assert a.get(f"/kaisen/builds/{other}/push/{job}").status_code == 404
    assert a.get(f"/kaisen/builds/{bid}/push/99999").status_code == 404


def test_push_not_set_up_is_422(team, fakes):
    _, a, _b, _c, pid = team
    bid = _new_build(a, pid)
    assert a.post(f"/kaisen/builds/{bid}/push").status_code == 422
    assert db().query(KaisenPush).count() == 0


def test_a_running_push_is_returned_not_duplicated(team, fakes):
    a, bid = _ready(team)
    s = db()
    s.add(KaisenPush(build_id=bid, status="running"))
    s.commit()
    r = a.post(f"/kaisen/builds/{bid}/push")
    assert r.status_code == 202 and r.json()["status"] == "running"
    assert db().query(KaisenPush).count() == 1 and fakes.github.pushes == []


def test_a_stale_running_push_is_reported_interrupted_and_a_new_one_starts(team, fakes):
    a, bid = _ready(team)
    s = db()
    old = KaisenPush(build_id=bid, status="running", created_at=pushing._now() - timedelta(seconds=config.KAISEN_PUSH_STALE_S + 5))
    s.add(old)
    s.commit()
    got = a.get(f"/kaisen/builds/{bid}/push/{old.id}").json()
    assert got["status"] == "error" and got["error_code"] == "interrupted"
    assert a.post(f"/kaisen/builds/{bid}/push").json()["status"] == "done"


@pytest.mark.parametrize("err,code", [
    (GitHubError("GitHub rejected the token (401).", "token_invalid", 401), "token_invalid"),
    (GitHubError("GitHub refused the request (403).", "no_permission", 403), "no_permission"),
    (GitHubError("GitHub rate limit reached.", "rate_limited", 403), "rate_limited"),
    (GitHubError("Not found (404).", "not_found", 404), "not_found"),
    (GitHubError("not a fast-forward", "non_fast_forward", 422), "non_fast_forward"),
    (GitHubError("Could not reach GitHub.", "unreachable"), "unreachable"),
])
def test_github_failures_end_the_job_with_a_code_and_never_leak_the_token(team, fakes, err, code):
    a, bid = _ready(team)
    fakes.github.fail = GitHubError(f"{err} token={TOKEN}", err.code, err.status)
    r = a.post(f"/kaisen/builds/{bid}/push")
    assert r.status_code == 202
    j = r.json()
    assert j["status"] == "error" and j["error_code"] == code
    assert TOKEN not in r.text and KEY not in r.text
    assert j["finished_at"] and a.get(f"/kaisen/builds/{bid}/push/{j['id']}").json()["status"] == "error"
    assert db().get(KaisenPush, j["id"]).status == "error"
    assert a.post(f"/kaisen/builds/{bid}/push").json()["status"] == "error"  # an error job does not block a retry


def test_an_unexpected_exception_still_ends_the_job(team, fakes):
    a, bid = _ready(team)
    fakes.github.fail = RuntimeError(f"boom {TOKEN}")
    j = a.post(f"/kaisen/builds/{bid}/push").json()
    assert j["status"] == "error" and j["error_code"] == "internal" and TOKEN not in j["message"]


def test_nothing_to_push_completes_with_a_message(team, fakes):
    _, a, _b, _c, pid = team
    bid = a.post("/kaisen/builds", json={"project_id": pid, "name": "Empty", "brief": "x"}).json()["id"]
    a.put(f"/projects/{pid}/kaisen", json={"github_token": TOKEN})
    _out(a, bid, mode="github", repo="octo/empty")
    j = a.post(f"/kaisen/builds/{bid}/push").json()
    assert j["status"] == "done" and j["message"] == "Nothing to push yet." and j["url"] is None


def test_threaded_runner_runs_the_job_on_its_own_session():
    import threading
    seen = []
    done = threading.Event()

    def job(s):
        seen.append(s.get_bind() is not None)
        done.set()
    pushing.threaded_runner(job)
    assert done.wait(5) and seen == [True]


# ---- RealGitHub error mapping over a mock transport -----------------------------------------------

def _gh(handler):
    return RealGitHub("not-the-github-token", transport=httpx.MockTransport(handler))


def _push(gh):
    return gh.push("octo/demo", [("a.txt", "x")], "slug", None, "msg", False)


def _repo_then(status, body, headers=None):
    def handler(req):
        if req.url.path == "/repos/octo/demo":
            return httpx.Response(200, json={"default_branch": "main"})
        if req.url.path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "a" * 40}})
        if req.url.path.endswith(f"/git/commits/{'a' * 40}"):
            return httpx.Response(200, json={"tree": {"sha": "b" * 40}})
        return httpx.Response(status, json=body, headers=headers or {})
    return handler


@pytest.mark.parametrize("status,body,headers,code", [
    (401, {"message": "Bad credentials"}, None, "token_invalid"),
    (403, {"message": "Resource not accessible by personal access token"}, None, "no_permission"),
    (403, {"message": "API rate limit exceeded"}, {"x-ratelimit-remaining": "0"}, "rate_limited"),
    (429, {"message": "slow down"}, None, "rate_limited"),
    (404, {"message": "Not Found"}, None, "not_found"),
    (422, {"message": "Update is not a fast forward"}, None, "non_fast_forward"),
    (422, {"message": "Protected branch update failed"}, None, "protected_branch"),
    (413, {"message": "too big"}, None, "too_large"),
    (503, {"message": "down"}, None, "github_down"),
    (418, {"message": "teapot"}, None, "github_error"),
])
def test_real_client_maps_github_errors(status, body, headers, code):
    with pytest.raises(GitHubError) as e:
        _push(_gh(_repo_then(status, body, headers)))
    assert e.value.code == code and e.value.status == status


def test_real_client_maps_timeouts_unreachable_and_bad_replies():
    def slow(req):
        raise httpx.ReadTimeout("slow")

    def down(req):
        raise httpx.ConnectError("nope")

    def html(req):
        return httpx.Response(200, text="<html>")
    for h, code in ((slow, "timeout"), (down, "unreachable"), (html, "bad_reply")):
        with pytest.raises(GitHubError) as e:
            _push(_gh(h))
        assert e.value.code == code


def test_real_client_409_empty_repo_and_bad_repo_name():
    with pytest.raises(GitHubError) as e:
        _push(_gh(_repo_then(409, {"message": "Git Repository is empty."})))
    assert e.value.code == "empty_repo"
    with pytest.raises(GitHubError) as e:
        _gh(lambda r: httpx.Response(200, json={})).push("not a repo", [], "s", None, "m", False)
    assert e.value.code == "bad_repo"


# ---- per-project features ---------------------------------------------------------------------------

@pytest.fixture()
def world(make_client):
    admin = _user(make_client, "admin@example.com")
    a, b = _user(make_client, "a@example.com"), _user(make_client, "b@example.com")
    pid = a.post("/projects", json={"name": "Alpha Team"}).json()["id"]
    return admin, a, b, pid


def test_features_get_put_audit_and_old_route(world):
    admin, a, b, pid = world
    assert admin.get(f"/admin/projects/{pid}/features").json() == {
        "project_id": pid, "name": "Alpha Team", "kaisen_web_search": True, "kaisen_uploads": False}
    r = admin.put(f"/admin/projects/{pid}/features", json={"kaisen_web_search": False, "kaisen_uploads": True})
    assert r.status_code == 200 and r.json()["kaisen_web_search"] is False and r.json()["kaisen_uploads"] is True
    p = db().get(models.Project, pid)
    assert (p.kaisen_web_search_enabled, p.kaisen_uploads_enabled) == (False, True)
    acts = {x.action: x.detail for x in db().query(models.AuditLog).filter(models.AuditLog.action.in_(["kaisen.uploads_flag", "kaisen.web_search_flag"]))}
    assert acts == {"kaisen.uploads_flag": f"project={pid} enabled=True", "kaisen.web_search_flag": f"project={pid} enabled=False"}
    assert admin.put(f"/admin/projects/{pid}/features", json={"kaisen_uploads": False}).json()["kaisen_web_search"] is False  # omitted = kept
    assert admin.get(f"/admin/projects/{pid}/kaisen-uploads").json()["kaisen_uploads_enabled"] is False  # old route still works
    row = next(x for x in admin.get("/admin/projects").json() if x["id"] == pid)
    assert row["kaisen_web_search_enabled"] is False and row["kaisen_uploads_enabled"] is False


def test_features_authz_and_validation(world, make_client):
    admin, a, b, pid = world
    for c in (a, b):
        assert c.get(f"/admin/projects/{pid}/features").status_code == 403
        assert c.put(f"/admin/projects/{pid}/features", json={"kaisen_uploads": True}).status_code == 403
    anon = make_client()
    assert anon.get(f"/admin/projects/{pid}/features").status_code == 401
    assert anon.put(f"/admin/projects/{pid}/features", json={}).status_code == 401
    assert admin.get("/admin/projects/99999/features").status_code == 404
    assert admin.put("/admin/projects/99999/features", json={"kaisen_uploads": True}).status_code == 404
    assert admin.put(f"/admin/projects/{pid}/features", json={"kaisen_uploads": "maybe"}).status_code == 422
    assert admin.put(f"/admin/projects/{pid}/features", json={"nope": True}).status_code == 422
    assert db().query(models.AuditLog).filter(models.AuditLog.action.like("kaisen.%flag")).count() == 0


def test_web_search_flag_off_forces_web_access_off_and_refuses_enabling(world):
    admin, a, b, pid = world
    assert a.put(f"/projects/{pid}/kaisen", json={"web_access": True}).status_code == 200
    s = a.get(f"/projects/{pid}/kaisen").json()
    assert s["web_access"] is True and s["web_project_enabled"] is True and s["web_disabled_reason"] is None
    admin.put(f"/admin/projects/{pid}/features", json={"kaisen_web_search": False})
    s = a.get(f"/projects/{pid}/kaisen").json()
    assert s["web_access"] is False and s["web_project_enabled"] is False
    assert s["web_disabled_reason"] == "Web search isn't enabled for this project." and s["web_note"] == s["web_disabled_reason"]
    from app.kaisen import settings as ks
    assert ks.load(db(), pid).web_access is False  # what the builder reads
    r = a.put(f"/projects/{pid}/kaisen", json={"web_access": True})
    assert r.status_code == 403 and "isn't enabled" in r.json()["detail"]
    assert a.put(f"/projects/{pid}/kaisen", json={"web_access": False}).status_code == 200  # turning off is fine
    assert a.put(f"/projects/{pid}/kaisen", json={"model": s["model"]}).status_code == 200  # other edits fine
    admin.put(f"/admin/projects/{pid}/features", json={"kaisen_web_search": True})
    assert a.put(f"/projects/{pid}/kaisen", json={"web_access": True}).json()["web_access"] is True


def test_stored_web_choice_survives_an_unrelated_save_while_flag_is_off(world):
    admin, a, b, pid = world
    a.put(f"/projects/{pid}/kaisen", json={"web_access": True})
    admin.put(f"/admin/projects/{pid}/features", json={"kaisen_web_search": False})
    a.put(f"/projects/{pid}/kaisen", json={"effort": "low"})
    admin.put(f"/admin/projects/{pid}/features", json={"kaisen_web_search": True})
    assert a.get(f"/projects/{pid}/kaisen").json()["web_access"] is True


# ---- admin project search and paging ----------------------------------------------------------------

def test_admin_projects_search_paging_and_personal(world, make_client):
    admin, a, b, pid = world
    for n in ("Beta Shop", "beta blog", "Gamma"):
        b.post("/projects", json={"name": n})
    plain = admin.get("/admin/projects")
    assert plain.status_code == 200 and isinstance(plain.json(), list) and len(plain.json()) == 4
    assert plain.headers["X-Total-Count"] == "4" and all(not x["is_personal"] for x in plain.json())
    r = admin.get("/admin/projects", params={"q": "BETA"}).json()
    assert r["total"] == 2 and {x["name"] for x in r["items"]} == {"Beta Shop", "beta blog"}
    r = admin.get("/admin/projects", params={"q": "b@example.com"}).json()
    assert r["total"] == 3  # owner email match (shared projects only)
    r = admin.get("/admin/projects", params={"include_personal": "true"}).json()
    assert r["total"] == 4 + 3 and any(x["is_personal"] for x in r["items"])  # three users, three personal projects
    r = admin.get("/admin/projects", params={"include_personal": "true", "limit": 2, "offset": 2}).json()
    assert len(r["items"]) == 2 and r["limit"] == 2 and r["offset"] == 2 and r["total"] == 7
    assert admin.get("/admin/projects", params={"q": "100%_"}).json()["total"] == 0  # wildcards are literal
    assert admin.get("/admin/projects", params={"q": "personal-name-miss"}).json()["items"] == []


def test_admin_projects_authz_and_validation(world, make_client):
    admin, a, b, pid = world
    assert a.get("/admin/projects", params={"q": "x"}).status_code == 403
    assert make_client().get("/admin/projects", params={"q": "x"}).status_code == 401
    assert admin.get("/admin/projects", params={"limit": 0}).status_code == 422
    assert admin.get("/admin/projects", params={"limit": config.ADMIN_PROJECTS_PAGE_MAX + 1}).status_code == 422
    assert admin.get("/admin/projects", params={"offset": -1}).status_code == 422
