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


def test_kube_error_summary_uses_k8s_status_message():
    from types import SimpleNamespace

    from app.reconciler import _kube_error_summary

    exc = SimpleNamespace(status=422, reason="Unprocessable Entity", body='{"message": "spec.replicas: Invalid value"}')
    assert _kube_error_summary(exc) == "422 Unprocessable Entity: spec.replicas: Invalid value"
    assert _kube_error_summary(RuntimeError("boom with password=hunter2")) == "RuntimeError"


def _sweep(session, kube, mysql, store):
    reconcile_all(session, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store, settings={})


def test_sweep_reapplies_stale_running_app_once_and_skips_unchanged(session):
    kube, mysql, store = FakeKubeProvider(), FakeMySQLAdmin(), FakeObjectStore()
    app = _make_app(session, env=[("TOKEN", "s3cret-value-xyz", True)])
    _sweep(session, kube, mysql, store)  # pending -> running, hash stored
    assert app.status == "running" and app.rendered_hash
    ns = f"app-{app.id}"
    assert "s3cret-value-xyz" not in app.rendered_hash
    import hashlib, json
    plain = hashlib.sha256(json.dumps(kube.applied[ns], sort_keys=True, default=str).encode()).hexdigest()
    assert app.rendered_hash != plain

    n = len(kube.applied[ns])
    _sweep(session, kube, mysql, store)  # unchanged: no apply
    assert len(kube.applied[ns]) == n

    app.rendered_hash = "stale"
    session.commit()
    _sweep(session, kube, mysql, store)
    assert len(kube.applied[ns]) == 2 * n and app.status == "running"
    _sweep(session, kube, mysql, store)
    assert len(kube.applied[ns]) == 2 * n


def test_sweep_reapplies_running_app_with_missing_namespace(session):
    kube, mysql, store = FakeKubeProvider(), FakeMySQLAdmin(), FakeObjectStore()
    gone = _make_app(session)
    kept = _make_app(session)
    _sweep(session, kube, mysql, store)
    assert gone.status == "running" and kept.status == "running"
    ns_gone, ns_kept = f"app-{gone.id}", f"app-{kept.id}"
    n_gone, n_kept = len(kube.applied[ns_gone]), len(kube.applied[ns_kept])

    kube.applied.pop(ns_gone)  # cluster rebuilt: namespace gone, hash unchanged
    kube.list_ns_calls = 0
    _sweep(session, kube, mysql, store)
    assert kube.list_ns_calls == 1  # one list call per sweep, not per app
    assert len(kube.applied[ns_gone]) == n_gone
    assert len(kube.applied[ns_kept]) == n_kept  # present + matching hash: untouched
    assert gone.status == "running"


def test_provisioning_failure_is_logged_without_secrets(session, caplog):
    import logging

    class Boom(FakeMySQLAdmin):
        def create_db_user(self, app_id):
            raise RuntimeError("connect mysql://doadmin:hunter2@db:25/x password=hunter2 failed")

    _make_server(session)
    app = _make_app(session, want_db=True)
    kube, store = FakeKubeProvider(), FakeObjectStore()
    with caplog.at_level(logging.WARNING, logger="app.reconciler"):
        reconcile_all(session, kube=kube, mysql_admin_factory=lambda s: Boom(), object_store=store, settings={})
    assert "db provisioning failed" in caplog.text
    assert "hunter2" not in caplog.text
    assert app.status == "error" and app.status_detail == "database provisioning failed"


def test_provider_error_summary_pymysql_and_botocore():
    from app.reconciler import _provider_error_summary

    class OperationalError(Exception):
        pass

    assert _provider_error_summary(OperationalError(1045, "Access denied for user 'doadmin'")) == \
        "OperationalError 1045: Access denied for user 'doadmin'"

    class ClientError(Exception):
        response = {"Error": {"Code": "AccessDenied", "Message": "x"}}

    assert _provider_error_summary(ClientError("nope")) == "ClientError AccessDenied"


def _deleted_app_with_resources(session):
    from datetime import datetime, timezone

    _make_server(session)
    kube, mysql, store = FakeKubeProvider(), FakeMySQLAdmin(), FakeObjectStore()
    app = _make_app(session, want_db=True, want_storage=True)
    reconcile_app(session, app, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store, settings={})
    app.deleted_at = datetime.now(timezone.utc)
    session.commit()
    return app, kube, mysql, store


