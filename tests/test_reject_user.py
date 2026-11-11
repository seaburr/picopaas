import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import models
from app.config import AUDIT_RETENTION_DAYS
from tests.conftest import DEFAULT_PASSWORD, register_and_login

MSG = "Your access request wasn't approved. Contact the platform administrator if you think this is a mistake."


def _setup(make_client):
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    victim = make_client()
    uid = register_and_login(victim, "rej@example.com", approve=False).json()["id"]
    return admin, victim, uid


def _reject(admin, uid):
    return admin.post(f"/admin/users/{uid}/reject")


def test_reject_sets_status_audits_and_revokes_keys(make_client, db_session):
    admin, victim, uid = _setup(make_client)
    db_session.add(models.ApiKey(user_id=uid, hashed="g" * 64, prefix="pp_sk_yy", name="k", scope="full"))
    db_session.commit()
    assert _reject(admin, uid).status_code == 204
    db_session.expire_all()
    user = db_session.get(models.User, uid)
    assert user.status == "rejected"
    proj = db_session.query(models.Project).filter_by(slug=f"personal-{uid}").one()
    assert proj.deleted_at is None  # same as delete: the (app-less) personal project is left as is
    assert db_session.query(models.ApiKey).filter_by(user_id=uid, revoked_at=None).count() == 0
    assert db_session.query(models.AuditLog).filter_by(action="user.reject", target_id=str(uid)).count() == 1


def test_reject_requires_admin(make_client):
    admin, victim, uid = _setup(make_client)
    assert make_client().post(f"/admin/users/{uid}/reject").status_code == 401
    assert victim.post(f"/admin/users/{uid}/reject").status_code == 403


def test_reject_unknown_user_404(make_client):
    admin, _, _ = _setup(make_client)
    assert _reject(admin, 99999).status_code == 404


def test_reject_non_pending_is_409(make_client):
    admin, victim, uid = _setup(make_client)
    assert admin.post(f"/admin/users/{uid}/approve").status_code == 200
    assert _reject(admin, uid).status_code == 409
    assert _reject(admin, uid).status_code == 409


def test_reject_twice_is_409(make_client):
    admin, _, uid = _setup(make_client)
    assert _reject(admin, uid).status_code == 204
    assert _reject(admin, uid).status_code == 409


def test_reject_platform_admin_refused(make_client, db_session):
    admin, _, _ = _setup(make_client)
    me = admin.get("/auth/me").json()["id"]
    assert _reject(admin, me).status_code in (400, 409)
    assert db_session.get(models.User, me).status == "active"


def test_login_rejected_correct_password_403_no_cookie(make_client):
    admin, _, uid = _setup(make_client)
    _reject(admin, uid)
    c = make_client()
    r = c.post("/auth/login", json={"email": "rej@example.com", "password": DEFAULT_PASSWORD})
    assert r.status_code == 403 and r.json()["detail"] == MSG
    assert "set-cookie" not in r.headers
    assert c.get("/auth/me").status_code == 401


def test_login_rejected_wrong_password_is_generic_401(make_client):
    admin, _, uid = _setup(make_client)
    _reject(admin, uid)
    c = make_client()
    bad = c.post("/auth/login", json={"email": "rej@example.com", "password": "not-the-password-x"})
    unknown = c.post("/auth/login", json={"email": "nobody@example.com", "password": "not-the-password-x"})
    assert bad.status_code == unknown.status_code == 401
    assert bad.json() == unknown.json()
    assert MSG not in bad.text


def test_old_session_of_rejected_user_refused(make_client):
    admin, victim, uid = _setup(make_client)
    assert victim.get("/auth/me").status_code == 200
    _reject(admin, uid)
    assert victim.get("/auth/me").status_code == 401
    assert victim.get("/apps").status_code == 401


def test_api_key_of_rejected_user_refused(make_client, db_session):
    admin, victim, uid = _setup(make_client)
    from app.security import generate_api_key, hash_api_key
    key = generate_api_key()
    h = hash_api_key(key)
    db_session.add(models.ApiKey(user_id=uid, hashed=h, prefix=key[:8], name="k", scope="full"))
    db_session.commit()
    c = make_client()
    hdr = {"Authorization": f"Bearer {key}"}
    assert c.get("/auth/me", headers=hdr).status_code == 200
    _reject(admin, uid)
    assert c.get("/auth/me", headers=hdr).status_code == 401


def test_register_with_rejected_email_stays_rejected(make_client, db_session):
    admin, _, uid = _setup(make_client)
    _reject(admin, uid)
    c = make_client()
    r = c.post("/auth/register", json={"email": "rej@example.com", "password": "not-the-password-y", "accept_terms": True})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    assert c.get("/auth/me").status_code == 401
    db_session.expire_all()
    assert db_session.get(models.User, uid).status == "rejected"
    assert c.post("/auth/login", json={"email": "rej@example.com", "password": DEFAULT_PASSWORD}).status_code == 403


