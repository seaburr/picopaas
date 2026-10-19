"""Image size cap enforcement on app create/update (app/routers/apps.py),
exercised through the real HTTP endpoints with the registry dependency
overridden to a FakeRegistry per test."""
from app.dependencies import get_registry
from app.main import app
from app.providers.base import RegistryError
from app.providers.fake import FakeRegistry
from tests.conftest import register_and_login

OVER_CAP_BYTES = 513 * 1024 * 1024
UNDER_CAP_BYTES = 10 * 1024 * 1024


def _override_registry(fake: FakeRegistry) -> None:
    app.dependency_overrides[get_registry] = lambda: fake


def _create_app(client, **overrides):
    payload = {
        "name": "demo",
        "image": "ghcr.io/example/demo:1.0",
        "port": 8080,
        "want_db": False,
        "want_storage": False,
        "env": [],
    }
    payload.update(overrides)
    return client.post("/apps", json=payload)


def test_oversized_image_rejected_on_create(client):
    register_and_login(client, "oversized@example.com")
    _override_registry(FakeRegistry(sizes={"ghcr.io/example/demo:1.0": OVER_CAP_BYTES}))

    resp = _create_app(client)
    assert resp.status_code == 422
    assert "512" in resp.json()["detail"] or "byte" in resp.json()["detail"]


def test_image_under_cap_is_accepted(client):
    register_and_login(client, "undercap@example.com")
    _override_registry(FakeRegistry(sizes={"ghcr.io/example/demo:1.0": UNDER_CAP_BYTES}))

    resp = _create_app(client)
    assert resp.status_code == 201


def test_registry_error_maps_to_422_with_clear_message(client):
    register_and_login(client, "notfound@example.com")
    _override_registry(FakeRegistry(missing={"ghcr.io/example/demo:1.0"}))

    resp = _create_app(client)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "image not found or not public"


def test_oversized_image_rejected_on_update(client):
    register_and_login(client, "updatecap@example.com")
    fake = FakeRegistry(
        sizes={
            "ghcr.io/example/demo:1.0": UNDER_CAP_BYTES,
            "ghcr.io/example/too-big:1.0": OVER_CAP_BYTES,
        }
    )
    _override_registry(fake)

    created = _create_app(client)
    assert created.status_code == 201
    app_id = created.json()["id"]

    resp = client.patch(f"/apps/{app_id}", json={"image": "ghcr.io/example/too-big:1.0"})
    assert resp.status_code == 422

    # The app's image is unchanged after the rejected update.
    assert client.get(f"/apps/{app_id}").json()["image"] == "ghcr.io/example/demo:1.0"


def test_update_without_image_change_does_not_touch_registry(client):
    register_and_login(client, "noimagecheck@example.com")

    class ExplodingRegistry:
        def image_size_bytes(self, image_ref: str) -> int:
            raise AssertionError("registry should not be called when image is unchanged")

    # Create with the default fixture-provided fake, then swap in one that
    # fails any call, to prove update-without-image-change skips it.
    created = _create_app(client)
    assert created.status_code == 201
    app_id = created.json()["id"]

    _override_registry(ExplodingRegistry())
    resp = client.patch(f"/apps/{app_id}", json={"name": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed"
