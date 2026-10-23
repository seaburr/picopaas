"""Audit retention (purge_old + scheduler job) and the streamed export."""
import csv
import io
from datetime import datetime, timedelta

import pytest

from app import audit, config, models, scheduler
from app.routers import admin as admin_router

from tests.conftest import register_and_login

NOW = datetime(2026, 10, 23, 12, 0, 0)


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


def _add(db, n, age_days, action="seed", now=NOW):
    for i in range(n):
        db.add(models.AuditLog(at=now - timedelta(days=age_days), actor_email="a@example.com", action=action))
    db.commit()


def test_purge_deletes_old_keeps_recent_and_writes_row(db_session):
    _add(db_session, 3, 120, "old")
    _add(db_session, 2, 10, "recent")
    assert audit.purge_old(db_session, 90, now=NOW) == 3
    actions = [r.action for r in db_session.query(models.AuditLog).order_by(models.AuditLog.id)]
    assert actions == ["recent", "recent", "audit.purge"]
    row = db_session.query(models.AuditLog).filter_by(action="audit.purge").one()
    assert row.actor_email == "system" and row.actor_id is None
    assert row.detail == f"cutoff={(NOW - timedelta(days=90)).isoformat()} rows=3"


def test_purge_batches(db_session):
    _add(db_session, 7, 200)
    assert audit.purge_old(db_session, 90, now=NOW, batch=3) == 7
    assert db_session.query(models.AuditLog).filter(models.AuditLog.action == "seed").count() == 0


def test_purge_zero_or_negative_is_noop(db_session):
    _add(db_session, 2, 500)
    assert audit.purge_old(db_session, 0, now=NOW) == 0
    assert audit.purge_old(db_session, -5, now=NOW) == 0
    assert db_session.query(models.AuditLog).count() == 2


def test_purge_nothing_to_delete_writes_no_row(db_session):
    _add(db_session, 2, 1)
    assert audit.purge_old(db_session, 90, now=NOW) == 0
    assert db_session.query(models.AuditLog).filter_by(action="audit.purge").count() == 0


def test_default_retention_is_90():
    assert config.AUDIT_RETENTION_DAYS == 90


def test_job_leader_only_and_disabled(db_session, monkeypatch):
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    _add(db_session, 2, 400, now=datetime.utcnow())
    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: False)
    scheduler.audit_purge_job()
    assert db_session.query(models.AuditLog).filter_by(action="seed").count() == 2
    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: True)
    monkeypatch.setattr(config, "AUDIT_RETENTION_DAYS", 0)
    scheduler.audit_purge_job()
    assert db_session.query(models.AuditLog).filter_by(action="seed").count() == 2
    monkeypatch.setattr(config, "AUDIT_RETENTION_DAYS", 90)
    scheduler.audit_purge_job()
    assert db_session.query(models.AuditLog).filter_by(action="seed").count() == 0
    assert db_session.query(models.AuditLog).filter_by(action="audit.purge").count() == 1


def test_export_stream_batches_cap_order_and_audit(admin, db_session, monkeypatch):
    monkeypatch.setattr(admin_router, "_EXPORT_BATCH", 4)
    monkeypatch.setattr(admin_router, "AUDIT_EXPORT_MAX", 10)
    base = db_session.query(models.AuditLog).count()
    for i in range(25):
        db_session.add(models.AuditLog(actor_email="a@example.com", action=f"act{i}", detail="=x" if i == 24 else "ok"))
    db_session.commit()
    top = db_session.query(models.AuditLog).order_by(models.AuditLog.id.desc()).first().id
    r = admin.get("/admin/audit/export")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["id", "at", "actor_email", "action", "target_type", "target_id", "detail"]
    assert len(rows) == 11
    assert [int(x[0]) for x in rows[1:]] == list(range(top, top - 10, -1))
    assert rows[1][3] == "act24" and rows[1][6] == "'=x"
    last = db_session.query(models.AuditLog).filter_by(action="audit.export").one()
    assert last.detail == "format=csv rows=10" and last.id > top
    assert base >= 0


def test_export_json_is_array_with_same_columns(admin, db_session):
    db_session.add(models.AuditLog(actor_email="a@example.com", action="=raw", detail=None))
    db_session.commit()
    r = admin.get("/admin/audit/export?format=json")
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert isinstance(data, list)
    assert set(data[0]) == {"id", "at", "actor_email", "action", "target_type", "target_id", "detail"}
    assert data[0]["action"] == "=raw"
    ids = [d["id"] for d in data]
    assert ids == sorted(ids, reverse=True)


def test_export_empty_json_is_valid(admin, db_session, monkeypatch):
    out = "".join(admin_router._audit_export_stream(db_session.get_bind(), 0, 10, "json"))
    assert out == "[]"
