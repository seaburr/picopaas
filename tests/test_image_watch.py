"""Digest pinning, the floating-tag watcher, check-image and restart-as-pull-latest."""
import httpx
import pytest

from app import image_check, models
from app.config import MAX_IMAGE_SIZE_BYTES
from app.dependencies import get_registry
from app.main import app as fastapi_app
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore, FakeRegistry
from app.providers.registry import RegistryClient
from app.reconciler import reconcile_app
from tests.conftest import register_and_login

TAG = "ghcr.io/example/demo:latest"
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
OVER = MAX_IMAGE_SIZE_BYTES + 5 * 1024 * 1024


@pytest.fixture()
def reg():
    r = FakeRegistry(default_size_bytes=1024, digests={TAG: D1})
    fastapi_app.dependency_overrides[get_registry] = lambda: r
    return r


def _create(client, image=TAG, **kw):
    resp = client.post("/apps", json={"name": "demo", "image": image, "port": 8080, **kw})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _row(db_session, app_id) -> models.App:
    db_session.expire_all()
    return db_session.get(models.App, app_id)


def _running(db_session, app_id):
    row = _row(db_session, app_id)
    row.status = "running"
    db_session.commit()
    return row


# --- pinning ---

def test_create_pins_digest_and_manifest_uses_it(client, db_session, reg):
    register_and_login(client, "pin@example.com")
    out = _create(client)
    assert out["image_digest"] == D1 and out["auto_update"] is True and out["image_checked_at"]
    row = _row(db_session, out["id"])
    kube = FakeKubeProvider()
    reconcile_app(db_session, row, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={"mysql_cidr": "10.1.0.0/16"})
    dep = next(m for ms in kube.applied.values() for m in ms if m["kind"] == "Deployment")
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == f"ghcr.io/example/demo@{D1}"
    assert dep["metadata"]["annotations"]["picopaas.io/image-ref"] == TAG


def test_null_digest_renders_plain_image_and_digest_ref_as_is(client, db_session, reg):
    register_and_login(client, "plain@example.com")
    row = _row(db_session, _create(client)["id"])
    row.image_digest = None
    assert image_check.pinned_image(row) == TAG
    row.image = f"ghcr.io/example/demo@{D2}"
    row.image_digest = D2
    assert image_check.pinned_image(row) == f"ghcr.io/example/demo@{D2}"


def test_digest_ref_create_keeps_ref(client, db_session, reg):
    register_and_login(client, "digestref@example.com")
    out = _create(client, image=f"ghcr.io/example/demo@{D2}")
    assert out["image_digest"] == D2


