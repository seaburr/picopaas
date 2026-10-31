"""Coverage pass for the 2026-10-23 admin-ops commit: manage CLI, promote/demote, audit export, pod info/events."""
import csv
import datetime as dt
import io
import json
from urllib.parse import quote

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


def _pod(name, mins, rollout=None, ready=True, term=False, phase="Running"):
    return PodInfo(name=name, created_at=T0 + dt.timedelta(minutes=mins), phase=phase, ready=ready,
                   terminating=term, rollout=rollout)


# --- mark_current edge cases ---

def test_mark_current_empty_and_all_terminating():
    assert mark_current([]) == []
    infos = mark_current([_pod("a", 0, "h", term=True), _pod("b", 1, "h", term=True)])
    assert [i.current for i in infos] == [False, False]


def test_mark_current_crashlooping_newest_means_none_current_with_hash():
    # newest pod defines the rollout but is not ready: its rollout-mates are not current either
    infos = mark_current([_pod("old", 0, "a"), _pod("crash", 5, "b", ready=False, phase="Running")])
    assert [i.current for i in infos] == [False, False]


def test_mark_current_without_hash_ties_and_terminating():
    infos = mark_current([_pod("a", 0), _pod("b", 3), _pod("c", 3), _pod("d", 9, term=True)])
    assert {i.name: i.current for i in infos} == {"a": False, "b": True, "c": True, "d": False}


def test_mark_current_no_ready_pods_without_hash():
    infos = mark_current([_pod("a", 0, ready=False), _pod("b", 1, phase="Pending", ready=False)])
    assert [i.current for i in infos] == [False, False]


def test_mark_current_missing_created_at_and_stale_flag_reset():
    a = PodInfo(name="a", created_at=None, phase="Running", ready=True)
    a.current = True
    b = _pod("b", 1)
    mark_current([a, b])
    assert a.current is False and b.current is True


def test_mark_current_mixed_rollout_old_hash_ready_stays_old():
    infos = mark_current([_pod("o1", 0, "a"), _pod("o2", 1, "a"), _pod("n1", 2, "b"), _pod("n2", 3, "b", ready=False)])
    assert {i.name: i.current for i in infos} == {"o1": False, "o2": False, "n1": True, "n2": False}


