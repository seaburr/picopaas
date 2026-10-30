"""Projects BE-3/BE-4: CRUD, members, invites, limits, move, spec/CLI, admin override. Real register/login flow."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import config, models
from app.db import get_db
from app.main import app as fastapi_app
from cli import picopaas
from tests.conftest import register_and_login

_APP = {"name": "demo", "image": "nginx:1", "port": 8080}
PW = "not-the-password-1"


def _db():
    return next(fastapi_app.dependency_overrides[get_db]())


def _user(make_client, email):
    c = make_client()
    register_and_login(c, email)
    return c


def _uid(email):
    return _db().query(models.User).filter_by(email=email).one().id


def _mk(c, name="Team"):
    r = c.post("/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _join(owner, joiner, pid, role="member", email=None):
    inv = owner.post(f"/projects/{pid}/invites", json={"role": role, **({"email": email} if email else {})})
    assert inv.status_code == 201, inv.text
    r = joiner.post(f"/invites/{inv.json()['token']}/accept")
    assert r.status_code == 200, r.text
    return r.json()


def _audit(action):
    return [a for a in _db().query(models.AuditLog).filter_by(action=action).all()]


@pytest.fixture()
def team(make_client):
    """admin(first user), owner a, member b, outsider c, shared project."""
    admin = _user(make_client, "admin@example.com")
    a, b, c = (_user(make_client, f"{n}@example.com") for n in "abc")
    proj = _mk(a)
    _join(a, b, proj["id"])
    return admin, a, b, c, proj


# --- CRUD / authn / authz -------------------------------------------------------------------

def test_create_and_list_projects(make_client):
    a = _user(make_client, "a@example.com")
    p = _mk(a, "My Team")
    assert p["role"] == "owner" and not p["is_personal"] and p["slug"].startswith("my-team-")
    assert p["app_limit_effective"] == config.PROJECT_DEFAULT_APP_LIMIT
    names = [x["name"] for x in a.get("/projects").json()]
    assert names == ["Personal", "My Team"]
    assert _audit("project.create")[0].detail.startswith(f"project={p['id']}")


def test_projects_anonymous_401(client):
    for m, u in (("get", "/projects"), ("post", "/projects"), ("get", "/projects/1"), ("get", "/projects/1/members"),
                 ("post", "/projects/1/invites"), ("post", "/invites/x/accept"), ("post", "/apps/1/move")):
        r = getattr(client, m)(u, **({"json": {}} if m == "post" else {}))
        assert r.status_code == 401, (m, u)


def test_validation_errors(make_client):
    a = _user(make_client, "a@example.com")
    assert a.post("/projects", json={"name": "  "}).status_code == 422
    assert a.post("/projects", json={"name": "x", "bogus": 1}).status_code == 422
    p = _mk(a)
    assert a.post(f"/projects/{p['id']}/invites", json={"email": "not-an-email"}).status_code == 422
    assert a.post(f"/projects/{p['id']}/invites", json={"role": "god"}).status_code == 422


def test_non_member_gets_404_everywhere(team):
    _, a, b, c, p = team
    pid = p["id"]
    for r in (c.get(f"/projects/{pid}"), c.patch(f"/projects/{pid}", json={"name": "x"}), c.delete(f"/projects/{pid}"),
              c.get(f"/projects/{pid}/members"), c.get(f"/projects/{pid}/invites"),
              c.post(f"/projects/{pid}/invites", json={}), c.post(f"/projects/{pid}/leave"),
              c.delete(f"/projects/{pid}/members/{_uid('a@example.com')}"),
              c.patch(f"/projects/{pid}/members/{_uid('a@example.com')}", json={"role": "member"}),
              c.get(f"/apps?project_id={pid}"), c.post("/apps", json={**_APP, "project_id": pid})):
        assert r.status_code == 404, r.request.url
    assert c.get("/projects").json()[0]["is_personal"] and len(c.get("/projects").json()) == 1


def test_member_cannot_do_owner_things(team):
    _, a, b, c, p = team
    pid = p["id"]
    assert b.get(f"/projects/{pid}").json()["role"] == "member"
    assert b.get(f"/projects/{pid}/members").status_code == 200
    for r in (b.patch(f"/projects/{pid}", json={"name": "x"}), b.delete(f"/projects/{pid}"),
              b.get(f"/projects/{pid}/invites"), b.post(f"/projects/{pid}/invites", json={}),
              b.patch(f"/projects/{pid}/members/{_uid('b@example.com')}", json={"role": "owner"}),
              b.delete(f"/projects/{pid}/members/{_uid('a@example.com')}")):
        assert r.status_code == 403, r.request.url
    assert models  # members still intact
    assert len(a.get(f"/projects/{pid}/members").json()) == 2


def test_rename_and_delete_project(team):
    _, a, b, c, p = team
    assert a.patch(f"/projects/{p['id']}", json={"name": "Renamed"}).json()["name"] == "Renamed"
    assert a.delete(f"/projects/{p['id']}").status_code == 204
    assert a.get(f"/projects/{p['id']}").status_code == 404
    assert b.get(f"/projects/{p['id']}").status_code == 404
    assert _db().get(models.Project, p["id"]).deleted_at is not None
    assert _audit("project.delete")[0].detail == f"project={p['id']}"


def test_delete_blocked_while_apps_exist(team):
    _, a, b, c, p = team
    app_id = b.post("/apps", json={**_APP, "project_id": p["id"]}).json()["id"]
    r = a.delete(f"/projects/{p['id']}")
    assert r.status_code == 409 and _db().get(models.Project, p["id"]).deleted_at is None
    assert a.delete(f"/apps/{app_id}").status_code in (200, 202, 204)
    assert a.delete(f"/projects/{p['id']}").status_code == 204


def test_personal_project_is_not_shareable(make_client):
    a = _user(make_client, "a@example.com")
    pid = a.get("/projects").json()[0]["id"]
    assert a.delete(f"/projects/{pid}").status_code == 409
    assert a.patch(f"/projects/{pid}", json={"name": "x"}).status_code == 409
    assert a.post(f"/projects/{pid}/invites", json={}).status_code == 409
    assert a.post(f"/projects/{pid}/leave").status_code == 409


def test_project_count_cap(make_client, monkeypatch):
    monkeypatch.setattr(config, "MAX_PROJECTS_PER_USER", 1)
    a = _user(make_client, "a@example.com")
    _mk(a)
    assert a.post("/projects", json={"name": "two"}).status_code == 403


# --- members ----------------------------------------------------------------------------------

def test_role_change_and_last_owner_rules(team):
    _, a, b, c, p = team
    pid, ua, ub = p["id"], _uid("a@example.com"), _uid("b@example.com")
    assert a.patch(f"/projects/{pid}/members/{ua}", json={"role": "member"}).status_code == 409  # last owner
    assert a.post(f"/projects/{pid}/leave").status_code == 409
    assert a.delete(f"/projects/{pid}/members/{ua}").status_code == 409
    assert a.patch(f"/projects/{pid}/members/{ub}", json={"role": "owner"}).json()["role"] == "owner"
    assert "owner" in _audit("project.role_change")[0].detail
    assert a.patch(f"/projects/{pid}/members/{ua}", json={"role": "member"}).status_code == 200  # now b owns too
    assert b.patch(f"/projects/{pid}/members/{ub}", json={"role": "member"}).status_code == 409  # b is last owner


def test_member_can_leave_owner_can_remove(team, make_client):
    _, a, b, c, p = team
    pid = p["id"]
    assert b.post(f"/projects/{pid}/leave").status_code == 204
    assert b.get(f"/projects/{pid}").status_code == 404
    _join(a, c, pid)
    assert a.delete(f"/projects/{pid}/members/{_uid('c@example.com')}").status_code == 204
    assert c.get(f"/projects/{pid}").status_code == 404
    acts = [x.action for x in _db().query(models.AuditLog).all()]
    assert acts.count("project.remove") == 2
    assert a.delete(f"/projects/{pid}/members/{_uid('c@example.com')}").status_code == 404


# --- invites ----------------------------------------------------------------------------------

def test_invite_token_hashed_and_single_use(team):
    _, a, b, c, p = team
    inv = a.post(f"/projects/{p['id']}/invites", json={}).json()
    row = _db().query(models.ProjectInvite).filter_by(id=inv["id"]).one()
    assert row.token_hash != inv["token"] and len(row.token_hash) == 64 and inv["path"] == f"/invite/{inv['token']}"
    assert abs((row.expires_at - row.created_at) - timedelta(days=config.PROJECT_INVITE_TTL_DAYS)) < timedelta(seconds=5)
    assert c.post(f"/invites/{inv['token']}/accept").status_code == 200
    assert c.get(f"/projects/{p['id']}").json()["role"] == "member"
    d = _user_after(inv)
    assert d.post(f"/invites/{inv['token']}/accept").status_code == 410  # reused
    assert _audit("project.join")[-1].detail.startswith(f"project={p['id']}")
    assert _audit("project.invite")


def _user_after(inv, _c=[0]):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    _c[0] += 1
    cl = TestClient(fastapi_app)
    register_and_login(cl, f"late{_c[0]}@example.com")
    return cl


def test_invite_expired_and_revoked(team):
    _, a, b, c, p = team
    inv = a.post(f"/projects/{p['id']}/invites", json={}).json()
    db = _db()
    row = db.query(models.ProjectInvite).filter_by(id=inv["id"]).one()
    row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    db.commit()
    assert c.post(f"/invites/{inv['token']}/accept").status_code == 410
    assert a.get(f"/projects/{p['id']}/invites").json() == []
    inv2 = a.post(f"/projects/{p['id']}/invites", json={}).json()
    assert a.delete(f"/projects/{p['id']}/invites/{inv2['id']}").status_code == 204
    assert c.post(f"/invites/{inv2['token']}/accept").status_code == 410
    assert c.get(f"/projects/{p['id']}").status_code == 404
    assert c.post("/invites/not-a-token/accept").status_code == 404


def test_email_bound_invite(team):
    _, a, b, c, p = team
    inv = a.post(f"/projects/{p['id']}/invites", json={"email": "C@Example.com", "role": "owner"}).json()
    assert inv["email"] == "c@example.com"
    assert a.get(f"/projects/{p['id']}/invites").json()[0]["id"] == inv["id"]
    assert "token" not in a.get(f"/projects/{p['id']}/invites").json()[0]
    wrong = _user_after(inv)
    assert wrong.post(f"/invites/{inv['token']}/accept").status_code == 403
    assert _db().query(models.ProjectInvite).filter_by(id=inv["id"]).one().accepted_at is None
    assert c.post(f"/invites/{inv['token']}/accept").json()["role"] == "owner"


def test_invite_for_existing_member_rejected(team):
    _, a, b, c, p = team
    assert a.post(f"/projects/{p['id']}/invites", json={"email": "b@example.com"}).status_code == 409
    inv = a.post(f"/projects/{p['id']}/invites", json={}).json()
    assert b.post(f"/invites/{inv['token']}/accept").status_code == 409


def test_pending_user_cannot_accept(team, make_client):
    _, a, b, c, p = team
    inv = a.post(f"/projects/{p['id']}/invites", json={}).json()
    pend = make_client()
    register_and_login(pend, "pending@example.com", approve=False)
    r = pend.post(f"/invites/{inv['token']}/accept")
    assert r.status_code == 403
    assert _db().query(models.ProjectMember).filter_by(user_id=_uid("pending@example.com")).count() == 1  # personal only
    assert _db().query(models.ProjectInvite).filter_by(id=inv["id"]).one().accepted_at is None


def test_api_key_cannot_manage_members(team):
    _, a, b, c, p = team
    key = a.post("/api/keys", json={"name": "k"}).json()["key"]
    k = httpx_client(key)
    pid = p["id"]
    assert k.get(f"/projects/{pid}/members").status_code == 200  # read is fine
    assert k.post(f"/projects/{pid}/invites", json={}).status_code == 403
    assert k.delete(f"/projects/{pid}/members/{_uid('b@example.com')}").status_code == 403
    assert k.patch(f"/projects/{pid}/members/{_uid('b@example.com')}", json={"role": "owner"}).status_code == 403
    assert k.post(f"/projects/{pid}/leave").status_code == 403
    inv = a.post(f"/projects/{pid}/invites", json={}).json()
    assert k.post(f"/invites/{inv['token']}/accept").status_code == 403


def httpx_client(key):
    from fastapi.testclient import TestClient
    cl = TestClient(fastapi_app)
    cl.headers["Authorization"] = f"Bearer {key}"
    return cl


def test_key_access_ends_when_membership_removed(team):
    _, a, b, c, p = team
    key = b.post("/api/keys", json={"name": "k"}).json()["key"]
    k = httpx_client(key)
    app_id = b.post("/apps", json={**_APP, "project_id": p["id"]}).json()["id"]
    assert k.get(f"/apps/{app_id}").status_code == 200
    assert k.post("/apps", json={**_APP, "name": "x2", "project_id": p["id"]}).status_code == 201
    assert a.delete(f"/projects/{p['id']}/members/{_uid('b@example.com')}").status_code == 204
    assert k.get(f"/apps/{app_id}").status_code == 404
    assert k.get("/apps").json() == []
    assert k.post("/apps", json={**_APP, "name": "x3", "project_id": p["id"]}).status_code == 404
    assert k.post(f"/apps/{app_id}/restart").status_code == 404


def test_accept_rate_limited(team, monkeypatch):
    _, a, b, c, p = team
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "INVITE_ACCEPT_MAX", 3)
    codes = [c.post("/invites/guess/accept").status_code for _ in range(5)]
    assert codes[:3] == [404] * 3 and codes[3:] == [429, 429]


def test_member_and_invite_caps(team, monkeypatch):
    _, a, b, c, p = team
    monkeypatch.setattr(config, "MAX_PROJECT_MEMBERS", 2)
    inv = a.post(f"/projects/{p['id']}/invites", json={}).json()
    assert c.post(f"/invites/{inv['token']}/accept").status_code == 403
    monkeypatch.setattr(config, "MAX_PENDING_INVITES_PER_PROJECT", 1)
    assert a.post(f"/projects/{p['id']}/invites", json={}).status_code == 403


# --- apps: access, limits, move -----------------------------------------------------------------

def test_cross_project_idor(team):
    _, a, b, c, p = team
    app_id = a.post("/apps", json={**_APP, "project_id": p["id"]}).json()["id"]
    personal_id = c.post("/apps", json=_APP).json()["id"]
    assert b.get(f"/apps/{app_id}").status_code == 200  # member
    assert c.get(f"/apps/{app_id}").status_code == 404
    assert b.get(f"/apps/{personal_id}").status_code == 404
    assert b.post(f"/apps/{personal_id}/move", json={"project_id": p["id"]}).status_code == 404
    assert b.get(f"/apps?project_id={p['id']}").json()[0]["id"] == app_id
    assert b.get(f"/apps?project_id={p['id']}").json()[0]["project_id"] == p["id"]
    assert [x["id"] for x in b.get("/apps").json()] == [app_id]


def test_project_app_limit_counts_per_project(team, monkeypatch):
    _, a, b, c, p = team
    monkeypatch.setattr(config, "PROJECT_DEFAULT_APP_LIMIT", 2)
    for i in range(2):
        assert b.post("/apps", json={**_APP, "name": f"n{i}", "project_id": p["id"]}).status_code == 201
    r = a.post("/apps", json={**_APP, "name": "n3", "project_id": p["id"]})
    assert r.status_code == 403 and "project" in r.json()["detail"]
    assert a.post("/apps", json={**_APP, "name": "mine"}).status_code == 201  # personal project has its own count


def test_personal_limit_keeps_user_semantics(make_client):
    admin = _user(make_client, "admin@example.com")
    a = _user(make_client, "a@example.com")
    db = _db()
    db.query(models.User).filter_by(email="a@example.com").one().app_limit = 1
    db.commit()
    p = _mk(a)
    assert a.post("/apps", json=_APP).status_code == 201
    assert a.post("/apps", json={**_APP, "name": "two"}).status_code == 403
    assert a.post("/apps", json={**_APP, "name": "team", "project_id": p["id"]}).status_code == 201


def test_admin_override_of_project_limit(team):
    admin, a, b, c, p = team
    pid = p["id"]
    assert a.patch(f"/admin/projects/{pid}", json={"app_limit": 4}).status_code == 403
    assert admin.patch(f"/admin/projects/{pid}", json={"app_limit": 1}).json()["app_limit_effective"] == 1
    assert a.post("/apps", json={**_APP, "project_id": pid}).status_code == 201
    assert a.post("/apps", json={**_APP, "name": "y", "project_id": pid}).status_code == 403
    assert admin.patch(f"/admin/projects/{pid}", json={"app_limit": 99}).status_code == 422
    assert admin.patch(f"/admin/projects/{pid}", json={"app_limit": -1}).status_code == 422
    assert admin.patch("/admin/projects/9999", json={"app_limit": 1}).status_code == 404
    assert admin.patch(f"/admin/projects/{pid}", json={"app_limit": None}).json()["app_limit"] is None
    assert [x["id"] for x in admin.get("/admin/projects").json()] == [pid]
    assert a.get("/admin/projects").status_code == 403
    assert any(x.detail and "app_limit=1" in x.detail for x in _audit("project.update"))


def test_move_app(team):
    _, a, b, c, p = team
    mine = a.post("/apps", json=_APP).json()["id"]
    assert b.post(f"/apps/{mine}/move", json={"project_id": p["id"]}).status_code == 404  # b isn't in a's personal project
    r = a.post(f"/apps/{mine}/move", json={"project_id": p["id"]})
    assert r.status_code == 200 and r.json()["project_id"] == p["id"]
    assert _db().get(models.App, mine).project_id == p["id"]
    assert b.post(f"/apps/{mine}/move", json={"project_id": b.get("/projects").json()[0]["id"]}).status_code == 403  # member, not owner
    assert a.post(f"/apps/{mine}/move", json={"project_id": p["id"]}).status_code == 409
    assert a.post(f"/apps/{mine}/move", json={"project_id": 99999}).status_code == 404
    assert a.post(f"/apps/{mine}/move", json={"project_id": 1, "x": 1}).status_code == 422
    assert _audit("app.move")


def test_move_checks_target_limit_and_owner_of_target(team, monkeypatch):
    _, a, b, c, p = team
    monkeypatch.setattr(config, "PROJECT_DEFAULT_APP_LIMIT", 1)
    a.post("/apps", json={**_APP, "name": "t", "project_id": p["id"]})
    mine = a.post("/apps", json=_APP).json()["id"]
    assert a.post(f"/apps/{mine}/move", json={"project_id": p["id"]}).status_code == 403
    cp = _mk(c, "other")
    assert a.post(f"/apps/{mine}/move", json={"project_id": cp["id"]}).status_code == 404  # not a member of target


# --- spec / CLI -----------------------------------------------------------------------------------

def _spec(name="demo", project=None):
    extra = f"project: {project}\n" if project else ""
    return f"apiVersion: picopaas/v1\nname: {name}\nimage: nginx:1\nport: 8080\n{extra}"


def test_spec_project_key(team):
    _, a, b, c, p = team
    r = b.post("/apps/spec", json={"spec": _spec(project=p["slug"]), "secrets": {}})
    assert r.status_code == 200 and r.json()["created"] and r.json()["project_id"] == p["id"]
    assert a.post("/apps/spec", json={"spec": _spec(project=p["slug"]), "secrets": {}}).json()["created"] is False
    personal = a.post("/apps/spec", json={"spec": _spec(), "secrets": {}}).json()
    assert personal["created"] and personal["project_id"] != p["id"]
    assert c.post("/apps/spec", json={"spec": _spec(project=p["slug"]), "secrets": {}}).status_code == 404
    assert a.post("/apps/spec", json={"spec": _spec(project=p["slug"]), "project": "other", "secrets": {}}).status_code == 422
    assert a.post("/apps/spec", json={"spec": _spec("other-app"), "project": p["slug"], "secrets": {}}).json()["project_id"] == p["id"]
    assert a.post("/apps/spec", json={"spec": _spec("demo", "personal"), "secrets": {}}).status_code == 200


def test_spec_deploy_limit_per_project(team, monkeypatch):
    _, a, b, c, p = team
    monkeypatch.setattr(config, "PROJECT_DEFAULT_APP_LIMIT", 1)
    assert a.post("/apps/spec", json={"spec": _spec("one", p["slug"]), "secrets": {}}).status_code == 200
    assert a.post("/apps/spec", json={"spec": _spec("two", p["slug"]), "secrets": {}}).status_code == 403


def _cli(argv, routes, monkeypatch, capsys, env=None):
    seen = []

    def handler(req):
        seen.append(req)
        r = routes.get((req.method, req.url.path))
        return (r(req) if callable(r) else r) if r is not None else httpx.Response(404, json={"detail": "nope"})

    monkeypatch.setenv("PICOPAAS_TOKEN", "pp_sk_test")
    monkeypatch.setenv("PICOPAAS_URL", "https://pp.test")
    if env:
        monkeypatch.setenv("PICOPAAS_PROJECT", env)
    else:
        monkeypatch.delenv("PICOPAAS_PROJECT", raising=False)
    code = picopaas.main(argv, transport=httpx.MockTransport(handler))
    return code, capsys.readouterr(), seen


PROJ = [{"id": 1, "name": "Personal", "slug": "personal-1", "is_personal": True, "role": "owner", "member_count": 1,
         "app_count": 0, "app_limit_effective": 5},
        {"id": 7, "name": "Team", "slug": "team-ab12", "is_personal": False, "role": "member", "member_count": 2,
         "app_count": 1, "app_limit_effective": 3}]


def test_cli_projects_and_scoped_apps(monkeypatch, capsys):
    apps = [{"id": 5, "name": "demo", "hostname": "a.x.y", "status": "running"}]
    routes = {("GET", "/projects"): httpx.Response(200, json=PROJ), ("GET", "/apps"): httpx.Response(200, json=apps)}
    code, out, seen = _cli(["projects"], routes, monkeypatch, capsys)
    assert code == 0 and "team-ab12" in out.out and "1/3" in out.out
    code, out, seen = _cli(["apps", "--project", "team-ab12"], routes, monkeypatch, capsys)
    assert code == 0 and "demo" in out.out and seen[-1].url.params["project_id"] == "7"
    code, out, seen = _cli(["apps"], routes, monkeypatch, capsys, env="Team")
    assert code == 0 and seen[-1].url.params["project_id"] == "7"
    code, out, seen = _cli(["apps", "--project", "nope"], routes, monkeypatch, capsys)
    assert code == 1 and "No project nope" in out.err


def test_cli_apply_sends_project(monkeypatch, capsys, tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text(_spec())
    body = {}

    def deploy(req):
        import json
        body.update(json.loads(req.content))
        return httpx.Response(200, json={"created": True, "status": "pending", "name": "demo", "hostname": "a.x.y"})

    routes = {("GET", "/projects"): httpx.Response(200, json=PROJ), ("POST", "/apps/spec"): deploy}
    code, out, _ = _cli(["apply", "-f", str(f), "--project", "team-ab12"], routes, monkeypatch, capsys)
    assert code == 0 and body["project"] == "team-ab12"
    body.clear()
    code, out, _ = _cli(["apply", "-f", str(f)], routes, monkeypatch, capsys)
    assert code == 0 and "project" not in body


def test_cli_ambiguous_app_name(monkeypatch, capsys):
    apps = [{"id": 1, "name": "demo", "hostname": "a", "status": "running"}, {"id": 2, "name": "demo", "hostname": "b", "status": "running"}]
    code, out, _ = _cli(["status", "demo"], {("GET", "/apps"): httpx.Response(200, json=apps)}, monkeypatch, capsys)
    assert code == 1 and "--project" in out.err


# --- admin: users, notifications ---------------------------------------------------------------------

def test_suspend_user_does_not_stop_team_apps(team):
    admin, a, b, c, p = team
    team_app = b.post("/apps", json={**_APP, "project_id": p["id"]}).json()["id"]
    personal = b.post("/apps", json={**_APP, "name": "mine"}).json()["id"]
    assert admin.post(f"/admin/users/{_uid('b@example.com')}/suspend").status_code == 200
    db = _db()
    db.expire_all()
    assert db.get(models.App, team_app).status != "suspended"
    assert db.get(models.App, personal).status == "suspended"


def test_delete_user_blocked_if_sole_owner_with_apps(team):
    admin, a, b, c, p = team
    app_id = a.post("/apps", json={**_APP, "project_id": p["id"]}).json()["id"]
    r = admin.delete(f"/admin/users/{_uid('a@example.com')}")
    assert r.status_code == 409 and _db().query(models.User).filter_by(email="a@example.com").one().status == "active"
    _join(a, c, p["id"], role="owner", email="c@example.com")
    assert admin.delete(f"/admin/users/{_uid('a@example.com')}").status_code == 204
    db = _db()
    db.expire_all()
    assert db.get(models.App, app_id).deleted_at is None  # team app survives its creator
    assert c.get(f"/projects/{p['id']}/members").json()[0]["email"] in ("b@example.com", "c@example.com")


def test_delete_sole_owner_without_apps_removes_project(team):
    admin, a, b, c, p = team
    assert admin.delete(f"/admin/users/{_uid('a@example.com')}").status_code == 204
    db = _db()
    db.expire_all()
    assert db.get(models.Project, p["id"]).deleted_at is not None


def test_owner_notifications_target_project_owners(team):
    admin, a, b, c, p = team
    b.post("/apps", json={**_APP, "project_id": p["id"]})  # created by b (member)
    rows = admin.get("/admin/health").json() if False else None
    from app import projects
    emails = projects.owner_emails_map(_db(), [p["id"]])
    assert emails == {p["id"]: ["a@example.com"]}
    app_row = _db().query(models.App).filter_by(project_id=p["id"]).one()
    assert projects.owners_label(_db(), app_row, "x") == "a@example.com"


def test_link_invite_owner_role_rejected(team):
    _, a, b, c, p = team
    r = a.post(f"/projects/{p['id']}/invites", json={"role": "owner"})
    assert r.status_code == 422
    assert _db().query(models.ProjectInvite).filter_by(project_id=p["id"]).count() == 1  # only b's earlier invite
    ok = a.post(f"/projects/{p['id']}/invites", json={"role": "member"})
    assert ok.status_code == 201 and ok.json()["role"] == "member"
    bound = a.post(f"/projects/{p['id']}/invites", json={"role": "owner", "email": "c@example.com"})
    assert bound.status_code == 201 and bound.json()["role"] == "owner"


def test_last_owner_invariant_holds_across_paths(team):
    admin, a, b, c, p = team
    pid, ua = p["id"], _uid("a@example.com")
    assert a.patch(f"/projects/{pid}/members/{ua}", json={"role": "member"}).status_code == 409
    assert a.delete(f"/projects/{pid}/members/{ua}").status_code == 409
    assert a.post(f"/projects/{pid}/leave").status_code == 409
    owners = _db().query(models.ProjectMember).filter_by(project_id=pid, role="owner").count()
    assert owners == 1


def test_member_cap_enforced_on_accept(team, monkeypatch):
    _, a, b, c, p = team
    monkeypatch.setattr(config, "MAX_PROJECT_MEMBERS", 2)
    inv = a.post(f"/projects/{p['id']}/invites", json={}).json()
    assert c.post(f"/invites/{inv['token']}/accept").status_code == 403
    assert _db().query(models.ProjectMember).filter_by(project_id=p["id"]).count() == 2


def test_delete_user_locks_projects_in_ascending_id_order(team, monkeypatch):
    from app import projects as projects_mod
    admin, a, b, c, p = team
    # b joins three more projects; create them so b's membership order is not id order
    ids = [p["id"]]
    for n in range(2):
        ids.append(_join_new(c, b, f"P{n}"))
    calls = []
    real = projects_mod.lock_project
    monkeypatch.setattr(projects_mod, "lock_project", lambda db, pid: (calls.append(pid), real(db, pid))[1])
    assert admin.delete(f"/admin/users/{_uid('b@example.com')}").status_code == 204
    assert len(calls) == 3 and calls == sorted(calls) and set(calls) == set(ids)


def _join_new(owner, joiner, name):
    pid = _mk(owner, name)["id"]
    _join(owner, joiner, pid)
    return pid


# --- unique project names (F2) -------------------------------------------------------------

def test_create_project_duplicate_name_is_409_case_insensitive_trimmed(make_client):
    a = _user(make_client, "dupa@example.com")
    assert a.post("/projects", json={"name": "Alpha"}).status_code == 201
    r = a.post("/projects", json={"name": "  aLPHA "})
    assert r.status_code == 409 and r.json()["detail"] == "You already have a project named aLPHA."
    assert _db().query(models.Project).filter(models.Project.name.ilike("alpha")).count() == 1


def test_duplicate_of_personal_project_name_is_409(make_client):
    a = _user(make_client, "dupp@example.com")
    assert a.post("/projects", json={"name": "personal"}).status_code == 409


def test_other_users_names_do_not_collide_or_leak(make_client):
    a = _user(make_client, "dupb@example.com")
    b = _user(make_client, "dupc@example.com")
    assert a.post("/projects", json={"name": "Shared name"}).status_code == 201
    r = b.post("/projects", json={"name": "shared NAME"})
    assert r.status_code == 201


def test_deleted_project_name_can_be_reused(make_client):
    a = _user(make_client, "dupd@example.com")
    pid = a.post("/projects", json={"name": "Gone"}).json()["id"]
    assert a.delete(f"/projects/{pid}").status_code == 204
    assert a.post("/projects", json={"name": "gone"}).status_code == 201


def test_rename_to_existing_name_is_409_but_own_name_and_case_change_ok(make_client):
    a = _user(make_client, "dupe@example.com")
    p1 = a.post("/projects", json={"name": "One"}).json()["id"]
    a.post("/projects", json={"name": "Two"})
    r = a.patch(f"/projects/{p1}", json={"name": " two"})
    assert r.status_code == 409 and r.json()["detail"] == "You already have a project named two."
    assert a.patch(f"/projects/{p1}", json={"name": "ONE"}).status_code == 200
    assert _db().get(models.Project, p1).name == "ONE"


def test_project_name_validation_and_anonymous(make_client):
    a = _user(make_client, "dupf@example.com")
    assert a.post("/projects", json={"name": "   "}).status_code == 422
    assert make_client().post("/projects", json={"name": "X"}).status_code == 401
