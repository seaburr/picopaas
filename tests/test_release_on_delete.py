"""Release-on-delete: unique host claims (platform_subdomain, custom_domain*) are freed only when the reconciler has
finished tearing the app down, for all three delete paths, with partial-failure and idempotency edge cases."""
import pytest

from app import crypto, models
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import delete_app, reconcile_all
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def user(make_client, admin, db_session):
    c = make_client()
    register_and_login(c, "user@example.com")
    db_session.query(models.User).filter(models.User.email == "user@example.com").one().app_limit = 3
    db_session.commit()
    return c


def _claim(admin, db_session, aid, label, domain):
    assert admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": label}).status_code == 200
    row = db_session.get(models.App, aid)
    row.custom_domain, row.custom_domain_status = domain, "active"
    row.custom_domain_token = "tok"
    row.custom_domain_error = "e"
    row.custom_domain_verified_at = row.created_at
    db_session.commit()


def _sweep(db_session, kube=None, mysql=None, store=None):
    kube = kube or FakeKubeProvider()
    mysql, store = mysql or FakeMySQLAdmin(), store or FakeObjectStore()
    reconcile_all(db_session, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store, settings={})
    db_session.expire_all()
    return kube


def _fields(row):
    return (row.platform_subdomain, row.custom_domain, row.custom_domain_token, row.custom_domain_status,
            row.custom_domain_error, row.custom_domain_verified_at)


# --- models.release_unique_hosts ---------------------------------------------------------------------------

def test_release_unique_hosts_nulls_only_the_claim_fields():
    app = models.App(hostname="gen-abc1234", name="n", image="i", port=8080, platform_subdomain="blog",
                     custom_domain="x.example.org", custom_domain_token="t", custom_domain_status="active",
                     custom_domain_error="e", custom_domain_verified_at=object(), status="running")
    models.release_unique_hosts(app)
    assert _fields(app) == (None,) * 6
    assert app.hostname == "gen-abc1234" and app.name == "n" and app.status == "running"
    models.release_unique_hosts(app)  # idempotent
    assert _fields(app) == (None,) * 6


# --- timing: claims survive soft-delete, go at teardown -----------------------------------------------------

@pytest.mark.parametrize("path", ["user", "admin_app", "admin_user"])
def test_claim_held_between_soft_delete_and_teardown(admin, user, db_session, path):
    aid = user.post("/apps", json=APP).json()["id"]
    _claim(admin, db_session, aid, "blog", "blog.example.org")
    uid = db_session.get(models.App, aid).owner_id
    r = {"user": lambda: user.delete(f"/apps/{aid}"),
         "admin_app": lambda: admin.delete(f"/admin/apps/{aid}"),
         "admin_user": lambda: admin.delete(f"/admin/users/{uid}")}[path]()
    assert r.status_code == 204
    db_session.expire_all()
    row = db_session.get(models.App, aid)
    assert row.deleted_at is not None and row.status != "deleted"
    assert row.platform_subdomain == "blog" and row.custom_domain == "blog.example.org"
    # The claim blocks both the admin label API and the custom-domain API while the Ingress may still exist.
    other = admin.post("/apps", json=APP).json()["id"]
    assert admin.put(f"/admin/apps/{other}/platform-host", json={"subdomain": "blog"}).status_code == 409
    assert admin.put(f"/apps/{other}/domain", json={"host": "blog.example.org"}).status_code == 409
    _sweep(db_session)
    assert _fields(db_session.get(models.App, aid)) == (None,) * 6
    assert db_session.get(models.App, aid).status == "deleted"
    assert admin.put(f"/admin/apps/{other}/platform-host", json={"subdomain": "blog"}).status_code == 200
    assert admin.put(f"/apps/{other}/domain", json={"host": "blog.example.org"}).status_code == 200


def test_released_label_survives_later_sweeps_after_reassignment(admin, user, db_session):
    """After teardown another app claims the label; later sweeps must never touch that new claim."""
    a = user.post("/apps", json=APP).json()["id"]
    b = admin.post("/apps", json=APP).json()["id"]
    admin.put(f"/admin/apps/{a}/platform-host", json={"subdomain": "blog"})
    user.delete(f"/apps/{a}")
    _sweep(db_session)
    assert admin.put(f"/admin/apps/{b}/platform-host", json={"subdomain": "blog"}).status_code == 200
    for _ in range(3):
        _sweep(db_session)
    assert db_session.get(models.App, b).platform_subdomain == "blog"
    assert db_session.get(models.App, a).platform_subdomain is None


def test_deleted_app_is_hidden_and_not_reconciled_as_live(admin, user, db_session):
    aid = user.post("/apps", json=APP).json()["id"]
    admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"})
    user.delete(f"/apps/{aid}")
    assert user.get(f"/apps/{aid}").status_code == 404
    assert admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "other"}).status_code == 404
    assert admin.delete(f"/admin/apps/{aid}/platform-host").status_code == 404
    kube = _sweep(db_session)
    # Torn down, never (re)applied as a live app: only the namespace delete happened.
    assert f"app-{aid}" in kube.deleted and f"app-{aid}" not in kube.applied


