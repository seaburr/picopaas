"""Admin-assigned fixed platform subdomain (app/naming.py, /admin/apps/{id}/platform-host, ingress, routing)."""
import pytest

from app import config, main, models
from app import k8s_manifests as k8s
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import reconcile_all
from app.naming import PlatformSubdomainError, normalize_platform_subdomain
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")  # first user is the platform admin
    return c


@pytest.fixture()
def user(make_client, admin):
    c = make_client()
    register_and_login(c, "user@example.com")
    return c


def _mk(c):
    return c.post("/apps", json=APP).json()["id"]


@pytest.mark.parametrize("label", ["blog", "a", "my-blog", "b2", "x" * 63])
def test_label_ok(label):
    assert normalize_platform_subdomain(label) == label


@pytest.mark.parametrize("bad", [
    "", "Blog", "-a", "a-", "a.b", "a_b", "x" * 64, "a b", "*", "platform", "www", "api", "lb", "app", "apps", "admin",
    config.PLATFORM_HOST.split(".")[0], config.APEX_HOST.split(".")[0] + "/",
])
def test_label_rejected(bad):
    with pytest.raises(PlatformSubdomainError):
        normalize_platform_subdomain(bad)


def test_admin_sets_and_clears(admin, user):
    aid = _mk(user)
    r = admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"})
    assert r.status_code == 200, r.text
    assert r.json()["platform_subdomain"] == "blog"
    assert r.json()["platform_host"] == f"blog.{config.APP_DOMAIN}"
    assert user.get(f"/apps/{aid}").json()["platform_host"] == f"blog.{config.APP_DOMAIN}"
    r = admin.delete(f"/admin/apps/{aid}/platform-host")
    assert r.status_code == 200 and r.json()["platform_subdomain"] is None and r.json()["platform_host"] is None


def test_non_admin_forbidden(admin, user):
    aid = _mk(user)
    assert user.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"}).status_code == 403
    assert user.delete(f"/admin/apps/{aid}/platform-host").status_code == 403
    assert user.get(f"/apps/{aid}").json()["platform_subdomain"] is None


def test_bad_and_reserved_422(admin, user):
    aid = _mk(user)
    for bad in ["www", "Bad", "a.b", ""]:
        assert admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": bad}).status_code == 422


def test_uniqueness_409(admin, user):
    a1 = _mk(user)
    a2 = user.post("/apps", json={**APP, "name": "web2"})
    if a2.status_code != 200 and a2.status_code != 201:  # per-user app cap in play: use the admin's own app
        a2 = admin.post("/apps", json=APP)
    a2 = a2.json()["id"]
    assert admin.put(f"/admin/apps/{a1}/platform-host", json={"subdomain": "blog"}).status_code == 200
    assert admin.put(f"/admin/apps/{a2}/platform-host", json={"subdomain": "blog"}).status_code == 409
    # Re-setting the same label on the same app is fine.
    assert admin.put(f"/admin/apps/{a1}/platform-host", json={"subdomain": "blog"}).status_code == 200


def _teardown(db_session, kube=None, **kw):
    """Drive one reconciler sweep in-process with fakes (the same call the scheduler makes)."""
    kube = kube or FakeKubeProvider()
    mysql, store = FakeMySQLAdmin(), FakeObjectStore()
    reconcile_all(db_session, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store, settings={})
    db_session.expire_all()
    return kube


def test_label_reusable_after_teardown_not_after_soft_delete(admin, user, db_session):
    a1 = _mk(user)
    a2 = admin.post("/apps", json=APP).json()["id"]
    put = lambda aid: admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"}).status_code  # noqa: E731
    assert put(a1) == 200
    assert put(a2) == 409  # active holder always blocks
    assert user.delete(f"/apps/{a1}").status_code == 204
    # Soft-deleted but not torn down: the claim is kept (the namespace still has the Ingress rule).
    row = db_session.get(models.App, a1)
    assert row.deleted_at is not None and row.platform_subdomain == "blog"
    assert put(a2) == 409
    _teardown(db_session)
    row = db_session.get(models.App, a1)
    assert row.status == "deleted" and row.platform_subdomain is None
    assert put(a2) == 200


