"""Coverage for the 2026-11-11 rejected-status wave: every place that now treats 'rejected' like 'deleted'."""
from app import manage, metrics, models, projects

from tests.test_reject_user import _reject, _setup


def test_approve_non_pending_messages(make_client):
    admin, _, uid = _setup(make_client)
    assert admin.post(f"/admin/users/{uid}/approve").status_code == 200
    r = admin.post(f"/admin/users/{uid}/approve")
    assert r.status_code == 409 and "already approved" in r.json()["detail"]
    assert admin.post(f"/admin/users/{uid}/suspend", json={"reason": "x"}).status_code in (200, 204)
    r = admin.post(f"/admin/users/{uid}/approve")
    assert r.status_code == 409 and "suspended" in r.json()["detail"]


def test_reject_blocked_when_sole_owner_of_project_with_apps(make_client, db_session):
    admin, _, uid = _setup(make_client)
    team = models.Project(name="Solo", slug="solo-team", created_by=uid)
    team.members.append(models.ProjectMember(user_id=uid, role="owner"))
    db_session.add(team)
    db_session.flush()
    db_session.add(models.App(name="held", hostname="held.example.test", image="i", port=80, owner_id=uid, project_id=team.id))
    db_session.commit()
    r = _reject(admin, uid)
    assert r.status_code == 409 and "only owner" in r.json()["detail"]
    assert db_session.get(models.User, uid).status == "pending"


def test_reject_tears_down_legacy_apps_and_soft_deletes_ownerless_project(make_client, db_session):
    admin, _, uid = _setup(make_client)
    db_session.add(models.App(name="legacy", hostname="legacy.example.test", image="i", port=80, owner_id=uid))
    team = models.Project(name="Empty", slug="empty-team", created_by=uid)
    team.members.append(models.ProjectMember(user_id=uid, role="owner"))
    db_session.add(team)
    db_session.commit()
    assert _reject(admin, uid).status_code == 204
    db_session.expire_all()
    assert db_session.query(models.App).filter_by(name="legacy").one().deleted_at is not None
    assert db_session.get(models.Project, team.id).deleted_at is not None


def test_reject_leaves_shared_project_with_other_owner_alive(make_client, db_session):
    admin, _, uid = _setup(make_client)
    pid = admin.post("/projects", json={"name": "Crew"}).json()["id"]
    db_session.add(models.ProjectMember(project_id=pid, user_id=uid, role="owner"))
    db_session.commit()
    assert _reject(admin, uid).status_code == 204
    db_session.expire_all()
    assert db_session.get(models.Project, pid).deleted_at is None
    assert [m.user_id for m in db_session.get(models.Project, pid).members] != [uid]


def test_rejected_member_hidden_from_owner_lists_and_members(make_client, db_session):
    admin, _, uid = _setup(make_client)
    pid = admin.post("/projects", json={"name": "Crew"}).json()["id"]
    _reject(admin, uid)
    # a stray membership row (e.g. restored data) must still not surface a rejected account
    db_session.add(models.ProjectMember(project_id=pid, user_id=uid, role="owner"))
    db_session.commit()
    assert projects.owner_emails_map(db_session, [pid]) == {pid: ["admin@example.com"]}
    emails = [m["email"] for m in admin.get(f"/projects/{pid}/members").json()]
    assert emails == ["admin@example.com"]


def test_manage_cli_cannot_find_rejected_user(make_client, db_session):
    admin, _, uid = _setup(make_client)
    _reject(admin, uid)
    assert manage._find(db_session, "rej@example.com") is None
    assert manage.main(["grant-admin", "rej@example.com"], lambda: db_session) == 1
    assert manage.main(["reset-password", "rej@example.com"], lambda: db_session) == 1
    assert db_session.get(models.User, uid).status == "rejected"


def test_metrics_users_gauge_ignores_rejected(make_client, db_session):
    admin, _, uid = _setup(make_client)
    metrics.refresh_gauges(db_session)
    before = metrics.USERS._value.get()
    _reject(admin, uid)
    metrics.refresh_gauges(db_session)
    assert metrics.USERS._value.get() == before - 1


def test_health_admin_count_ignores_rejected_admin_role(make_client, db_session):
    from app import health
    admin, _, uid = _setup(make_client)
    _reject(admin, uid)
    u = db_session.get(models.User, uid)
    u.role = "platform_admin"  # impossible through the API; proves the filter, not a flow
    db_session.commit()
    assert health.db_counts(db_session)["users"]["admins"] == 1
