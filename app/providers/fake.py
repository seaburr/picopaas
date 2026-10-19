"""In-memory fake providers for tests. Never used outside tests/dev."""
from __future__ import annotations

import secrets
from typing import Any, Optional

from app.providers.base import (
    ImageInfo,
    KubeProvider,
    MySQLAdmin,
    ObjectStore,
    Registry,
    RegistryError,
)


class FakeKubeProvider(KubeProvider):
    def __init__(self) -> None:
        self.applied: dict[str, list[dict[str, Any]]] = {}
        self.deleted: set[str] = set()
        self.statuses: dict[str, str] = {}

    @staticmethod
    def _namespace_of(manifest: dict[str, Any]) -> Optional[str]:
        metadata = manifest.get("metadata", {})
        ns = metadata.get("namespace")
        if ns:
            return ns
        if manifest.get("kind") == "Namespace":
            return metadata.get("name")
        return None

    def apply(self, manifests: list[dict[str, Any]]) -> None:
        for manifest in manifests:
            ns = self._namespace_of(manifest)
            if ns is None:
                continue
            self.applied.setdefault(ns, []).append(manifest)
            self.statuses[ns] = "running"

    def delete_namespace(self, namespace: str) -> None:
        self.deleted.add(namespace)
        self.applied.pop(namespace, None)
        self.statuses[namespace] = "deleted"

    def get_status(self, namespace: str) -> str:
        return self.statuses.get(namespace, "unknown")


class FakeMySQLAdmin(MySQLAdmin):
    def __init__(self, server: Any = None) -> None:
        # `server` (a DbServer, in real use) is accepted but unused — kept
        # for API parity with the real MySQLAdminClient, which is
        # constructed per-server (see app/providers/base.py::MySQLAdmin).
        self.server = server
        self.creds: dict[int, dict[str, Any]] = {}
        self.sizes: dict[int, int] = {}
        self.read_only: dict[int, bool] = {}

    def create_db_user(self, app_id: int) -> dict[str, Any]:
        creds = {
            "host": "mysql.picopaas-data.svc.cluster.local",
            "port": 3306,
            "name": f"app_{app_id}",
            "user": f"app_{app_id}",
            "password": secrets.token_urlsafe(24),
        }
        creds["url"] = (
            f"mysql://{creds['user']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['name']}"
        )
        self.creds[app_id] = creds
        self.sizes[app_id] = 0
        self.read_only[app_id] = False
        return creds

    def drop(self, app_id: int) -> None:
        self.creds.pop(app_id, None)
        self.sizes.pop(app_id, None)
        self.read_only.pop(app_id, None)

    def db_size_bytes(self, app_id: int) -> int:
        return self.sizes.get(app_id, 0)

    def set_read_only(self, app_id: int, read_only: bool) -> None:
        self.read_only[app_id] = read_only


class FakeObjectStore(ObjectStore):
    def __init__(self) -> None:
        self.creds: dict[int, dict[str, Any]] = {}
        self.sizes: dict[int, int] = {}
        self.read_only: dict[int, bool] = {}

    def create_bucket_and_key(self, app_id: int) -> dict[str, Any]:
        creds = {
            "endpoint": "atl1.digitaloceanspaces.com",
            "region": "atl1",
            "bucket": f"pp-{app_id}",
            "access_key": secrets.token_hex(10),
            "secret_key": secrets.token_urlsafe(32),
        }
        self.creds[app_id] = creds
        self.sizes[app_id] = 0
        self.read_only[app_id] = False
        return creds

    def delete(self, app_id: int) -> None:
        self.creds.pop(app_id, None)
        self.sizes.pop(app_id, None)
        self.read_only.pop(app_id, None)

    def bucket_size_bytes(self, app_id: int) -> int:
        return self.sizes.get(app_id, 0)

    def set_read_only(self, app_id: int, read_only: bool) -> None:
        self.read_only[app_id] = read_only


class FakeRegistry(Registry):
    """Configurable fake: exact sizes per ref, a fallback default, and a
    `missing` set to simulate "not found or not public"."""

    def __init__(
        self,
        sizes: Optional[dict[str, int]] = None,
        default_size_bytes: Optional[int] = None,
        missing: Optional[set[str]] = None,
        users: Optional[dict[str, str]] = None,
        default_user: str = "10001",
    ) -> None:
        self.users = dict(users or {})
        self.default_user = default_user
        self.sizes = dict(sizes or {})
        self.default_size_bytes = default_size_bytes
        self.missing = set(missing or set())

    def image_size_bytes(self, image_ref: str) -> int:
        if image_ref in self.missing:
            raise RegistryError(f"image not found or not public: {image_ref!r}")
        if image_ref in self.sizes:
            return self.sizes[image_ref]
        if self.default_size_bytes is not None:
            return self.default_size_bytes
        raise RegistryError(f"no fake size configured for {image_ref!r}")

    def inspect(self, image_ref: str) -> ImageInfo:
        return ImageInfo(self.image_size_bytes(image_ref), self.users.get(image_ref, self.default_user))