def test_second_delete_of_same_app_is_404_not_double_release(admin, user):
    aid = user.post("/apps", json=APP).json()["id"]
    assert user.delete(f"/apps/{aid}").status_code == 204
    assert user.delete(f"/apps/{aid}").status_code == 404
    assert admin.delete(f"/admin/apps/{aid}").status_code == 404


def test_admin_delete_user_releases_every_app_but_only_that_users(admin, user, db_session):
    a1 = user.post("/apps", json=APP).json()["id"]
    a2 = user.post("/apps", json={**APP, "name": "web2"}).json()["id"]
    keep = admin.post("/apps", json=APP).json()["id"]
    for aid, label in ((a1, "one"), (a2, "two"), (keep, "keepme")):
        admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": label})
    uid = db_session.get(models.App, a1).owner_id
    assert admin.delete(f"/admin/users/{uid}").status_code == 204
    _sweep(db_session)
    assert db_session.get(models.App, a1).platform_subdomain is None
    assert db_session.get(models.App, a2).platform_subdomain is None
    kept = db_session.get(models.App, keep)
    assert kept.platform_subdomain == "keepme" and kept.deleted_at is None


# --- partial teardown failure keeps claims -----------------------------------------------------------------

class _NsFailsFor(FakeKubeProvider):
    def __init__(self, bad):
        super().__init__()
        self.bad = bad

    def delete_namespace(self, namespace):
        if namespace == self.bad:
            raise RuntimeError("api down")
        super().delete_namespace(namespace)


def test_one_failing_namespace_does_not_block_or_release_the_other(admin, user, db_session):
    a1 = user.post("/apps", json=APP).json()["id"]
    a2 = user.post("/apps", json={**APP, "name": "web2"}).json()["id"]
    admin.put(f"/admin/apps/{a1}/platform-host", json={"subdomain": "one"})
    admin.put(f"/admin/apps/{a2}/platform-host", json={"subdomain": "two"})
    uid = db_session.get(models.App, a1).owner_id
    admin.delete(f"/admin/users/{uid}")
    _sweep(db_session, _NsFailsFor(f"app-{a1}"))
    r1, r2 = db_session.get(models.App, a1), db_session.get(models.App, a2)
    assert r1.status != "deleted" and r1.platform_subdomain == "one"
    assert r2.status == "deleted" and r2.platform_subdomain is None
    _sweep(db_session)
    assert db_session.get(models.App, a1).platform_subdomain is None


class _Boom(Exception):
    pass


def _provisioned_deleted_app(db_session, label="blog"):
    server = models.DbServer(name="primary", host="h", port=3306, admin_user="root",
                             admin_password_encrypted=crypto.encrypt("x"), max_databases=25)
    owner = models.User(email="own@example.com", password_hash="x")
    db_session.add_all([server, owner])
    db_session.commit()
    app = models.App(owner_id=owner.id, name="n", hostname="gen-abc1234", image="i", port=8080, want_db=True,
                     want_storage=True, status="running", platform_subdomain=label, custom_domain="c.example.org",
                     custom_domain_status="active")
    db_session.add(app)
    db_session.commit()
    db_session.add(models.AppDatabase(app_id=app.id, db_server_id=server.id, db_name=f"app_{app.id}",
                                      db_user=f"app_{app.id}", password_encrypted=crypto.encrypt("p")))
    db_session.add(models.AppStorage(app_id=app.id, bucket="b", endpoint="e", region="r", access_key="k",
                                     secret_key_encrypted=crypto.encrypt("s")))
    app.deleted_at = app.created_at
    db_session.commit()
    return app


@pytest.mark.parametrize("failing", ["db", "storage"])
def test_db_or_storage_teardown_failure_keeps_host_claims(db_session, failing):
    app = _provisioned_deleted_app(db_session)

    class BadMySQL(FakeMySQLAdmin):
        def drop(self, app_id):
            raise _Boom("mysql down")

    class BadStore(FakeObjectStore):
        def delete(self, app_id):
            raise _Boom("spaces down")

    kube = FakeKubeProvider()
    mysql = BadMySQL() if failing == "db" else FakeMySQLAdmin()
    store = BadStore() if failing == "storage" else FakeObjectStore()
    delete_app(db_session, app, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store)
    assert f"app-{app.id}" in kube.deleted  # namespace went, but the other resource didn't: claims stay
    assert app.status == "running" and app.platform_subdomain == "blog" and app.custom_domain == "c.example.org"
    # Recovery: next attempt with healthy providers finishes the job and releases.
    delete_app(db_session, app, kube=FakeKubeProvider(), mysql_admin_factory=lambda s: FakeMySQLAdmin(),
               object_store=FakeObjectStore())
    assert app.status == "deleted" and _fields(app) == (None,) * 6
    assert app.database is None and app.storage is None


def test_all_resources_gone_releases_in_one_pass(db_session):
    app = _provisioned_deleted_app(db_session)
    delete_app(db_session, app, kube=FakeKubeProvider(), mysql_admin_factory=lambda s: FakeMySQLAdmin(),
               object_store=FakeObjectStore())
    assert app.status == "deleted" and _fields(app) == (None,) * 6 and app.status_detail is None