def test_logs_default_pod_meta_marks_newest_current(owner, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    kube.pod_lines[f"app-{aid}"] = {"p1": ["x"], "p2": ["y"]}
    info = {i["name"]: i["current"] for i in owner.get(f"/apps/{aid}/logs").json()["pod_info"]}
    assert info == {"p1": False, "p2": True}


# --- pod events ---

class _Boom(FakeKubeProvider):
    def pod_events(self, namespace, pod):
        raise RuntimeError("https://secret-host.invalid token=abc")


def test_pod_events_provider_failure_is_502_without_leak(owner, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    fastapi_app.dependency_overrides[get_kube] = lambda: _Boom()
    r = owner.get(f"/apps/{aid}/pods/web-a/events")
    assert r.status_code == 502 and "secret-host" not in r.text and "token" not in r.text


def test_pod_events_empty_and_missing_fields(owner, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    ns = f"app-{aid}"
    kube.pod_lines[ns] = {"web-a": ["a"]}
    r = owner.get(f"/apps/{aid}/pods/web-a/events")
    assert r.status_code == 200 and r.text.splitlines()[-1] == "(no events)"
    kube.k8s_events[ns] = {"web-a": [{"message": "m"}]}
    assert owner.get(f"/apps/{aid}/pods/web-a/events").text.splitlines()[1] == "-" + "  " * 3 + "x1  m"
    j = owner.get(f"/apps/{aid}/pods/web-a/events?format=json").json()
    assert j == [{"at": None, "type": None, "reason": None, "count": None, "message": "m"}]


def test_pod_events_bad_format_422(owner, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    assert owner.get(f"/apps/{aid}/pods/web-a/events?format=xml").status_code == 422


def test_pod_events_filename_sanitised(owner, kube, db_session):
    aid = owner.post("/apps", json=APP).json()["id"]
    row = db_session.get(models.App, aid)
    row.name = 'my app";x\r\nSet-Cookie: a=b'
    db_session.commit()
    pod = 'we b"/..;'
    kube.pod_lines[f"app-{aid}"] = {pod: ["a"]}
    r = owner.get(f"/apps/{aid}/pods/{quote(pod, safe='')}/events")
    if r.status_code == 200:
        cd = r.headers["content-disposition"]
        assert cd.count('"') == 2 and "\r" not in cd and "\n" not in cd and " " not in cd.split("filename=")[1]
        assert "/" not in cd.split("filename=")[1]
    else:
        assert r.status_code == 404


def test_pod_events_other_user_cannot_read_even_known_pod(owner, make_client, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    kube.pod_lines[f"app-{aid}"] = {"web-a": ["a"]}
    other = make_client()
    register_and_login(other, "other@example.com")
    r = other.get(f"/apps/{aid}/pods/web-a/events?format=json")
    assert r.status_code == 404
    assert other.get(f"/apps/{aid}/logs").status_code == 404


def test_logs_unknown_pod_404_and_failure_note(owner, kube):
    aid = owner.post("/apps", json=APP).json()["id"]
    kube.pod_lines[f"app-{aid}"] = {"web-a": ["a"]}
    assert owner.get(f"/apps/{aid}/logs?pod=nope").status_code == 404

    class Bad(FakeKubeProvider):
        def pod_logs(self, *a, **k):
            raise RuntimeError("boom")
    fastapi_app.dependency_overrides[get_kube] = lambda: Bad()
    body = owner.get(f"/apps/{aid}/logs").json()
    assert body["lines"] == [] and "boom" not in json.dumps(body)


# --- restart: image refresh failure is swallowed ---

def test_restart_survives_image_refresh_exception(owner, monkeypatch):
    from app import image_check
    aid = owner.post("/apps", json=APP).json()["id"]

    def boom(*a, **k):
        raise RuntimeError("registry down")
    monkeypatch.setattr(image_check, "refresh_app_image", boom)
    r = owner.post(f"/apps/{aid}/restart")
    assert r.status_code == 200 and r.json()["status"] == "pending"


# --- audit export ---

def test_audit_export_cap_and_order(admin, db_session, monkeypatch):
    from app.routers import admin as admin_router
    monkeypatch.setattr(admin_router, "AUDIT_EXPORT_MAX", 3)
    for i in range(6):
        db_session.add(models.AuditLog(actor_email="a@example.com", action=f"act{i}"))
    db_session.commit()
    rows = list(csv.reader(io.StringIO(admin.get("/admin/audit/export").text)))
    assert len(rows) == 1 + 3
    assert [r[3] for r in rows[1:]] == ["act5", "act4", "act3"]
    data = admin.get("/admin/audit/export?format=json").json()
    assert len(data) == 3
    last = db_session.query(models.AuditLog).filter_by(action="audit.export").order_by(models.AuditLog.id.desc()).first()
    assert last.detail == "format=json rows=3"


def test_audit_export_default_cap_is_50k():
    from app.routers import admin as admin_router
    assert admin_router.AUDIT_EXPORT_MAX == 50_000


@pytest.mark.parametrize("val", ["=1+1", "+1", "-1", "@x", "\tx", "\rx"])
def test_audit_export_neutralises_every_formula_prefix(admin, db_session, val):
    db_session.add(models.AuditLog(actor_email="a@example.com", action="t", detail=val))
    db_session.commit()
    rows = list(csv.reader(io.StringIO(admin.get("/admin/audit/export").text)))
    row = next(r for r in rows[1:] if r[3] == "t")
    assert row[6] == "'" + val


def test_audit_export_plain_and_null_values_untouched(admin, db_session):
    db_session.add(models.AuditLog(actor_email="a@example.com", action="plain", detail=None, target_id=7))
    db_session.commit()
    rows = list(csv.reader(io.StringIO(admin.get("/admin/audit/export").text)))
    row = next(r for r in rows[1:] if r[3] == "plain")
    assert row[2] == "a@example.com" and row[6] == "" and row[5] == "7"


def test_audit_export_json_keeps_raw_values(admin, db_session):
    db_session.add(models.AuditLog(actor_email="a@example.com", action="=raw"))
    db_session.commit()
    assert any(e["action"] == "=raw" for e in admin.get("/admin/audit/export?format=json").json())


def test_audit_export_requires_login(make_client):
    assert make_client().get("/admin/audit/export").status_code in (401, 403)


# --- promote / demote / last admin ---

def test_promote_deleted_or_suspended_user(admin, owner):
    oid = _uid(owner)
    admin.post(f"/admin/users/{oid}/suspend", json={"reason": "not-the-reason"})
    assert admin.post(f"/admin/users/{oid}/promote").status_code == 409
    admin.post(f"/admin/users/{oid}/reactivate")
    admin.delete(f"/admin/users/{oid}")
    assert admin.post(f"/admin/users/{oid}/promote").status_code == 404
    assert admin.post(f"/admin/users/{oid}/demote").status_code == 404


def test_demote_unknown_404_and_non_admin_forbidden(admin, owner):
    assert admin.post("/admin/users/9999/demote").status_code == 404
    assert owner.post(f"/admin/users/{_uid(admin)}/demote").status_code == 403
    assert owner.post(f"/admin/users/{_uid(owner)}/promote").status_code == 403


def test_last_active_admin_counts_only_active(admin, owner, db_session):
    """A suspended second admin must not count as a spare: the sole active admin stays protected."""
    oid, aid = _uid(owner), _uid(admin)
    admin.post(f"/admin/users/{oid}/promote")
    row = db_session.get(models.User, oid)
    row.status = "suspended"
    db_session.commit()
    assert admin.post(f"/admin/users/{aid}/demote").status_code == 409
    assert admin.delete(f"/admin/users/{aid}").status_code == 409
    assert admin.post(f"/admin/users/{aid}/suspend", json={"reason": "not-the-reason"}).status_code == 409


def test_second_admin_allows_demoting_first(admin, owner):
    oid, aid = _uid(owner), _uid(admin)
    admin.post(f"/admin/users/{oid}/promote")
    assert owner.post(f"/admin/users/{aid}/demote").status_code == 200
    # now owner is the only admin
    assert owner.post(f"/admin/users/{oid}/demote").status_code == 409


# --- health by_db_engine ---

def test_health_by_db_engine_empty_and_excludes_deleted(admin, db_session):
    assert admin.get("/admin/health").json()["workloads"]["by_db_engine"] == {"mysql": 0, "postgres": 0}
    u = db_session.query(models.User).filter_by(email="admin@example.com").one()
    db_session.add(models.App(owner_id=u.id, name="g", hostname="g-1", image="i", port=8080, want_db=True,
                              db_engine="pg", deleted_at=dt.datetime(2026, 1, 1)))
    db_session.add(models.App(owner_id=u.id, name="m", hostname="m-1", image="i", port=8080, want_db=True,
                              db_engine="mysql"))
    db_session.commit()
    health.reset_cache()
    w = admin.get("/admin/health").json()["workloads"]
    assert w["by_db_engine"]["mysql"] == 1 and w["by_db_engine"]["postgres"] == 0


# --- manage CLI ---

def _cli(db_session, monkeypatch):
    monkeypatch.setattr(db_session, "close", lambda: None)
    return lambda: db_session


def test_manage_reset_password_unknown_and_default_session(db_session, admin, monkeypatch, capsys):
    from app import db as app_db, manage
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(app_db, "SessionLocal", lambda: db_session)
    assert manage.main(["reset-password", "ghost@example.com"]) == 1
    assert "No such user" in capsys.readouterr().err
    assert manage.main(["list-admins"]) == 0


def test_manage_skips_deleted_users_and_closes_session(db_session, admin, owner, monkeypatch, capsys):
    from app import manage
    row = db_session.query(models.User).filter_by(email="owner@example.com").one()
    row.status = "deleted"
    db_session.commit()
    closed = []
    monkeypatch.setattr(db_session, "close", lambda: closed.append(1))
    assert manage.main(["grant-admin", "owner@example.com"], lambda: db_session) == 1
    assert closed == [1]
    assert "No such user" in capsys.readouterr().err


def test_manage_requires_subcommand():
    from app import manage
    with pytest.raises(SystemExit):
        manage.main([])


def test_manage_grant_admin_audit_detail_and_idempotent(db_session, admin, owner, monkeypatch, capsys):
    from app import manage
    f = _cli(db_session, monkeypatch)
    assert manage.main(["grant-admin", "owner@example.com"], f) == 0
    assert manage.main(["grant-admin", "owner@example.com"], f) == 0
    rows = db_session.query(models.AuditLog).filter_by(actor_email="manage-cli", action="user.promote").all()
    assert len(rows) == 2 and "was role=user status=active" in rows[0].detail
    assert "was role=platform_admin" in rows[1].detail


def test_manage_reset_password_lets_user_log_in(db_session, admin, owner, make_client, monkeypatch, capsys):
    from app import manage
    f = _cli(db_session, monkeypatch)
    assert manage.main(["reset-password", "owner@example.com"], f) == 0
    temp = capsys.readouterr().out.strip().splitlines()[-1]
    c = make_client()
    r = c.post("/auth/login", json={"email": "owner@example.com", "password": temp})
    assert r.status_code == 200


# --- fake provider behaviours used above ---

def test_fake_kube_pod_events_known_via_meta_and_canned_logs(kube):
    kube.pod_meta["ns"] = {"m": _pod("m", 0)}
    assert kube.pod_events("ns", "m") == []
    with pytest.raises(LookupError):
        kube.pod_events("ns", "zzz")
    kube.k8s_events["ns"] = {"m": [{"at": T0 + dt.timedelta(minutes=2), "message": "b"}, {"message": "none"},
                                   {"at": T0, "message": "a"}]}
    assert [e["message"] for e in kube.pod_events("ns", "m")] == ["none", "a", "b"]