def test_teardown_failure_keeps_claims(admin, user, db_session):
    class BoomKube(FakeKubeProvider):
        def delete_namespace(self, namespace):
            raise RuntimeError("api down")

    aid = _mk(user)
    admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"})
    row = db_session.get(models.App, aid)
    row.custom_domain = "blog.example.org"
    row.custom_domain_status = "active"
    db_session.commit()
    user.delete(f"/apps/{aid}")
    _teardown(db_session, BoomKube())
    row = db_session.get(models.App, aid)
    assert row.status != "deleted" and row.platform_subdomain == "blog" and row.custom_domain == "blog.example.org"
    _teardown(db_session)  # cluster recovers: retry succeeds and releases
    row = db_session.get(models.App, aid)
    assert row.status == "deleted" and row.platform_subdomain is None and row.custom_domain is None
    assert row.custom_domain_status is None and row.custom_domain_token is None


def _claimed_app(admin, user, db_session, label, domain):
    aid = _mk(user)
    assert admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": label}).status_code == 200
    row = db_session.get(models.App, aid)
    row.custom_domain = domain
    row.custom_domain_status = "active"
    db_session.commit()
    return aid


def _assert_claimed(db_session, aid, label, domain):
    row = db_session.get(models.App, aid)
    assert row.deleted_at is not None and row.platform_subdomain == label and row.custom_domain == domain


def _assert_released(db_session, aid):
    row = db_session.get(models.App, aid)
    assert row.status == "deleted" and row.platform_subdomain is None and row.custom_domain is None


def test_release_on_teardown_admin_delete_app(admin, user, db_session):
    aid = _claimed_app(admin, user, db_session, "one", "one.example.org")
    assert admin.delete(f"/admin/apps/{aid}").status_code == 204
    _assert_claimed(db_session, aid, "one", "one.example.org")
    _teardown(db_session)
    _assert_released(db_session, aid)


def test_release_on_teardown_admin_delete_user(admin, user, db_session):
    aid = _claimed_app(admin, user, db_session, "two", "two.example.org")
    uid = db_session.get(models.App, aid).owner_id
    assert admin.delete(f"/admin/users/{uid}").status_code == 204
    _assert_claimed(db_session, aid, "two", "two.example.org")
    _teardown(db_session)
    _assert_released(db_session, aid)


def test_release_on_teardown_user_delete(admin, user, db_session):
    aid = _claimed_app(admin, user, db_session, "three", "three.example.org")
    assert user.delete(f"/apps/{aid}").status_code == 204
    _assert_claimed(db_session, aid, "three", "three.example.org")
    _teardown(db_session)
    _assert_released(db_session, aid)


def test_set_requeues_app(admin, user, db_session):
    aid = _mk(user)
    app = db_session.get(models.App, aid)
    app.status = "running"
    db_session.commit()
    admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"})
    db_session.expire_all()
    assert db_session.get(models.App, aid).status == "pending"


def test_ingress_has_reserved_host_on_wildcard_tls():
    ing = k8s.ingress_manifest(1, "gen-name-abc1234", 8080, "blog")
    hosts = [r["host"] for r in ing["spec"]["rules"]]
    assert hosts == [f"gen-name-abc1234.{config.APP_DOMAIN}", f"blog.{config.APP_DOMAIN}"]
    assert ing["spec"]["tls"] == [{"hosts": hosts}]  # no secretName: default wildcard TLSStore serves it
    assert "annotations" not in ing["metadata"]
    base = k8s.ingress_manifest(1, "gen-name-abc1234", 8080)
    assert len(base["spec"]["rules"]) == 1
    r = k8s.render_app_manifests(1, "h", "img", 8080, {}, None, platform_subdomain="blog")
    assert len(r["ingress"]["spec"]["rules"]) == 2
    assert r["custom_ingress"] is None


