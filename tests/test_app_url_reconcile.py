"""`${{ app.url }}` resolves through reconciler._app_url (active custom domain > reserved subdomain > generated),
end to end: API -> reconcile sweep -> rendered Secret, and re-resolves when the preferred host changes."""
import pytest

from app import config, models
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import _app_url, reconcile_all
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080,
       "env": [{"key": "SITE_URL", "value": "${{ app.url }}"}]}


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


class Cluster:
    def __init__(self, db_session):
        self.db = db_session
        self.kube = FakeKubeProvider()
        self.mysql, self.store = FakeMySQLAdmin(), FakeObjectStore()

    def sweep(self):
        reconcile_all(self.db, kube=self.kube, mysql_admin_factory=lambda s: self.mysql,
                      object_store=self.store, settings={})
        self.db.expire_all()

    def site_url(self, aid):
        secrets = [m for m in self.kube.applied[f"app-{aid}"] if m["kind"] == "Secret"]
        return secrets[-1]["stringData"]["SITE_URL"]  # fake kube appends: the last apply wins


@pytest.fixture()
def cluster(db_session):
    return Cluster(db_session)


def _row(db_session, aid):
    db_session.expire_all()
    return db_session.get(models.App, aid)


def test_generated_host_is_the_default(admin, cluster, db_session):
    aid = admin.post("/apps", json=APP).json()["id"]
    cluster.sweep()
    host = _row(db_session, aid).hostname
    assert cluster.site_url(aid) == f"https://{host}.{config.APP_DOMAIN}"


def test_reserved_subdomain_then_active_custom_domain_then_back(admin, cluster, db_session):
    aid = admin.post("/apps", json=APP).json()["id"]
    cluster.sweep()
    generated = cluster.site_url(aid)

    # 1. Reserved subdomain replaces the generated URL in the rendered Secret (the Secret is re-applied).
    assert admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"}).status_code == 200
    cluster.sweep()
    assert cluster.site_url(aid) == f"https://blog.{config.APP_DOMAIN}" != generated

    # 2. A pending (unverified) custom domain must NOT leak into app.url.
    assert admin.put(f"/apps/{aid}/domain", json={"host": "www.example.org"}).status_code == 200
    cluster.sweep()
    assert cluster.site_url(aid) == f"https://blog.{config.APP_DOMAIN}"

    # 3. Once verified the custom domain wins.
    row = _row(db_session, aid)
    row.custom_domain_status = "active"
    row.status = "pending"
    db_session.commit()
    cluster.sweep()
    assert cluster.site_url(aid) == "https://www.example.org"

    # 4. Detach the custom domain: back to the reserved subdomain.
    assert admin.delete(f"/apps/{aid}/domain").status_code == 200
    cluster.sweep()
    assert cluster.site_url(aid) == f"https://blog.{config.APP_DOMAIN}"

    # 5. Clear the reserved subdomain: back to generated.
    assert admin.delete(f"/admin/apps/{aid}/platform-host").status_code == 200
    cluster.sweep()
    assert cluster.site_url(aid) == generated


def test_active_custom_domain_without_reserved_subdomain(admin, cluster, db_session):
    aid = admin.post("/apps", json=APP).json()["id"]
    admin.put(f"/apps/{aid}/domain", json={"host": "www.example.org"})
    row = _row(db_session, aid)
    row.custom_domain_status = "active"
    db_session.commit()
    cluster.sweep()
    assert cluster.site_url(aid) == "https://www.example.org"


def test_both_ingresses_rendered_with_custom_and_reserved_hosts(admin, cluster, db_session):
    aid = admin.post("/apps", json=APP).json()["id"]
    admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"})
    admin.put(f"/apps/{aid}/domain", json={"host": "www.example.org"})
    row = _row(db_session, aid)
    row.custom_domain_status = "active"
    db_session.commit()
    cluster.sweep()
    ingresses = {m["metadata"]["name"]: m for m in cluster.kube.applied[f"app-{aid}"] if m["kind"] == "Ingress"}
    assert set(ingresses) == {f"app-{aid}", f"app-{aid}-custom"}
    primary_hosts = [r["host"] for r in ingresses[f"app-{aid}"]["spec"]["rules"]]
    assert f"blog.{config.APP_DOMAIN}" in primary_hosts and "www.example.org" not in primary_hosts
    assert [r["host"] for r in ingresses[f"app-{aid}-custom"]["spec"]["rules"]] == ["www.example.org"]


def test_suspended_app_keeps_rendering_but_drops_ingresses(admin, cluster, db_session):
    aid = admin.post("/apps", json=APP).json()["id"]
    admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"})
    row = _row(db_session, aid)
    row.custom_domain, row.custom_domain_status = "www.example.org", "active"
    db_session.commit()
    admin.post(f"/admin/apps/{aid}/suspend")
    cluster.sweep()
    assert not [m for m in cluster.kube.applied[f"app-{aid}"] if m["kind"] == "Ingress"]
    dep = next(m for m in cluster.kube.applied[f"app-{aid}"] if m["kind"] == "Deployment")
    assert dep["spec"]["replicas"] == 0


@pytest.mark.parametrize("fields,expected", [
    # custom_domain set but status not active (None / pending / error): ignored.
    (dict(custom_domain="c.example.org", custom_domain_status=None), "gen"),
    (dict(custom_domain="c.example.org", custom_domain_status="pending"), "gen"),
    (dict(custom_domain="c.example.org", custom_domain_status="error"), "gen"),
    # active status but no host (inconsistent row) must not produce "https://None".
    (dict(custom_domain=None, custom_domain_status="active"), "gen"),
    (dict(custom_domain="", custom_domain_status="active"), "gen"),
    # empty-string reserved subdomain is falsy: generated.
    (dict(platform_subdomain=""), "gen"),
    (dict(platform_subdomain="blog", custom_domain=None, custom_domain_status="active"), "blog"),
    (dict(platform_subdomain="blog", custom_domain="c.example.org", custom_domain_status="active"), "c.example.org"),
])
def test_app_url_priority_table(fields, expected):
    app = models.App(hostname="gen-name-abc1234", **fields)
    want = {
        "gen": f"https://gen-name-abc1234.{config.APP_DOMAIN}",
        "blog": f"https://blog.{config.APP_DOMAIN}",
        "c.example.org": "https://c.example.org",
    }[expected]
    assert _app_url(app) == want
    assert "None" not in _app_url(app)
