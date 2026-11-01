"""The single place that builds cloud-specific providers (docs/PORTABILITY_PLAN.md).

Routers, the scheduler and health ask this module; they never import a cloud implementation directly.
CLOUD_PROVIDER (app/config.py) picks the implementation; aws, gcp and generic are planned and raise
NotImplementedError when a provider is built.
"""
from __future__ import annotations

import os

from app import config
from app.providers.base import BackupTarget, DbHostMetrics, ObjectStore


def _unsupported(provider: str) -> NotImplementedError:
    return NotImplementedError(f"CLOUD_PROVIDER={provider} is planned, see docs/PORTABILITY_PLAN.md")


def _provider() -> str:
    return config.CLOUD_PROVIDER


def object_store() -> ObjectStore:
    """The platform object store. Raises ObjectStoreError when it isn't configured."""
    p = _provider()
    if p == "digitalocean":
        from app.providers.digitalocean.spaces import SpacesStore

        return SpacesStore.from_env()
    raise _unsupported(p)


def storage_configured() -> bool:
    """True when per-app storage can work (credentials present). Env check only, no network."""
    p = _provider()
    if p == "digitalocean":
        return bool(os.environ.get("DIGITALOCEAN_TOKEN") and config.object_store_access_key_id()
                    and config.object_store_secret_access_key())
    raise _unsupported(p)


def backup_target() -> BackupTarget:
    p = _provider()
    if p == "digitalocean":
        from app.providers.digitalocean.backup_target import DoBackupTarget

        return DoBackupTarget()
    raise _unsupported(p)


def db_host_metrics() -> DbHostMetrics | None:
    """Managed-DB host metrics, or None when disabled or not available (the Health page then shows data size only)."""
    p = _provider()
    if p == "digitalocean":
        if not config.DB_METRICS_ENABLED or not os.environ.get("DIGITALOCEAN_TOKEN"):
            return None
        from app.providers.digitalocean.db_metrics import DoDbHostMetrics

        return DoDbHostMetrics()
    raise _unsupported(p)
