"""Security review 2026-11-11: the rejected-status migration must never turn a once-active account into 'rejected'."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import models


def _run_upgrade(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "a8d2f4c6e1b3_user_status_rejected.py"
    spec = importlib.util.spec_from_file_location("mig_rejected_sec", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with Operations.context(MigrationContext.configure(db_session.connection())):
        mod.upgrade()
    db_session.expire_all()


def test_migration_skips_demoted_admin_and_unaudited_activations(db_session):
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def user(email, role="user"):
        u = models.User(email=email, password_hash="x", role=role, status="deleted", created_at=now - timedelta(days=1))
        db_session.add(u)
        db_session.flush()
        return u

    def audit(action, u, detail=None):
        db_session.add(models.AuditLog(action=action, target_type="user", target_id=str(u.id), detail=detail))

    signup = user("s@example.com")
    audit("user.register", signup, "status=pending")
    ex_admin = user("x@example.com")           # first user (registered active), later demoted, then deleted
    audit("user.register", ex_admin, "status=active")
    audit("user.demote", ex_admin)
    promoted = user("p@example.com")           # pending signup, approve audit write was lost, later promoted
    audit("user.register", promoted, "status=pending")
    audit("user.promote", promoted)
    no_trail = user("n@example.com")           # no register row at all: no proof, left alone
    db_session.commit()
    _run_upgrade(db_session)
    st = lambda u: db_session.get(models.User, u.id).status  # noqa: E731
    assert [st(u) for u in (signup, ex_admin, promoted, no_trail)] == ["rejected", "deleted", "deleted", "deleted"]


def test_login_unknown_email_still_runs_bcrypt(make_client, monkeypatch):
    from app.routers import auth
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda p, h: (calls.append(1), real(p, h))[1])
    r = make_client().post("/auth/login", json={"email": "nobody@example.com", "password": "whatever-123456"})
    assert r.status_code == 401 and calls == [1]
