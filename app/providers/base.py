"""Thin interfaces to external systems (k8s, MySQL admin, Spaces, image registries).

See CLAUDE.md: "External systems ... go behind small interfaces in app/providers/
with fake implementations for tests." Real implementations live in sibling
modules; only `registry.py` is real so far (see its module docstring) —
`kube.py`, `mysql.py`, and `spaces.py` are stubs that raise NotImplementedError.
Fakes for all four live in `fake.py` and are what tests should depend on.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


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
    def get_status(self, namespace: str) -> str:
        """Return a coarse status string (e.g. 'running', 'pending', 'unknown')."""
        raise NotImplementedError


def generate_password(length: int = 32) -> str:
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class MySQLAdminError(Exception):
    pass


class MySQLAdmin(ABC):
    """Manages one MySQL database + user per app (ARCHITECTURE.md §2 "DB").

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

    @abstractmethod
    def db_size_bytes(self, app_id: int) -> int:
        """Sum of the app database's table sizes, for the 100MB quota check."""
        raise NotImplementedError

    @abstractmethod
    def set_read_only(self, app_id: int, read_only: bool) -> None:
        """Revoke/restore INSERT/UPDATE/CREATE once the app exceeds its DB quota."""
        raise NotImplementedError

    @abstractmethod
    def set_password(self, app_id: int, password: str) -> dict[str, Any]:
        """ALTER the app's db user to `password`; return the full creds. Lets the caller
        persist the password BEFORE it takes effect (issue #2)."""
        raise NotImplementedError

    def rotate_password(self, app_id: int) -> dict[str, Any]:
        """Set a new random password for the app's db user; return the full new creds."""
        return self.set_password(app_id, generate_password())


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
        """Sum of object sizes in the app's bucket, for the 250MB quota check."""
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
