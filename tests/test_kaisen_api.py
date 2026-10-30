"""Kaisen (Preview) API: settings, builds, deploy into the build's project, bundles, feature flag. Real login flow."""
import hashlib
import io
import tarfile

import pytest

from app import config, crypto, models
from app.dependencies import get_registry
from app.main import app as fastapi_app
from app.providers.fake import FakeRegistry
from app.routers.kaisen import get_github_factory, get_model_factory
from tests.conftest import register_and_login
from tests.kaisen_helpers import KEY, TOKEN, Fakes, db

IMG = "ghcr.io/example/kaisen-runtime:1"


@pytest.fixture()
def fakes(make_client, monkeypatch):
    f = Fakes()
    fastapi_app.dependency_overrides[get_github_factory] = lambda: (lambda token: f.github)
    fastapi_app.dependency_overrides[get_model_factory] = lambda: (lambda key: f.anthropic)
    monkeypatch.setattr(config, "KAISEN_RUNTIME_IMAGE", IMG)
    return f


def _user(make_client, email):
    c = make_client()
    register_and_login(c, email)
    return c


def _pid(c):
    return next(p["id"] for p in c.get("/projects").json() if p["is_personal"])


def _join(owner, joiner, pid):
    inv = owner.post(f"/projects/{pid}/invites", json={"role": "member"}).json()
    assert joiner.post(f"/invites/{inv['token']}/accept").status_code == 200


@pytest.fixture()
def team(make_client, fakes):
    """admin (first user), owner a, member b, outsider c, shared project with a Kaisen key saved."""
    admin = _user(make_client, "admin@example.com")
    a, b, c = (_user(make_client, f"{n}@example.com") for n in "abc")
    pid = a.post("/projects", json={"name": "Team"}).json()["id"]
    _join(a, b, pid)
    assert a.put(f"/projects/{pid}/kaisen", json={"anthropic_key": KEY}).status_code == 200
    return admin, a, b, c, pid


def _add_files(build_id, files):
    from app.kaisen.models import KaisenFile
    s = db()
    for path, content in files.items():
        s.add(KaisenFile(build_id=build_id, path=path, content=content, size=len(content)))
    s.commit()


def _new_build(c, pid, name="Todo list", files=None):
    r = c.post("/kaisen/builds", json={"project_id": pid, "name": name, "brief": "A tiny todo list"})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    _add_files(bid, files or {"kaisen.json": '{"runtime": "static"}', "public/index.html": "<h1>hi</h1>"})
    return bid


def _audit(action):
    return db().query(models.AuditLog).filter_by(action=action).all()


# ---- settings ----------------------------------------------------------------------------------

def test_settings_require_login(client):
    for method, path in (("get", "/projects/1/kaisen"), ("put", "/projects/1/kaisen"),
                         ("post", "/projects/1/kaisen/test/anthropic")):
        assert getattr(client, method)(path).status_code == 401


def test_settings_non_member_gets_404(team):
    _, a, b, c, pid = team
    assert c.get(f"/projects/{pid}/kaisen").status_code == 404
    assert c.put(f"/projects/{pid}/kaisen", json={"model": "claude-sonnet-5-5"}).status_code == 404
    assert c.post(f"/projects/{pid}/kaisen/test/anthropic").status_code == 404


def test_member_reads_but_cannot_edit(team):
    _, a, b, c, pid = team
    r = b.get(f"/projects/{pid}/kaisen")
    assert r.status_code == 200
    body = r.json()
    assert body["can_edit"] is False and body["has_anthropic_key"] is True
    assert b.put(f"/projects/{pid}/kaisen", json={"model": "claude-sonnet-5-5"}).status_code == 403
    assert a.get(f"/projects/{pid}/kaisen").json()["can_edit"] is True


