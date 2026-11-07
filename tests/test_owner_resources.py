"""Owner resource sliders (PATCH /apps/{id} resources, spec resources, template metadata) and users.theme.
Real register/login flow; Kubernetes, MySQL and the object store are the fakes behind their interfaces."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import create_engine, inspect, text

from app import config, k8s_manifests as k8s, models, reconciler, resources
from app.main import app as fastapi_app
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.routers.apps import get_mysql_admin_factory, get_object_store
from app.routers.templates import CATALOG
from tests.conftest import register_and_login

APP = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
MB = 1024 * 1024
SPEC = """apiVersion: picopaas/v1
name: sliders
image: ghcr.io/example/demo:1.0
port: 8080
"""


@pytest.fixture()
def env(make_client, db_session):
    mysql, store = FakeMySQLAdmin(), FakeObjectStore()
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda server: mysql)
    fastapi_app.dependency_overrides[get_object_store] = lambda: store
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    owner = make_client()
    register_and_login(owner, "owner@example.com")
    return SimpleNamespace(admin=admin, owner=owner, store=store, db=db_session, make=make_client)


@pytest.fixture()
def aid(env):
    r = env.owner.post("/apps", json=APP)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _row(env, aid):
    env.db.expire_all()
    return env.db.get(models.App, aid)


def _patch(env, aid, res, client=None):
    return (client or env.owner).patch(f"/apps/{aid}", json={"resources": res})


def _admin(env, aid, body):
    r = env.admin.patch(f"/admin/apps/{aid}/resources", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _audit(env, action):
    env.db.expire_all()
    return env.db.query(models.AuditLog).filter_by(action=action).all()


# --- shapes ---------------------------------------------------------------------------------------

def test_new_app_exposes_controls_and_admin_values(env, aid):
    res = env.owner.get(f"/apps/{aid}").json()["resources"]
    assert res["controls"]["cpu_limit_m"] == {"value": 200, "owner": None, "min": config.OWNER_CPU_MIN_M, "max": 200}
    assert res["controls"]["mem_limit_mi"] == {"value": 384, "owner": None, "min": config.OWNER_MEM_MIN_MI, "max": 384}
    assert res["controls"]["ephemeral_limit_mi"]["max"] == 250
    assert res["controls"]["storage_quota_mb"] is None  # no bucket
    assert set(res["admin_values"]) == set(resources.FIELDS) and not any(res["admin_values"].values())


def test_me_limits_carry_owner_controls(env):
    lim = env.owner.get("/auth/me").json()["limits"]["owner_controls"]
    assert lim["cpu_limit_m"] == {"value": 200, "owner": None, "min": 50, "max": 200}
    assert lim["storage_quota_mb"]["max"] == 100 and lim["storage_quota_mb"]["min"] == config.OWNER_STORAGE_MIN_MB
    assert set(lim) == set(resources.OWNER_COLUMNS)


# --- PATCH happy path ----------------------------------------------------------------------------

def test_owner_lowers_values(env, aid):
    a = _row(env, aid)
    a.status = "running"
    env.db.commit()
    r = _patch(env, aid, {"cpu_limit_m": 100, "mem_limit_mi": 128, "ephemeral_limit_mi": 100})
    assert r.status_code == 200, r.text
    out = r.json()["resources"]
    assert (out["cpu_limit_m"], out["mem_limit_mi"], out["ephemeral_limit_mi"]) == (100, 128, 100)
    assert out["controls"]["cpu_limit_m"] == {"value": 100, "owner": 100, "min": 50, "max": 200}
    assert out["admin_values"]["cpu_limit_m"] is None and not out["overridden"]
    a = _row(env, aid)
    assert (a.owner_cpu_limit_m, a.owner_mem_limit_mi, a.owner_ephemeral_limit_mi) == (100, 128, 100)
    assert a.cpu_limit_m is None and a.mem_limit_mi is None  # admin columns never written
    assert a.status == "pending"
    e = _audit(env, "app.resources")[-1]
    assert e.target_id == str(aid) and e.detail.startswith("cpu_limit_m=None,") and " -> cpu_limit_m=100,mem_limit_mi=128," in e.detail


def test_request_stays_clamped_to_lowered_limit(env, aid):
    _patch(env, aid, {"cpu_limit_m": 50, "mem_limit_mi": 64})
    res = env.owner.get(f"/apps/{aid}").json()["resources"]
    assert res["cpu_request_m"] == 50 and res["mem_request_mi"] == 64


def test_value_at_ceiling_follows_the_ceiling(env, aid):
    assert _patch(env, aid, {"cpu_limit_m": 200}).status_code == 200
    assert _row(env, aid).owner_cpu_limit_m is None
    _patch(env, aid, {"cpu_limit_m": 100})
    _admin(env, aid, {"cpu_limit_m": 400})
    assert _patch(env, aid, {"cpu_limit_m": 400}).json()["resources"]["cpu_limit_m"] == 400
    assert _row(env, aid).owner_cpu_limit_m is None


def test_omit_is_unchanged_and_null_resets(env, aid):
    _patch(env, aid, {"cpu_limit_m": 100, "mem_limit_mi": 128})
    assert env.owner.patch(f"/apps/{aid}", json={"name": "renamed"}).json()["resources"]["cpu_limit_m"] == 100
    out = _patch(env, aid, {"mem_limit_mi": 256}).json()["resources"]
    assert (out["cpu_limit_m"], out["mem_limit_mi"]) == (100, 256)  # omitted cpu unchanged
    out = _patch(env, aid, {"cpu_limit_m": None}).json()["resources"]
    assert (out["cpu_limit_m"], out["mem_limit_mi"]) == (200, 256)
    assert _row(env, aid).owner_cpu_limit_m is None


def test_noop_change_does_not_restart(env, aid):
    _patch(env, aid, {"cpu_limit_m": 100})
    a = _row(env, aid)
    a.status = "running"
    env.db.commit()
    assert _patch(env, aid, {"cpu_limit_m": 100}).status_code == 200
    assert _row(env, aid).status == "running"
    assert len(_audit(env, "app.resources")) == 1


# --- admin interplay -----------------------------------------------------------------------------

def test_admin_lowering_below_owner_choice_wins_and_raising_keeps_it(env, aid):
    _admin(env, aid, {"cpu_limit_m": 500})
    _patch(env, aid, {"cpu_limit_m": 300})
    assert env.owner.get(f"/apps/{aid}").json()["resources"]["cpu_limit_m"] == 300
    d = _admin(env, aid, {"cpu_limit_m": 150})
    assert d["resources"]["cpu_limit_m"] == 150  # min(owner 300, ceiling 150)
    assert d["resources"]["controls"]["cpu_limit_m"] == {"value": 150, "owner": 300, "min": 50, "max": 150}
    assert d["resources"]["admin_values"]["cpu_limit_m"] == 150
    d = _admin(env, aid, {"cpu_limit_m": 1000})
    assert d["resources"]["cpu_limit_m"] == 300  # raising does not change the owner's choice
    assert d["resources"]["admin_values"]["cpu_limit_m"] == 1000
    assert _row(env, aid).owner_cpu_limit_m == 300


def test_owner_choice_does_not_hide_admin_raise_flag(env, aid):
    d = _admin(env, aid, {"cpu_limit_m": 500})
    assert d["exceeds_platform_caps"] is True
    _patch(env, aid, {"cpu_limit_m": 100})
    res = env.owner.get(f"/apps/{aid}").json()["resources"]
    assert res["exceeds_platform_caps"] is True and res["overridden"] is True and res["cpu_limit_m"] == 100


def test_admin_resource_save_never_writes_owner_values(env, aid):
    _patch(env, aid, {"mem_limit_mi": 128})
    _admin(env, aid, {"cpu_limit_m": 300})
    a = _row(env, aid)
    assert a.mem_limit_mi is None and a.owner_mem_limit_mi == 128 and a.owner_cpu_limit_m is None


def test_ceiling_below_floor_is_still_saveable(env, aid):
    _admin(env, aid, {"cpu_limit_m": 20, "cpu_request_m": 10})
    c = env.owner.get(f"/apps/{aid}").json()["resources"]["controls"]["cpu_limit_m"]
    assert c["min"] == 20 and c["max"] == 20  # floor 50 never exceeds the ceiling
    assert _patch(env, aid, {"cpu_limit_m": 20}).status_code == 200


# --- validation ----------------------------------------------------------------------------------

@pytest.mark.parametrize("body,field", [
    ({"cpu_limit_m": 201}, "cpu_limit_m"), ({"cpu_limit_m": 49}, "cpu_limit_m"),
    ({"mem_limit_mi": 385}, "mem_limit_mi"), ({"mem_limit_mi": 63}, "mem_limit_mi"),
    ({"ephemeral_limit_mi": 251}, "ephemeral_limit_mi"), ({"ephemeral_limit_mi": 63}, "ephemeral_limit_mi"),
])
def test_out_of_range_422_names_range(env, aid, body, field):
    r = _patch(env, aid, body)
    assert r.status_code == 422
    err = r.json()["detail"][0]
    assert err["loc"] == ["body", "resources", field]
    assert "must be between" in err["msg"] and " and " in err["msg"]
    assert _row(env, aid).owner_cpu_limit_m is None and _row(env, aid).owner_mem_limit_mi is None


def test_error_does_not_apply_other_changes(env, aid):
    r = env.owner.patch(f"/apps/{aid}", json={"name": "renamed", "resources": {"cpu_limit_m": 9999}})
    assert r.status_code == 422
    assert _row(env, aid).name == "demo"


@pytest.mark.parametrize("res", [{"bogus": 1}, {"cpu_limit_m": "100"}, {"cpu_limit_m": True}, {"cpu_limit_m": 100.5}])
def test_resources_strict(env, aid, res):
    assert _patch(env, aid, res).status_code == 422


def test_access_control(env, aid):
    assert env.make().patch(f"/apps/{aid}", json={"resources": {"cpu_limit_m": 100}}).status_code == 401
    other = env.make()
    register_and_login(other, "other@example.com")
    assert _patch(env, aid, {"cpu_limit_m": 100}, other).status_code == 404
    assert _row(env, aid).owner_cpu_limit_m is None


def test_project_member_can_set_like_patch(env):
    owner = env.owner
    pid = owner.post("/projects", json={"name": "Team"}).json()["id"]
    app_id = owner.post("/apps", json={**APP, "project_id": pid}).json()["id"]
    member = env.make()
    register_and_login(member, "member@example.com")
    inv = owner.post(f"/projects/{pid}/invites", json={"role": "member"}).json()["token"]
    assert member.post(f"/invites/{inv}/accept").status_code == 200
    assert member.patch(f"/apps/{app_id}", json={"name": "x"}).status_code == 200  # PATCH is open to members
    assert _patch(env, app_id, {"cpu_limit_m": 100}, member).status_code == 200


def test_suspended_app_cannot_be_changed(env, aid):
    assert env.admin.post(f"/admin/apps/{aid}/suspend").status_code in (200, 204)
    assert _patch(env, aid, {"cpu_limit_m": 100}).status_code == 403
    assert _row(env, aid).owner_cpu_limit_m is None


def test_admin_resource_change_on_suspended_app_keeps_suspension(env, aid):
    env.admin.post(f"/admin/apps/{aid}/suspend")
    _patch(env, aid, {"cpu_limit_m": 100})
    assert _row(env, aid).status == "suspended"


# --- storage quota -------------------------------------------------------------------------------

def _bucket_app(env, used_mb):
    aid = env.owner.post("/apps", json={**APP, "name": "bucket", "want_storage": True}).json()["id"]
    a = _row(env, aid)
    a.status = "running"
    env.db.add(models.AppStorage(app_id=aid, bucket="b", endpoint="e", region="r", access_key="k",
                                 secret_key_encrypted="x", size_bytes=used_mb * MB))
    env.db.commit()
    env.store.sizes[aid] = used_mb * MB
    return aid


def test_storage_quota_needs_a_bucket(env, aid):
    r = _patch(env, aid, {"storage_quota_mb": 10})
    assert r.status_code == 422 and "no storage bucket" in r.json()["detail"][0]["msg"]


def test_storage_quota_cannot_go_below_current_use(env):
    aid = _bucket_app(env, 30)
    r = _patch(env, aid, {"storage_quota_mb": 20})
    assert r.status_code == 422 and "already using 30 MB" in r.json()["detail"][0]["msg"]
    assert _row(env, aid).owner_storage_quota_mb is None


def test_storage_quota_change_rechecks_and_does_not_restart(env):
    aid = _bucket_app(env, 5)
    r = _patch(env, aid, {"storage_quota_mb": 10})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["resources"]["storage_quota_mb"] == 10 and out["quota"]["storage"]["limit_bytes"] == 10 * MB
    assert out["resources"]["controls"]["storage_quota_mb"] == {"value": 10, "owner": 10, "min": 1, "max": 100}
    a = _row(env, aid)
    assert a.status == "running" and a.owner_storage_quota_mb == 10 and a.storage_quota_mb is None  # no pod roll
    assert a.storage.over_quota is False
    # the quota poll sees the owner's limit
    store_size = env.store.sizes[aid]
    assert store_size == 5 * MB and resources.storage_quota_bytes(a) == 10 * MB


def test_storage_quota_limit_is_enforced_by_poll(env):
    from app import quota
    aid = _bucket_app(env, 5)
    _patch(env, aid, {"storage_quota_mb": 10})
    env.store.sizes[aid] = 12 * MB
    quota.check_app_quota(env.db, _row(env, aid), mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=env.store)
    assert _row(env, aid).storage.over_quota is True


# --- create --------------------------------------------------------------------------------------

def test_create_with_resources(env):
    r = env.owner.post("/apps", json={**APP, "resources": {"cpu_limit_m": 100, "mem_limit_mi": 64}})
    assert r.status_code == 201, r.text
    assert r.json()["resources"]["cpu_limit_m"] == 100 and r.json()["resources"]["mem_limit_mi"] == 64


def test_create_with_bad_resources_422_and_nothing_created(env):
    r = env.owner.post("/apps", json={**APP, "resources": {"cpu_limit_m": 5000}})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "resources", "cpu_limit_m"]
    assert env.owner.get("/apps").json() == []
    assert env.owner.post("/apps", json={**APP, "resources": {"storage_quota_mb": 5}}).status_code == 422  # no bucket


# --- manifests / reconciler ----------------------------------------------------------------------

def test_reconciler_renders_lowered_limits_consistently(env, aid):
    _patch(env, aid, {"cpu_limit_m": 100, "mem_limit_mi": 128, "ephemeral_limit_mi": 100})
    kube = FakeKubeProvider()
    reconciler.reconcile_all(env.db, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=env.store,
                             settings=SimpleNamespace(mysql_egress_mode="cidr", mysql_cidr=None))
    applied = kube.applied[f"app-{aid}"]
    dep = [m for m in applied if m["kind"] == "Deployment"][-1]
    c = dep["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert c["limits"] == {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "100Mi"}
    assert c["requests"]["cpu"] == "64m" and c["requests"]["memory"] == "128Mi"
    lr = [m for m in applied if m["kind"] == "LimitRange"][-1]["spec"]["limits"][0]
    assert lr["max"] == {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "100Mi"}
    q = [m for m in applied if m["kind"] == "ResourceQuota"][-1]["spec"]["hard"]
    assert q["limits.cpu"] == "200m" and q["limits.memory"] == "256Mi"


def test_manifest_clamps_request_to_owner_limit():
    eff = resources.effective(SimpleNamespace(owner_cpu_limit_m=50, owner_mem_limit_mi=64, owner_ephemeral_limit_mi=64))
    m = k8s.render_app_manifests(1, "h", "img", 80, {}, None, resources=eff)
    c = m["deployment"]["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert c["requests"]["cpu"] == "50m" and c["requests"]["memory"] == "64Mi"
    assert c["limits"] == {"cpu": "50m", "memory": "64Mi", "ephemeral-storage": "64Mi"}


# --- spec ----------------------------------------------------------------------------------------

def _spec(env, extra="", client=None, **kw):
    return (client or env.owner).post("/apps/spec", json={"spec": SPEC + extra, **kw})


def test_spec_resources_accepted(env):
    r = _spec(env, "resources:\n  cpuM: 100\n  memoryMi: 64\n  scratchMi: 64\n")
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True
    res = r.json()["resources"]
    assert (res["cpu_limit_m"], res["mem_limit_mi"], res["ephemeral_limit_mi"]) == (100, 64, 64)


def test_spec_omitted_resources_leave_values_unchanged_on_reapply(env):
    _spec(env, "resources:\n  cpuM: 100\n")
    a = _row(env, env.owner.get("/apps").json()[0]["id"])
    a.status = "running"
    env.db.commit()
    r = _spec(env)  # Kaisen-style redeploy: no resources key
    assert r.status_code == 200 and r.json()["created"] is False
    assert r.json()["resources"]["cpu_limit_m"] == 100
    assert _row(env, a.id).status == "running"  # nothing changed, so no pod roll
    r = _spec(env, "resources:\n  storage: false\n")  # resources present but no sliders
    assert r.json()["resources"]["cpu_limit_m"] == 100


def test_spec_change_rolls_pod_and_null_resets(env):
    _spec(env, "resources:\n  cpuM: 100\n")
    aid = env.owner.get("/apps").json()[0]["id"]
    a = _row(env, aid)
    a.status = "running"
    env.db.commit()
    assert _spec(env, "resources:\n  cpuM: 80\n").json()["resources"]["cpu_limit_m"] == 80
    assert _row(env, aid).status == "pending"
    assert _spec(env, "resources:\n  cpuM: null\n").json()["resources"]["cpu_limit_m"] == 200
    assert any(e.target_id == str(aid) for e in _audit(env, "app.resources"))


@pytest.mark.parametrize("extra,needle", [
    ("resources:\n  cpuM: 5000\n", "CPU must be between"),
    ("resources:\n  memoryMi: 8\n", "Memory must be between"),
    ("resources:\n  storageMb: 5\n", "no storage bucket"),
])
def test_spec_out_of_range_rejected(env, extra, needle):
    r = _spec(env, extra)
    assert r.status_code == 422 and needle in r.json()["detail"]
    assert env.owner.get("/apps").json() == []


@pytest.mark.parametrize("extra", ['resources:\n  cpuM: "100"\n', "resources:\n  cpuM: 1.5\n", "resources:\n  cpu: 100\n",
                                   "resources:\n  cpuM: true\n"])
def test_spec_strict(env, extra):
    assert _spec(env, extra).status_code == 422


# --- templates -----------------------------------------------------------------------------------

STATIC = {"static-site", "it-tools", "excalidraw"}


def test_templates_expose_resources(env):
    by_id = {t["id"]: t for t in env.owner.get("/templates").json()}
    for tid in STATIC:
        res = by_id[tid]["resources"]
        assert res["mem_limit_mi"] >= config.OWNER_MEM_MIN_MI and res["cpu_limit_m"] >= config.OWNER_CPU_MIN_M
        assert res["cpu_limit_m"] < config.DEFAULT_CPU_LIMIT_M and res["mem_limit_mi"] < config.DEFAULT_MEM_LIMIT_MI
    assert by_id["ghost"]["resources"] is None


def test_template_resources_deploy_within_ceilings(env):
    for i, t in enumerate(t for t in CATALOG if t.resources):
        c = env.make()  # one user each: the per-user app limit is small
        register_and_login(c, f"tpl{i}@example.com")
        body = {**APP, "name": t.id, "resources": t.resources.model_dump(exclude_none=True)}
        assert c.post("/apps", json=body).status_code == 201, t.id


def test_example_yaml_matches_template_resources():
    names = {"cpuM": "cpu_limit_m", "memoryMi": "mem_limit_mi", "scratchMi": "ephemeral_limit_mi"}
    for t in CATALOG:
        if not t.resources:
            continue
        path = Path(__file__).parent.parent / "examples" / t.id / f"{t.id}.yaml"
        if not path.exists():
            continue
        got = yaml.safe_load(path.read_text()).get("resources", {})
        want = t.resources.model_dump(exclude_none=True)
        assert {names[k]: v for k, v in got.items() if k in names} == want, t.id


# --- theme ---------------------------------------------------------------------------------------

def test_theme_default_and_set(env):
    assert env.owner.get("/auth/me").json()["theme"] == "dark"
    r = env.owner.patch("/auth/me", json={"theme": "light"})
    assert r.status_code == 200 and r.json()["theme"] == "light"
    assert env.owner.get("/auth/me").json()["theme"] == "light"
    env.db.expire_all()
    assert env.db.query(models.User).filter_by(email="owner@example.com").one().theme == "light"
    assert any("theme=light" in e.detail for e in _audit(env, "user.update_profile"))


@pytest.mark.parametrize("bad", ["blue", None, "", 1])
def test_theme_validated(env, bad):
    assert env.owner.patch("/auth/me", json={"theme": bad}).status_code == 422


def test_theme_and_timezone_are_independent(env):
    env.owner.patch("/auth/me", json={"timezone": "Europe/Paris"})
    r = env.owner.patch("/auth/me", json={"theme": "light"})
    assert r.json()["timezone"] == "Europe/Paris" and r.json()["theme"] == "light"  # omitted = unchanged
    r = env.owner.patch("/auth/me", json={"timezone": None})
    assert r.json()["timezone"] is None and r.json()["theme"] == "light"  # explicit null still clears the timezone


def test_theme_requires_login(env):
    assert env.make().patch("/auth/me", json={"theme": "light"}).status_code == 401


# --- onboarding_dismissed ------------------------------------------------------------------------

def test_onboarding_dismissed_default_false(env):
    assert env.owner.get("/auth/me").json()["onboarding_dismissed"] is False


def test_onboarding_dismissed_set_persists_and_audited(env):
    r = env.owner.patch("/auth/me", json={"onboarding_dismissed": True})
    assert r.status_code == 200 and r.json()["onboarding_dismissed"] is True
    assert env.owner.get("/auth/me").json()["onboarding_dismissed"] is True
    env.db.expire_all()
    assert env.db.query(models.User).filter_by(email="owner@example.com").one().onboarding_dismissed is True
    assert any("onboarding_dismissed=True" in e.detail for e in _audit(env, "user.update_profile"))


def test_onboarding_dismissed_omitted_unchanged(env):
    env.owner.patch("/auth/me", json={"onboarding_dismissed": True})
    r = env.owner.patch("/auth/me", json={"theme": "light"})
    assert r.json()["onboarding_dismissed"] is True and r.json()["theme"] == "light"
    r = env.owner.patch("/auth/me", json={"onboarding_dismissed": False})
    assert r.json()["onboarding_dismissed"] is False and r.json()["theme"] == "light"


@pytest.mark.parametrize("bad", [None, "maybe", [1]])
def test_onboarding_dismissed_validated(env, bad):
    assert env.owner.patch("/auth/me", json={"onboarding_dismissed": bad}).status_code == 422


def test_onboarding_dismissed_other_user_unaffected(env):
    env.owner.patch("/auth/me", json={"onboarding_dismissed": True})
    assert env.admin.get("/auth/me").json()["onboarding_dismissed"] is False


def test_onboarding_dismissed_requires_login(env):
    assert env.make().patch("/auth/me", json={"onboarding_dismissed": True}).status_code == 401


# --- migration -----------------------------------------------------------------------------------

def test_migration_defaults(tmp_path):
    url = f"sqlite:///{tmp_path}/m.db"
    root = os.path.join(os.path.dirname(__file__), "..")
    e = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=e, check=True,
                              capture_output=True, text=True).stdout
    alembic("upgrade", "a7f2c9d41b36")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("INSERT INTO users (email, password_hash, role, status, app_limit, created_at, must_change_password) "
                       "VALUES ('u@example.com', 'x', 'user', 'active', 3, '2026-01-01', 0)"))
    alembic("upgrade", "head")
    assert len(alembic("heads").strip().splitlines()) == 1
    cols = {c["name"] for c in inspect(create_engine(url)).get_columns("apps")}
    assert set(resources.OWNER_COLUMNS.values()) <= cols
    with create_engine(url).connect() as c:
        assert c.execute(text("SELECT theme FROM users")).scalar() == "dark"
        assert not c.execute(text("SELECT onboarding_dismissed FROM users")).scalar()
    alembic("downgrade", "a7f2c9d41b36")
    assert "owner_cpu_limit_m" not in {c["name"] for c in inspect(create_engine(url)).get_columns("apps")}
