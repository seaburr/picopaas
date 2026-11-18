"""Rule-based security alerts (app/security_alerts.py): each rule with seeded, time-shifted rows, a just-under-threshold
negative, merge-on-repeat, new alert after acknowledge, the admin routes, the scheduler job and the migration."""
import os
import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, inspect

from app import config, models, scheduler, security_alerts
from app.kaisen.models import KaisenEvent
from app.main import app as fastapi_app
from tests.conftest import register_and_login, utcnow_naive


NOW = utcnow_naive()


def audit_rows(db, action, n, *, actor_id=None, actor_email=None, target_id=None, start=None, step=timedelta(seconds=10)):
    start = start or NOW - timedelta(minutes=5)
    for i in range(n):
        db.add(models.AuditLog(at=start + step * i, action=action, actor_id=actor_id, actor_email=actor_email,
                               target_type="user", target_id=target_id))
    db.commit()


def alerts(db, kind=None):
    q = db.query(models.SecurityAlert)
    if kind:
        q = q.filter(models.SecurityAlert.kind == kind)
    return q.order_by(models.SecurityAlert.id).all()


def kev(db, project_id, usd, at, user_id=None):
    db.add(KaisenEvent(project_id=project_id, model="m", usd=usd, at=at, user_id=user_id))
    db.commit()


# 1 admin burst
def test_admin_burst_fires_at_threshold(db_session):
    audit_rows(db_session, "user.approve", config.SECURITY_ALERT_ADMIN_BURST_ACTIONS, actor_id=7)
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "admin_burst")
    assert (a.severity, a.subject_type, a.subject_id, a.count) == ("high", "user", "7", 1)
    assert a.evidence["actions"] == config.SECURITY_ALERT_ADMIN_BURST_ACTIONS


def test_admin_burst_under_threshold_and_spread_out(db_session):
    audit_rows(db_session, "user.suspend", config.SECURITY_ALERT_ADMIN_BURST_ACTIONS - 1, actor_id=7)
    audit_rows(db_session, "settings.update", 10, actor_id=8, step=timedelta(minutes=3))  # slow: never 10 in 10 min
    security_alerts.run(db_session, NOW)
    assert alerts(db_session, "admin_burst") == []


def test_admin_burst_cli_actor_has_no_user_subject(db_session):
    audit_rows(db_session, "user.promote", 10, actor_email="manage-cli")
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "admin_burst")
    assert a.subject_type is None and a.evidence["actor"] == "manage-cli"


# 2 outside web flow
@pytest.mark.parametrize("actor", ["manage-cli", "root-management-key"])
def test_privilege_outside_web(db_session, actor):
    audit_rows(db_session, "user.reset_password", 1, actor_email=actor, target_id="5")
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "privilege_outside_web")
    assert (a.severity, a.subject_type, a.subject_id) == ("medium", "user", "5")


def test_promote_by_web_admin_is_not_outside_web(db_session):
    audit_rows(db_session, "user.promote", 1, actor_id=1, target_id="5")
    security_alerts.run(db_session, NOW)
    assert alerts(db_session, "privilege_outside_web") == []


def test_root_auth_failed_medium_then_high(db_session):
    audit_rows(db_session, "root.auth_failed", 1, actor_email="root-management-key")
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "root_auth_failed")
    assert a.severity == "medium"
    audit_rows(db_session, "root.auth_failed", config.SECURITY_ALERT_ROOT_FAIL_HIGH, actor_email="root-management-key",
               start=NOW - timedelta(minutes=2))
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "root_auth_failed")
    assert a.severity == "high" and a.count == 2


# 3 new key then burst
def test_new_key_then_burst(db_session):
    audit_rows(db_session, "apikey.create", 1, actor_id=3, target_id="11", start=NOW - timedelta(minutes=40))
    audit_rows(db_session, "app.update", config.SECURITY_ALERT_NEWKEY_ACTIONS, actor_id=3, start=NOW - timedelta(minutes=35),
               step=timedelta(seconds=20))
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "new_key_burst")
    assert (a.severity, a.subject_type, a.subject_id) == ("medium", "api_key", "11")


def test_new_key_just_under_burst_or_late(db_session):
    audit_rows(db_session, "apikey.create", 1, actor_id=3, target_id="11", start=NOW - timedelta(minutes=60))
    audit_rows(db_session, "app.update", config.SECURITY_ALERT_NEWKEY_ACTIONS - 1, actor_id=3, start=NOW - timedelta(minutes=55))
    audit_rows(db_session, "app.update", 40, actor_id=3, start=NOW - timedelta(minutes=10))  # after the 30 min watch
    security_alerts.run(db_session, NOW)
    assert alerts(db_session, "new_key_burst") == []