def test_secrets_encrypted_at_rest_and_never_returned(team):
    _, a, b, c, pid = team
    r = a.put(f"/projects/{pid}/kaisen", json={
        "output_mode": "github", "github_repo": "https://github.com/octo/demo.git", "github_token": TOKEN})
    assert r.status_code == 200, r.text
    for resp in (r, a.get(f"/projects/{pid}/kaisen"), b.get(f"/projects/{pid}/kaisen")):
        assert KEY not in resp.text and TOKEN not in resp.text
        body = resp.json()
        assert body["anthropic_key"] == {"set": True, "last4": KEY[-4:]}
        assert body["github_token"] == {"set": True, "last4": TOKEN[-4:]}
        assert body["github_repo"] == "octo/demo"
    from app.kaisen.models import KaisenSettings
    row = db().get(KaisenSettings, pid)
    assert KEY not in row.anthropic_key_enc and crypto.decrypt(row.anthropic_key_enc) == KEY
    assert TOKEN not in row.github_token_enc and crypto.decrypt(row.github_token_enc) == TOKEN


def test_settings_update_is_audited_without_values(team):
    _, a, b, c, pid = team
    a.put(f"/projects/{pid}/kaisen", json={"anthropic_key": "fixture-other-key-9999", "cap_run": 1.5})
    rows = _audit("kaisen.settings_update")
    assert rows and "anthropic_key" in rows[-1].detail and "cap_run" in rows[-1].detail
    assert "fixture-other-key" not in " ".join(r.detail for r in rows)


def test_settings_validation(team):
    _, a, b, c, pid = team
    url = f"/projects/{pid}/kaisen"
    assert a.put(url, json={"model": "gpt-9"}).status_code == 422
    assert a.put(url, json={"effort": "extreme"}).status_code == 422
    assert a.put(url, json={"cap_run": -1}).status_code == 422
    assert a.put(url, json={"cap_day": config.KAISEN_CAP_MAX + 1}).status_code == 422
    assert a.put(url, json={"output_mode": "github"}).status_code == 422  # needs repo + token
    assert a.put(url, json={"github_repo": "not a repo"}).status_code == 422
    assert a.put(url, json={"surprise": 1}).status_code == 422


def test_clear_and_keep_secrets(team):
    _, a, b, c, pid = team
    assert a.put(f"/projects/{pid}/kaisen", json={"model": "claude-sonnet-5-5"}).json()["anthropic_key"]["set"]
    assert a.put(f"/projects/{pid}/kaisen", json={"clear_anthropic_key": True}).json()["has_anthropic_key"] is False


def test_test_buttons_work_for_members(team, fakes):
    _, a, b, c, pid = team
    assert b.post(f"/projects/{pid}/kaisen/test/anthropic").json()["ok"] is True
    fakes.anthropic.ok = False
    bad = b.post(f"/projects/{pid}/kaisen/test/anthropic").json()
    assert bad["ok"] is False and KEY not in bad["message"]
    assert b.post(f"/projects/{pid}/kaisen/test/github").json()["ok"] is False  # nothing saved yet
    a.put(f"/projects/{pid}/kaisen", json={"output_mode": "github", "github_repo": "octo/demo", "github_token": TOKEN})
    assert b.post(f"/projects/{pid}/kaisen/test/github").json()["ok"] is True
    assert b.post(f"/projects/{pid}/kaisen/test/nope").status_code == 404


# ---- builds: CRUD and scoping --------------------------------------------------------------------

def test_builds_require_login(client):
    assert client.get("/kaisen/builds").status_code == 401
    assert client.post("/kaisen/builds", json={"name": "x", "brief": "y"}).status_code == 401
    assert client.get("/kaisen/builds/1").status_code == 401
    assert client.get("/kaisen/bundles/x.tar.gz").status_code == 404  # public: unknown token, not 401


