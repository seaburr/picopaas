"""Project / user / app suspension (docs/design/suspensions.md): restore only what a suspension did, 403 writes with the
reason, Kaisen stops, the admin Suspensions list. Real register/login flow, infra faked behind the interfaces."""
import os
import sqlite3
import subprocess
import sys

import pytest

from app import config, models
from app.db import get_db
from app.kaisen import builder
from app.kaisen.llm import FakeModelClient, reply, tool_use
from app.kaisen.models import KaisenBuild, KaisenInbox, KaisenRun
from app.main import app as fastapi_app
from app.routers.kaisen import get_github_factory, get_model_factory
from tests.conftest import register_and_login
from tests.kaisen_helpers import KEY, Fakes, FakeRuntime
from tests.test_kaisen_worker import go, write

_APP = {"name": "demo", "image": "nginx:1", "port": 8080}
REASON = "Mining crypto on the shared cluster"


def _db():
    s = next(fastapi_app.dependency_overrides[get_db]())
    s.expire_all()
    return s


def _user(make_client, email):
    c = make_client()
    register_and_login(c, email)
    return c


def _uid(email):
    return _db().query(models.User).filter_by(email=email).one().id


def _join(owner, joiner, pid, role="member"):
    inv = owner.post(f"/projects/{pid}/invites", json={"role": role}).json()
    assert joiner.post(f"/invites/{inv['token']}/accept").status_code == 200


def _audit(action):
    return _db().query(models.AuditLog).filter_by(action=action).all()


def _app(c, pid, name="demo"):
    r = c.post("/apps", json={**_APP, "name": name, "project_id": pid})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _row(app_id):
    return _db().get(models.App, app_id)


def _personal(c):
    return next(p["id"] for p in c.get("/projects").json() if p["is_personal"])


@pytest.fixture()
def on_backups(monkeypatch):
    monkeypatch.setattr(config, "BACKUPS_ENABLED", True)
    monkeypatch.setattr(config, "BACKUP_BUCKET", "picopaas-backups")
    monkeypatch.setattr(config, "BACKUP_REGION", "sfo3")
    monkeypatch.setattr(config, "BACKUP_ACCESS_KEY_ID", "backup-key-id")
    monkeypatch.setattr(config, "BACKUP_SECRET_ACCESS_KEY", "not-the-backup-secret")


@pytest.fixture()
def team(make_client):
    """admin (first user = platform admin), owner a, member b, outsider c; a shared project 'Team' with two apps."""
    admin = _user(make_client, "admin@example.com")
    a, b, c = (_user(make_client, f"{n}@example.com") for n in "abc")
    pid = a.post("/projects", json={"name": "Team"}).json()["id"]
    _join(a, b, pid)
    apps = [_app(a, pid, "one"), _app(b, pid, "two")]
    return type("T", (), {"admin": admin, "a": a, "b": b, "c": c, "pid": pid, "apps": apps})


def _suspend(t, pid=None, reason=REASON):
    return t.admin.post(f"/admin/projects/{pid or t.pid}/suspend", json={"reason": reason})


# ---- project suspend / unsuspend ---------------------------------------------------------------------

def test_suspend_project_suspends_live_apps_and_audits(team):
    r = _suspend(team)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "suspended" and body["suspended_reason"] == REASON
    assert body["suspended_by_email"] == "admin@example.com" and body["app_count"] == 2
    p = _db().get(models.Project, team.pid)
    assert (p.status, p.suspended_reason, p.suspended_by_user) == ("suspended", REASON, False)
    assert p.suspended_by == _uid("admin@example.com") and p.suspended_at is not None
    for aid in team.apps:
        a = _row(aid)
        assert (a.status, a.suspended_via, a.suspended_reason) == ("suspended", "project", REASON)
    [log] = _audit("project.suspend")
    assert log.target_id == str(team.pid) and REASON in log.detail and "apps=2" in log.detail


def test_unsuspend_project_restores_its_apps_to_pending_and_audits(team):
    _suspend(team)
    r = team.admin.post(f"/admin/projects/{team.pid}/unsuspend")
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["suspended_reason"] is None
    p = _db().get(models.Project, team.pid)
    assert (p.status, p.suspended_reason, p.suspended_by, p.suspended_at) == ("active", None, None, None)
    for aid in team.apps:
        a = _row(aid)
        assert (a.status, a.suspended_via, a.suspended_reason) == ("pending", None, None)
    [log] = _audit("project.unsuspend")
    assert log.target_id == str(team.pid) and "apps=2" in log.detail


def test_project_unsuspend_keeps_an_app_admin_suspended_before(team):
    one, two = team.apps
    assert team.admin.post(f"/admin/apps/{one}/suspend", json={"reason": "runaway CPU"}).status_code == 200
    _suspend(team)
    a = _row(one)
    assert (a.suspended_via, a.suspended_reason) == ("admin", "runaway CPU")  # keeps its own reason
    assert _row(two).suspended_via == "project"
    team.admin.post(f"/admin/projects/{team.pid}/unsuspend")
    assert _row(one).status == "suspended" and _row(one).suspended_via == "admin"
    assert _row(two).status == "pending"
    assert team.admin.post(f"/admin/apps/{one}/unsuspend").status_code == 200
    assert _row(one).status == "pending" and _row(one).suspended_via is None


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * (config.SUSPEND_REASON_MAX + 1)},
                                  {"reason": "ok", "extra": 1}])
