"""app/reconciler.py — fakes only, plain SQLite session (no HTTP layer)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crypto, models
from app.db import Base
from app.providers.base import MySQLAdminError, ObjectStoreError
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import delete_app, reconcile_all, reconcile_app


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _make_server(session, name="primary", max_databases=25, status="active") -> models.DbServer:
    server = models.DbServer(
        name=name,
        host="mysql.example.internal",
        port=3306,
        admin_user="root",
        admin_password_encrypted=crypto.encrypt("s3cret"),
        max_databases=max_databases,
        status=status,
    )
    session.add(server)
    session.commit()
    return server


def _make_app(session, want_db=False, want_storage=False, env=None) -> models.App:
    owner = models.User(email=f"u{models.User.__tablename__}{uuid.uuid4().hex}@example.com", password_hash="x")
    session.add(owner)
    session.commit()
    app = models.App(
        owner_id=owner.id,
        name="demo",
        hostname=f"demo-{owner.id}",
        image="example/demo:latest",
        port=8080,
        want_db=want_db,
        want_storage=want_storage,
    )
    for key, value, is_secret in env or []:
        app.env_vars.append(
            models.AppEnvVar(key=key, value_encrypted=crypto.encrypt(value), is_secret=is_secret)
        )
    session.add(app)
    session.commit()
    return app


class _FailingMySQLAdmin(FakeMySQLAdmin):
    def create_db_user(self, app_id: int):
        raise MySQLAdminError("connection refused: user=root password=s3cret-admin-pw")


class _FailingObjectStore(FakeObjectStore):
    def create_bucket_and_key(self, app_id: int):
        raise ObjectStoreError("spaces auth failed: secret=do-super-secret-key")


def _providers():
    return {
        "kube": FakeKubeProvider(),
        "mysql": FakeMySQLAdmin(),
        "store": FakeObjectStore(),
    }


def test_happy_path_db_and_storage_resolves_env_and_deploys(session):
    server = _make_server(session)
    kube = FakeKubeProvider()
    mysql = FakeMySQLAdmin()
    store = FakeObjectStore()
    app = _make_app(
        session,
        want_db=True,
        want_storage=True,
        env=[("DATABASE_URL", "${{ db.url }}", True), ("BUCKET", "${{ storage.bucket }}", False)],
    )

    reconcile_app(
        session,
        app,
        kube=kube,
        mysql_admin_factory=lambda s: mysql,
        object_store=store,
        settings={"mysql_cidr": "10.1.0.0/16"},
    )

    assert app.status == "running"
    assert app.status_detail is None
    assert app.database is not None
    assert app.database.db_server_id == server.id
    assert app.storage is not None

    namespace = f"app-{app.id}"
    secret = next(m for m in kube.applied[namespace] if m["kind"] == "Secret")
    resolved_db_url = secret["stringData"]["DATABASE_URL"]
    assert resolved_db_url.startswith("mysql://app_")
    assert "${{" not in resolved_db_url
    # The real password (not a placeholder) made it into the rendered secret.
    stored_pw = crypto.decrypt(app.database.password_encrypted)
    assert stored_pw in resolved_db_url
    assert secret["stringData"]["BUCKET"] == f"pp-{app.id}"


def test_placement_chooses_least_loaded_server(session):
    full_ish = _make_server(session, "a", max_databases=25)
    roomy = _make_server(session, "b", max_databases=25)
    for i in range(20):
        session.add(
            models.AppDatabase(
                app_id=1000 + i,
                db_server_id=full_ish.id,
                db_name=f"app_{1000+i}",
                db_user=f"app_{1000+i}",
                password_encrypted=crypto.encrypt("pw"),
            )
        )
    session.commit()

    app = _make_app(session, want_db=True)
    reconcile_app(
        session,
        app,
        kube=FakeKubeProvider(),
        mysql_admin_factory=lambda s: FakeMySQLAdmin(s),
        object_store=FakeObjectStore(),
        settings={},
    )

    assert app.database.db_server_id == roomy.id


def test_no_db_capacity_sets_error_status(session):
    app = _make_app(session, want_db=True)  # no DbServer rows at all
    reconcile_app(
        session,
        app,
        kube=FakeKubeProvider(),
        mysql_admin_factory=lambda s: FakeMySQLAdmin(s),
        object_store=FakeObjectStore(),
        settings={},
    )
    assert app.status == "error"
    assert app.status_detail == "no database capacity"
    assert app.database is None


def test_idempotent_second_reconcile_does_not_reprovision(session):
    _make_server(session)
    kube = FakeKubeProvider()
    mysql = FakeMySQLAdmin()
    store = FakeObjectStore()
    app = _make_app(session, want_db=True, want_storage=True)

    for _ in range(2):
        reconcile_app(
            session,
            app,
            kube=kube,
            mysql_admin_factory=lambda s: mysql,
            object_store=store,
            settings={},
        )

    assert app.status == "running"
    assert session.query(models.AppDatabase).filter_by(app_id=app.id).count() == 1
    assert session.query(models.AppStorage).filter_by(app_id=app.id).count() == 1
    assert len(mysql.creds) == 1
    assert len(store.creds) == 1


def test_provider_failure_sets_error_without_leaking_secrets(session):
    _make_server(session)
    app = _make_app(session, want_db=True)
    reconcile_app(
        session,
        app,
        kube=FakeKubeProvider(),
        mysql_admin_factory=lambda s: _FailingMySQLAdmin(s),
        object_store=FakeObjectStore(),
        settings={},
    )
    assert app.status == "error"
    assert app.status_detail == "database provisioning failed"
    assert "s3cret" not in app.status_detail
    assert "password" not in app.status_detail

    _make_server(session, name="b")
    app2 = _make_app(session, want_storage=True)
    reconcile_app(
        session,
        app2,
        kube=FakeKubeProvider(),
        mysql_admin_factory=lambda s: FakeMySQLAdmin(s),
        object_store=_FailingObjectStore(),
        settings={},
    )
    assert app2.status == "error"
    assert app2.status_detail == "storage provisioning failed"
    assert "secret" not in app2.status_detail.lower().replace("secret_", "")


def test_deletion_cleans_up_db_storage_and_namespace(session):
    _make_server(session)
    kube = FakeKubeProvider()
    mysql = FakeMySQLAdmin()
    store = FakeObjectStore()
    app = _make_app(session, want_db=True, want_storage=True)
    reconcile_app(
        session, app, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store, settings={}
    )
    assert app.status == "running"

    from datetime import datetime, timezone

    app.deleted_at = datetime.now(timezone.utc)
    session.commit()

    delete_app(session, app, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store)

    assert app.status == "deleted"
    namespace = f"app-{app.id}"
    assert namespace in kube.deleted
    assert app.id not in mysql.creds
    assert app.id not in store.creds
    assert session.query(models.AppDatabase).filter_by(app_id=app.id).count() == 0
    assert session.query(models.AppStorage).filter_by(app_id=app.id).count() == 0


def test_reconcile_all_sweeps_pending_and_deleted_apps(session):
    _make_server(session)
    kube = FakeKubeProvider()
    mysql = FakeMySQLAdmin()
    store = FakeObjectStore()

    pending_app = _make_app(session, want_db=True)
    to_delete_app = _make_app(session)
    reconcile_app(
        session,
        to_delete_app,
        kube=kube,
        mysql_admin_factory=lambda s: mysql,
        object_store=store,
        settings={},
    )
    from datetime import datetime, timezone

    to_delete_app.deleted_at = datetime.now(timezone.utc)
    session.commit()

    reconcile_all(
        session,
        kube=kube,
        mysql_admin_factory=lambda s: mysql,
        object_store=store,
        settings={},
    )

    assert pending_app.status == "running"
    assert to_delete_app.status == "deleted"
