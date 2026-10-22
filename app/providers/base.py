"""Thin interfaces to external systems (k8s, MySQL admin, Spaces, image registries).

See CLAUDE.md: "External systems ... go behind small interfaces in app/providers/
with fake implementations for tests." Real implementations live in sibling
modules; only `registry.py` is real so far (see its module docstring) —
`kube.py`, `mysql.py`, and `spaces.py` are stubs that raise NotImplementedError.
Fakes for all four live in `fake.py` and are what tests should depend on.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PodSection:
    """Logs of one pod (previous-container lines included when it restarted)."""
    pod: str
    lines: list[str]
    note: Optional[str] = None
    restarts: int = 0


@dataclass
class PodLogs:
    """Logs of an app's pods. `sections` has one entry per pod read (all pods, or just the requested one);
    `pods` lists every available pod name (so a UI can offer a picker). `pod`/`lines` are the flat view:
    `pod` is the single pod's name (None with several), `lines` the grouped lines ("=== pod x ===" headers
    when more than one pod)."""
    pod: Optional[str]
    lines: list[str]
    note: Optional[str] = None
    pods: list[str] = field(default_factory=list)
    sections: list[PodSection] = field(default_factory=list)

    @classmethod
    def from_sections(cls, sections: list[PodSection], pods: list[str], note: Optional[str] = None) -> "PodLogs":
        if len(sections) == 1:
            s = sections[0]
            return cls(pod=s.pod, lines=list(s.lines), note=s.note or note, pods=pods, sections=sections)
        lines: list[str] = []
        for s in sections:
            lines.append(f"=== pod {s.pod} ===")
            lines += s.lines if s.lines else [f"({s.note or 'no output'})"]
        return cls(pod=None, lines=lines, note=note, pods=pods, sections=sections)


class KubeProvider(ABC):
    """Applies rendered manifests (app/k8s_manifests.py) to the cluster."""

    @abstractmethod
    def apply(self, manifests: list[dict[str, Any]]) -> None:
        """Create/update the given manifests (server-side apply semantics)."""
        raise NotImplementedError

    @abstractmethod
    def delete_namespace(self, namespace: str) -> None:
        """Delete an app's namespace (and everything in it)."""
        raise NotImplementedError

    @abstractmethod
    def delete_ingress(self, namespace: str, name: str) -> None:
        """Delete one Ingress (no error if absent). Used so suspended apps fall through to the catch-all."""
        raise NotImplementedError

    def delete_network_policy(self, namespace: str, name: str) -> None:
        """Delete one NetworkPolicy (no error if absent). Targeted prune only (allow-same-namespace on scale-down)."""
        return None

    @abstractmethod
    def list_app_namespaces(self) -> set[str]:
        """Names of existing app namespaces (label picopaas.io/app-id), in one API call."""
        raise NotImplementedError

    @abstractmethod
    def get_status(self, namespace: str) -> str:
        """Return a coarse status string (e.g. 'running', 'pending', 'unknown')."""
        raise NotImplementedError

    @abstractmethod
    def get_used_limits(self, namespace: str) -> Optional[dict[str, int]]:
        """Summed container limits ({"cpu_limit_m", "mem_limit_mi", "ephemeral_limit_mi"}) of the existing (non-terminal) pods in the
        namespace, i.e. what already consumes the ResourceQuota; None when there are none. Sizes the quota for a rollout."""
        raise NotImplementedError

    @abstractmethod
    def pending_hint(self, namespace: str) -> str:
        """What a not-yet-ready app looks like, for the deploy-timeout message:
        'not_ready' (a pod is running but fails readiness), 'waiting' (pod exists, not running), 'none' (no pod)."""
        raise NotImplementedError

    def app_pod_events(self) -> dict[str, list[dict[str, Any]]]:
        """Restart/eviction occurrences of the pods in every APP namespace, in one call: {namespace: [event]},
        event = {"key": str (stable per occurrence: pod uid + restartCount/finishedAt), "at": naive-UTC datetime,
        "kind": "disk" | "evicted" | "oom" | "crashed", "exit_code": int | None}."""
        return {}

    @abstractmethod
    def pod_logs(self, namespace: str, tail: int, pod: Optional[str] = None) -> "PodLogs":
        """Logs of the app container of every pod (or only `pod`, which must be one of the app's pods; raises
        LookupError otherwise) in an APP namespace (never the platform namespace); `tail` applies per pod.
        Includes the previous container's last lines when a pod's container restarted."""
        raise NotImplementedError

    @abstractmethod
    def cluster_snapshot(self) -> dict[str, Any]:
        """Read-only health snapshot for the admin dashboard (plain dicts, no specs/env/secrets):
        {"nodes": [{name, ready, age_s, cpu_capacity_m, cpu_allocatable_m, cpu_requested_m, cpu_used_m|None,
                    mem_capacity_mi, mem_allocatable_mi, mem_requested_mi, mem_used_mi|None, pods, pods_capacity}],
         "pods":  [{name, namespace, node, phase, ready, restarts, cpu_used_m|None, mem_used_mi|None}],
         "metrics_available": bool, "errors": [str]}
        Non-terminated pods only. Must not raise for a metrics.k8s.io failure (set metrics_available False)."""
        raise NotImplementedError


def generate_password(length: int = 32) -> str:
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class DbAdminError(Exception):
    pass


# Historic name, kept so existing `except MySQLAdminError` sites also catch PostgreSQL provider failures.
MySQLAdminError = DbAdminError


