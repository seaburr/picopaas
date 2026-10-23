"""Per-app compute resources: platform defaults (bootstrap.md caps) and platform_admin overrides."""
from __future__ import annotations

from typing import Any

from app.config import (  # noqa: F401  (single source of truth for the caps)
    DEFAULT_CPU_LIMIT_M,
    DEFAULT_CPU_REQUEST_M,
    DEFAULT_MEM_LIMIT_MI,
    DEFAULT_MEM_REQUEST_MI,
)

def effective_app_limit(user: Any) -> int:
    """Active-app ceiling for a user: platform admins get the hard max; others min(app_limit, hard max)."""
    from app import config  # late lookup so tests can monkeypatch MAX_APPS_PER_USER
    if getattr(user, "is_platform_admin", False):
        return config.MAX_APPS_PER_USER
    return min(user.app_limit, config.MAX_APPS_PER_USER)


CPU_MIN_M, CPU_MAX_M = 10, 1000
MEM_MIN_MI, MEM_MAX_MI = 32, 2048

# Pod-spec fields: changing one rolls the pods (Deployment resources / LimitRange / quota).
POD_FIELDS = ("cpu_request_m", "cpu_limit_m", "mem_request_mi", "mem_limit_mi", "ephemeral_limit_mi")
# Everything a platform admin can override per app (NULL = platform default).
FIELDS = POD_FIELDS + ("db_max_connections", "db_quota_mb", "storage_quota_mb", "image_size_mb")

_MIB = 1024 * 1024


def _g(app: Any, name: str) -> Any:
    return getattr(app, name, None) if app is not None else None


def default_db_connections(app: Any) -> int:
    from app import config
    if (_g(app, "db_engine") or "mysql") == "pg":
        return config.PG_MAX_DB_USER_CONNECTIONS
    return config.MAX_DB_USER_CONNECTIONS


def db_quota_bytes(app: Any) -> int:
    from app import config
    mb = _g(app, "db_quota_mb")
    return mb * _MIB if mb else config.DB_QUOTA_BYTES


def storage_quota_bytes(app: Any) -> int:
    from app import config
    mb = _g(app, "storage_quota_mb")
    return mb * _MIB if mb else config.STORAGE_QUOTA_BYTES


def image_size_bytes(app: Any) -> int:
    from app import config
    mb = _g(app, "image_size_mb")
    return mb * _MIB if mb else config.MAX_IMAGE_SIZE_BYTES


def ephemeral_limit_mi(app: Any) -> int:
    from app import config
    return _g(app, "ephemeral_limit_mi") or config.EPHEMERAL_LIMIT_MI


def effective(app: Any) -> dict[str, int]:
    """Effective values for an App row (or None for all defaults). A request is clamped to the limit."""
    g = lambda name: _g(app, name)  # noqa: E731
    cpu_limit = g("cpu_limit_m") or DEFAULT_CPU_LIMIT_M
    mem_limit = g("mem_limit_mi") or DEFAULT_MEM_LIMIT_MI
    return {
        "cpu_request_m": min(g("cpu_request_m") or DEFAULT_CPU_REQUEST_M, cpu_limit),
        "cpu_limit_m": cpu_limit,
        "mem_request_mi": min(g("mem_request_mi") or DEFAULT_MEM_REQUEST_MI, mem_limit),
        "mem_limit_mi": mem_limit,
        "ephemeral_limit_mi": ephemeral_limit_mi(app),
        "db_max_connections": g("db_max_connections") or default_db_connections(app),
        "db_quota_mb": db_quota_bytes(app) // _MIB,
        "storage_quota_mb": storage_quota_bytes(app) // _MIB,
        "image_size_mb": image_size_bytes(app) // _MIB,
    }


def exceeds_caps(eff: dict[str, int]) -> bool:
    from app import config
    return (eff["cpu_limit_m"] > DEFAULT_CPU_LIMIT_M or eff["mem_limit_mi"] > DEFAULT_MEM_LIMIT_MI
            or eff["ephemeral_limit_mi"] > config.EPHEMERAL_LIMIT_MI)


def overridden(app: Any) -> bool:
    return any(getattr(app, f, None) is not None for f in FIELDS)


def defaults(app: Any = None) -> dict[str, int]:
    """Platform defaults per overridable field (engine-aware for db_max_connections)."""
    return effective(type("D", (), {"db_engine": _g(app, "db_engine") or "mysql"}))


def describe(app: Any) -> dict[str, Any]:
    eff = effective(app)
    return {**eff, "overridden": overridden(app), "exceeds_platform_caps": exceeds_caps(eff),
            "defaults": defaults(app)}
