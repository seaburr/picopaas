"""Root images are rejected at create time (ARCHITECTURE.md "No root images")."""
import httpx
import pytest

from app.config import NAMED_USER_RUN_AS_UID
from app.providers.fake import FakeRegistry
from app.providers.registry import RegistryClient
from tests.conftest import register_and_login
from tests.test_image_cap import _create_app, _override_registry

IMAGE = "ghcr.io/example/demo:1.0"


@pytest.mark.parametrize("user", ["", "root", "0", "0:0", "root:root"])
def test_root_images_rejected(client, user):
    register_and_login(client, "root@example.com")
    _override_registry(FakeRegistry(default_size_bytes=1, users={IMAGE: user}))
    resp = _create_app(client)
    assert resp.status_code == 422
    assert "root" in resp.json()["detail"]


def _run_as_uid(db_session, app_id):
    from app import models

    db_session.expire_all()
    return db_session.get(models.App, app_id).run_as_uid


@pytest.mark.parametrize("user", ["app", "app:app", "nobody"])
def test_named_user_accepted_with_forced_uid(client, db_session, user):
    register_and_login(client, "named@example.com")
    _override_registry(FakeRegistry(default_size_bytes=1, users={IMAGE: user}))
    resp = _create_app(client)
    assert resp.status_code == 201
    assert _run_as_uid(db_session, resp.json()["id"]) == NAMED_USER_RUN_AS_UID


@pytest.mark.parametrize("user", ["10001", "1000:app"])
def test_numeric_user_leaves_uid_unset(client, db_session, user):
    register_and_login(client, "numeric@example.com")
    _override_registry(FakeRegistry(default_size_bytes=1, users={IMAGE: user}))
    resp = _create_app(client)
    assert resp.status_code == 201
    assert _run_as_uid(db_session, resp.json()["id"]) is None


def test_image_update_recomputes_run_as_uid(client, db_session):
    register_and_login(client, "upd@example.com")
    other = "ghcr.io/example/other:1.0"
    _override_registry(FakeRegistry(default_size_bytes=1, users={IMAGE: "app", other: "10001"}))
    app_id = _create_app(client).json()["id"]
    assert _run_as_uid(db_session, app_id) == NAMED_USER_RUN_AS_UID
    assert client.patch(f"/apps/{app_id}", json={"image": other}).status_code == 200
    assert _run_as_uid(db_session, app_id) is None
    assert client.patch(f"/apps/{app_id}", json={"image": IMAGE}).status_code == 200
    assert _run_as_uid(db_session, app_id) == NAMED_USER_RUN_AS_UID


def test_root_update_rejected(client):
    register_and_login(client, "rootupd@example.com")
    other = "ghcr.io/example/rootimg:1.0"
    _override_registry(FakeRegistry(default_size_bytes=1, users={IMAGE: "10001", other: "root"}))
    app_id = _create_app(client).json()["id"]
    assert client.patch(f"/apps/{app_id}", json={"image": other}).status_code == 422


@pytest.mark.parametrize("user", ["10001", "10001:10001", "1000:0"])
def test_numeric_non_root_accepted(client, user):
    register_and_login(client, "ok@example.com")
    _override_registry(FakeRegistry(default_size_bytes=1, users={IMAGE: user}))
    assert _create_app(client).status_code == 201


def test_registry_inspect_reads_user_from_config_blob():
    import hashlib
    import json

    cfg_raw = json.dumps({"config": {"User": "10001"}}).encode()
    cfg_digest = "sha256:" + hashlib.sha256(cfg_raw).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": cfg_digest, "size": 10},
        "layers": [{"size": 5}, {"size": 7}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/":
            return httpx.Response(200)
        if request.url.path == "/v2/example/demo/manifests/1.0":
            return httpx.Response(200, json=manifest, headers={"content-type": manifest["mediaType"]})
        if request.url.path == "/v2/example/demo/blobs/" + cfg_digest:
            return httpx.Response(307, headers={"location": "https://blobs.example.com/cfg"})
        if request.url.host == "blobs.example.com":
            return httpx.Response(200, content=cfg_raw)
        return httpx.Response(404)

    info = RegistryClient(client=httpx.Client(transport=httpx.MockTransport(handler))).inspect(IMAGE)
    assert info.size_bytes == 12
    assert info.user == "10001"
