"""Pod info in logs, pod events download, audit export, health engine counts, promote/demote, manage CLI."""
import csv
import datetime as dt
import io

import pytest

from app import health, models
from app.dependencies import get_kube
from app.main import app as fastapi_app
from app.providers.base import PodInfo, mark_current
from app.providers.fake import FakeKubeProvider
from tests.conftest import register_and_login

APP = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


@pytest.fixture()
def kube():
    health.reset_cache()
    k = FakeKubeProvider()
    fastapi_app.dependency_overrides[get_kube] = lambda: k
    yield k
    health.reset_cache()


@pytest.fixture()
def admin(make_client, kube):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def owner(make_client, admin):
    c = make_client()
    register_and_login(c, "owner@example.com")
    return c


def _uid(c):
    return c.get("/auth/me").json()["id"]


# --- pod_info ---

def test_mark_current_newest_rollout_by_hash():
    def p(n, mins, hash_, ready=True, term=False, phase="Running"):
        return PodInfo(name=n, created_at=T0 + dt.timedelta(minutes=mins), phase=phase, ready=ready,
                       terminating=term, rollout=hash_)
    infos = mark_current([p("old", 0, "a"), p("new1", 5, "b"), p("new2", 6, "b"), p("new3", 7, "b", ready=False),
                          p("dying", 8, "b", term=True)])
    cur = {i.name: i.current for i in infos}
    assert cur == {"old": False, "new1": True, "new2": True, "new3": False, "dying": False}


def test_mark_current_without_hash_uses_newest_ready():
    a = PodInfo(name="a", created_at=T0, phase="Running", ready=True)
    b = PodInfo(name="b", created_at=T0 + dt.timedelta(minutes=1), phase="Running", ready=True)
    c = PodInfo(name="c", created_at=T0 + dt.timedelta(minutes=2), phase="Pending", ready=False)
    mark_current([a, b, c])
    assert (a.current, b.current, c.current) == (False, True, False)