def test_new_key_rate_limited(db_session):
    audit_rows(db_session, "apikey.create", 1, actor_id=3, target_id="11", start=NOW - timedelta(minutes=20))
    audit_rows(db_session, "api_key.rate_limited", 1, target_id="3", start=NOW - timedelta(minutes=10))
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "new_key_burst")
    assert a.evidence["rate_limited"] is True


# 4 login pressure
def test_login_pressure_total(db_session):
    audit_rows(db_session, "login.failed", config.SECURITY_ALERT_LOGIN_FAILS, actor_email="x@example.com",
               step=timedelta(seconds=5))
    security_alerts.run(db_session, NOW)
    assert len(alerts(db_session, "login_pressure")) == 1
    assert len(alerts(db_session, "login_lockouts")) == 0  # one email only


def test_login_pressure_under_threshold(db_session):
    audit_rows(db_session, "login.failed", config.SECURITY_ALERT_LOGIN_FAILS - 1, actor_email="x@example.com",
               step=timedelta(seconds=5))
    security_alerts.run(db_session, NOW)
    assert alerts(db_session, "login_pressure") == []


def test_login_lockouts_distinct_emails(db_session):
    for i in range(config.SECURITY_ALERT_LOGIN_LOCKED_EMAILS):
        audit_rows(db_session, "login.failed", config.LOGIN_EMAIL_MAX_FAILS, actor_email=f"u{i}@example.com")
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "login_lockouts")
    assert a.evidence["locked_emails"] == config.SECURITY_ALERT_LOGIN_LOCKED_EMAILS
    assert "example.com" not in str(a.evidence) and "example.com" not in a.message


def test_login_lockouts_under_threshold(db_session):
    for i in range(config.SECURITY_ALERT_LOGIN_LOCKED_EMAILS - 1):
        audit_rows(db_session, "login.failed", config.LOGIN_EMAIL_MAX_FAILS, actor_email=f"u{i}@example.com")
    security_alerts.run(db_session, NOW)
    assert alerts(db_session, "login_lockouts") == []


# 5 invalid API keys
def _throttle(db, count, minutes_ago=1):
    ws = NOW - timedelta(minutes=minutes_ago)
    db.add(models.AuthThrottle(key=config.SECURITY_ALERT_INVALID_KEY_PREFIX + ws.strftime("%Y%m%d%H%M"),
                               window_start=ws, count=count))
    db.commit()


def test_invalid_keys_fire_and_skip(db_session):
    security_alerts.run(db_session, NOW)  # no rows at all: skipped
    assert alerts(db_session) == []
    _throttle(db_session, config.SECURITY_ALERT_INVALID_KEY_COUNT)
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "invalid_api_keys")
    assert a.severity == "medium" and a.evidence["invalid_keys"] == config.SECURITY_ALERT_INVALID_KEY_COUNT


def test_invalid_keys_under_threshold(db_session):
    _throttle(db_session, config.SECURITY_ALERT_INVALID_KEY_COUNT - 1)
    security_alerts.run(db_session, NOW)
    assert alerts(db_session) == []


# 6 Kaisen spend
def test_kaisen_spike_vs_baseline(db_session):
    day0 = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(1, 15):
        kev(db_session, 1, 0.1, day0 - timedelta(days=d, hours=-1))
    kev(db_session, 1, 2.5, day0 + timedelta(seconds=1))  # 2.5 >= 3 x 0.1 and >= $2 (hour spend stays under $5)
    security_alerts.run(db_session, day0 + timedelta(hours=2))
    [a] = alerts(db_session, "kaisen_spend_spike")
    assert (a.subject_type, a.subject_id, a.severity) == ("project", "1", "medium")


def test_kaisen_spike_under_threshold(db_session):
    day0 = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(1, 15):
        kev(db_session, 1, 1.0, day0 - timedelta(days=d, hours=-1))  # avg 1.0, today 2.5 < 3.0
    kev(db_session, 2, 1.9, day0 + timedelta(seconds=1))  # under the $2 floor
    kev(db_session, 1, 2.5, day0 + timedelta(seconds=1))
    security_alerts.run(db_session, day0 + timedelta(hours=2))
    assert alerts(db_session, "kaisen_spend_spike") == []


def test_kaisen_single_hour(db_session):
    h = (NOW - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)  # one fixed clock hour, never straddles
    kev(db_session, 3, 2.6, h + timedelta(minutes=1))
    kev(db_session, 3, 2.6, h + timedelta(minutes=2))
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "kaisen_hour_spend")
    assert (a.severity, a.subject_id) == ("medium", "3") and a.evidence["hour_usd"] == 5.2


