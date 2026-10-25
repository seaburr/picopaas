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
    """Active-app limit: users get min(app_limit, user ceiling). Platform admins use their own pair: at least the admin
    default, raised by their own app_limit, never above the admin ceiling."""
    from sqlalchemy.orm import object_session
    from app import platform_settings  # late lookup so tests can monkeypatch config.MAX_APPS_PER_USER
    try:
        db = object_session(user)
    except Exception:  # noqa: BLE001 - not an ORM object
        db = None
    if getattr(user, "is_platform_admin", False):
        return min(max(user.app_limit, platform_settings.get(db, "admin_default_app_limit")),
                   platform_settings.get(db, "max_apps_per_admin"))
    hard_max = platform_settings.get(db, "max_apps_per_user")
    return min(user.app_limit, hard_max)


CPU_MIN_M, CPU_MAX_M = 10, 1000
MEM_MIN_MI, MEM_MAX_MI = 32, 2048

# Pod-spec fields: changing one rolls the pods (Deployment resources / LimitRange / quota).
POD_FIELDS = ("cpu_request_m", "cpu_limit_m", "mem_request_mi", "mem_limit_mi", "ephemeral_limit_mi", "egress_mbit")
# Everything a platform admin can override per app (NULL = platform default).
FIELDS = POD_FIELDS + ("db_max_connections", "db_quota_mb", "storage_quota_mb", "image_size_mb")

_MIB = 1024 * 1024


def _g(app: Any, name: str) -> Any:
    return getattr(app, name, None) if app is not None else None


def _db(app: Any):
    from sqlalchemy.orm import object_session
    try:
        return object_session(app) if app is not None else None
    except Exception:  # noqa: BLE001 - not an ORM object
        return None


def _ps(db: Any, key: str) -> Any:
    from app import platform_settings
    return platform_settings.get(db, key)


def default_db_connections(app: Any) -> int:
    from app import config
    if (_g(app, "db_engine") or "mysql") == "pg":
        return config.PG_MAX_DB_USER_CONNECTIONS
    return config.MAX_DB_USER_CONNECTIONS


def db_quota_bytes(app: Any, db: Any = None) -> int:
    mb = _g(app, "db_quota_mb")
    return mb * _MIB if mb else _ps(db or _db(app), "default_db_quota_mb") * _MIB


def storage_quota_bytes(app: Any, db: Any = None) -> int:
    mb = _g(app, "storage_quota_mb")
    return mb * _MIB if mb else _ps(db or _db(app), "default_storage_quota_mb") * _MIB


def image_size_bytes(app: Any, db: Any = None) -> int:
    mb = _g(app, "image_size_mb")
    return mb * _MIB if mb else _ps(db or _db(app), "default_image_size_mb") * _MIB


def ephemeral_limit_mi(app: Any, db: Any = None) -> int:
    return _g(app, "ephemeral_limit_mi") or _ps(db or _db(app), "default_ephemeral_mi")


def config_egress(db: Any = None) -> int:
    return _ps(db, "default_egress_mbit")


def effective(app: Any, db: Any = None) -> dict[str, int]:
    """Effective values for an App row (or None for all defaults). A request is clamped to the limit.
    Defaults come from Admin > Settings (config values when not overridden); `db` is found from the App row if omitted."""
    g = lambda name: _g(app, name)  # noqa: E731
    db = db or _db(app)
    cpu_limit = g("cpu_limit_m") or _ps(db, "default_cpu_limit_m")
    mem_limit = g("mem_limit_mi") or _ps(db, "default_mem_limit_mi")
    return {
        "cpu_request_m": min(g("cpu_request_m") or DEFAULT_CPU_REQUEST_M, cpu_limit),
        "cpu_limit_m": cpu_limit,
        "mem_request_mi": min(g("mem_request_mi") or DEFAULT_MEM_REQUEST_MI, mem_limit),
        "mem_limit_mi": mem_limit,
        "ephemeral_limit_mi": ephemeral_limit_mi(app, db),
        "db_max_connections": g("db_max_connections") or default_db_connections(app),
        "db_quota_mb": db_quota_bytes(app, db) // _MIB,
        "storage_quota_mb": storage_quota_bytes(app, db) // _MIB,
        "image_size_mb": image_size_bytes(app, db) // _MIB,
        "egress_mbit": _g(app, "egress_mbit") or config_egress(db),
    }


def exceeds_caps(eff: dict[str, int], db: Any = None) -> bool:
    """True if the app has more than the platform defaults (an admin raised something)."""
    return (eff["cpu_limit_m"] > _ps(db, "default_cpu_limit_m") or eff["mem_limit_mi"] > _ps(db, "default_mem_limit_mi")
            or eff["ephemeral_limit_mi"] > _ps(db, "default_ephemeral_mi"))


def overridden(app: Any) -> bool:
    return any(getattr(app, f, None) is not None for f in FIELDS)


def defaults(app: Any = None, db: Any = None) -> dict[str, int]:
    """Platform defaults per overridable field (engine-aware for db_max_connections)."""
    return effective(type("D", (), {"db_engine": _g(app, "db_engine") or "mysql"}), db or _db(app))


def describe(app: Any) -> dict[str, Any]:
    db = _db(app)
    eff = effective(app, db)
    return {**eff, "overridden": overridden(app), "exceeds_platform_caps": exceeds_caps(eff, db),
            "defaults": defaults(app, db)}
