"""GET /apps/{id}/advice and GET /admin/advice: access rules, shape, batching, and the real data loader."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import advisor, models
from tests.conftest import register_and_login

APP = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def alice(make_client, admin):
    c = make_client()
    register_and_login(c, "alice@example.com")
    return c


@pytest.fixture()
def bob(make_client, admin):
    c = make_client()
    register_and_login(c, "bob@example.com")
    return c


@pytest.fixture()
def aid(alice, db_session):
    r = alice.post("/apps", json=APP)
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]
    a = db_session.get(models.App, app_id)
    a.created_at = _now() - timedelta(days=30)
    a.status = "running"
    db_session.commit()
    return app_id


def _oom(db, app_id, minutes_ago=30):
    db.add(models.AppEvent(app_id=app_id, at=_now() - timedelta(minutes=minutes_ago), kind="oom", message="Restarted: your app ran out of memory.",
                           dedupe_key=f"k{app_id}-{minutes_ago}"))
    db.commit()


def test_member_gets_findings_shape(alice, aid, db_session):
    _oom(db_session, aid)
    r = alice.get(f"/apps/{aid}/advice")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"findings"}
    oom = next(f for f in body["findings"] if f["rule"] == "mem.oom")
    assert set(oom) == {"rule", "severity", "title", "evidence", "action"}
    assert oom["severity"] == "warning" and oom["title"] == "Out of memory"
    # a fresh app runs at the platform cap (384 MB): no action, no upsell
    assert oom["action"] is None and "At the platform cap (384 MB). Reduce memory use." in oom["evidence"]
    assert "admin" not in oom["evidence"].lower()


def test_owner_lowered_limit_gets_set_resource_action_that_the_patch_endpoint_accepts(alice, aid, db_session):
    assert alice.patch(f"/apps/{aid}", json={"resources": {"mem_limit_mi": 256}}).status_code == 200
    _oom(db_session, aid)
    oom = next(f for f in alice.get(f"/apps/{aid}/advice").json()["findings"] if f["rule"] == "mem.oom")
    assert oom["action"] == {"kind": "set_resource", "label": "Raise memory to 320 MB", "field": "mem_limit_mi",
                             "from": 256, "to": 320, "unit": "MB", "restarts_app": True}
    a = oom["action"]
    r = alice.patch(f"/apps/{aid}", json={"resources": {a["field"]: a["to"]}})
    assert r.status_code == 200 and r.json()["resources"]["mem_limit_mi"] == 320


def test_advice_is_read_only(alice, aid, db_session):
    _oom(db_session, aid)
    before = db_session.query(models.AuditLog).count()
    alice.get(f"/apps/{aid}/advice")
    assert db_session.query(models.AuditLog).count() == before
    assert db_session.get(models.App, aid).status == "running"


def test_sorted_critical_first_and_admin_rules_omitted(alice, aid, db_session):
    db_session.add(models.AppEvent(app_id=aid, at=_now() - timedelta(minutes=5), kind="oom", message="m", dedupe_key="a"))
    db_session.add(models.AppDatabase(app_id=aid, db_server_id=1, db_name="d", db_user="u", password_encrypted="x",
                                      size_bytes=49 * 1024 * 1024))
    a = db_session.get(models.App, aid)
    a.status, a.status_reason, a.last_attempt_at, a.error_count, a.error_retryable = "error", "image_pull", _now() - timedelta(days=9), 1, False
    a.want_db = True
    db_session.add(models.DbServer(id=1, name="s", host="h", port=3306, admin_user="u", admin_password_encrypted="x"))
    db_session.commit()
    findings = alice.get(f"/apps/{aid}/advice").json()["findings"]
    order = {"critical": 0, "warning": 1, "info": 2}
    assert [order[f["severity"]] for f in findings] == sorted(order[f["severity"]] for f in findings)
    rules = [f["rule"] for f in findings]
    assert "db.near_quota" in rules and "mem.oom" in rules and "app.stuck_error" not in rules


def test_anonymous_gets_401(client, aid):
    assert client.get(f"/apps/{aid}/advice").status_code == 401


def test_non_member_gets_404_not_403(bob, aid, db_session):
    _oom(db_session, aid)
    r = bob.get(f"/apps/{aid}/advice")
    assert r.status_code == 404 and "mem" not in r.text


def test_unknown_app_404(alice):
    assert alice.get("/apps/99999/advice").status_code == 404


def test_platform_admin_can_read_any_apps_advice(admin, aid):
    assert admin.get(f"/apps/{aid}/advice").status_code == 200


def test_invalid_app_id_is_a_validation_error(alice):
    assert alice.get("/apps/not-a-number/advice").status_code == 422


def test_suspended_and_young_apps_get_no_findings(alice, aid, db_session):
    _oom(db_session, aid)
    a = db_session.get(models.App, aid)
    a.created_at = _now() - timedelta(minutes=10)
    db_session.commit()
    assert alice.get(f"/apps/{aid}/advice").json() == {"findings": []}


def test_metric_samples_drive_near_limit_using_per_pod_maximum(alice, aid, db_session):
    base = _now() - timedelta(minutes=20)
    for i in range(6):  # two pods summed to 700 MB, but each is at 350 MB of 384 (91%)
        db_session.add(models.AppMetricSample(app_id=aid, ts=base + timedelta(minutes=i), cpu_m=20, mem_mi=700, pods=2,
                                              per_pod=json.dumps({"p1": [10, 350], "p2": [10, 350]})))
    db_session.commit()
    rules = [f["rule"] for f in alice.get(f"/apps/{aid}/advice").json()["findings"]]
    assert "mem.near_limit" in rules


def test_summed_usage_over_limit_but_each_pod_fine_does_not_fire(alice, aid, db_session):
    base = _now() - timedelta(minutes=20)
    for i in range(6):
        db_session.add(models.AppMetricSample(app_id=aid, ts=base + timedelta(minutes=i), cpu_m=20, mem_mi=700, pods=2,
                                              per_pod=json.dumps({"p1": [10, 200], "p2": [10, 500 - 200]})))
    db_session.commit()
    assert "mem.near_limit" not in [f["rule"] for f in alice.get(f"/apps/{aid}/advice").json()["findings"]]


# --- admin list ---

def test_admin_list_shape_and_fields(admin, alice, aid, db_session):
    _oom(db_session, aid)
    r = admin.get("/admin/advice")
    assert r.status_code == 200
    row = next(f for f in r.json()["findings"] if f["rule"] == "mem.oom" and f["app_id"] == aid)
    assert row["app_name"] == "demo" and row["owner_email"] == "alice@example.com"
    assert row["project_id"] is not None and row["project_name"]
    assert row["action"] == {"kind": "link", "label": "Open app", "to": f"/apps/{aid}"}
    assert set(row) == {"rule", "severity", "title", "evidence", "action", "app_id", "app_name", "project_id", "project_name", "owner_email"}


def test_admin_list_includes_stuck_error_and_every_app(admin, alice, bob, aid, db_session):
    other = bob.post("/apps", json={**APP, "name": "other"}).json()["id"]
    for i in (aid, other):
        a = db_session.get(models.App, i)
        a.created_at = _now() - timedelta(days=30)
        a.status, a.status_reason, a.error_count, a.error_retryable = "error", "image_pull", 1, False
        a.last_attempt_at = _now() - timedelta(days=9)
    db_session.commit()
    rows = admin.get("/admin/advice").json()["findings"]
    assert {r["app_id"] for r in rows if r["rule"] == "app.stuck_error"} == {aid, other}
    assert {r["owner_email"] for r in rows if r["app_id"] == other} == {"bob@example.com"}


def test_admin_list_query_count_does_not_grow_with_apps(admin, alice, aid, db_session):
    from sqlalchemy import event
    first = db_session.get(models.App, aid)
    engine = db_session.get_bind()
    counts = []
    for n in (1, 6):
        for i in range(n):
            db_session.add(models.App(owner_id=first.owner_id, project_id=first.project_id, name=f"x{n}{i}", hostname=f"x{n}{i}-h",
                                      image="ghcr.io/example/x:latest", port=8080, status="running",
                                      created_at=_now() - timedelta(days=30)))
        db_session.commit()
        stmts = []
        listener = lambda conn, cur, stmt, *a: stmts.append(stmt)  # noqa: E731
        event.listen(engine, "before_cursor_execute", listener)
        try:
            assert admin.get("/admin/advice").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        counts.append(len(stmts))
    assert counts[1] <= counts[0] + 2, counts


def test_admin_list_refuses_normal_user_and_anonymous(alice, client):
    assert alice.get("/admin/advice").status_code == 403
    assert client.get("/admin/advice").status_code == 401


def test_admin_list_skips_suspended_and_deleted_apps(admin, alice, aid, db_session):
    _oom(db_session, aid)
    a = db_session.get(models.App, aid)
    a.status = "suspended"
    db_session.commit()
    assert [r for r in admin.get("/admin/advice").json()["findings"] if r["app_id"] == aid] == []
    a.status, a.deleted_at = "running", _now()
    db_session.commit()
    assert [r for r in admin.get("/admin/advice").json()["findings"] if r["app_id"] == aid] == []


def test_loader_matches_resources_ceiling(db_session, alice, aid):
    a = db_session.get(models.App, aid)
    f = advisor.load_facts(db_session, [a])[aid]
    assert f.ceilings["mem_limit_mi"] == 384 and f.limits["mem_limit_mi"] == 384 and f.floors["mem_limit_mi"] == 64