def test_create_build_happy_path_and_audit(team):
    _, a, b, c, pid = team
    r = b.post("/kaisen/builds", json={"project_id": pid, "name": "Todo list", "brief": "A tiny todo list"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "idle" and body["slug"] == "todo-list" and body["project_id"] == pid
    assert body["app"] is None and body["has_anthropic_key"] is True and body["can_run"] is True
    assert _audit("kaisen.build_create")[-1].target_id == str(body["id"])
    assert [x["id"] for x in a.get("/kaisen/builds", params={"project_id": pid}).json()] == [body["id"]]


def test_create_build_in_personal_project_by_default(make_client, fakes):
    u = _user(make_client, "solo@example.com")
    r = u.post("/kaisen/builds", json={"name": "Mine", "brief": "x"})
    assert r.status_code == 201 and r.json()["project_id"] == _pid(u)


def test_create_build_validation(team):
    _, a, b, c, pid = team
    assert a.post("/kaisen/builds", json={"project_id": pid, "name": "", "brief": "x"}).status_code == 422
    assert a.post("/kaisen/builds", json={"project_id": pid, "name": "x", "brief": " "}).status_code == 422
    assert a.post("/kaisen/builds", json={"project_id": pid, "name": "x" * 200, "brief": "x"}).status_code == 422
    assert a.post("/kaisen/builds", json={"project_id": pid, "name": "x", "brief": "x" * (config.KAISEN_BRIEF_MAX + 1)}).status_code == 422
    assert a.post("/kaisen/builds", json={"project_id": pid, "name": "x", "brief": "x", "extra": 1}).status_code == 422


def test_create_build_in_foreign_project_is_404(team):
    _, a, b, c, pid = team
    assert c.post("/kaisen/builds", json={"project_id": pid, "name": "x", "brief": "x"}).status_code == 404
    assert c.get("/kaisen/builds", params={"project_id": pid}).status_code == 404
    assert c.get("/kaisen/spend", params={"project_id": pid}).status_code == 404


def test_build_cap_per_project(team, monkeypatch):
    _, a, b, c, pid = team
    monkeypatch.setattr(config, "KAISEN_MAX_BUILDS_PER_PROJECT", 1)
    _new_build(a, pid)
    r = a.post("/kaisen/builds", json={"project_id": pid, "name": "two", "brief": "x"})
    assert r.status_code == 403 and "most allowed" in r.json()["detail"]


def test_other_user_cannot_reach_a_build(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    for resp in (c.get(f"/kaisen/builds/{bid}"), c.delete(f"/kaisen/builds/{bid}"), c.post(f"/kaisen/builds/{bid}/run"),
                 c.post(f"/kaisen/builds/{bid}/feedback", json={"text": "x"}), c.post(f"/kaisen/builds/{bid}/deploy"),
                 c.post(f"/kaisen/builds/{bid}/push"), c.get(f"/kaisen/builds/{bid}/download"),
                 c.get(f"/kaisen/builds/{bid}/files/kaisen.json"),
                 c.post(f"/kaisen/builds/{bid}/inbox/1/answer", json={"text": "x"})):
        assert resp.status_code == 404, resp.request.url
    assert c.get("/kaisen/builds").json() == []


def test_member_reads_files_and_downloads_tarball(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    f = b.get(f"/kaisen/builds/{bid}/files/public/index.html")
    assert f.status_code == 200 and f.json()["content"] == "<h1>hi</h1>"
    assert b.get(f"/kaisen/builds/{bid}/files/nope.txt").status_code == 404
    assert b.get(f"/kaisen/builds/{bid}/files/../x").status_code == 404
    d = b.get(f"/kaisen/builds/{bid}/download")
    assert d.status_code == 200
    names = tarfile.open(fileobj=io.BytesIO(d.content), mode="r:gz").getnames()
    assert "todo-list/public/index.html" in names


def test_run_needs_a_key_and_queues(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    r = b.post(f"/kaisen/builds/{bid}/run")
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert b.post(f"/kaisen/builds/{bid}/run").status_code == 409  # already in progress
    assert _audit("kaisen.run")
    a.put(f"/projects/{pid}/kaisen", json={"clear_anthropic_key": True})
    other = _new_build(a, pid, "Other")
    assert a.post(f"/kaisen/builds/{other}/run").status_code == 422


def test_feedback_needs_a_prior_run_and_carries_context(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    assert a.post(f"/kaisen/builds/{bid}/feedback", json={"text": "bigger"}).status_code == 409
    from app.kaisen.models import KaisenRun
    s = db()
    s.add(KaisenRun(build_id=bid, kind="build", status="done", summary="Built a todo list", messages=[{"role": "user", "content": "x"}]))
    s.commit()
    assert a.post(f"/kaisen/builds/{bid}/feedback", json={"text": ""}).status_code == 422
    r = a.post(f"/kaisen/builds/{bid}/feedback", json={"text": "make the title bigger"})
    assert r.status_code == 200 and r.json()["runs"][0]["kind"] == "feedback"
    run = s.query(KaisenRun).filter_by(build_id=bid, kind="feedback").one()
    first = run.messages[0]["content"]
    assert "make the title bigger" in first and "Built a todo list" in first and "public/index.html" in first


def test_read_scope_key_cannot_mutate(make_client, fakes):
    u = _user(make_client, "keys@example.com")
    pid = _pid(u)
    u.put(f"/projects/{pid}/kaisen", json={"anthropic_key": KEY})
    bid = _new_build(u, pid)
    key = u.post("/api/keys", json={"scope": "read"}).json()
    ro = make_client()
    ro.headers["Authorization"] = f"Bearer {key['key']}"
    assert ro.get(f"/kaisen/builds/{bid}").json()["can_run"] is False
    assert ro.post(f"/kaisen/builds/{bid}/run").status_code == 403


# ---- deploy into the build's project ---------------------------------------------------------------

def _deploy(c, bid, registry=None):
    return c.post(f"/kaisen/builds/{bid}/deploy")


def test_deploy_creates_app_in_build_project_as_creator(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    r = _deploy(b, bid)  # a member presses Deploy; the app is still created as the build's creator
    assert r.status_code == 200, r.text
    app_id = r.json()["app"]["id"]
    out = a.get(f"/apps/{app_id}").json()
    assert out["project_id"] == pid and out["kaisen_build_id"] == bid
    assert out["image"] == IMG and out["port"] == 8080 and out["name"] == "Todo list"
    assert out["health_check_enabled"] is False and out["want_db"] is False
    env = {e["key"]: e["value"] for e in out["env"]}
    assert env["KAISEN_BUNDLE_URL"].startswith(f"{config.platform_public_url()}/kaisen/bundles/")
    assert len(env["KAISEN_BUNDLE_SHA256"]) == 64 and "DB_HOST" not in env
    s = db()
    app = s.get(models.App, app_id)
    assert app.owner_id == s.query(models.User).filter_by(email="a@example.com").one().id
    assert [x["id"] for x in a.get("/apps", params={"project_id": pid}).json()] == [app_id]
    assert _audit("kaisen.deploy")[-1].detail.startswith(f"app={app_id}")
    # deploying again updates the same app
    again = _deploy(a, bid)
    assert again.status_code == 200 and again.json()["app"]["id"] == app_id
    assert len(a.get("/apps", params={"project_id": pid}).json()) == 1


def test_deploy_with_mysql_database(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid, files={"kaisen.json": '{"runtime": "python", "database": "mysql"}', "app.py": "print(1)"})
    app_id = _deploy(a, bid).json()["app"]["id"]
    out = a.get(f"/apps/{app_id}").json()
    assert out["want_db"] is True and out["db_engine"] == "mysql"
    env = {e["key"]: e["value"] for e in out["env"]}
    for k in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_CA_CERT", "DATABASE_URL"):
        assert env[k].startswith("${{ db.")


def test_deploy_rejects_unknown_database(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid, files={"kaisen.json": '{"database": "oracle"}'})
    r = _deploy(a, bid)
    assert r.status_code == 422 and "must be one of mysql" in r.json()["detail"]


def test_deploy_refused_without_runtime_image(team, monkeypatch):
    _, a, b, c, pid = team
    monkeypatch.setattr(config, "KAISEN_RUNTIME_IMAGE", "")
    r = _deploy(a, _new_build(a, pid))
    assert r.status_code == 409 and "runtime image" in r.json()["detail"]
    assert db().query(models.App).count() == 0


def test_deploy_surfaces_app_limit_as_plain_message(team):
    admin, a, b, c, pid = team
    assert admin.patch(f"/admin/projects/{pid}", json={"app_limit": 1}).status_code == 200
    assert a.post("/apps", json={"name": "taken", "image": "nginx:1", "port": 8080, "project_id": pid}).status_code == 201
    r = _deploy(a, _new_build(a, pid))
    assert r.status_code == 403 and "reached its limit of 1 app" in r.json()["detail"]


def test_deploy_name_clash_does_not_touch_other_app(team):
    _, a, b, c, pid = team
    a.post("/apps", json={"name": "Todo list", "image": "nginx:1", "port": 8080, "project_id": pid})
    r = _deploy(a, _new_build(a, pid))
    assert r.status_code == 409 and "already has an app named" in r.json()["detail"]
    assert db().query(models.App).filter(models.App.kaisen_build_id.is_not(None)).count() == 0


def test_deleting_the_app_unlinks_the_build(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    app_id = _deploy(a, bid).json()["app"]["id"]
    assert a.delete(f"/apps/{app_id}").status_code == 204
    detail = a.get(f"/kaisen/builds/{bid}").json()
    assert detail["app"] is None and detail["app_id"] is None


def test_moved_app_is_no_longer_the_builds_app(team):
    """An app moved to another project must not stay reachable (view/delete/overwrite) through the old project's build."""
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    app_id = _deploy(a, bid).json()["app"]["id"]
    other = a.post("/projects", json={"name": "Elsewhere"}).json()["id"]
    assert a.post(f"/apps/{app_id}/move", json={"project_id": other}).status_code == 200
    assert b.get(f"/kaisen/builds/{bid}").json()["app"] is None
    assert b.delete(f"/kaisen/builds/{bid}", params={"delete_app": True}).status_code == 204
    s = db()
    s.expire_all()
    assert s.get(models.App, app_id).deleted_at is None  # the member of the old project cannot delete it


def test_deploy_of_a_deleted_build_is_refused(team):
    from app.kaisen import deploy as deploy_mod
    from app.kaisen.models import KaisenBuild
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    s = db()
    build = s.get(KaisenBuild, bid)
    from datetime import datetime
    build.deleted_at = datetime.utcnow()
    s.commit()
    with pytest.raises(deploy_mod.DeployError):
        deploy_mod.deploy_build(s, build, FakeRegistry(default_size_bytes=1024))


def test_repo_and_branch_dot_segments_are_rejected(team):
    _, a, b, c, pid = team
    for body in ({"github_repo": "owner/.."}, {"github_branch": "../../user"}, {"github_branch": "/x"}, {"github_branch": "a//b"}):
        assert a.put(f"/projects/{pid}/kaisen", json=body).status_code == 422, body
    assert a.put(f"/projects/{pid}/kaisen", json={"github_repo": "owner/repo", "github_branch": "feat/x-1"}).status_code == 200


@pytest.mark.parametrize("delete_app", [False, True])
def test_delete_build_optionally_deletes_app(team, delete_app):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    app_id = _deploy(a, bid).json()["app"]["id"]
    assert a.delete(f"/kaisen/builds/{bid}", params={"delete_app": delete_app}).status_code == 204
    s = db()
    s.expire_all()
    app = s.get(models.App, app_id)
    assert (app.deleted_at is not None) is delete_app and app.kaisen_build_id is None
    assert a.get(f"/kaisen/builds/{bid}").status_code == 404
    assert a.get("/kaisen/builds", params={"project_id": pid}).json() == []
    from app.kaisen.models import KaisenBundle, KaisenFile
    assert s.query(KaisenFile).filter_by(build_id=bid).count() == 0 and s.query(KaisenBundle).filter_by(build_id=bid).count() == 0
    assert _audit("kaisen.build_delete")[-1].detail == f"delete_app={delete_app}"


# ---- bundles ---------------------------------------------------------------------------------------

def _bundle_url(c, app_id):
    env = {e["key"]: e["value"] for e in c.get(f"/apps/{app_id}").json()["env"]}
    return env["KAISEN_BUNDLE_URL"].removeprefix(config.platform_public_url()), env["KAISEN_BUNDLE_SHA256"]


def test_bundle_is_public_with_valid_token_only(team, client):
    _, a, b, c, pid = team
    app_id = _deploy(a, _new_build(a, pid)).json()["app"]["id"]
    path, sha = _bundle_url(a, app_id)
    r = client.get(path)  # anonymous client: the preview pod has no login
    assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == sha
    assert r.headers["x-bundle-sha256"] == sha and r.headers["cache-control"] == "no-store"
    assert client.get("/kaisen/bundles/" + "A" * 43 + ".tar.gz").status_code == 404
    assert client.get(path.replace("/bundles/", "/bundles/x")).status_code == 404


def test_bundle_token_stored_only_as_hash_and_last_three_kept(team, client):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    app_id = _deploy(a, bid).json()["app"]["id"]
    first, _ = _bundle_url(a, app_id)
    token = first.split("/")[-1].removesuffix(".tar.gz")
    from app.kaisen.models import KaisenBundle
    s = db()
    assert all(token not in r.token_hash for r in s.query(KaisenBundle).all())
    for _i in range(config.KAISEN_BUNDLES_KEPT):
        _deploy(a, bid)
    assert s.query(KaisenBundle).filter_by(build_id=bid).count() == config.KAISEN_BUNDLES_KEPT
    assert client.get(first).status_code == 404  # rotated out
    latest, _ = _bundle_url(a, app_id)
    assert client.get(latest).status_code == 200


# ---- push ------------------------------------------------------------------------------------------

def test_push_to_github(team, fakes):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    assert b.post(f"/kaisen/builds/{bid}/push").status_code == 422  # not set up
    a.put(f"/projects/{pid}/kaisen", json={"output_mode": "github", "github_repo": "octo/demo", "github_token": TOKEN})
    r = b.post(f"/kaisen/builds/{bid}/push")
    assert r.status_code == 200, r.text
    push = fakes.github.pushes[-1]
    assert push["repo"] == "octo/demo" and push["branch"] == "kaisen/todo-list" and "public/index.html" in push["files"]
    assert "Pushed to https://github.com/octo/demo/tree/kaisen/todo-list" in r.json()["inbox"][0]["text"]
    assert _audit("kaisen.push")[-1].detail == "repo=octo/demo"
    fakes.github.can_push = False
    bad = b.post(f"/kaisen/builds/{bid}/push")
    assert bad.status_code == 502 and TOKEN not in bad.text


# ---- spend -----------------------------------------------------------------------------------------

def test_spend_endpoint(team):
    _, a, b, c, pid = team
    assert b.get("/kaisen/spend", params={"project_id": pid}).json() == {
        "project_id": pid, "month_usd": 0.0, "today_usd": 0.0, "cap_day": config.KAISEN_DEFAULT_CAP_DAY}
    assert b.get("/kaisen/spend").status_code == 422


# ---- feature flag ----------------------------------------------------------------------------------

def test_feature_flag_off_via_admin_setting(team):
    admin, a, b, c, pid = team
    bid = _new_build(a, pid)
    assert a.get("/auth/me").json()["features"]["kaisen"] is True
    assert admin.put("/admin/settings", json={"settings": {"kaisen_enabled": False}}).status_code == 200
    assert a.get("/auth/me").json()["features"]["kaisen"] is False
    for resp in (a.get("/kaisen/builds"), a.get(f"/kaisen/builds/{bid}"), a.get(f"/projects/{pid}/kaisen"),
                 a.post("/kaisen/builds", json={"name": "x", "brief": "y"}), a.get("/kaisen/spend", params={"project_id": pid})):
        assert resp.status_code == 404
    assert _bundle_404(a)


def _bundle_404(c):
    return c.get("/kaisen/bundles/" + "A" * 43 + ".tar.gz").status_code == 404


def test_feature_flag_off_via_env(team, monkeypatch):
    _, a, b, c, pid = team
    monkeypatch.setattr(config, "KAISEN_ENABLED", False)
    assert a.get("/auth/me").json()["features"]["kaisen"] is False
    assert a.get("/kaisen/builds").status_code == 404 and a.get(f"/projects/{pid}/kaisen").status_code == 404
