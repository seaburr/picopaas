"""In-memory fake providers for tests. Never used outside tests/dev."""
from __future__ import annotations

import secrets
from typing import Any, Optional

from app.providers.base import (
    ImageInfo,
    PodLogs,
    PodSection,
    KubeProvider,
    MySQLAdmin,
    MySQLAdminError,
    ObjectStore,
    ObjectStoreError,
    Registry,
    RegistryError,
)


class FakeKubeProvider(KubeProvider):
    def __init__(self) -> None:
        self.applied: dict[str, list[dict[str, Any]]] = {}
        self.deleted: set[str] = set()
        self.statuses: dict[str, str] = {}
        self.snapshot: dict[str, Any] = {"nodes": [], "pods": [], "metrics_available": True, "errors": []}
        self.snapshot_calls = 0
        self.list_ns_calls = 0
        # Tests: force what get_status reports after the next apply (ns -> "error: quota", "deploying", ...).
        self.forced_status: dict[str, str] = {}
        self.hints: dict[str, str] = {}  # ns -> pending_hint
        self.logs: dict[str, PodLogs] = {}  # ns -> canned logs
        self.pod_lines: dict[str, dict[str, list[str]]] = {}  # ns -> {pod name: log lines} (multi-pod)
        self.log_calls: list[tuple[str, int]] = []
        self.used_limits: dict[str, Optional[dict[str, int]]] = {}
        self.pod_events: dict[str, list[dict[str, Any]]] = {}  # ns -> events (shape: KubeProvider.app_pod_events)
        self.pod_event_calls = 0

    def cluster_snapshot(self) -> dict[str, Any]:
        self.snapshot_calls += 1
        return self.snapshot

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
            self.statuses[ns] = self.forced_status.get(ns, "running")

    def delete_namespace(self, namespace: str) -> None:
        self.deleted.add(namespace)
        self.applied.pop(namespace, None)
        self.statuses[namespace] = "deleted"

    def delete_ingress(self, namespace: str, name: str) -> None:
        self.applied[namespace] = [
            m for m in self.applied.get(namespace, [])
            if not (m.get("kind") == "Ingress" and m["metadata"]["name"] == name)
        ]

    def delete_network_policy(self, namespace: str, name: str) -> None:
        self.applied[namespace] = [
            m for m in self.applied.get(namespace, [])
            if not (m.get("kind") == "NetworkPolicy" and m["metadata"]["name"] == name)
        ]

    def list_app_namespaces(self) -> set[str]:
        self.list_ns_calls += 1
        return {ns for ns in self.applied if ns.startswith("app-")}

    def get_status(self, namespace: str) -> str:
        return self.statuses.get(namespace, "unknown")

    def get_used_limits(self, namespace: str) -> Optional[dict[str, int]]:
        """Limits consumed by existing pods. Tests set `used_limits[ns]` to simulate pods; otherwise none."""
        return self.used_limits.get(namespace)

    def app_pod_events(self) -> dict[str, list[dict[str, Any]]]:
        self.pod_event_calls += 1
        return {ns: list(evs) for ns, evs in self.pod_events.items()}

    def pending_hint(self, namespace: str) -> str:
        return self.hints.get(namespace, "none")

    def pod_logs(self, namespace: str, tail: int, pod: str | None = None) -> PodLogs:
        self.log_calls.append((namespace, tail))
        multi = self.pod_lines.get(namespace)
        if multi:  # multi-pod fixture: {pod name: lines}; tail applies per pod, like the real provider
            names = sorted(multi)
            if pod is not None and pod not in multi:
                raise LookupError(pod)
            chosen = [pod] if pod is not None else names
            return PodLogs.from_sections([PodSection(pod=n, lines=multi[n][-tail:]) for n in chosen], names)
        out = self.logs.get(namespace, PodLogs(pod=None, lines=[], note="No running pod yet."))
        if out.pod and not out.sections:  # canned single-pod logs
            out.pods = [out.pod]
            out.sections = [PodSection(pod=out.pod, lines=out.lines, note=out.note)]
        return out


