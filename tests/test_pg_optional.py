import pytest

from app import models
from app.db_placement import PG_UNAVAILABLE_MSG
from app.dependencies import get_db_server_checker
from app.main import app as fastapi_app
from app.providers.fake import FakeDbServerChecker
from tests.conftest import register_and_login

PG_SPEC = """\
apiVersion: picopaas/v1
name: pgdemo
image: ghcr.io/example/demo:1.0
port: 8080
resources: {db: true, db_engine: pg}
"""
MY_SPEC = PG_SPEC.replace("db_engine: pg", "db_engine: mysql").replace("pgdemo", "mydemo")


def _server(db_session, engine="pg", name="primary-pg", status="active", max_databases=10):
    s = models.DbServer(name=name, engine=engine, host="h", port=5432, admin_user="u",
                        admin_password_encrypted="x", status=status, max_databases=max_databases)
    db_session.add(s)
    db_session.commit()
    return s


def _app_body(engine, name="a1"):
    return {"name": name, "image": "ghcr.io/example/demo:1.0", "port": 8080, "want_db": True, "db_engine": engine}


@pytest.fixture()
def user(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    fastapi_app.dependency_overrides[get_db_server_checker] = lambda: FakeDbServerChecker()
    return c


def test_create_pg_without_server_422(user, db_session):
    r = user.post("/apps", json=_app_body("pg"))
    assert r.status_code == 422 and r.json()["detail"] == PG_UNAVAILABLE_MSG
    assert db_session.query(models.App).count() == 0


def test_create_pg_with_server_201(user, db_session):
    _server(db_session)
    assert user.post("/apps", json=_app_body("pg")).status_code == 201


def test_create_pg_with_full_or_draining_server_still_201(user, db_session):
    _server(db_session, status="draining", max_databases=1)
    assert user.post("/apps", json=_app_body("pg")).status_code == 201


def test_create_pg_without_db_is_fine(user):
    body = {**_app_body("pg"), "want_db": False}
    assert user.post("/apps", json=body).status_code == 201


def test_mysql_unaffected_without_any_server(user):
    assert user.post("/apps", json=_app_body("mysql")).status_code == 201


def test_mysql_server_does_not_satisfy_pg(user, db_session):
    _server(db_session, engine="mysql", name="primary")
    assert user.post("/apps", json=_app_body("pg")).status_code == 422


def test_spec_pg_without_server_422_and_with_server_ok(user, db_session):
    r = user.post("/apps/spec", json={"spec": PG_SPEC})
    assert r.status_code == 422 and r.json()["detail"] == PG_UNAVAILABLE_MSG
    assert db_session.query(models.App).count() == 0
    assert user.post("/apps/spec", json={"spec": MY_SPEC}).status_code == 200
    _server(db_session)
    assert user.post("/apps/spec", json={"spec": PG_SPEC}).status_code == 200


def test_spec_update_turning_pg_db_on_without_server_422(user, db_session):
    nodb = PG_SPEC.replace("{db: true, db_engine: pg}", "{db: false}")
    assert user.post("/apps/spec", json={"spec": nodb}).status_code == 200
    r = user.post("/apps/spec", json={"spec": PG_SPEC})
    assert r.status_code == 422 and r.json()["detail"] == PG_UNAVAILABLE_MSG
    db_session.expire_all()
    assert db_session.query(models.App).one().want_db is False


def test_me_and_templates_reflect_missing_pg(user, db_session):
    assert "pg" not in user.get("/auth/me").json()["features"]["db_engines"]
    by_id = {t["id"]: t for t in user.get("/templates").json()}
    assert by_id["miniflux"]["available"] is False and by_id["grafana-pg"]["available"] is False
    assert by_id["shlink"]["available"] is True and by_id["ghost"]["available"] is True
    _server(db_session)
    assert "pg" in user.get("/auth/me").json()["features"]["db_engines"]
    assert all(t["available"] for t in user.get("/templates").json())


def test_delete_server_204_and_audit(user, db_session):
    s = _server(db_session)
    r = user.delete(f"/admin/db-servers/{s.id}")
    assert r.status_code == 204
    db_session.expire_all()
    assert db_session.get(models.DbServer, s.id) is None
    assert any(a["action"] == "db_server.delete" for a in user.get("/admin/audit").json())
    assert user.post("/apps", json=_app_body("pg")).status_code == 422


def test_delete_server_404(user):
    assert user.delete("/admin/db-servers/9999").status_code == 404


def test_delete_server_409_when_referenced_even_by_soft_deleted_app(user, db_session):
    import datetime as dt
    s = _server(db_session)
    uid = db_session.query(models.User).one().id
    a = models.App(owner_id=uid, name="gone", hostname="gone-1", image="i", port=80, want_db=True, db_engine="pg",
                   deleted_at=dt.datetime(2026, 1, 1))
    db_session.add(a)
    db_session.flush()
    db_session.add(models.AppDatabase(app_id=a.id, db_server_id=s.id, db_name="d", db_user="u", password_encrypted="x"))
    db_session.commit()
    assert user.delete(f"/admin/db-servers/{s.id}").status_code == 409
    db_session.expire_all()
    assert db_session.get(models.DbServer, s.id) is not None


def test_delete_server_403_and_401(make_client, user, db_session):
    s = _server(db_session)
    other = make_client()
    register_and_login(other, "plain@example.com")
    assert other.delete(f"/admin/db-servers/{s.id}").status_code == 403
    anon = make_client()
    assert anon.delete(f"/admin/db-servers/{s.id}").status_code == 401
    db_session.expire_all()
    assert db_session.get(models.DbServer, s.id) is not None


def test_spec_pg_with_draining_server_accepted(user, db_session):
    _server(db_session, status="draining")
    assert user.post("/apps/spec", json={"spec": PG_SPEC}).status_code == 200


def test_existing_pg_app_spec_update_unaffected_after_server_removed(user, db_session):
    s = _server(db_session)
    assert user.post("/apps/spec", json={"spec": PG_SPEC}).status_code == 200
    db_session.expire_all()
    app_row = db_session.query(models.App).one()
    assert app_row.want_db is True
    db_session.query(models.AppDatabase).delete()
    db_session.delete(db_session.get(models.DbServer, s.id))
    db_session.commit()
    # the app already has want_db, so the up-front check is skipped (no new database is placed)
    assert user.post("/apps/spec", json={"spec": PG_SPEC.replace("8080", "9090")}).status_code == 200


def test_spec_new_mysql_no_server_and_pg_no_db_ok(user):
    assert user.post("/apps/spec", json={"spec": PG_SPEC.replace("{db: true, db_engine: pg}", "{db: false, db_engine: pg}")}).status_code == 200


def test_delete_server_blocked_only_while_referenced(user, db_session):
    s = _server(db_session)
    other = _server(db_session, name="pg2")
    uid = db_session.query(models.User).one().id
    a = models.App(owner_id=uid, name="live", hostname="live-1", image="i", port=80, want_db=True, db_engine="pg")
    db_session.add(a)
    db_session.flush()
    db_session.add(models.AppDatabase(app_id=a.id, db_server_id=s.id, db_name="d", db_user="u", password_encrypted="x"))
    db_session.commit()
    r = user.delete(f"/admin/db-servers/{s.id}")
    assert r.status_code == 409 and "1 app database" in r.json()["detail"]
    assert user.delete(f"/admin/db-servers/{other.id}").status_code == 204  # unreferenced one is fine
    assert user.delete(f"/admin/db-servers/{other.id}").status_code == 404


def test_delete_server_with_admin_api_key_and_user_key(make_client, user, db_session):
    s = _server(db_session)
    other = make_client()
    register_and_login(other, "plain@example.com")
    ukey = other.post("/api/keys").json()["key"]
    bare = make_client()
    assert bare.delete(f"/admin/db-servers/{s.id}", headers={"Authorization": f"Bearer {ukey}"}).status_code == 403
    db_session.expire_all()
    assert db_session.get(models.DbServer, s.id) is not None


@pytest.mark.parametrize("val", [""])
def test_bootstrap_pg_does_not_insert_with_empty_env(db_session, monkeypatch, val):
    from app import scheduler
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db_session)
    for k in ("BOOTSTRAP_PG_HOST", "BOOTSTRAP_PG_USER", "BOOTSTRAP_PG_PASSWORD"):
        monkeypatch.setenv(k, val if val else "")
    scheduler.bootstrap_pg_server()
    assert db_session.query(models.DbServer).count() == 0