def test_admin_list_shows_rejected_and_counts_ignore_it(make_client):
    admin, _, uid = _setup(make_client)
    before = admin.get("/admin/health").json()["users"]
    assert before["pending"] == 1
    _reject(admin, uid)
    rows = {u["id"]: u for u in admin.get("/admin/users").json()}
    assert rows[uid]["status"] == "rejected"
    after = admin.get("/admin/health").json()["users"]
    assert after["pending"] == 0 and after["total"] == before["total"] - 1
    assert all(p["id"] != uid for p in admin.get("/admin/health").json()["pending_users"])


def test_admin_can_still_delete_rejected(make_client, db_session):
    admin, _, uid = _setup(make_client)
    _reject(admin, uid)
    assert admin.delete(f"/admin/users/{uid}").status_code == 204
    assert db_session.get(models.User, uid).status == "deleted"


def test_suspend_rejected_is_409(make_client):
    admin, _, uid = _setup(make_client)
    _reject(admin, uid)
    assert admin.post(f"/admin/users/{uid}/suspend", json={"reason": "no"}).status_code == 409


def test_approve_rejected_unlocks_with_working_personal_project(make_client, db_session):
    admin, _, uid = _setup(make_client)
    _reject(admin, uid)
    assert make_client().post("/auth/login", json={"email": "rej@example.com", "password": DEFAULT_PASSWORD}).status_code == 403
    r = admin.post(f"/admin/users/{uid}/approve")
    assert r.status_code == 200 and r.json()["status"] == "active"
    c = make_client()
    assert c.post("/auth/login", json={"email": "rej@example.com", "password": DEFAULT_PASSWORD}).status_code == 200
    projs = c.get("/projects")
    assert projs.status_code == 200
    assert any(p.get("is_personal") for p in projs.json())
    log = db_session.query(models.AuditLog).filter_by(action="user.approve", target_id=str(uid)).one()
    assert log.detail == "from=rejected"


def test_approve_rejected_creates_fresh_project_if_gone(make_client, db_session):
    admin, _, uid = _setup(make_client)
    _reject(admin, uid)
    p = db_session.query(models.Project).filter_by(slug=f"personal-{uid}").one()
    db_session.delete(p)
    db_session.commit()
    assert admin.post(f"/admin/users/{uid}/approve").status_code == 200
    assert db_session.query(models.Project).filter_by(slug=f"personal-{uid}", deleted_at=None).count() == 1


def test_approve_keeps_api_keys_revoked(make_client, db_session):
    admin, victim, uid = _setup(make_client)
    db_session.add(models.ApiKey(user_id=uid, hashed="h" * 64, prefix="pp_sk_zz", name="k", scope="full"))
    db_session.commit()
    _reject(admin, uid)
    admin.post(f"/admin/users/{uid}/approve")
    db_session.expire_all()
    assert db_session.query(models.ApiKey).filter_by(user_id=uid, revoked_at=None).count() == 0


def _run_migration(db_session, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "a8d2f4c6e1b3_user_status_rejected.py"
    spec = importlib.util.spec_from_file_location("mig_rejected", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    conn = db_session.connection()
    with Operations.context(MigrationContext.configure(conn)):
        getattr(mod, fn)()
    db_session.expire_all()


def test_migration_converts_only_never_approved_deleted_users(db_session):
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def user(email, status, role="user", age_days=1):
        u = models.User(email=email, password_hash="x", role=role, status=status, created_at=now - timedelta(days=age_days))
        db_session.add(u)
        db_session.flush()
        return u

    rejected = user("a@example.com", "deleted")
    approved_then_deleted = user("b@example.com", "deleted")
    old = user("c@example.com", "deleted", age_days=AUDIT_RETENTION_DAYS + 5)
    admin = user("d@example.com", "deleted", role="platform_admin")
    pending = user("e@example.com", "pending")
    active = user("f@example.com", "active")
    db_session.add(models.AuditLog(action="user.approve", target_type="user", target_id=str(approved_then_deleted.id)))
    for u in (rejected, approved_then_deleted, old, admin, pending, active):
        db_session.add(models.AuditLog(action="user.register", target_type="user", target_id=str(u.id), detail="status=pending"))
    db_session.commit()
    _run_migration(db_session, "upgrade")
    st = lambda u: db_session.get(models.User, u.id).status  # noqa: E731
    assert [st(u) for u in (rejected, approved_then_deleted, old, admin, pending, active)] == [
        "rejected", "deleted", "deleted", "deleted", "pending", "active"]
    _run_migration(db_session, "downgrade")
    assert st(rejected) == "deleted"


def test_approve_restores_soft_deleted_personal_project(make_client, db_session):
    admin, _, uid = _setup(make_client)
    _reject(admin, uid)
    p = db_session.query(models.Project).filter_by(slug=f"personal-{uid}").one()
    p.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    for m in list(p.members):
        db_session.delete(m)
    db_session.commit()
    assert admin.post(f"/admin/users/{uid}/approve").status_code == 200
    db_session.expire_all()
    p = db_session.query(models.Project).filter_by(slug=f"personal-{uid}").one()
    assert p.deleted_at is None and [m.user_id for m in p.members] == [uid]