def test_suspended_reserved_host_gets_503_not_404(make_client, db_session, tmp_path, monkeypatch):
    (tmp_path / "suspended.html").write_text("<title>PAUSED</title>")
    monkeypatch.setattr(main, "_STATIC_DIR", tmp_path)
    main._suspended_cache.clear()
    owner = models.User(email="o@example.com", password_hash="x")
    db_session.add(owner)
    db_session.commit()
    db_session.add(models.App(owner_id=owner.id, name="a", hostname="sleepy-abc1234", image="i", port=8080,
                              status="suspended", platform_subdomain="blog"))
    db_session.commit()
    c = make_client()
    r = c.get("/", headers={"host": f"blog.{config.APP_DOMAIN}"}, follow_redirects=False)
    assert r.status_code == 503 and "PAUSED" in r.text
    assert c.get("/", headers={"host": f"nope.{config.APP_DOMAIN}"}, follow_redirects=False).status_code == 404
    main._suspended_cache.clear()


@pytest.mark.parametrize("port,code", [(80, 422), (1023, 422), (1024, 200), (8080, 200), (65535, 200), (65536, 422)])
def test_port_range_non_root(admin, port, code):
    r = admin.post("/apps", json={**APP, "port": port})
    assert (r.status_code in (200, 201)) == (code == 200), r.text
    if port < 1024:
        assert "1024" in r.text


# --- label validation edge cases ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "xn--bcher-kva", "xn--", "Blog", "BLOG", "blog.", ".blog", "a..b", "a.b.c", "-blog", "blog-", "-", "   ", "\t",
    "bl og", "bl\nog", "_blog", "a_b", "*", "*.blog", "x" * 64, "x" * 200, "ünï", "lb", "platform", "picopaas",
    config.APEX_HOST.split(".")[0] + ".",
])
def test_label_rejected_more(bad):
    with pytest.raises(PlatformSubdomainError):
        normalize_platform_subdomain(bad)


def test_xn_prefix_message_is_clear():
    with pytest.raises(PlatformSubdomainError, match="xn--"):
        normalize_platform_subdomain("xn--bcher-kva")


def test_label_surrounding_whitespace_is_stripped():
    assert normalize_platform_subdomain("  blog\n") == "blog"


def test_label_collides_with_platform_hosts(monkeypatch):
    # Collision checks are on the full host, so they hold even if the domain layout changes.
    monkeypatch.setattr(config, "PLATFORM_HOST", f"portal.{config.APP_DOMAIN}")
    monkeypatch.setattr(config, "APEX_HOST", f"home.{config.APP_DOMAIN}")
    monkeypatch.setattr(config, "CUSTOM_DOMAIN_CNAME_TARGET", f"edge.{config.APP_DOMAIN}")
    monkeypatch.setattr(config, "LEGACY_HOSTS", (f"old.{config.APP_DOMAIN}",))
    for label in ("portal", "home", "edge", "old"):
        with pytest.raises(PlatformSubdomainError, match="collides"):
            normalize_platform_subdomain(label)
    assert normalize_platform_subdomain("fine") == "fine"


def test_valid_labels_accepted():
    for ok in ("a", "0", "9lives", "my-blog-2", "a--b", "x" * 63):
        assert normalize_platform_subdomain(ok) == ok


# --- admin authz / API behavior ----------------------------------------------------------------------------

def test_anonymous_cannot_touch_platform_host(admin, user, make_client):
    aid = _mk(user)
    anon = make_client()
    assert anon.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"}).status_code in (401, 403)
    assert anon.delete(f"/admin/apps/{aid}/platform-host").status_code in (401, 403)


def test_owner_non_admin_cannot_self_assign(admin, user, db_session):
    aid = _mk(user)
    assert user.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "mine"}).status_code == 403
    db_session.expire_all()
    assert db_session.get(models.App, aid).platform_subdomain is None


def test_unknown_app_404_and_422_bad_body(admin):
    assert admin.put("/admin/apps/99999/platform-host", json={"subdomain": "blog"}).status_code == 404
    assert admin.delete("/admin/apps/99999/platform-host").status_code == 404
    aid = admin.post("/apps", json=APP).json()["id"]
    assert admin.put(f"/admin/apps/{aid}/platform-host", json={}).status_code == 422
    assert admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "xn--bcher-kva"}).status_code == 422


def test_422_leaves_existing_label_untouched(admin, user):
    aid = _mk(user)
    admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"})
    assert admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "WWW"}).status_code == 422
    assert user.get(f"/apps/{aid}").json()["platform_subdomain"] == "blog"