def test_suspend_project_validates_reason(team, body):
    r = team.admin.post(f"/admin/projects/{team.pid}/suspend", json=body)
    assert r.status_code == 422
    assert _db().get(models.Project, team.pid).status == "active" and _audit("project.suspend") == []


def test_reason_at_the_cap_is_accepted(team):
    assert _suspend(team, reason="x" * config.SUSPEND_REASON_MAX).status_code == 200


def test_project_suspension_endpoints_are_admin_only(team, make_client):
    for who in (team.a, team.b, team.c):  # even the project's own owner
        assert who.post(f"/admin/projects/{team.pid}/suspend", json={"reason": "x"}).status_code == 403
        assert who.post(f"/admin/projects/{team.pid}/unsuspend").status_code == 403
        assert who.get("/admin/suspensions").status_code == 403
    anon = make_client()
    assert anon.post(f"/admin/projects/{team.pid}/suspend", json={"reason": "x"}).status_code == 401
    assert anon.post(f"/admin/projects/{team.pid}/unsuspend").status_code == 401
    assert anon.get("/admin/suspensions").status_code == 401
    assert _db().get(models.Project, team.pid).status == "active"


def test_suspend_unknown_project_and_state_conflicts(team):
    assert team.admin.post("/admin/projects/9999/suspend", json={"reason": "x"}).status_code == 404
    assert team.admin.post("/admin/projects/9999/unsuspend").status_code == 404
    assert team.admin.post(f"/admin/projects/{team.pid}/unsuspend").status_code == 409  # not suspended
    _suspend(team)
    assert _suspend(team).status_code == 409  # already suspended


def test_admin_projects_list_carries_status_and_reason(team):
    row = next(p for p in team.admin.get("/admin/projects").json() if p["id"] == team.pid)
    assert row["status"] == "active" and row["suspended_reason"] is None
    _suspend(team)
    row = next(p for p in team.admin.get("/admin/projects").json() if p["id"] == team.pid)
    assert row["status"] == "suspended" and row["suspended_reason"] == REASON and row["suspended_at"]


# ---- user suspend / unsuspend through the personal project --------------------------------------------

def test_suspend_user_suspends_personal_project_and_leaves_team_projects(team):
    mine = _app(team.b, _personal(team.b), "mine")
    bid = _uid("b@example.com")
    r = team.admin.post(f"/admin/users/{bid}/suspend", json={"reason": "spam"})
    assert r.status_code == 200 and r.json()["status"] == "suspended" and r.json()["suspended_reason"] == "spam"
    u = _db().get(models.User, bid)
    assert (u.suspended_reason, u.suspended_by) == ("spam", _uid("admin@example.com"))
    personal = _db().query(models.Project).filter_by(slug=f"personal-{bid}").one()
    assert (personal.status, personal.suspended_by_user, personal.suspended_reason) == ("suspended", True, "spam")
    a = _row(mine)
    assert (a.status, a.suspended_via) == ("suspended", "user")
    assert all(_row(x).status != "suspended" for x in team.apps)  # the team project keeps running
    assert _audit("user.suspend")[0].detail == "reason=spam"


@pytest.mark.parametrize("body", [None, {}, {"reason": ""}, {"reason": "  "}])
def test_suspend_user_requires_a_reason(team, body):
    bid = _uid("b@example.com")
    r = team.admin.post(f"/admin/users/{bid}/suspend", **({} if body is None else {"json": body}))
    assert r.status_code == 422
    assert _db().get(models.User, bid).status == "active" and _audit("user.suspend") == []


def test_unsuspend_user_restores_personal_project_and_its_apps(team):
    mine = _app(team.b, _personal(team.b), "mine")
    bid = _uid("b@example.com")
    team.admin.post(f"/admin/users/{bid}/suspend", json={"reason": "spam"})
    r = team.admin.post(f"/admin/users/{bid}/unsuspend")
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["suspended_reason"] is None
    personal = _db().query(models.Project).filter_by(slug=f"personal-{bid}").one()
    assert (personal.status, personal.suspended_by_user, personal.suspended_reason) == ("active", False, None)
    assert (_row(mine).status, _row(mine).suspended_via) == ("pending", None)
    assert _audit("user.unsuspend")[0].target_id == str(bid)


def test_admin_suspended_personal_project_stays_suspended_after_user_suspend_and_unsuspend(team):
    bid = _uid("b@example.com")
    pp = _personal(team.b)
    mine = _app(team.b, pp, "mine")
    assert _suspend(team, pp, "abuse found").status_code == 200
    team.admin.post(f"/admin/users/{bid}/suspend", json={"reason": "also the account"})
    p = _db().get(models.Project, pp)
    assert (p.status, p.suspended_by_user, p.suspended_reason) == ("suspended", False, "abuse found")
    team.admin.post(f"/admin/users/{bid}/unsuspend")
    p = _db().get(models.Project, pp)
    assert (p.status, p.suspended_reason) == ("suspended", "abuse found")
    assert (_row(mine).status, _row(mine).suspended_via) == ("suspended", "project")
    assert team.admin.post(f"/admin/projects/{pp}/unsuspend").status_code == 200
    assert _row(mine).status == "pending"