def test_logs_pod_info(owner, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    ns = f"app-{aid}"
    kube.pod_lines[ns] = {"web-a": ["a"], "web-b": ["b"]}
    kube.pod_meta[ns] = {
        "web-a": PodInfo(name="web-a", created_at=T0, phase="Running", ready=True, rollout="x"),
        "web-b": PodInfo(name="web-b", created_at=T0 + dt.timedelta(hours=1), phase="Running", ready=True, rollout="y"),
    }
    body = owner.get(f"/apps/{aid}/logs").json()
    assert body["pods"] == ["web-b", "web-a"]
    info = {i["name"]: i for i in body["pod_info"]}
    assert set(info["web-a"]) == {"name", "created_at", "phase", "ready", "current", "started_at", "uptime_seconds"}
    assert info["web-a"]["current"] is False and info["web-b"]["current"] is True
    assert info["web-b"]["phase"] == "Running" and info["web-b"]["created_at"].startswith("2026-01-01T01:00")


# --- pod events ---

def test_pod_events_download(owner, admin, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    ns = f"app-{aid}"
    kube.pod_lines[ns] = {"web-a": ["a"]}
    kube.k8s_events[ns] = {"web-a": [
        {"at": T0 + dt.timedelta(minutes=2), "type": "Warning", "reason": "BackOff", "count": 4, "message": "Back-off\nrestarting"},
        {"at": T0, "type": "Normal", "reason": "Pulled", "count": 1, "message": "pulled image"},
    ]}
    r = owner.get(f"/apps/{aid}/pods/web-a/events")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-disposition"] == 'attachment; filename="demo-web-a-events.txt"'
    lines = r.text.splitlines()
    assert "1 hour" in lines[0]
    assert lines[1] == "2026-01-01T00:00:00Z  Normal  Pulled  x1  pulled image"
    assert lines[2] == "2026-01-01T00:02:00Z  Warning  BackOff  x4  Back-off restarting"
    assert admin.get(f"/apps/{aid}/pods/web-a/events").status_code == 200
    j = owner.get(f"/apps/{aid}/pods/web-a/events?format=json").json()
    assert [e["reason"] for e in j] == ["Pulled", "BackOff"]
    assert owner.get(f"/apps/{aid}/pods/nope/events").status_code == 404


def test_pod_events_other_user_404(owner, make_client, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    kube.pod_lines[f"app-{aid}"] = {"web-a": ["a"]}
    other = make_client()
    register_and_login(other, "other@example.com")
    assert other.get(f"/apps/{aid}/pods/web-a/events").status_code == 404


# --- audit export ---

def test_audit_export_csv_and_json(admin, owner, db_session):
    db_session.add(models.AuditLog(actor_email="=cmd|x", action="+evil", detail="@sum"))
    db_session.commit()
    r = admin.get("/admin/audit/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    assert r.headers["content-disposition"] == f'attachment; filename="picopaas-audit-{today}.csv"'
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["id", "at", "actor_email", "action", "target_type", "target_id", "detail"]
    evil = next(x for x in rows[1:] if x[3] == "'+evil")
    assert evil[2] == "'=cmd|x" and evil[6] == "'@sum"
    ids = [int(x[0]) for x in rows[1:]]
    assert ids == sorted(ids, reverse=True)
    j = admin.get("/admin/audit/export?format=json")
    assert j.headers["content-disposition"].endswith(f'{today}.json"')
    assert any(e["action"] == "audit.export" for e in j.json())
    assert j.json()[0]["action"] == "audit.export"  # newest first
    assert set(j.json()[0]) == {"id", "at", "actor_email", "action", "target_type", "target_id", "detail"}


def test_audit_export_admin_only(owner):
    assert owner.get("/admin/audit/export").status_code == 403
    assert owner.get("/admin/audit/export?format=xml").status_code in (403, 422)


# --- health ---

def test_health_by_db_engine(admin, owner, kube):
    owner.post("/apps", json={**APP, "want_db": True})
    from app.db import get_db
    db = next(fastapi_app.dependency_overrides[get_db]())
    u = db.query(models.User).filter_by(email="owner@example.com").one()
    for i, eng in enumerate(["mysql", "pg", "pg"]):
        db.add(models.App(owner_id=u.id, name=f"x{i}", hostname=f"x-{i}", image="i", port=8080, want_db=True, db_engine=eng))
    db.add(models.App(owner_id=u.id, name="nodb", hostname="nodb-1", image="i", port=8080, want_db=False, db_engine="pg"))
    db.commit()
    w = admin.get("/admin/health").json()["workloads"]
    assert w["by_db_engine"] == {"mysql": 2, "postgres": 2}


# --- promote / demote ---

def test_promote_demote_rules(admin, owner, make_client):
    oid = _uid(owner)
    aid = _uid(admin)
    r = admin.post(f"/admin/users/{oid}/promote")
    assert r.status_code == 200 and r.json()["role"] == "platform_admin"
    assert r.json()["app_limit_effective"] >= 1
    assert admin.post(f"/admin/users/{oid}/promote").status_code == 409
    # promoted admin can use admin routes
    assert owner.get("/admin/users").status_code == 200
    # self-demote OK with another active admin
    assert owner.post(f"/admin/users/{oid}/demote").json()["role"] == "user"
    assert owner.get("/admin/users").status_code == 403
    # last admin cannot be demoted, suspended or deleted
    assert admin.post(f"/admin/users/{aid}/demote").status_code == 409
    assert admin.post(f"/admin/users/{aid}/suspend").status_code == 409
    assert admin.delete(f"/admin/users/{aid}").status_code == 409
    assert admin.post(f"/admin/users/{oid}/demote").status_code == 409  # not an admin
    actions = [e["action"] for e in admin.get("/admin/audit").json()]
    assert "user.promote" in actions and "user.demote" in actions


def test_promote_requires_active(admin, make_client):
    p = make_client()
    register_and_login(p, "pending@example.com", approve=False)
    assert admin.post(f"/admin/users/{_uid(p)}/promote").status_code == 409
    assert admin.post("/admin/users/999/promote").status_code == 404


def test_promoted_admin_unlimited_apps(admin, owner):
    admin.post(f"/admin/users/{_uid(owner)}/promote")
    assert owner.post("/apps", json=APP).status_code == 201
    assert owner.post("/apps", json={**APP, "name": "second"}).status_code == 201


def test_demoted_admin_and_other_admin_suspend_protected(admin, owner):
    admin.post(f"/admin/users/{_uid(owner)}/promote")
    # with two admins the pre-existing protection still applies to admins (demote first)
    assert admin.post(f"/admin/users/{_uid(owner)}/suspend").status_code == 400


# --- manage CLI ---

def test_manage_cli(db_session, admin, owner, capsys):
    from app import manage
    from app.security import verify_password
    factory = lambda: db_session  # noqa: E731
    db_session.close = lambda: None  # keep the shared session open for assertions
    assert manage.main(["list-admins"], factory) == 0
    out = capsys.readouterr().out
    assert "admin@example.com\tactive" in out and "owner@example.com" not in out

    owner_row = db_session.query(models.User).filter_by(email="owner@example.com").one()
    owner_row.status = "suspended"
    db_session.commit()
    assert manage.main(["grant-admin", "Owner@Example.com"], factory) == 0
    db_session.refresh(owner_row)
    assert owner_row.role == "platform_admin" and owner_row.status == "active"

    assert manage.main(["reset-password", "owner@example.com"], factory) == 0
    temp = capsys.readouterr().out.strip().splitlines()[-1]
    db_session.refresh(owner_row)
    assert owner_row.must_change_password is True and verify_password(temp, owner_row.password_hash)

    assert manage.main(["grant-admin", "ghost@example.com"], factory) == 1
    rows = db_session.query(models.AuditLog).filter_by(actor_email="manage-cli").all()
    assert {r.action for r in rows} == {"user.promote", "user.reset_password"}
    assert temp not in " ".join(r.detail or "" for r in rows)


# --- security review 2026-10-23 ---

def test_pod_events_odd_pod_names_never_reach_kube_and_filename_safe(owner, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    kube.pod_lines[f"app-{aid}"] = {"web-a": ["a"]}
    for bad in ("..%2F..%2Fother", "web-a%0d%0aSet-Cookie:x", "app-1", "x%22y"):
        assert owner.get(f"/apps/{aid}/pods/{bad}/events").status_code in (404, 422)


def test_pod_events_filename_sanitised(owner, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    kube.pod_lines[f"app-{aid}"] = {'we"b\r\nX: y': ["a"]}
    r = owner.get(f"/apps/{aid}/pods/{'we%22b%0D%0AX: y'}/events")
    if r.status_code == 200:
        cd = r.headers["content-disposition"]
        assert "\r" not in cd and "\n" not in cd and cd.count('"') == 2


def test_read_key_cannot_promote_but_admin_gate_needs_active(admin, owner, db_session):
    key = admin.post("/api/keys", json={"scope": "read"}).json()
    c = type(admin)(admin.app)
    h = {"Authorization": f"Bearer {key['key']}"}
    assert c.post(f"/admin/users/{_uid(owner)}/promote", headers=h).status_code == 403
    me = db_session.get(models.User, _uid(admin))
    me.status = "suspended"
    db_session.commit()
    assert admin.get("/admin/audit/export").status_code == 403


def test_csrf_guard_blocks_same_site_cookie_posts(admin, owner):
    uid = _uid(owner)
    for hdrs in ({"Origin": "https://evil-app.picopaas.xyz"}, {"Sec-Fetch-Site": "same-site"},
                 {"Sec-Fetch-Site": "cross-site"}):
        r = admin.post(f"/admin/users/{uid}/promote", headers=hdrs)
        assert r.status_code == 403 and "Cross-site" in r.json()["detail"]
    assert owner.get("/auth/me").json()["role"] == "user"
    ok = admin.post(f"/admin/users/{uid}/promote", headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200