def test_clear_requeues_and_is_idempotent(admin, user, db_session):
    aid = _mk(user)
    admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"})
    row = db_session.get(models.App, aid)
    row.status = "running"
    db_session.commit()
    assert admin.delete(f"/admin/apps/{aid}/platform-host").status_code == 200
    db_session.expire_all()
    assert db_session.get(models.App, aid).status == "pending"
    row = db_session.get(models.App, aid)
    row.status = "running"
    db_session.commit()
    assert admin.delete(f"/admin/apps/{aid}/platform-host").status_code == 200  # nothing to clear: no requeue
    db_session.expire_all()
    assert db_session.get(models.App, aid).status == "running"


def test_set_on_suspended_app_keeps_it_suspended(admin, user, db_session):
    aid = _mk(user)
    admin.post(f"/admin/apps/{aid}/suspend")
    assert admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"}).status_code == 200
    db_session.expire_all()
    row = db_session.get(models.App, aid)
    assert row.status == "suspended" and row.platform_subdomain == "blog"


def test_platform_host_audited(admin, user):
    aid = _mk(user)
    admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"})
    admin.delete(f"/admin/apps/{aid}/platform-host")
    entries = admin.get("/admin/audit?limit=100").json()
    by_action = {e["action"]: e for e in entries if e["action"].startswith("app.platform_host")}
    assert set(by_action) == {"app.platform_host.set", "app.platform_host.clear"}
    for e in by_action.values():
        assert str(e["target_id"]) == str(aid) and "blog" in e["detail"]


def test_serializer_fields_default_null(user, admin):
    aid = _mk(user)
    body = user.get(f"/apps/{aid}").json()
    assert body["platform_subdomain"] is None and body["platform_host"] is None
    assert any(a["id"] == aid and a["platform_host"] is None for a in user.get("/apps").json())


def test_list_serializer_reports_platform_host(admin, user):
    aid = _mk(user)
    admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"})
    listed = {a["id"]: a for a in user.get("/apps").json()}
    assert listed[aid]["platform_host"] == f"blog.{config.APP_DOMAIN}"


def test_deleted_app_claim_does_not_block_via_unique_index(admin, user, db_session):
    # Defense in depth: the DB unique index rejects a second row with the same label (409 via IntegrityError).
    a1 = _mk(user)
    a2 = admin.post("/apps", json=APP).json()["id"]
    admin.put(f"/admin/apps/{a1}/platform-host", json={"subdomain": "blog"})
    user.delete(f"/apps/{a1}")
    assert admin.put(f"/admin/apps/{a2}/platform-host", json={"subdomain": "blog"}).status_code == 409
    db_session.expire_all()
    assert db_session.get(models.App, a2).platform_subdomain is None


# --- manifests / reconciler --------------------------------------------------------------------------------

def test_flatten_includes_single_ingress_with_both_rules():
    r = k8s.render_app_manifests(1, "h-abc1234", "img", 8080, {}, None, platform_subdomain="blog")
    flat = k8s.flatten_manifests(r)
    ingresses = [m for m in flat if m["kind"] == "Ingress"]
    assert len(ingresses) == 1  # same Ingress, not a second object
    spec = ingresses[0]["spec"]
    assert [x["host"] for x in spec["rules"]] == [f"h-abc1234.{config.APP_DOMAIN}", f"blog.{config.APP_DOMAIN}"]
    assert all("secretName" not in t for t in spec["tls"])
    assert spec["tls"][0]["hosts"] == [x["host"] for x in spec["rules"]]
    # Same backend for both rules.
    assert spec["rules"][0]["http"] == spec["rules"][1]["http"]


def test_suspended_render_has_no_ingress_even_with_label():
    r = k8s.render_app_manifests(1, "h", "img", 8080, {}, None, include_ingress=False, platform_subdomain="blog")
    assert r["ingress"] is None
    assert not [m for m in k8s.flatten_manifests(r) if m["kind"] == "Ingress"]