def test_project_suspend_takes_over_a_user_suspended_personal_project(team):
    bid = _uid("b@example.com")
    pp = _personal(team.b)
    mine = _app(team.b, pp, "mine")
    team.admin.post(f"/admin/users/{bid}/suspend", json={"reason": "spam"})
    assert _suspend(team, pp, "keep it off").status_code == 200
    team.admin.post(f"/admin/users/{bid}/unsuspend")
    assert _db().get(models.Project, pp).status == "suspended"
    assert (_row(mine).status, _row(mine).suspended_via, _row(mine).suspended_reason) == ("suspended", "project", "keep it off")


def test_user_suspend_keeps_an_admin_suspended_app_suspended(team):
    bid = _uid("b@example.com")
    pp = _personal(team.b)
    d = _db()
    d.get(models.User, bid).app_limit = 3
    d.commit()
    keep, other = _app(team.b, pp, "keep"), _app(team.b, pp, "other")
    team.admin.post(f"/admin/apps/{keep}/suspend", json={"reason": "cpu"})
    team.admin.post(f"/admin/users/{bid}/suspend", json={"reason": "not-the-reason"})
    team.admin.post(f"/admin/users/{bid}/unsuspend")
    assert _row(keep).status == "suspended" and _row(keep).suspended_via == "admin"
    assert _row(other).status == "pending"


def test_main_administrator_cannot_be_suspended(team):
    r = team.admin.post(f"/admin/users/{_uid('admin@example.com')}/suspend", json={"reason": "x"})
    assert r.status_code in (400, 409)
    assert _db().query(models.Project).filter_by(status="suspended").count() == 0


def test_user_suspend_reason_too_long_is_422(team):
    r = team.admin.post(f"/admin/users/{_uid('b@example.com')}/suspend", json={"reason": "x" * (config.SUSPEND_REASON_MAX + 1)})
    assert r.status_code == 422 and _db().get(models.User, _uid("b@example.com")).status == "active"


# ---- writes 403, reads 200 ------------------------------------------------------------------------------

def test_suspended_project_is_read_only_for_every_member_with_the_reason(team):
    _suspend(team)
    one = team.apps[0]
    for who in (team.a, team.b):  # the owner and a plain member alike
        for r in (who.post("/apps", json={**_APP, "name": "new", "project_id": team.pid}),
                  who.delete(f"/apps/{one}"),
                  who.patch(f"/apps/{one}", json={"name": "renamed"}),
                  who.post(f"/projects/{team.pid}/invites", json={"role": "member"}) if who is team.a else
                  who.post(f"/apps/{one}/restart")):
            assert r.status_code == 403, r.text
            assert "suspended" in r.json()["detail"]
            assert (REASON in r.json()["detail"]) == (who is team.a)  # only owners are told why
    assert team.a.patch(f"/projects/{team.pid}", json={"name": "Renamed"}).status_code == 403
    assert team.a.delete(f"/projects/{team.pid}").status_code == 403
    p = _db().get(models.Project, team.pid)
    assert p.name == "Team" and p.deleted_at is None
    assert _db().query(models.App).filter_by(project_id=team.pid).count() == 2


def test_suspended_project_still_reads_and_shows_status_to_members(team):
    _suspend(team)
    for who in (team.a, team.b):
        d = who.get(f"/projects/{team.pid}")
        assert d.status_code == 200
        want = REASON if who is team.a else None  # the reason is for owners only
        assert (d.json()["status"], d.json()["suspended_reason"]) == ("suspended", want)
        row = next(p for p in who.get("/projects").json() if p["id"] == team.pid)
        assert (row["status"], row["suspended_reason"]) == ("suspended", want)
        assert who.get(f"/apps/{team.apps[0]}").status_code == 200
        assert {x["id"] for x in who.get(f"/apps?project_id={team.pid}").json()} == set(team.apps)
        assert who.get(f"/projects/{team.pid}/members").status_code == 200
    assert team.c.get(f"/projects/{team.pid}").status_code == 404  # non-members still learn nothing
    assert team.c.post("/apps", json={**_APP, "project_id": team.pid}).status_code == 404


def test_active_project_reports_active_status(team):
    d = team.a.get(f"/projects/{team.pid}").json()
    assert d["status"] == "active" and d["suspended_reason"] is None


def test_members_can_still_leave_a_suspended_project(team):
    _suspend(team)
    assert team.b.post(f"/projects/{team.pid}/leave").status_code == 204
    assert team.b.get(f"/projects/{team.pid}").status_code == 404


def test_invite_accept_into_a_suspended_project_is_refused(team):
    inv = team.a.post(f"/projects/{team.pid}/invites", json={"role": "member"}).json()
    _suspend(team)
    r = team.c.post(f"/invites/{inv['token']}/accept")
    assert r.status_code == 403 and "suspended" in r.json()["detail"] and REASON not in r.json()["detail"]


def test_spec_deploy_into_a_suspended_project_is_refused(team):
    slug = _db().get(models.Project, team.pid).slug
    _suspend(team)
    spec = "apiVersion: picopaas/v1\nname: fresh\nimage: nginx:1\nport: 8080\n"
    r = team.a.post("/apps/spec", json={"spec": spec, "project": slug})
    assert r.status_code == 403 and REASON in r.json()["detail"]
    assert _db().query(models.App).filter_by(name="fresh").count() == 0


