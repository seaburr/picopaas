"""app/quota.py with fakes."""
from __future__ import annotations

from app import crypto, models
from app.providers.fake import FakeMySQLAdmin, FakeObjectStore
from app.quota import DB_QUOTA_BYTES, STORAGE_QUOTA_BYTES, capacity_summary, poll_quotas


def _setup(db_session, n_apps=1, max_databases=25):
    server = models.DbServer(
        name="primary", host="h", port=3306, admin_user="root",
        admin_password_encrypted=crypto.encrypt("x"), max_databases=max_databases,
    )
    db_session.add(server)
    user = models.User(email="q@example.com", password_hash="x")
    db_session.add(user)
    db_session.commit()
    apps = []
    for i in range(n_apps):
        app = models.App(owner_id=user.id, name=f"a{i}", hostname=f"h{i}", image="i", port=80,
                         want_db=True, want_storage=True, status="running")
        db_session.add(app)
        db_session.commit()
        db_session.add(models.AppDatabase(app_id=app.id, db_server_id=server.id, db_name=f"app_{app.id}",
                                          db_user=f"app_{app.id}", password_encrypted=crypto.encrypt("p")))
        db_session.add(models.AppStorage(app_id=app.id, bucket="b", endpoint="e", region="r",
                                         access_key="old", secret_key_encrypted=crypto.encrypt("old")))
        db_session.commit()
        apps.append(app)
    return server, apps


def test_db_flips_read_only_both_directions(db_session):
    _, (app,) = _setup(db_session)
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    admin.sizes[app.id] = DB_QUOTA_BYTES + 1
    poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=store)
    assert app.database.over_quota and app.database.size_bytes == DB_QUOTA_BYTES + 1
    assert admin.read_only[app.id] is True
    admin.sizes[app.id] = 10
    poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=store)
    assert not app.database.over_quota and admin.read_only[app.id] is False


def test_storage_over_quota_swaps_key_and_marks_pending(db_session):
    _, (app,) = _setup(db_session)
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    store.create_bucket_and_key(app.id)
    store.sizes[app.id] = STORAGE_QUOTA_BYTES + 1
    poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=store)
    assert app.storage.over_quota and store.read_only[app.id] is True
    assert app.storage.access_key != "old"
    assert crypto.decrypt(app.storage.secret_key_encrypted) == store.creds[app.id]["secret_key"]
    assert app.status == "pending"
    # idempotent: no further key swap while the state is unchanged
    key = app.storage.access_key
    poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=store)
    assert app.storage.access_key == key


def test_errors_do_not_stop_the_loop(db_session):
    _, apps = _setup(db_session, n_apps=2)

    class Boom(FakeMySQLAdmin):
        def db_size_bytes(self, app_id):
            if app_id == apps[0].id:
                raise RuntimeError("boom")
            return DB_QUOTA_BYTES + 5

    admin = Boom()
    poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=FakeObjectStore())
    assert apps[1].database.over_quota and not apps[0].database.over_quota


def test_capacity_summary(db_session):
    _setup(db_session, n_apps=4, max_databases=5)
    (row,) = capacity_summary(db_session)
    assert row == {"name": "primary", "status": "active", "used": 4, "max_databases": 5, "pct": 80.0, "warn": True}


def _over_storage(db_session):
    _, (app,) = _setup(db_session)
    admin, store = FakeMySQLAdmin(), FakeObjectStore()
    orig = store.create_bucket_and_key(app.id)
    app.storage.access_key = orig["access_key"]
    app.storage.secret_key_encrypted = crypto.encrypt(orig["secret_key"])
    db_session.commit()
    store.sizes[app.id] = STORAGE_QUOTA_BYTES + 1
    return app, admin, store, orig["access_key"]


def test_storage_swap_create_failure_keeps_old_key(db_session):
    app, admin, store, old = _over_storage(db_session)
    store.fail.add("create")
    poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=store)
    assert app.storage.access_key == old and store.valid_keys[app.id] == {old}
    assert not app.storage.over_quota  # retried next poll


def test_storage_swap_commit_failure_discards_new_key(db_session, monkeypatch):
    from sqlalchemy.orm import Session

    app, admin, store, old = _over_storage(db_session)
    real_commit = Session.commit
    monkeypatch.setattr(Session, "commit", lambda self: (_ for _ in ()).throw(RuntimeError("db down")))
    poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=store)
    monkeypatch.setattr(Session, "commit", real_commit)
    db_session.expire_all()
    assert app.storage.access_key == old and store.valid_keys[app.id] == {old}


def test_storage_swap_revoke_failure_is_retried_next_poll(db_session):
    app, admin, store, old = _over_storage(db_session)
    store.fail.add("revoke")
    poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=store)
    new = app.storage.access_key
    assert new != old and new in store.valid_keys[app.id] and old in store.valid_keys[app.id]
    store.fail.clear()
    poll_quotas(db_session, mysql_admin_factory=lambda s: admin, object_store=store)
    assert store.valid_keys[app.id] == {new}