def test_kaisen_single_hour_under(db_session):
    h = (NOW - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    kev(db_session, 3, 2.4, h + timedelta(minutes=1))
    kev(db_session, 3, 2.4, h + timedelta(minutes=2))
    security_alerts.run(db_session, NOW)
    assert alerts(db_session, "kaisen_hour_spend") == []


def test_kaisen_non_owner_member_spend(db_session):
    db_session.add(models.ProjectMember(project_id=1, user_id=1, role="owner"))
    db_session.commit()
    h = (NOW - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    for uid in (1, 2):  # owner spend 5.0 does not count as a member alert; member spend 5.0 does
        kev(db_session, 1, 5.0, h + timedelta(minutes=uid), user_id=uid)
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "kaisen_member_spend")
    assert a.evidence["member_user_id"] == 2


def test_kaisen_owner_only_no_member_alert(db_session):
    db_session.add(models.ProjectMember(project_id=1, user_id=1, role="owner"))
    db_session.commit()
    h = (NOW - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    kev(db_session, 1, 5.0, h + timedelta(minutes=1), user_id=1)
    security_alerts.run(db_session, NOW)
    assert alerts(db_session, "kaisen_member_spend") == []


# 7 mass account changes
def test_mass_account_changes(db_session):
    audit_rows(db_session, "user.approve", 10, actor_id=1, step=timedelta(minutes=1), start=NOW - timedelta(minutes=30))
    audit_rows(db_session, "user.delete", 10, actor_id=1, step=timedelta(minutes=1), start=NOW - timedelta(minutes=20))
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "mass_account_changes")
    assert a.severity == "high" and a.evidence["changes"] == 20


def test_mass_account_changes_under(db_session):
    audit_rows(db_session, "user.approve", config.SECURITY_ALERT_MASS_ACCOUNT_CHANGES - 1, actor_id=1,
               step=timedelta(minutes=1), start=NOW - timedelta(minutes=30))
    security_alerts.run(db_session, NOW)
    assert alerts(db_session, "mass_account_changes") == []


# merge / acknowledge / purge
def test_repeat_merges_then_new_alert_after_ack(db_session):
    audit_rows(db_session, "user.approve", 10, actor_id=7, start=NOW - timedelta(minutes=30))
    security_alerts.run(db_session, NOW - timedelta(minutes=20))
    security_alerts.run(db_session, NOW - timedelta(minutes=19))  # same evidence: no inflation
    [a] = alerts(db_session, "admin_burst")
    assert a.count == 1
    audit_rows(db_session, "user.approve", 3, actor_id=7, start=NOW - timedelta(minutes=25))
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "admin_burst")
    assert a.count == 2 and a.last_seen > a.first_seen
    admin = models.User(email="ack@example.com", password_hash="x", role="admin", status="active")
    db_session.add(admin)
    db_session.commit()
    security_alerts.acknowledge(db_session, a, admin)
    security_alerts.run(db_session, NOW)  # same evidence, acknowledged: stays quiet
    assert len(alerts(db_session, "admin_burst")) == 1
    audit_rows(db_session, "user.approve", 10, actor_id=7, start=NOW - timedelta(minutes=3))
    security_alerts.run(db_session, NOW)
    rows = alerts(db_session, "admin_burst")
    assert len(rows) == 2 and rows[1].acknowledged_at is None and rows[1].count == 1


def test_purge_old_acknowledged_and_cap(db_session, monkeypatch):
    old = NOW - timedelta(days=config.SECURITY_ALERT_ACK_RETENTION_DAYS + 1)
    for i in range(3):
        db_session.add(models.SecurityAlert(kind="k", severity="low", message="m", first_seen=NOW, last_seen=NOW - timedelta(minutes=i),
                                            count=1, dedupe_key=f"k{i}"))
    db_session.add(models.SecurityAlert(kind="k", severity="low", message="m", first_seen=old, last_seen=old, count=1,
                                        dedupe_key="old#ack9", acknowledged_at=old))
    db_session.commit()
    monkeypatch.setattr(config, "SECURITY_ALERT_MAX_ROWS", 2)
    security_alerts.run(db_session, NOW)
    keys = {a.dedupe_key for a in alerts(db_session)}
    assert keys == {"k0", "k1"}  # old ack purged, oldest open row dropped by the cap


def test_cap_evicts_low_severity_before_open_high(db_session, monkeypatch):
    for key, sev, mins in (("hi", "high", 500), ("lo1", "low", 1), ("lo2", "low", 2)):
        db_session.add(models.SecurityAlert(kind="k", severity=sev, message="m", first_seen=NOW,
                                            last_seen=NOW - timedelta(minutes=mins), count=1, dedupe_key=key))
    db_session.commit()
    monkeypatch.setattr(config, "SECURITY_ALERT_MAX_ROWS", 2)
    security_alerts.run(db_session, NOW)
    assert "hi" in {a.dedupe_key for a in alerts(db_session)}  # oldest, but high: survives


def test_new_key_rule_ignores_users_without_a_new_key(db_session):
    audit_rows(db_session, "app.update", 60, actor_id=77)
    assert security_alerts.rule_new_key_burst(db_session, NOW) == []