def test_platform_admin_can_still_act_through_admin_routes(team):
    _suspend(team)
    one = team.apps[0]
    assert team.admin.patch(f"/admin/projects/{team.pid}", json={"app_limit": 2}).status_code == 200
    assert team.admin.get("/admin/apps").status_code == 200
    assert team.admin.post(f"/admin/apps/{one}/unsuspend").status_code == 409  # the project still holds it (K46)
    assert _row(one).status == "suspended"
    assert team.admin.post(f"/admin/apps/{one}/suspend", json={"reason": "again"}).status_code == 200
    assert team.admin.delete(f"/admin/apps/{team.apps[1]}").status_code in (200, 204)


def test_app_suspended_on_its_own_cannot_restart_inside_a_suspended_project(team):
    # live e2e 2026-10-31: app suspended first (via=admin), then the project; the app unsuspend used to return 200
    one = team.apps[0]
    assert team.admin.post(f"/admin/apps/{one}/suspend", json={"reason": "noisy"}).status_code == 200
    _suspend(team)
    r = team.admin.post(f"/admin/apps/{one}/unsuspend")
    assert r.status_code == 409 and _row(one).status == "suspended"
    assert team.admin.post(f"/admin/projects/{team.pid}/unsuspend").status_code == 200
    assert _row(one).status == "suspended"  # its own suspension still stands
    assert team.admin.post(f"/admin/apps/{one}/unsuspend").status_code == 200
    assert _row(one).status == "pending"


def test_unsuspended_project_accepts_writes_again(team):
    _suspend(team)
    team.admin.post(f"/admin/projects/{team.pid}/unsuspend")
    assert team.a.patch(f"/projects/{team.pid}", json={"name": "Renamed"}).status_code == 200
    assert team.b.post("/apps", json={**_APP, "name": "later", "project_id": team.pid}).status_code == 201


def test_suspended_users_personal_project_blocks_default_app_create_via_api_key(team):
    """A personal project suspended by an admin (the user can still log in): creating an app without a project_id
    goes to the personal project and is refused with the reason."""
    pp = _personal(team.c)
    _suspend(team, pp, "abuse found")
    r = team.c.post("/apps", json=_APP)
    assert r.status_code == 403 and "abuse found" in r.json()["detail"]


# ---- the Suspensions list ----------------------------------------------------------------------------------

def test_suspensions_list_has_users_projects_and_apps(team):
    bid = _uid("b@example.com")
    mine = _app(team.b, _personal(team.b), "mine")
    team.admin.post(f"/admin/users/{bid}/suspend", json={"reason": "spam"})
    _suspend(team)
    solo = _app(team.c, _personal(team.c), "solo")
    team.admin.post(f"/admin/apps/{solo}/suspend", json={"reason": "cpu"})
    rows = team.admin.get("/admin/suspensions").json()
    by = {(r["type"], r["id"]): r for r in rows}
    u = by[("user", bid)]
    assert (u["name"], u["reason"], u["suspended_by_email"], u["app_count"]) == ("b@example.com", "spam", "admin@example.com", 1)
    assert u["suspended_at"]
    p = by[("project", team.pid)]
    assert (p["name"], p["reason"], p["app_count"], p["owner_email"]) == ("Team", REASON, 2, "a@example.com")
    assert p["suspended_by_email"] == "admin@example.com" and p["suspended_at"]
    assert by[("app", mine)]["via"] == "user" and by[("app", mine)]["reason"] == "spam"
    assert (by[("app", mine)]["cause_type"], by[("app", mine)]["cause_id"]) == ("user", bid)
    assert by[("app", team.apps[0])]["via"] == "project" and by[("app", team.apps[0])]["project_name"] == "Team"
    assert (by[("app", team.apps[0])]["cause_type"], by[("app", team.apps[0])]["cause_id"]) == ("project", team.pid)
    assert u["suspended_by_id"] == _uid("admin@example.com") == p["suspended_by_id"]
    s = by[("app", solo)]
    assert (s["via"], s["reason"], s["owner_email"]) == ("admin", "cpu", "c@example.com")
    assert s["cause_type"] is None and s["cause_id"] is None
    assert ("project", _db().query(models.Project).filter_by(slug=f"personal-{bid}").one().id) not in by  # shown as the user


def test_suspensions_list_is_empty_when_nothing_is_suspended(team):
    assert team.admin.get("/admin/suspensions").json() == []


# ---- audit ---------------------------------------------------------------------------------------------------

def test_every_state_change_is_audited_with_the_actor(team):
    _suspend(team)
    team.admin.post(f"/admin/projects/{team.pid}/unsuspend")
    for action in ("project.suspend", "project.unsuspend"):
        [log] = _audit(action)
        assert log.actor_email == "admin@example.com"


# ---- Kaisen ---------------------------------------------------------------------------------------------------

@pytest.fixture()
def k(team, monkeypatch):
    f = Fakes()
    fastapi_app.dependency_overrides[get_github_factory] = lambda: (lambda token: f.github)
    fastapi_app.dependency_overrides[get_model_factory] = lambda: (lambda key: f.anthropic)
    monkeypatch.setattr(config, "KAISEN_RUNTIME_IMAGE", "ghcr.io/example/kaisen-runtime:1")
    bid = team.a.post("/kaisen/builds", json={"project_id": team.pid, "name": "Todo list", "brief": "A tiny todo list"}).json()["id"]
    assert team.a.put(f"/projects/{team.pid}/kaisen", json={"anthropic_key": KEY}).status_code == 200
    team.bid = bid
    return team


