"""Root images are rejected at create time (ARCHITECTURE.md "No root images")."""
import httpx
import pytest

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


def test_named_user_rejected_as_unverifiable(client):
    register_and_login(client, "named@example.com")
    _override_registry(FakeRegistry(default_size_bytes=1, users={IMAGE: "app"}))
    resp = _create_app(client)
    assert resp.status_code == 422
    assert "numeric UID" in resp.json()["detail"]


@pytest.mark.parametrize("user", ["10001", "10001:10001", "1000:0"])
def test_numeric_non_root_accepted(client, user):
    register_and_login(client, "ok@example.com")
    _override_registry(FakeRegistry(default_size_bytes=1, users={IMAGE: user}))
    assert _create_app(client).status_code == 201


def test_registry_inspect_reads_user_from_config_blob():
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": "sha256:cfg", "size": 10},
        "layers": [{"size": 5}, {"size": 7}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/":
            return httpx.Response(200)
        if request.url.path == "/v2/example/demo/manifests/1.0":
            return httpx.Response(200, json=manifest, headers={"content-type": manifest["mediaType"]})
        if request.url.path == "/v2/example/demo/blobs/sha256:cfg":
            return httpx.Response(307, headers={"location": "https://blobs.example.com/cfg"})
        if request.url.host == "blobs.example.com":
            return httpx.Response(200, json={"config": {"User": "10001"}})
        return httpx.Response(404)

    info = RegistryClient(client=httpx.Client(transport=httpx.MockTransport(handler))).inspect(IMAGE)
    assert info.size_bytes == 12
    assert info.user == "10001"