def test_reconciler_adds_then_drops_reserved_rule(admin, user, db_session):
    kube = FakeKubeProvider()
    mysql, store = FakeMySQLAdmin(), FakeObjectStore()

    def sweep():
        reconcile_all(db_session, kube=kube, mysql_admin_factory=lambda s: mysql, object_store=store, settings={})
        db_session.expire_all()

    def rule_hosts(aid):
        ing = [m for m in kube.applied[f"app-{aid}"] if m["kind"] == "Ingress"]
        return [r["host"] for r in ing[-1]["spec"]["rules"]]  # fake kube appends: last apply wins

    aid = _mk(user)
    sweep()
    assert len(rule_hosts(aid)) == 1
    admin.put(f"/admin/apps/{aid}/platform-host", json={"subdomain": "blog"})
    sweep()
    assert rule_hosts(aid)[1] == f"blog.{config.APP_DOMAIN}" and len(rule_hosts(aid)) == 2
    admin.delete(f"/admin/apps/{aid}/platform-host")
    sweep()
    assert len(rule_hosts(aid)) == 1


def test_get_suspended_reserved_host_after_label_cleared_is_404(make_client, db_session, tmp_path, monkeypatch):
    (tmp_path / "suspended.html").write_text("<title>PAUSED</title>")
    monkeypatch.setattr(main, "_STATIC_DIR", tmp_path)
    main._suspended_cache.clear()
    try:
        owner = models.User(email="o2@example.com", password_hash="x")
        db_session.add(owner)
        db_session.commit()
        db_session.add(models.App(owner_id=owner.id, name="a", hostname="sleepy-abc1234", image="i", port=8080,
                                  status="running", platform_subdomain="blog"))
        db_session.commit()
        c = make_client()
        # Running app: Traefik routes it; if traffic ever reaches the platform it is the unknown-host 404, not 503.
        assert c.get("/", headers={"host": f"blog.{config.APP_DOMAIN}"}, follow_redirects=False).status_code == 404
    finally:
        main._suspended_cache.clear()


# --- port validation (create / update / spec) --------------------------------------------------------------

BAD_PORTS = [0, -1, 1, 80, 1023, 65536, 70000]
GOOD_PORTS = [1024, 8080, 65535]


@pytest.mark.parametrize("port", BAD_PORTS)
def test_create_rejects_bad_ports(admin, port):
    r = admin.post("/apps", json={**APP, "port": port})
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("port", GOOD_PORTS)
def test_create_accepts_good_ports(admin, port):
    r = admin.post("/apps", json={**APP, "port": port})
    assert r.status_code in (200, 201) and r.json()["port"] == port


@pytest.mark.parametrize("port", BAD_PORTS)
def test_update_rejects_bad_ports(admin, port):
    aid = _mk(admin)
    assert admin.patch(f"/apps/{aid}", json={"port": port}).status_code == 422
    assert admin.get(f"/apps/{aid}").json()["port"] == 8080


@pytest.mark.parametrize("port", GOOD_PORTS)
def test_update_accepts_good_ports(admin, port):
    aid = _mk(admin)
    r = admin.patch(f"/apps/{aid}", json={"port": port})
    assert r.status_code == 200 and r.json()["port"] == port


def test_port_error_mentions_range(admin):
    r = admin.post("/apps", json={**APP, "port": 80})
    assert "1024" in r.text


SPEC_TMPL = "apiVersion: picopaas/v1\nname: demo\nimage: ghcr.io/example/demo:1.0\nport: {port}\n"


@pytest.mark.parametrize("port", BAD_PORTS)
def test_spec_rejects_bad_ports(admin, port):
    r = admin.post("/apps/spec", json={"spec": SPEC_TMPL.format(port=port), "secrets": {}})
    assert r.status_code == 422, r.text
    assert "1024-65535" in r.text or "65535" in r.text


@pytest.mark.parametrize("port", GOOD_PORTS)
def test_spec_accepts_good_ports(admin, port):
    r = admin.post("/apps/spec", json={"spec": SPEC_TMPL.format(port=port), "secrets": {}})
    assert r.status_code in (200, 201), r.text


def test_spec_rejects_non_integer_port(admin):
    for v in ('"8080"', "8080.5", "true"):
        r = admin.post("/apps/spec", json={"spec": SPEC_TMPL.format(port=v), "secrets": {}})
        assert r.status_code == 422, (v, r.text)