def _run(c, bid):
    r = c.post(f"/kaisen/builds/{bid}/run")
    assert r.status_code == 200, r.text


def _last_run(bid):
    return _db().query(KaisenRun).filter_by(build_id=bid).order_by(KaisenRun.id.desc()).first()


def _stopped_with(bid, text):
    run = _last_run(bid)
    assert run.status == "stopped" and run.error == text
    assert any(i.text == text for i in _db().query(KaisenInbox).filter_by(build_id=bid))


def test_api_refuses_kaisen_changes_in_a_suspended_project_but_reads_work(k):
    _suspend(k)
    bid = k.bid
    refused = [
        k.a.post("/kaisen/builds", json={"project_id": k.pid, "name": "More", "brief": "b"}),
        k.a.post(f"/kaisen/builds/{bid}/run"),
        k.a.post(f"/kaisen/builds/{bid}/feedback", json={"text": "make it blue"}),
        k.a.post(f"/kaisen/builds/{bid}/deploy"),
        k.a.put(f"/projects/{k.pid}/kaisen", json={"model": "x"}),
        k.a.put(f"/kaisen/builds/{bid}/secrets/API_TOKEN", json={"value": "not-a-real-secret"}),
        k.a.delete(f"/kaisen/builds/{bid}/secrets/API_TOKEN"),
        k.a.put(f"/kaisen/builds/{bid}/output", json={"mode": "download"}),
        k.a.post(f"/kaisen/builds/{bid}/snapshots/1/restore"),
        k.b.post(f"/kaisen/builds/{bid}/run"),
        k.a.delete(f"/kaisen/builds/{bid}"),
    ]
    for r in refused:
        assert r.status_code == 403, (r.request.url, r.text)
        assert "suspended" in r.json()["detail"]
    assert REASON in k.a.post(f"/kaisen/builds/{bid}/run").json()["detail"]  # the owner is told why
    assert REASON not in k.b.post(f"/kaisen/builds/{bid}/run").json()["detail"]  # a member is not
    assert _db().query(KaisenBuild).filter_by(project_id=k.pid).count() == 1
    assert _db().query(KaisenRun).filter_by(build_id=bid).count() == 0
    for path in (f"/kaisen/builds/{bid}", f"/kaisen/builds?project_id={k.pid}", f"/projects/{k.pid}/kaisen",
                 f"/kaisen/builds/{bid}/secrets", f"/kaisen/builds/{bid}/snapshots", f"/kaisen/spend?project_id={k.pid}"):
        assert k.b.get(path).status_code == 200, path


def test_kaisen_works_again_after_unsuspend(k):
    _suspend(k)
    k.admin.post(f"/admin/projects/{k.pid}/unsuspend")
    _run(k.a, k.bid)


def test_project_suspended_mid_run_stops_the_run_before_the_next_turn(k):
    _run(k.a, k.bid)
    model = FakeModelClient([
        lambda m: (_suspend(k), reply(write("a.txt", "one")))[1],  # lands while the model is answering
        reply(write("b.txt", "two"), tool_use("finish", {"summary": "done"})),
    ])
    assert go(model) is True
    assert len(model.calls) == 1  # no second model turn was paid for
    _stopped_with(k.bid, "This project was suspended by an administrator.")
    assert _db().get(KaisenBuild, k.bid).status == "stopped"


def test_project_suspended_before_the_worker_picks_the_run_up_stops_it(k):
    _run(k.a, k.bid)
    _suspend(k)
    model = FakeModelClient([reply(write("a.txt", "one"))])
    go(model)
    assert model.calls == []
    _stopped_with(k.bid, "This project was suspended by an administrator.")


def test_run_creator_suspended_mid_run_stops_it(k):
    _run(k.a, k.bid)
    model = FakeModelClient([
        lambda m: (k.admin.post(f"/admin/users/{_uid('a@example.com')}/suspend", json={"reason": "spam"}), reply(write("a.txt", "one")))[1],
        reply(tool_use("finish", {"summary": "done"})),
    ])
    go(model)
    assert len(model.calls) == 1
    _stopped_with(k.bid, "The person who started this run was suspended.")
    assert _db().get(models.Project, k.pid).status == "active"  # the team project itself keeps running


def test_suspending_one_member_does_not_stop_another_members_run(k):
    _run(k.b, k.bid)  # b started this run
    k.admin.post(f"/admin/users/{_uid('a@example.com')}/suspend", json={"reason": "spam"})
    model = FakeModelClient([reply(write("a.txt", "one"), tool_use("finish", {"summary": "done"}))])
    go(model)
    assert len(model.calls) == 1
    assert _last_run(k.bid).status == "done"


def test_run_creator_removed_from_the_project_stops_it(k):
    _run(k.b, k.bid)
    model = FakeModelClient([
        lambda m: (k.a.delete(f"/projects/{k.pid}/members/{_uid('b@example.com')}"), reply(write("a.txt", "one")))[1],
        reply(tool_use("finish", {"summary": "done"})),
    ])
    go(model)
    assert len(model.calls) == 1
    _stopped_with(k.bid, "The person who started this run is no longer a member of this project.")