def test_delete_retries_when_mysql_drop_fails(session):
    app, kube, mysql, store = _deleted_app_with_resources(session)
    real_drop = mysql.drop

    def boom(app_id):
        raise MySQLAdminError("down password=hunter2")

    mysql.drop = boom
    delete_app(session, app, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store)
    assert app.status != "deleted"
    assert session.query(models.AppDatabase).filter_by(app_id=app.id).count() == 1
    assert session.query(models.AppStorage).filter_by(app_id=app.id).count() == 0  # storage succeeded

    mysql.drop = real_drop
    reconcile_all(session, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store, settings={})
    assert app.status == "deleted"
    assert session.query(models.AppDatabase).filter_by(app_id=app.id).count() == 0


def test_delete_retries_when_bucket_delete_fails(session):
    app, kube, mysql, store = _deleted_app_with_resources(session)
    real_delete = store.delete

    def boom(app_id):
        raise ObjectStoreError("spaces down")

    store.delete = boom
    delete_app(session, app, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store)
    assert app.status != "deleted"
    assert session.query(models.AppStorage).filter_by(app_id=app.id).count() == 1

    store.delete = real_delete
    delete_app(session, app, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store)
    assert app.status == "deleted"
    assert session.query(models.AppStorage).filter_by(app_id=app.id).count() == 0


def test_delete_treats_not_found_as_success(session):
    app, kube, mysql, store = _deleted_app_with_resources(session)

    class NoBucket(Exception):
        response = {"Error": {"Code": "NoSuchBucket"}}

    def gone_db(app_id):
        raise MySQLAdminError(1049, "Unknown database")

    def gone_bucket(app_id):
        raise NoBucket()

    mysql.drop, store.delete = gone_db, gone_bucket
    delete_app(session, app, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store)
    assert app.status == "deleted"


# --- retry with backoff (issue #8) ---
from datetime import datetime, timedelta  # noqa: E402

from app.reconciler import retry_at  # noqa: E402

T0 = datetime(2026, 1, 1, 12, 0, 0)


class _FlakyKube(FakeKubeProvider):
    fail = True

    def apply(self, manifests):
        if self.fail:
            raise RuntimeError("boom")
        return super().apply(manifests)


def _rsweep(session, kube, now, admin=None):
    reconcile_all(
        session, kube=kube, mysql_admin_factory=lambda s: admin or FakeMySQLAdmin(),
        object_store=FakeObjectStore(), settings={}, now=now,
    )


def test_backoff_schedule_with_injected_clock(session):
    app = _make_app(session)
    kube = _FlakyKube()
    now = T0
    _rsweep(session, kube, now)  # pending -> error (count 1)
    assert (app.status, app.error_count) == ("error", 1)
    for count, wait in [(1, 1), (2, 5), (3, 15), (4, 60), (5, 60), (6, 60)]:
        assert app.error_count == count
        assert retry_at(app) == now + timedelta(minutes=wait)
        _rsweep(session, kube, now + timedelta(minutes=wait) - timedelta(seconds=1))
        assert app.error_count == count  # not due yet
        now = now + timedelta(minutes=wait)
        _rsweep(session, kube, now)
        assert app.error_count == count + 1


def test_success_resets_error_state(session):
    app = _make_app(session)
    kube = _FlakyKube()
    _rsweep(session, kube, T0)
    kube.fail = False
    _rsweep(session, kube, T0 + timedelta(minutes=1))
    assert app.status != "error"
    assert (app.error_count, app.last_attempt_at, retry_at(app)) == (0, None, None)


def test_non_retryable_errors_never_retried(session):
    app = _make_app(session, env=[("X", "${{ db.url }}", False)])  # db ref without want_db
    kube = _FlakyKube()
    kube.fail = False
    _rsweep(session, kube, T0)
    assert app.status == "error" and app.error_retryable is False and retry_at(app) is None
    assert app.error_count == 1
    _rsweep(session, kube, T0 + timedelta(days=30))
    assert app.error_count == 1 and app.status == "error"


def test_no_db_capacity_is_retryable(session):
    app = _make_app(session, want_db=True)  # no DbServer
    kube = _FlakyKube()
    kube.fail = False
    _rsweep(session, kube, T0)
    assert app.status_detail == "no database capacity" and app.error_retryable is True
    _make_server(session)
    _rsweep(session, kube, T0 + timedelta(minutes=1))
    assert app.status != "error" and app.error_count == 0


def test_config_change_resets_error_state(session):
    app = _make_app(session)
    _rsweep(session, _FlakyKube(), T0)
    assert app.error_count == 1
    app.reset_retry()
    assert (app.error_count, app.last_attempt_at, app.error_retryable) == (0, None, True)
