"""Fake provider implementations (app/providers/fake.py) satisfy their interfaces
and behave sanely enough for tests that depend on them."""
import pytest

from app.providers.base import RegistryError
from app.providers.fake import (
    FakeKubeProvider,
    FakeMySQLAdmin,
    FakeObjectStore,
    FakeRegistry,
)


def test_fake_kube_provider_apply_tracks_namespace_and_status():
    kube = FakeKubeProvider()
    manifests = [
        {"kind": "Namespace", "metadata": {"name": "app-1"}},
        {"kind": "Deployment", "metadata": {"name": "app-1", "namespace": "app-1"}},
    ]
    kube.apply(manifests)
    assert kube.get_status("app-1") == "running"
    assert len(kube.applied["app-1"]) == 2


def test_fake_kube_provider_delete_namespace():
    kube = FakeKubeProvider()
    kube.apply([{"kind": "Namespace", "metadata": {"name": "app-1"}}])
    kube.delete_namespace("app-1")
    assert kube.get_status("app-1") == "deleted"
    assert "app-1" not in kube.applied
    assert "app-1" in kube.deleted


def test_fake_kube_provider_unknown_namespace_status():
    kube = FakeKubeProvider()
    assert kube.get_status("app-999") == "unknown"


def test_fake_mysql_admin_lifecycle():
    mysql = FakeMySQLAdmin()
    creds = mysql.create_db_user(5)
    assert creds["name"] == "app_5"
    assert creds["user"] == "app_5"
    assert mysql.db_size_bytes(5) == 0

    mysql.set_read_only(5, True)
    assert mysql.read_only[5] is True

    mysql.drop(5)
    assert mysql.db_size_bytes(5) == 0  # dropped apps report 0, not an error


def test_fake_object_store_lifecycle():
    store = FakeObjectStore()
    creds = store.create_bucket_and_key(5)
    assert creds["bucket"] == "pp-5"
    assert store.bucket_size_bytes(5) == 0

    store.set_read_only(5, True)
    assert store.read_only[5] is True

    store.delete(5)
    assert store.bucket_size_bytes(5) == 0


def test_fake_registry_exact_size():
    registry = FakeRegistry(sizes={"my/app:1.0": 123})
    assert registry.image_size_bytes("my/app:1.0") == 123


def test_fake_registry_default_size():
    registry = FakeRegistry(default_size_bytes=42)
    assert registry.image_size_bytes("anything:latest") == 42


def test_fake_registry_missing_raises():
    registry = FakeRegistry(missing={"private/app:latest"})
    with pytest.raises(RegistryError):
        registry.image_size_bytes("private/app:latest")


def test_fake_registry_unconfigured_raises():
    registry = FakeRegistry()
    with pytest.raises(RegistryError):
        registry.image_size_bytes("unconfigured:latest")