def test_deploy_tool_refuses_when_the_project_is_suspended_mid_turn(k):
    _run(k.a, k.bid)
    runtime = FakeRuntime()
    model = FakeModelClient([
        lambda m: (_suspend(k), reply(write("kaisen.json", '{"runtime": "python", "start": "python app.py"}'),
                                      write("app.py", "print(1)"), tool_use("deploy_preview", {}),
                                      tool_use("finish", {"summary": "done"})))[1],
    ])
    go(model, runtime=runtime)
    _stopped_with(k.bid, "This project was suspended by an administrator.")
    assert _last_run(k.bid).status == "stopped"  # a finish in the same turn does not revive it
    assert _db().query(models.App).filter(models.App.kaisen_build_id == k.bid).count() == 0
    assert runtime.probes == []


def test_automatic_deploy_after_finish_is_refused_when_suspended(k, monkeypatch):
    from app.kaisen import worker
    _run(k.a, k.bid)
    files = reply(write("kaisen.json", '{"runtime": "python", "start": "python app.py"}'), write("app.py", "print(1)"),
                  tool_use("finish", {"summary": "done"}))
    real = worker.deploy_on_finish

    def late(*a, **kw):  # the suspension lands between the finish and the automatic deploy
        _suspend(k)
        return real(*a, **kw)
    monkeypatch.setattr(worker, "deploy_on_finish", late)
    go(FakeModelClient([files]))
    assert _last_run(k.bid).status == "done"
    assert _db().query(models.App).filter(models.App.kaisen_build_id == k.bid).count() == 0
    texts = [i.text for i in _db().query(KaisenInbox).filter_by(build_id=k.bid)]
    assert any("suspended by an administrator" in t and t.startswith("Preview not deployed") for t in texts)


def test_reply_that_arrives_after_the_suspension_is_discarded_and_runs_nothing(k):
    """K49: the turn in flight when the project is suspended must not write files or finish."""
    _run(k.a, k.bid)
    files = reply(write("kaisen.json", '{"runtime": "python", "start": "python app.py"}'), write("app.py", "print(1)"),
                  tool_use("finish", {"summary": "done"}))
    go(FakeModelClient([lambda m: (_suspend(k), files)[1]]))
    _stopped_with(k.bid, "This project was suspended by an administrator.")
    assert _db().query(models.App).filter(models.App.kaisen_build_id == k.bid).count() == 0
    from app.kaisen.models import KaisenFile
    assert _db().query(KaisenFile).filter_by(build_id=k.bid, path="app.py").count() == 0


def test_deploy_build_refuses_a_suspended_project_directly(k):
    from app.kaisen import deploy as deploy_mod
    from app.providers.fake import FakeRegistry
    _suspend(k)
    s = _db()
    with pytest.raises(deploy_mod.DeployError) as e:
        deploy_mod.deploy_build(s, s.get(KaisenBuild, k.bid), FakeRegistry(default_size_bytes=1024))
    assert e.value.status == 403 and e.value.final


def test_stop_reason_is_none_for_an_active_member_run(k):
    _run(k.a, k.bid)
    s = _db()
    assert builder.stop_reason(s, _last_run(k.bid)) is None


# ---- migration data mapping ---------------------------------------------------------------------------------------

