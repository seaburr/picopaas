"""Regression tests for defects found by the 2026-10-21 coverage review (all fixed the same day; they were
strict-xfail pins until then). Do not weaken the assertions."""
import pytest

from app import custom_domains as cd
from app import crypto, models
from app.providers.fake import FakeMySQLAdmin, FakeObjectStore
from app.quota import STORAGE_QUOTA_BYTES, poll_quotas
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def owner(make_client, admin):
    c = make_client()
    register_and_login(c, "owner@example.com")
    return c


def _suspended_app_with_domain(owner, admin):
    aid = owner.post("/apps", json=APP).json()["id"]
    assert owner.put(f"/apps/{aid}/domain", json={"host": "blog.example.org"}).status_code == 200
    assert admin.post(f"/admin/apps/{aid}/suspend").status_code == 200
    return aid


def test_verify_domain_cannot_unsuspend_app(owner, admin, monkeypatch, db_session):
    aid = _suspended_app_with_domain(owner, admin)
    monkeypatch.setattr("app.routers.apps.custom_domains.verify", lambda *a, **k: None)
    r = owner.post(f"/apps/{aid}/domain/verify")
    assert r.status_code == 403
    db_session.expire_all()
    row = db_session.get(models.App, aid)
    assert row.status == "suspended"
    assert row.custom_domain_status != "active"


def test_clear_active_domain_cannot_unsuspend_app(owner, admin, db_session):
    aid = _suspended_app_with_domain(owner, admin)
    row = db_session.get(models.App, aid)
    row.custom_domain_status = "active"
    db_session.commit()
    r = owner.delete(f"/apps/{aid}/domain")
    assert r.status_code == 403
    db_session.expire_all()
    row = db_session.get(models.App, aid)
    assert row.status == "suspended"
    assert row.custom_domain == "blog.example.org"  # untouched


def test_storage_quota_swap_does_not_unsuspend_app(db_session):
    server = models.DbServer(name="p", host="h", port=3306, admin_user="r",
                             admin_password_encrypted=crypto.encrypt("x"), max_databases=5)
    user = models.User(email="q@example.com", password_hash="x")
    db_session.add_all([server, user])
    db_session.commit()
    app = models.App(owner_id=user.id, name="a", hostname="h0", image="i", port=8080, want_storage=True,
                     status="suspended")
    db_session.add(app)
    db_session.commit()
    db_session.add(models.AppStorage(app_id=app.id, bucket="b", endpoint="e", region="r", access_key="old",
                                     secret_key_encrypted=crypto.encrypt("old")))
    db_session.commit()
    store = FakeObjectStore()
    store.create_bucket_and_key(app.id)
    store.sizes[app.id] = STORAGE_QUOTA_BYTES + 1
    poll_quotas(db_session, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=store)
    assert app.status == "suspended"
    assert app.storage.over_quota is True  # the swap itself still happened


@pytest.mark.parametrize("status,expected", [("running", "pending"), ("error", "error")])
def test_storage_quota_swap_still_requeues_non_suspended_app(db_session, status, expected):
    server = models.DbServer(name="p", host="h", port=3306, admin_user="r",
                             admin_password_encrypted=crypto.encrypt("x"), max_databases=5)
    user = models.User(email="q2@example.com", password_hash="x")
    db_session.add_all([server, user])
    db_session.commit()
    app = models.App(owner_id=user.id, name="a", hostname="h1", image="i", port=8080, want_storage=True,
                     status=status)
    db_session.add(app)
    db_session.commit()
    db_session.add(models.AppStorage(app_id=app.id, bucket="b", endpoint="e", region="r", access_key="old",
                                     secret_key_encrypted=crypto.encrypt("old")))
    db_session.commit()
    store = FakeObjectStore()
    store.create_bucket_and_key(app.id)
    store.sizes[app.id] = STORAGE_QUOTA_BYTES + 1
    poll_quotas(db_session, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=store)
    assert app.status == expected


def test_reserved_subdomain_cannot_equal_another_apps_generated_host(admin, owner, db_session):
    a = owner.post("/apps", json=APP).json()["id"]
    b = admin.post("/apps", json=APP).json()["id"]
    other_host = db_session.get(models.App, b).hostname
    r = admin.put(f"/admin/apps/{a}/platform-host", json={"subdomain": other_host})
    assert r.status_code == 409
    db_session.expire_all()
    assert db_session.get(models.App, a).platform_subdomain is None