def test_rule_failure_is_isolated(db_session, monkeypatch):
    def boom(db, now):
        raise RuntimeError("x")
    monkeypatch.setattr(security_alerts, "RULES", (boom, security_alerts.rule_mass_account_changes))
    audit_rows(db_session, "user.approve", 20, actor_id=1)
    security_alerts.run(db_session, NOW)
    assert len(alerts(db_session, "mass_account_changes")) == 1


# job
def test_job_runs_for_leader_only(db_session, monkeypatch):
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    audit_rows(db_session, "user.approve", 20, actor_id=1)
    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: False)
    scheduler.security_alerts_job()
    assert alerts(db_session) == []
    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: True)
    scheduler.security_alerts_job()
    assert alerts(db_session, "mass_account_changes")


# routes
@pytest.fixture()
def web(make_client, db_session):
    admin, other = make_client(), make_client()
    register_and_login(admin, "admin@example.com")
    register_and_login(other, "other@example.com")
    db_session.add(models.SecurityAlert(kind="admin_burst", severity="high", subject_type="user", subject_id="2",
                                        message="m", evidence={"actions": 10}, first_seen=NOW, last_seen=NOW, count=1,
                                        dedupe_key="admin_burst:user:2"))
    db_session.commit()
    return admin, other, make_client()


def test_list_alerts_and_status_filter(web, db_session):
    admin, _, _ = web
    r = admin.get("/admin/security-alerts")
    assert r.status_code == 200
    [row] = r.json()
    assert row["kind"] == "admin_burst" and row["evidence"] == {"actions": 10} and row["acknowledged_at"] is None
    assert admin.get("/admin/security-alerts/summary").json() == {"open": 1}
    assert admin.post(f"/admin/security-alerts/{row['id']}/acknowledge").status_code == 200
    assert admin.get("/admin/security-alerts").json() == []
    assert len(admin.get("/admin/security-alerts?status=all").json()) == 1
    assert admin.get("/admin/security-alerts/summary").json() == {"open": 0}


def test_acknowledge_persists_and_audits(web, db_session):
    admin, _, _ = web
    a = db_session.query(models.SecurityAlert).one()
    r = admin.post(f"/admin/security-alerts/{a.id}/acknowledge")
    assert r.status_code == 200 and r.json()["acknowledged_by"] is not None
    db_session.refresh(a)
    assert a.acknowledged_at is not None and a.dedupe_key.endswith(f"#ack{a.id}")
    assert db_session.query(models.AuditLog).filter_by(action="security_alert.acknowledge", target_id=str(a.id)).count() == 1


def test_acknowledge_unknown_and_bad_status(web):
    admin, _, _ = web
    assert admin.post("/admin/security-alerts/9999/acknowledge").status_code == 404
    assert admin.get("/admin/security-alerts?status=bogus").status_code == 422
    assert admin.post("/admin/security-alerts/abc/acknowledge").status_code == 422


def test_anonymous_gets_401(web):
    _, _, anon = web
    assert anon.get("/admin/security-alerts").status_code == 401
    assert anon.post("/admin/security-alerts/1/acknowledge").status_code == 401


def test_non_admin_gets_403_and_cannot_acknowledge(web, db_session):
    _, other, _ = web
    assert other.get("/admin/security-alerts").status_code == 403
    assert other.post("/admin/security-alerts/1/acknowledge").status_code == 403
    assert db_session.query(models.SecurityAlert).one().acknowledged_at is None


def test_api_key_refused_even_for_admin(web, db_session):
    admin, _, anon = web
    key = admin.post("/api/keys").json()["key"]
    h = {"Authorization": f"Bearer {key}"}
    assert anon.get("/admin/security-alerts", headers=h).status_code == 403
    assert anon.post("/admin/security-alerts/1/acknowledge", headers=h).status_code == 403
    assert db_session.query(models.SecurityAlert).one().acknowledged_at is None


def test_health_reports_open_count(web):
    admin, _, _ = web
    from app.dependencies import get_kube
    from app.providers.fake import FakeKubeProvider
    fastapi_app.dependency_overrides[get_kube] = lambda: FakeKubeProvider()
    r = admin.get("/admin/health")
    assert r.status_code == 200 and r.json()["security_alerts_open"] == 1


# migration
def test_migration_up_down_up(tmp_path):
    url = f"sqlite:///{tmp_path}/m.db"
    root = os.path.join(os.path.dirname(__file__), "..")
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True, capture_output=True)

    def has():
        return "security_alerts" in inspect(create_engine(url)).get_table_names()
    alembic("upgrade", "head")
    assert has()
    alembic("downgrade", "c9e4b7a2d6f1")
    assert not has()
    alembic("upgrade", "head")
    assert has()