class DbAdmin(ABC):
    """Manages one database + user per app (MySQL or PostgreSQL; `app/providers/factory.py` picks by DbServer.engine) (ARCHITECTURE.md §2 "DB").

    Implementations are constructed per `DbServer` (host/port/admin creds come
    from `__init__`, chosen via `app/db_placement.py::choose_server` —
    never passed per call), since a platform can run several MySQL servers
    once a single one fills up (ARCHITECTURE.md "Scaling")."""

    @abstractmethod
    def create_db_user(self, app_id: int) -> dict[str, Any]:
        """Create `app_<id>` db + user, return creds (host/port/database/user/password)."""
        raise NotImplementedError

    @abstractmethod
    def drop(self, app_id: int) -> None:
        """Drop the app's database and user."""
        raise NotImplementedError

    def set_user_connection_limit(self, app_id: int) -> None:
        """Re-apply config.MAX_DB_USER_CONNECTIONS to the app's existing db user (idempotent). Optional."""
        return None

    def server_health(self) -> dict[str, Any]:
        """Server-wide connection counts + total data size (admin Health page). Optional."""
        raise NotImplementedError

    @abstractmethod
    def db_size_bytes(self, app_id: int) -> int:
        """Sum of the app database's table sizes, for the DB quota check."""
        raise NotImplementedError

    @abstractmethod
    def set_read_only(
        self, app_id: int, read_only: bool, app_password: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Revoke/restore INSERT/UPDATE/CREATE once the app exceeds its DB quota. When revoking, also kills the
        app user's open sessions (first as the app user itself via `app_password`, then via admin) and returns
        {"listed", "killed", "failed", "list_error", "path"} (None when restoring)."""
        raise NotImplementedError

    @abstractmethod
    def set_password(self, app_id: int, password: str) -> dict[str, Any]:
        """ALTER the app's db user to `password`; return the full creds. Lets the caller
        persist the password BEFORE it takes effect (issue #2)."""
        raise NotImplementedError

    def rotate_password(self, app_id: int) -> dict[str, Any]:
        """Set a new random password for the app's db user; return the full new creds."""
        return self.set_password(app_id, generate_password())


MySQLAdmin = DbAdmin  # historic name


class ObjectStoreError(Exception):
    pass


class ObjectStore(ABC):
    """Manages one Spaces bucket + access key per app (ARCHITECTURE.md §2 "Storage")."""

    @abstractmethod
    def create_bucket_and_key(self, app_id: int) -> dict[str, Any]:
        """Create bucket `pp-<id>` + a scoped key, return creds (endpoint/region/
        bucket/access_key/secret_key)."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, app_id: int) -> None:
        """Delete the app's bucket and key."""
        raise NotImplementedError

    @abstractmethod
    def bucket_size_bytes(self, app_id: int) -> int:
        """Sum of object sizes in the app's bucket, for the bucket quota check."""
        raise NotImplementedError

    @abstractmethod
    def usage(self, app_id: int) -> dict[str, Any]:
        """{"size_bytes", "objects", "top_prefixes": [{"prefix", "size_bytes", "objects"}]} by first path segment
        (at most 5, biggest first; root-level objects are grouped under "")."""
        raise NotImplementedError

    @abstractmethod
    def delete_objects(self, app_id: int, prefix: Optional[str] = None, max_objects: int = 5000) -> dict[str, Any]:
        """Delete objects with the PLATFORM's credentials (an over-quota app only holds a read-only key):
        every object, or those under `prefix`. Paginated and capped at `max_objects`; returns
        {"deleted": int, "truncated": bool} (truncated = more matching objects remain)."""
        raise NotImplementedError

    @abstractmethod
    def set_read_only(self, app_id: int, read_only: bool) -> Optional[dict[str, Any]]:
        """Make the app's key read-only (or restore readwrite). Spaces grants are
        immutable, so this may replace the key: returns the NEW creds when the key
        changed (caller must persist them), None when nothing changed. Idempotent."""
        raise NotImplementedError

    @abstractmethod
    def rotate_key(self, app_id: int) -> dict[str, Any]:
        """Create a new key with the same grant and return its creds. The OLD key is NOT
        deleted: persist the new creds, then call `revoke_other_keys` (issue #2)."""
        raise NotImplementedError

    @abstractmethod
    def revoke_other_keys(self, app_id: int, keep_access_key: str) -> None:
        """Delete every key of the app except `keep_access_key` (idempotent)."""
        raise NotImplementedError

    @abstractmethod
    def delete_key(self, app_id: int, access_key: str) -> None:
        """Delete one key (used to discard a new key whose creds couldn't be persisted)."""
        raise NotImplementedError


@dataclass(frozen=True)
class ImageInfo:
    size_bytes: int
    user: str  # the image config's `User` ("" means the image default, i.e. root)
    digest: str = ""  # manifest digest (sha256:...) of what was inspected (the amd64 manifest of an index)


class RegistryError(Exception):
    """Raised for any registry failure: not found, not public, or a malformed manifest."""


class Registry(ABC):
    """Reads image metadata without pulling the image."""

    @abstractmethod
    def image_size_bytes(self, image_ref: str) -> int:
        """Return the summed compressed layer size (bytes) for `image_ref`.

        Raises RegistryError if the image doesn't exist, isn't public, or the
        registry response can't be parsed.
        """
        raise NotImplementedError

    @abstractmethod
    def inspect(self, image_ref: str) -> ImageInfo:
        """Return size plus the configured `User` (from the image config blob). Same errors as above."""
        raise NotImplementedError

    @abstractmethod
    def resolve_digest(self, image_ref: str) -> str:
        """Cheaply resolve `image_ref` to its manifest digest (amd64 manifest for an index), without
        fetching the image config. Same errors as above."""
        raise NotImplementedError