def test_migration_maps_suspended_by_user_to_suspended_via_and_back(tmp_path):
    db_path = tmp_path / "m.db"
    root = os.path.join(os.path.dirname(__file__), "..")
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"}

    def alembic(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True, capture_output=True)

    alembic("upgrade", "a8c3e5f1b2d4")
    con = sqlite3.connect(db_path)
    for uid, status in ((1, "active"), (2, "suspended")):
        con.execute("INSERT INTO users (id,email,password_hash,role,app_limit,status,created_at) VALUES (?,?,?,?,?,?,?)",
                    (uid, f"u{uid}@example.com", "h", "user", 5, status, "2026-01-01"))
        con.execute("INSERT INTO projects (id,name,slug,is_personal,created_by,created_at) VALUES (?,?,?,?,?,?)",
                    (uid, "Personal", f"personal-{uid}", 1, uid, "2026-01-01"))
    cols = [r[1] for r in con.execute("PRAGMA table_info(apps)") if r[3] and r[4] is None and r[1] not in ("id",)]
    base = {"owner_id": 1, "project_id": 1, "name": "n", "hostname": "h", "image": "i", "port": 80, "want_db": 0,
            "want_storage": 0, "created_at": "2026-01-01"}
    extra = [c for c in cols if c not in base]
    rows = {10: ("suspended", 1), 11: ("suspended", 0), 12: ("running", 0)}  # id -> (status, suspended_by_user)
    for aid, (status, flag) in rows.items():
        vals = {**base, "id": aid, "hostname": f"h{aid}", "status": status, "suspended_by_user": flag,
                "owner_id": 2, "project_id": 2}
        names = list(vals) + [c for c in extra if c not in vals]
        con.execute(f"INSERT INTO apps ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                    list(vals.values()) + [0 if c.startswith(("want", "health", "auto")) else "x" for c in names[len(vals):]])
    con.commit()
    con.close()

    alembic("upgrade", "head")
    con = sqlite3.connect(db_path)
    got = dict(con.execute("SELECT id, suspended_via FROM apps"))
    assert got == {10: "user", 11: "admin", 12: None}
    assert "suspended_by_user" not in [r[1] for r in con.execute("PRAGMA table_info(apps)")]
    assert con.execute("SELECT status, suspended_by_user FROM projects WHERE id=2").fetchone() == ("suspended", 1)
    assert con.execute("SELECT status, suspended_by_user FROM projects WHERE id=1").fetchone() == ("active", 0)
    con.close()

    alembic("downgrade", "a8c3e5f1b2d4")
    con = sqlite3.connect(db_path)
    assert dict(con.execute("SELECT id, suspended_by_user FROM apps")) == {10: 1, 11: 0, 12: 0}
    assert "suspended_via" not in [r[1] for r in con.execute("PRAGMA table_info(apps)")]
    assert "status" not in [r[1] for r in con.execute("PRAGMA table_info(projects)")]
    con.close()
    alembic("upgrade", "head")  # and up again


def test_members_can_still_download_backups_from_a_suspended_project(team, on_backups):
    one = team.apps[0]
    _suspend(team)
    # the guard is skipped for downloads; with no backup run the route answers its own 404/422, never the 403
    for who in (team.a, team.b):
        r = who.post(f"/apps/{one}/backups/1/download")
        assert r.status_code != 403, r.text
        r = who.post(f"/apps/{one}/backups/sets/1/download")
        assert r.status_code != 403, r.text
    assert team.b.post(f"/apps/{one}/backups").status_code == 403  # but starting a backup is a write


# ---- security review 2026-10-31 (K45-K48) ----------------------------------------------------------

def test_guard_fails_closed_without_a_recorded_request_method(team):
    """K45: a session that never went through get_current_user (no method on db.info) is refused, not waved through."""
    from fastapi import HTTPException

    from app import projects
    d = _db()
    _suspend(team)
    p = d.get(models.Project, team.pid)
    d.info.clear()
    with pytest.raises(HTTPException) as e:
        projects.forbid_if_suspended(d, p)
    assert e.value.status_code == 403 and REASON not in e.value.detail  # no member known, so no reason either
    d.info["http_method"] = "GET"
    projects.forbid_if_suspended(d, p)  # reads pass
    for m in ("POST", "PUT", "PATCH", "DELETE"):
        d.info["http_method"] = m
        with pytest.raises(HTTPException):
            projects.forbid_if_suspended(d, p)
    d.info.clear()


def test_admin_app_unsuspend_cannot_revive_an_app_a_project_suspension_holds(team):
    """K46: unsuspending one app while its project is suspended would restart it under the suspension."""
    one = team.apps[0]
    _suspend(team)
    r = team.admin.post(f"/admin/apps/{one}/unsuspend")
    assert r.status_code == 409 and "project" in r.json()["detail"]
    assert _row(one).status == "suspended" and _row(one).suspended_via == "project"
    team.admin.post(f"/admin/projects/{team.pid}/unsuspend")
    assert _row(one).status == "pending"


def test_admin_app_unsuspend_cannot_revive_an_app_a_user_suspension_holds(team):
    bid = _uid("b@example.com")
    mine = _app(team.b, _personal(team.b), "mine")
    team.admin.post(f"/admin/users/{bid}/suspend", json={"reason": "spam"})
    r = team.admin.post(f"/admin/apps/{mine}/unsuspend")
    assert r.status_code == 409 and "user" in r.json()["detail"]
    assert _row(mine).status == "suspended"
    team.admin.post(f"/admin/users/{bid}/unsuspend")
    assert _row(mine).status == "pending"


def test_unsuspending_the_project_keeps_the_user_hold_after_a_takeover(team):
    """K46: user suspended, then the personal project suspended on top; lifting the project must not restart the
    apps while the account is still suspended."""
    bid = _uid("b@example.com")
    pp = _personal(team.b)
    mine = _app(team.b, pp, "mine")
    team.admin.post(f"/admin/users/{bid}/suspend", json={"reason": "spam"})
    _suspend(team, pp, "keep it off")
    r = team.admin.post(f"/admin/projects/{pp}/unsuspend")
    assert r.status_code == 200
    assert (_row(mine).status, _row(mine).suspended_via, _row(mine).suspended_reason) == ("suspended", "user", "spam")
    p = _db().get(models.Project, pp)
    assert (p.status, p.suspended_by_user) == ("suspended", True)
    team.admin.post(f"/admin/users/{bid}/unsuspend")
    assert _db().get(models.Project, pp).status == "active" and _row(mine).status == "pending"


def test_unsuspending_the_project_keeps_the_user_hold_when_the_project_came_first(team):
    bid = _uid("b@example.com")
    pp = _personal(team.b)
    mine = _app(team.b, pp, "mine")
    _suspend(team, pp, "abuse found")
    team.admin.post(f"/admin/users/{bid}/suspend", json={"reason": "also the account"})
    assert team.admin.post(f"/admin/projects/{pp}/unsuspend").status_code == 200
    assert _row(mine).status == "suspended"  # the suspended account still holds it
    assert team.admin.post(f"/admin/users/{bid}/unsuspend").status_code == 200
    assert _row(mine).status == "pending" and _db().get(models.Project, pp).status == "active"


@pytest.mark.parametrize("reason", ["line one\nline two\x1b[31m", "bidi \u202e text", "zero\u200bwidth", "nul\x00byte"])
def test_reason_with_control_or_format_characters_is_refused(team, reason):
    """K48: the reason reaches the audit log, the owner banner, a 403 message and the CLI."""
    assert _suspend(team, reason=reason).status_code == 422
    assert team.admin.post(f"/admin/users/{_uid('b@example.com')}/suspend", json={"reason": reason}).status_code == 422
    assert team.admin.post(f"/admin/apps/{team.apps[0]}/suspend", json={"reason": reason}).status_code == 422
    assert _db().get(models.Project, team.pid).status == "active"


def test_reason_whitespace_is_collapsed_to_one_line(team):
    assert _suspend(team, reason="  too   many\n\tspaces  ").status_code == 200
    assert _db().get(models.Project, team.pid).suspended_reason == "too many spaces"


def test_every_member_reachable_write_route_refuses_a_suspended_project(team, monkeypatch):
    """Coverage sweep (K45): each mutating app route, as a plain member and as an owner, gets 403 and changes nothing."""
    from app.providers.fake import FakeObjectStore
    from app.routers.apps import get_object_store
    store = FakeObjectStore()
    fastapi_app.dependency_overrides[get_object_store] = lambda: store
    one = team.apps[0]
    _suspend(team)
    routes = [
        ("PATCH", f"/apps/{one}", {"env": [{"key": "A", "value": "b"}]}),
        ("PATCH", f"/apps/{one}", {"env": [{"key": "S", "value": "b", "is_secret": True}]}),
        ("PATCH", f"/apps/{one}", {"image": "nginx:2"}),
        ("PATCH", f"/apps/{one}", {"volume": {"size_gi": 1, "mount_path": "/data"}}),
        ("DELETE", f"/apps/{one}", None),
        ("POST", f"/apps/{one}/move", {"project_id": team.pid}),
        ("POST", f"/apps/{one}/rotate-credentials", {"target": "db"}),
        ("PUT", f"/apps/{one}/domain", {"host": "demo.example.org"}),
        ("POST", f"/apps/{one}/domain/verify", None),
        ("DELETE", f"/apps/{one}/domain", None),
        ("POST", f"/apps/{one}/restart", None),
        ("POST", f"/apps/{one}/check-image", None),
        ("POST", f"/apps/{one}/rollback", {"digest": "sha256:abc"}),
        ("POST", f"/apps/{one}/alerts/event/dismiss", None),
        ("POST", f"/apps/{one}/events/1/dismiss", None),
        ("DELETE", f"/apps/{one}/events/1/dismiss", None),
        ("POST", f"/apps/{one}/backups", {}),
        ("PATCH", f"/apps/{one}/backups", {"enabled": True}),
        ("DELETE", f"/apps/{one}/backups/1", None),
        ("DELETE", f"/apps/{one}/backups/sets/1", None),
        ("POST", f"/apps/{one}/storage/presign", {"method": "PUT", "key": "x", "content_length": 5}),
        ("POST", f"/apps/{one}/storage/delete", {"prefix": "x/"}),
        ("POST", "/apps/spec", {"spec": "apiVersion: picopaas/v1\nname: one\nimage: nginx:1\nport: 8080\n",
                                "project": _db().get(models.Project, team.pid).slug}),
        ("POST", f"/projects/{team.pid}/invites", {"role": "member"}),
        ("DELETE", f"/projects/{team.pid}/invites/1", None),
        ("PATCH", f"/projects/{team.pid}/members/{_uid('b@example.com')}", {"role": "owner"}),
        ("DELETE", f"/projects/{team.pid}/members/{_uid('a@example.com')}", None),
        ("PATCH", f"/projects/{team.pid}", {"name": "Renamed"}),
        ("DELETE", f"/projects/{team.pid}", None),
        ("PUT", f"/projects/{team.pid}/kaisen", {"anthropic_key": KEY}),
        ("PUT", f"/projects/{team.pid}/kaisen/members/{_uid('b@example.com')}", {}),
        ("POST", f"/projects/{team.pid}/kaisen/test/anthropic", None),
        ("POST", "/kaisen/builds", {"project_id": team.pid, "name": "x", "brief": "y"}),
    ]
    for who in (team.b, team.a):
        for method, path, body in routes:
            if who is team.b and path.startswith(f"/projects/{team.pid}") and method != "DELETE":
                continue  # owner-only routes answer 403 to a plain member anyway
            if who is team.a and method == "DELETE" and "/members/" in path:
                continue  # an owner removing themselves is leaving, which stays open
            r = who.request(method, path, json=body)
            assert r.status_code == 403, (method, path, r.status_code, r.text)
            if r.status_code == 403 and "suspended" not in r.json()["detail"]:
                assert path.startswith(f"/projects/{team.pid}/members") or who is team.b, (method, path, r.text)
    assert _row(one).status == "suspended" and _db().get(models.Project, team.pid).name == "Team"
    assert _db().query(models.App).filter_by(project_id=team.pid).count() == 2


def test_full_api_key_is_refused_in_a_suspended_project_but_reads_work(team, make_client):
    """Bearer keys take the same get_current_user path as the browser session: no separate way around the guard."""
    raw = team.b.post("/api/keys", json={"name": "ci", "scope": "full"}).json()["key"]
    h = {"Authorization": f"Bearer {raw}"}
    c = make_client()  # no cookie
    _suspend(team)
    one = team.apps[0]
    r = c.patch(f"/apps/{one}", json={"name": "renamed"}, headers=h)
    assert r.status_code == 403 and REASON not in r.json()["detail"]  # b is a plain member
    assert c.get(f"/apps/{one}", headers=h).status_code == 200
    assert c.post("/apps/spec", json={"spec": "apiVersion: picopaas/v1\nname: z\nimage: nginx:1\nport: 8080\n",
                                      "project": _db().get(models.Project, team.pid).slug}, headers=h).status_code == 403
