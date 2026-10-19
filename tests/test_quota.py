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