def _mock_registry(handler):
    return RegistryClient(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_real_registry_returns_manifest_digest_and_amd64_for_index():
    manifest = {"schemaVersion": 2, "config": {"digest": "sha256:cfg", "size": 1}, "layers": [{"size": 5}]}
    index = {"mediaType": "application/vnd.oci.image.index.v1+json",
             "manifests": [{"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
                           {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}}]}

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/v2/":
            return httpx.Response(200)
        if p.endswith("/manifests/single"):
            return httpx.Response(200, json=manifest, headers={"docker-content-digest": "sha256:hdr"})
        if p.endswith("/manifests/nohdr"):
            return httpx.Response(200, json=manifest)
        if p.endswith("/manifests/multi"):
            return httpx.Response(200, json=index, headers={"content-type": "application/vnd.oci.image.index.v1+json"})
        if p.endswith("/manifests/sha256:amd"):
            return httpx.Response(200, json=manifest)
        if "/blobs/" in p:
            return httpx.Response(200, json={"config": {"User": "10001"}})
        return httpx.Response(404)

    c = _mock_registry(handler)
    assert c.resolve_digest("registry.example.com/a/b:single") == "sha256:hdr"
    assert c.inspect("registry.example.com/a/b:single").digest == "sha256:hdr"
    assert c.resolve_digest("registry.example.com/a/b:nohdr").startswith("sha256:") and len(
        c.resolve_digest("registry.example.com/a/b:nohdr")) == 71
    assert c.resolve_digest("registry.example.com/a/b:multi") == "sha256:amd"
    assert c.inspect("registry.example.com/a/b:multi").digest == "sha256:amd"


# --- watcher ---

def test_watcher_detects_change_and_redeploys(client, db_session, reg):
    register_and_login(client, "watch@example.com")
    aid = _create(client)["id"]
    _running(db_session, aid)
    reg.digests[TAG] = D2
    counts = image_check.watch_all(db_session, reg)
    assert counts == {"updated": 1}
    row = _row(db_session, aid)
    assert row.image_digest == D2 and row.status == "pending" and row.image_update_error is None
    log = db_session.query(models.AuditLog).filter_by(action="app.image_updated").one()
    assert "111111111111" in log.detail and "222222222222" in log.detail


def test_watcher_unchanged_is_cheap(client, db_session, reg):
    register_and_login(client, "cheap@example.com")
    aid = _create(client)["id"]
    _running(db_session, aid)
    reg.inspect_calls = 0
    assert image_check.watch_all(db_session, reg) == {"unchanged": 1}
    assert reg.inspect_calls == 0 and _row(db_session, aid).status == "running"


def test_watcher_backfills_null_digest_without_rollout(client, db_session, reg):
    register_and_login(client, "fill@example.com")
    aid = _create(client)["id"]
    row = _running(db_session, aid)
    row.image_digest = None
    db_session.commit()
    assert image_check.watch_all(db_session, reg) == {"filled": 1}
    row = _row(db_session, aid)
    assert row.image_digest == D1 and row.status == "running"


@pytest.mark.parametrize("kind", ["size", "root"])
def test_watcher_rejects_bad_repush_keeps_old_and_clears_on_good(client, db_session, reg, kind):
    register_and_login(client, f"bad{kind}@example.com")
    aid = _create(client)["id"]
    _running(db_session, aid)
    reg.digests[TAG] = D2
    if kind == "size":
        reg.sizes[TAG] = OVER
    else:
        reg.users[TAG] = "root"
    assert image_check.watch_all(db_session, reg) == {"rejected": 1}
    row = _row(db_session, aid)
    assert row.image_digest == D1 and row.status == "running"
    assert "previous version" in row.image_update_error
    if kind == "size":
        assert "MB, over the 256 MB limit" in row.image_update_error
    out = client.get(f"/apps/{aid}").json()
    assert out["image_update_error"] and out["image_digest"] == D1
    # Fixed re-push: error clears and it rolls out.
    reg.sizes.pop(TAG, None)
    reg.users.pop(TAG, None)
    reg.digests[TAG] = "sha256:" + "3" * 64
    assert image_check.watch_all(db_session, reg) == {"updated": 1}
    row = _row(db_session, aid)
    assert row.image_update_error is None and row.status == "pending"


def test_watcher_registry_error_keeps_state(client, db_session, reg):
    register_and_login(client, "regerr@example.com")
    aid = _create(client)["id"]
    before = _row(db_session, aid).image_checked_at
    reg.failing.add(TAG)
    assert image_check.watch_all(db_session, reg) == {"error": 1}
    row = _row(db_session, aid)
    assert row.image_checked_at == before and row.image_digest == D1


def test_watcher_skips_opt_out_digest_refs_suspended(client, db_session, reg):
    register_and_login(client, "skip@example.com")
    a = _create(client, auto_update=False)["id"]
    reg.digests[TAG] = D2
    assert image_check.watch_all(db_session, reg) == {}
    assert _row(db_session, a).image_digest == D1
    # digest ref
    row = _row(db_session, a)
    row.auto_update = True
    row.image = f"ghcr.io/example/demo@{D1}"
    db_session.commit()
    reg.resolve_calls = 0
    assert image_check.watch_all(db_session, reg) == {} and reg.resolve_calls == 0
    # suspended
    row.image = TAG
    row.status = "suspended"
    db_session.commit()
    assert image_check.watch_all(db_session, reg) == {} and reg.resolve_calls == 0


def test_patch_auto_update(client, reg):
    register_and_login(client, "patchau@example.com")
    aid = _create(client)["id"]
    out = client.patch(f"/apps/{aid}", json={"auto_update": False}).json()
    assert out["auto_update"] is False


# --- endpoints ---

def test_check_image_authz_and_redeploy(make_client, db_session, reg):
    owner, other, admin = make_client(), make_client(), make_client()
    register_and_login(admin, "root@example.com")  # first user = uberadmin
    register_and_login(owner, "owner@example.com")
    register_and_login(other, "other@example.com")
    aid = _create(owner)["id"]
    _running(db_session, aid)
    anon = make_client()
    assert anon.post(f"/apps/{aid}/check-image").status_code in (401, 403)
    assert other.post(f"/apps/{aid}/check-image").status_code == 404
    reg.digests[TAG] = D2
    out = owner.post(f"/apps/{aid}/check-image")
    assert out.status_code == 200 and out.json()["image_digest"] == D2 and out.json()["status"] == "pending"
    reg.digests[TAG] = D1
    assert admin.post(f"/apps/{aid}/check-image").json()["image_digest"] == D1
    reg.failing.add(TAG)
    assert owner.post(f"/apps/{aid}/check-image").status_code == 502


def test_restart_re_resolves(client, db_session, reg):
    register_and_login(client, "restart@example.com")
    aid = _create(client)["id"]
    _running(db_session, aid)
    reg.digests[TAG] = D2
    out = client.post(f"/apps/{aid}/restart").json()
    assert out["image_digest"] == D2 and out["status"] == "pending"


def test_restart_with_bad_new_image_restarts_old(client, db_session, reg):
    register_and_login(client, "restartbad@example.com")
    aid = _create(client)["id"]
    _running(db_session, aid)
    reg.digests[TAG] = D2
    reg.sizes[TAG] = OVER
    out = client.post(f"/apps/{aid}/restart").json()
    assert out["image_digest"] == D1 and out["status"] == "pending"
    assert "previous version" in out["image_update_error"]
    assert _row(db_session, aid).restart_nonce
