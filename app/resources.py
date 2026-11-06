"""Per-app compute resources: platform defaults (bootstrap.md caps) and platform_admin overrides."""
from __future__ import annotations

from typing import Any, Optional

from app.config import (  # noqa: F401  (single source of truth for the caps)
    DEFAULT_CPU_LIMIT_M,
    DEFAULT_CPU_REQUEST_M,
    DEFAULT_MEM_LIMIT_MI,
    DEFAULT_MEM_REQUEST_MI,
    OWNER_CPU_MIN_M,
    OWNER_EPHEMERAL_MIN_MI,
    OWNER_MEM_MIN_MI,
    OWNER_STORAGE_MIN_MB,
    OWNER_VOLUME_EPHEMERAL_MIN_MI,
    OWNER_VOLUME_MEM_MIN_MI,
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


# Owner-chosen values (NULL = use the full ceiling), per ceiling field.
OWNER_COLUMNS = {"cpu_limit_m": "owner_cpu_limit_m", "mem_limit_mi": "owner_mem_limit_mi",
                 "ephemeral_limit_mi": "owner_ephemeral_limit_mi", "storage_quota_mb": "owner_storage_quota_mb"}
OWNER_FLOORS = {"cpu_limit_m": OWNER_CPU_MIN_M, "mem_limit_mi": OWNER_MEM_MIN_MI,
                "ephemeral_limit_mi": OWNER_EPHEMERAL_MIN_MI, "storage_quota_mb": OWNER_STORAGE_MIN_MB}

CPU_MIN_M, CPU_MAX_M = 10, 1000
MEM_MIN_MI, MEM_MAX_MI = 32, 2048

# Pod-spec fields: changing one rolls the pods (Deployment resources / LimitRange / quota).
POD_FIELDS = ("cpu_request_m", "cpu_limit_m", "mem_request_mi", "mem_limit_mi", "ephemeral_limit_mi", "egress_mbit")
# Everything a platform admin can override per app (NULL = platform default).
FIELDS = POD_FIELDS + ("db_max_connections", "db_quota_mb", "storage_quota_mb", "image_size_mb")

_MIB = 1024 * 1024


def _g(app: Any, name: str) -> Any:
    return getattr(app, name, None) if app is not None else None


def _volume_floor(app: Any, field: str) -> int:
    """Minimum for an app with a persistent volume, so its backup pod (clamped to the app's limits) can run. 0 otherwise."""
    if _g(app, "volume_size_gi") is None:
        return 0
    return {"mem_limit_mi": OWNER_VOLUME_MEM_MIN_MI, "ephemeral_limit_mi": OWNER_VOLUME_EPHEMERAL_MIN_MI}.get(field, 0)


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


def storage_quota_ceiling_bytes(app: Any, db: Any = None) -> int:
    """Highest bucket quota this app may have: the admin override, else the platform default."""
    mb = _g(app, "storage_quota_mb")
    return mb * _MIB if mb else _ps(db or _db(app), "default_storage_quota_mb") * _MIB


def storage_quota_bytes(app: Any, db: Any = None) -> int:
    """The enforced bucket quota: the owner's choice, never above the ceiling."""
    ceiling = storage_quota_ceiling_bytes(app, db)
    owner = _g(app, "owner_storage_quota_mb")
    return min(owner * _MIB, ceiling) if owner else ceiling


def image_size_bytes(app: Any, db: Any = None) -> int:
    mb = _g(app, "image_size_mb")
    return mb * _MIB if mb else _ps(db or _db(app), "default_image_size_mb") * _MIB


def ephemeral_ceiling_mi(app: Any, db: Any = None) -> int:
    return _g(app, "ephemeral_limit_mi") or _ps(db or _db(app), "default_ephemeral_mi")


def ephemeral_limit_mi(app: Any, db: Any = None) -> int:
    ceiling = ephemeral_ceiling_mi(app, db)
    owner = _g(app, "owner_ephemeral_limit_mi")
    return min(max(owner, _volume_floor(app, "ephemeral_limit_mi")), ceiling) if owner else ceiling


def config_egress(db: Any = None) -> int:
    return _ps(db, "default_egress_mbit")


def ceilings(app: Any, db: Any = None) -> dict[str, int]:
    """What the platform (defaults plus any admin override) allows this app; owner choices are NOT applied.
    A request is clamped to the limit. Defaults come from Admin > Settings; `db` is found from the App row if omitted."""
    g = lambda name: _g(app, name)  # noqa: E731
    db = db or _db(app)
    cpu_limit = g("cpu_limit_m") or _ps(db, "default_cpu_limit_m")
    mem_limit = g("mem_limit_mi") or _ps(db, "default_mem_limit_mi")
    return {
        "cpu_request_m": min(g("cpu_request_m") or DEFAULT_CPU_REQUEST_M, cpu_limit),
        "cpu_limit_m": cpu_limit,
        "mem_request_mi": min(g("mem_request_mi") or DEFAULT_MEM_REQUEST_MI, mem_limit),
        "mem_limit_mi": mem_limit,
        "ephemeral_limit_mi": ephemeral_ceiling_mi(app, db),
        "db_max_connections": g("db_max_connections") or default_db_connections(app),
        "db_quota_mb": db_quota_bytes(app, db) // _MIB,
        "storage_quota_mb": storage_quota_ceiling_bytes(app, db) // _MIB,
        "image_size_mb": image_size_bytes(app, db) // _MIB,
        "egress_mbit": _g(app, "egress_mbit") or config_egress(db),
    }


def effective(app: Any, db: Any = None) -> dict[str, int]:
    """What actually runs: min(owner choice, ceiling) for cpu, memory, scratch and bucket quota. Requests are
    clamped to the (possibly lowered) limit. An admin lowering a ceiling below an owner's choice wins; raising it
    leaves the owner's choice alone."""
    db = db or _db(app)
    eff = ceilings(app, db)
    for field, col in OWNER_COLUMNS.items():
        owner = _g(app, col)
        if owner:
            eff[field] = min(max(owner, _volume_floor(app, field)), eff[field])  # a volume added later lifts a low choice
    eff["cpu_request_m"] = min(eff["cpu_request_m"], eff["cpu_limit_m"])
    eff["mem_request_mi"] = min(eff["mem_request_mi"], eff["mem_limit_mi"])
    return eff


def floors(eff_ceilings: dict[str, int], app: Any = None) -> dict[str, int]:
    """Lowest value an owner may pick per field: the config floor (higher with a volume), but never above the ceiling
    (so a low admin ceiling can't make the app unsavable)."""
    return {f: min(max(OWNER_FLOORS[f], _volume_floor(app, f)), eff_ceilings[f]) for f in OWNER_COLUMNS}


def controls(app: Any, db: Any = None) -> dict[str, Optional[dict[str, Any]]]:
    """Per-field slider data: value (effective), owner (own choice or null), min, max (the ceiling)."""
    db = db or _db(app)
    cei = ceilings(app, db)
    eff = effective(app, db)
    low = floors(cei, app)
    out: dict[str, Optional[dict[str, Any]]] = {}
    for f, col in OWNER_COLUMNS.items():
        if f == "storage_quota_mb" and not _g(app, "want_storage"):
            out[f] = None
            continue
        out[f] = {"value": eff[f], "owner": _g(app, col), "min": low[f], "max": cei[f]}
    return out


def exceeds_caps(eff: dict[str, int], db: Any = None) -> bool:
    """True if the app has more than the platform defaults (an admin raised something). Pass ceilings(), not
    effective(): an owner choosing less must not hide an admin's raise."""
    return (eff["cpu_limit_m"] > _ps(db, "default_cpu_limit_m") or eff["mem_limit_mi"] > _ps(db, "default_mem_limit_mi")
            or eff["ephemeral_limit_mi"] > _ps(db, "default_ephemeral_mi"))


def overridden(app: Any) -> bool:
    return any(getattr(app, f, None) is not None for f in FIELDS)


def defaults(app: Any = None, db: Any = None) -> dict[str, int]:
    """Platform defaults per overridable field (engine-aware for db_max_connections)."""
    return ceilings(type("D", (), {"db_engine": _g(app, "db_engine") or "mysql"}), db or _db(app))


def describe(app: Any) -> dict[str, Any]:
    db = _db(app)
    eff = effective(app, db)
    return {**eff, "overridden": overridden(app), "exceeds_platform_caps": exceeds_caps(ceilings(app, db), db),
            "defaults": defaults(app, db), "controls": controls(app, db),
            # Raw admin override columns (NULL = platform default). The admin Resources dialog must prefill from
            # these, NOT from the effective values above (an owner's choice would otherwise be saved as an override).
            "admin_values": {f: _g(app, f) for f in FIELDS}}


# --- owner choices (the sliders) ---

OWNER_UNITS = {"cpu_limit_m": "m", "mem_limit_mi": "MiB", "ephemeral_limit_mi": "MiB", "storage_quota_mb": "MB"}
OWNER_LABELS = {"cpu_limit_m": "CPU", "mem_limit_mi": "Memory", "ephemeral_limit_mi": "Scratch disk",
                "storage_quota_mb": "Storage"}


def owner_values(app: Any) -> dict[str, Optional[int]]:
    return {f: _g(app, col) for f, col in OWNER_COLUMNS.items()}


def validate_owner_changes(app: Any, changes: dict[str, Optional[int]], db: Any = None) -> dict[str, str]:
    """Field -> message for every change outside the allowed range (empty = fine). `changes` holds only the fields
    the caller sent; None resets to the full ceiling and is always allowed. `app` may be a not-yet-saved App."""
    db = db or _db(app)
    cei = ceilings(app, db)
    low = floors(cei, app)
    errors: dict[str, str] = {}
    for f, v in changes.items():
        if v is None:
            continue
        unit, label = OWNER_UNITS[f], OWNER_LABELS[f]
        if f == "storage_quota_mb":
            if not _g(app, "want_storage"):
                errors[f] = "This app has no storage bucket, so there is no storage limit to set."
                continue
            st = _g(app, "storage")
            if low[f] <= v <= cei[f] and st is not None and (st.size_bytes or 0) > v * _MIB:
                used_mb = -(-(st.size_bytes or 0) // _MIB)
                errors[f] = (f"{label} is already using {used_mb} {unit}. Free space first, or choose at least "
                             f"{used_mb} {unit} so the bucket doesn't turn read-only.")
                continue
        if v < low[f] or v > cei[f]:
            why = " Apps with a persistent volume need this much so their backups can run." if v < low[f] and _volume_floor(app, f) else ""
            errors[f] = f"{label} must be between {low[f]} and {cei[f]} {unit}.{why}"
    return errors


def would_change(app: Any, changes: dict[str, Optional[int]], db: Any = None) -> bool:
    """True if storing `changes` would alter the owner's stored values (same normalisation as set_owner_values)."""
    cei = ceilings(app, db or _db(app))
    return any(_g(app, OWNER_COLUMNS[f]) != (None if v is None or v >= cei[f] else v) for f, v in changes.items())


def set_owner_values(app: Any, changes: dict[str, Optional[int]], db: Any = None) -> tuple[bool, bool]:
    """Store validated changes. A value equal to the ceiling is stored as NULL (= follow the ceiling), so the app
    keeps tracking an admin raising it. Returns (pod_fields_changed, storage_quota_changed) judged on the EFFECTIVE
    values, so a no-op or a change an admin ceiling already caps does not roll the pod."""
    db = db or _db(app)
    before = effective(app, db)
    cei = ceilings(app, db)
    for f, v in changes.items():
        setattr(app, OWNER_COLUMNS[f], None if v is None or v >= cei[f] else v)
    after = effective(app, db)
    pod = any(before[f] != after[f] for f in ("cpu_limit_m", "cpu_request_m", "mem_limit_mi", "mem_request_mi",
                                              "ephemeral_limit_mi"))
    return pod, before["storage_quota_mb"] != after["storage_quota_mb"]


def owner_str(app: Any) -> str:
    return ",".join(f"{f}={v}" for f, v in owner_values(app).items())