class FakeMySQLAdmin(MySQLAdmin):
    def __init__(self, server: Any = None) -> None:
        # `server` (a DbServer, in real use) is accepted but unused — kept
        # for API parity with the real MySQLAdminClient, which is
        # constructed per-server (see app/providers/base.py::MySQLAdmin).
        self.server = server
        self.creds: dict[int, dict[str, Any]] = {}
        self.sizes: dict[int, int] = {}
        self.read_only: dict[int, bool] = {}
        self.calls: list[tuple[int, bool]] = []  # set_read_only calls, in order
        self.kill_result: dict[str, Any] = {"listed": 1, "killed": 1, "failed": 0, "list_error": False}
        self.passwords_seen: list[Optional[str]] = []  # app_password passed to each set_read_only call
        self.app_user_kill_ok = False  # True: the app-user kill path succeeds when a password is supplied
        self.conn_limits: dict[int, int] = {}
        self.fail_set_password = False  # `creds[app_id]["password"]` is the one valid password

    def set_user_connection_limit(self, app_id: int) -> None:
        from app import config
        if getattr(self, "limit_error", None):
            raise MySQLAdminError(self.limit_error)
        self.conn_limits[app_id] = config.MAX_DB_USER_CONNECTIONS

    def server_health(self) -> dict[str, Any]:
        if getattr(self, "health_error", None):
            raise MySQLAdminError(self.health_error)
        return getattr(self, "health", None) or {
            "threads_connected": 4, "threads_running": 1, "max_used_connections": 9,
            "max_connections": 25, "data_size_bytes": 12 * 1024 * 1024, "uptime_seconds": 3600,
        }

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

    def set_read_only(
        self, app_id: int, read_only: bool, app_password: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        self.read_only[app_id] = read_only
        self.calls.append((app_id, read_only))
        self.passwords_seen.append(app_password)
        if not read_only:
            return None
        if app_password and self.app_user_kill_ok:
            return {"listed": 1, "killed": 1, "failed": 0, "list_error": False, "path": "app-user"}
        return dict(self.kill_result)

    def set_password(self, app_id: int, password: str) -> dict[str, Any]:
        if self.fail_set_password:
            raise MySQLAdminError("injected set_password failure")
        creds = self.creds.get(app_id)
        if creds is None:
            creds = self.create_db_user(app_id)
        creds = dict(creds, password=password)
        creds["url"] = (
            f"mysql://{creds['user']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['name']}"
        )
        self.creds[app_id] = creds
        return creds


class FakePostgresAdmin(FakeMySQLAdmin):
    """In-memory PostgreSQL admin: same bookkeeping as the MySQL fake, `postgres://` creds, and the two-stage
    set_read_only (`hard` = REVOKE CONNECT). `hard_calls` records (app_id, read_only, hard)."""

    def __init__(self, server: Any = None) -> None:
        super().__init__(server)
        self.hard: dict[int, bool] = {}
        self.hard_calls: list[tuple[int, bool, bool]] = []

    def create_db_user(self, app_id: int) -> dict[str, Any]:
        from app.env_refs import db_url
        creds = {
            "host": "pg.example.internal", "port": 25060, "name": f"app_{app_id}", "user": f"app_{app_id}",
            "password": secrets.token_urlsafe(24),
        }
        creds["url"] = db_url("pg", creds["user"], creds["password"], creds["host"], creds["port"], creds["name"], tls=True)
        self.creds[app_id] = creds
        self.sizes[app_id] = 0
        self.read_only[app_id] = False
        self.hard[app_id] = False
        return creds

    def set_read_only(self, app_id: int, read_only: bool, app_password: Optional[str] = None, hard: bool = False):
        self.read_only[app_id] = read_only
        self.hard[app_id] = bool(read_only and hard)
        self.calls.append((app_id, read_only))
        self.hard_calls.append((app_id, read_only, hard))
        if not read_only:
            return None
        return {"listed": 1, "killed": 1, "failed": 0, "list_error": False, "path": "pg"}

    def set_password(self, app_id: int, password: str) -> dict[str, Any]:
        from app.env_refs import db_url
        creds = dict(self.creds[app_id], password=password)
        creds["url"] = db_url("pg", creds["user"], password, creds["host"], creds["port"], creds["name"], tls=True)
        self.creds[app_id] = creds
        return creds


class FakeObjectStore(ObjectStore):
    def __init__(self) -> None:
        self.creds: dict[int, dict[str, Any]] = {}
        self.sizes: dict[int, int] = {}
        self.read_only: dict[int, bool] = {}
        self.objects: dict[int, dict[str, int]] = {}  # key -> size, via put_object
        self.valid_keys: dict[int, set[str]] = {}  # access keys currently accepted by "Spaces"
        self.fail: set[str] = set()  # inject failures: "create", "revoke", "delete_key"

    def create_bucket_and_key(self, app_id: int) -> dict[str, Any]:
        creds = {
            "endpoint": "https://atl1.digitaloceanspaces.com",
            "region": "atl1",
            "bucket": f"pp-{app_id}",
            "access_key": secrets.token_hex(10),
            "secret_key": secrets.token_urlsafe(32),
        }
        self.creds[app_id] = creds
        self.valid_keys[app_id] = {creds["access_key"]}
        self.sizes[app_id] = 0
        self.read_only[app_id] = False
        return creds

    def delete(self, app_id: int) -> None:
        self.valid_keys.pop(app_id, None)
        self.creds.pop(app_id, None)
        self.sizes.pop(app_id, None)
        self.read_only.pop(app_id, None)

    def bucket_size_bytes(self, app_id: int) -> int:
        return self.sizes.get(app_id, 0)

    def put_object(self, app_id: int, key: str, size: int) -> None:
        """Test helper: add an object and keep `sizes` (what the quota poll reads) in step."""
        self.objects.setdefault(app_id, {})[key] = size
        self.sizes[app_id] = sum(self.objects[app_id].values())

    def usage(self, app_id: int) -> dict[str, Any]:
        from app.providers.spaces import summarize_objects

        objs = [{"Key": k, "Size": v} for k, v in self.objects.get(app_id, {}).items()]
        out = summarize_objects(objs)
        if not objs:  # sizes set directly by a test, no listing
            out["size_bytes"] = self.sizes.get(app_id, 0)
        return out

    def delete_objects(self, app_id: int, prefix: Optional[str] = None, max_objects: int = 5000) -> dict[str, Any]:
        objs = self.objects.setdefault(app_id, {})
        if not objs and self.sizes.get(app_id, 0) and not prefix:  # sizes set directly: "all" empties it
            self.sizes[app_id] = 0
            return {"deleted": 0, "truncated": False}
        keys = [k for k in sorted(objs) if not prefix or k.startswith(prefix)]
        doomed = keys[:max_objects]
        for k in doomed:
            del objs[k]
        self.sizes[app_id] = sum(objs.values())
        return {"deleted": len(doomed), "truncated": len(keys) > len(doomed)}

    def set_read_only(self, app_id: int, read_only: bool) -> Optional[dict[str, Any]]:
        """Like the real store: no-op (None) if unchanged, else swaps the key and returns new creds."""
        if self.read_only.get(app_id, False) == read_only:
            return None
        self.read_only[app_id] = read_only
        return self.rotate_key(app_id)

    def rotate_key(self, app_id: int) -> dict[str, Any]:
        """Creates the new key; the old one stays valid until `revoke_other_keys`."""
        if "create" in self.fail:
            raise ObjectStoreError("injected create failure")
        creds = self.creds.get(app_id)
        if creds is None:
            creds = self.create_bucket_and_key(app_id)
        creds = dict(creds, access_key=secrets.token_hex(10), secret_key=secrets.token_urlsafe(32))
        self.valid_keys.setdefault(app_id, set()).add(creds["access_key"])
        self.creds[app_id] = creds
        return creds

    def revoke_other_keys(self, app_id: int, keep_access_key: str) -> None:
        if "revoke" in self.fail:
            raise ObjectStoreError("injected revoke failure")
        self.valid_keys[app_id] = {keep_access_key} & self.valid_keys.get(app_id, set())

    def delete_key(self, app_id: int, access_key: str) -> None:
        if "delete_key" in self.fail:
            raise ObjectStoreError("injected delete_key failure")
        self.valid_keys.get(app_id, set()).discard(access_key)


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
        digests: Optional[dict[str, str]] = None,
    ) -> None:
        self.digests = dict(digests or {})
        self.resolve_calls = 0
        self.inspect_calls = 0
        self.failing: set[str] = set()  # refs whose lookups raise RegistryError (registry outage)
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

    def _digest(self, image_ref: str) -> str:
        import hashlib

        if image_ref in self.failing:
            raise RegistryError("injected registry failure")
        if image_ref in self.digests:
            return self.digests[image_ref]
        return "sha256:" + hashlib.sha256(image_ref.encode()).hexdigest()

    def resolve_digest(self, image_ref: str) -> str:
        self.resolve_calls += 1
        if image_ref in self.missing:
            raise RegistryError(f"image not found or not public: {image_ref!r}")
        return self._digest(image_ref)

    def inspect(self, image_ref: str) -> ImageInfo:
        self.inspect_calls += 1
        return ImageInfo(
            self.image_size_bytes(image_ref), self.users.get(image_ref, self.default_user), self._digest(image_ref)
        )