def test_reserved_subdomain_equal_to_own_generated_host_is_allowed_check_is_other_apps_only(admin, owner, db_session):
    # A deleted app's generated hostname no longer blocks the label.
    a = owner.post("/apps", json=APP).json()["id"]
    b = admin.post("/apps", json=APP).json()["id"]
    other_host = db_session.get(models.App, b).hostname
    assert admin.delete(f"/admin/apps/{b}").status_code in (200, 204)
    db_session.expire_all()
    r = admin.put(f"/admin/apps/{a}/platform-host", json={"subdomain": other_host})
    assert r.status_code == 200


def test_generated_hostname_avoids_existing_platform_subdomain(db_session, monkeypatch):
    from app import naming

    user = models.User(email="n@example.com", password_hash="x")
    db_session.add(user)
    db_session.commit()
    db_session.add(models.App(owner_id=user.id, name="a", hostname="zzz", image="i", port=8080,
                              platform_subdomain="clever-robin-aaaaaaa"))
    db_session.commit()
    seq = iter(["clever-robin-aaaaaaa", "bold-otter-bbbbbbb"])
    monkeypatch.setattr(naming, "_random_hostname", lambda: next(seq))
    assert naming.generate_unique_hostname(db_session) == "bold-otter-bbbbbbb"


def test_empty_token_never_verifies():
    class L:
        def txt(self, n):
            return [""]

        def a(self, n):
            return {"203.0.113.1"}

    with pytest.raises(cd.CustomDomainError):
        cd.verify("x.example.com", "", lookup=L())


@pytest.mark.parametrize("token", ["", "   ", None])
def test_blank_token_never_verifies_even_with_matching_blank_txt(token):
    class L:
        def txt(self, n):
            return ["", " ", token or ""]

        def a(self, n):
            return {"203.0.113.1"}

    with pytest.raises(cd.CustomDomainError):
        cd.verify("x.example.com", token, lookup=L())


def test_empty_txt_values_are_ignored_but_real_token_still_verifies():
    class L:
        def txt(self, n):
            return ["", "tok"]

        def a(self, n):
            return {"203.0.113.1"}

    cd.verify("x.example.com", "tok", lookup=L())  # no raise


def test_verify_fails_closed_when_platform_lb_does_not_resolve():
    class L:
        def txt(self, n):
            return ["tok"]

        def a(self, n):
            return {"198.51.100.7"} if n == "x.example.com" else set()

    with pytest.raises(cd.CustomDomainError):
        cd.verify("x.example.com", "tok", lookup=L())


def test_missing_spaces_env_degrades_to_no_object_store(monkeypatch):
    from app import scheduler

    for k in ("DIGITALOCEAN_TOKEN", "SPACES_ACCESS_KEY_ID", "SPACES_SECRET_ACCESS_KEY", "SPACES_REGION"):
        monkeypatch.delenv(k, raising=False)
    assert scheduler._object_store() is None


def test_missing_spaces_env_reconcile_job_still_reconciles_apps_without_storage(db_session, monkeypatch):
    """Real (unmocked) _object_store with Spaces env unset: reconcile_job must still call reconcile_all."""
    import app.providers.kube as kube_mod
    import app.reconciler as rec
    from app import scheduler

    for k in ("DIGITALOCEAN_TOKEN", "SPACES_ACCESS_KEY_ID", "SPACES_SECRET_ACCESS_KEY", "SPACES_REGION"):
        monkeypatch.delenv(k, raising=False)
    seen = {}
    monkeypatch.setattr(scheduler, "_lock_conn", None)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: True)
    monkeypatch.setattr(kube_mod, "KubeClient", lambda: "KUBE")
    monkeypatch.setattr(rec, "reconcile_all", lambda session, **kw: seen.update(kw))
    scheduler.reconcile_job()
    assert isinstance(seen["object_store"], scheduler._NoStorage)
    with pytest.raises(RuntimeError):  # an app that DOES need storage fails loudly, not silently
        seen["object_store"].create_bucket_and_key(1)
