"""Admin-editable global settings: a typed registry plus DB overrides.

The default of every setting is the matching `app/config.py` value (env var, read late so tests can monkeypatch),
so a deploy with no overrides behaves exactly as before. `get(db, key)` returns the DB override, else the default.
Reads are cached per database for CACHE_TTL_SECONDS (other replicas see a change within that time; the replica that
handles a PUT sees it at once).
"""
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from app import config, models

CACHE_TTL_SECONDS = 5.0
_MIB = 1024 * 1024


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    description: str
    group: str
    type: str  # "int" | "bool"
    unit: str  # "", "m", "MiB", "MB", "GiB", "Mbit/s", "bytes", "s", ...
    default: Callable[[], Any]
    min: Optional[int] = None
    max: Optional[int] = None
    max_reason: str = ""  # shown instead of "must be at most N" when the max is a hard platform limit (node size)


G_CEIL, G_DEF, G_BAK = "Limit ceilings", "Defaults and quotas", "Backups"
G_APPDEF = "Per-app defaults"


def _c(name: str) -> Callable[[], Any]:
    return lambda: getattr(config, name)


def _res(name: str) -> Callable[[], Any]:
    from app import resources
    return lambda: getattr(resources, name)


REGISTRY: List[Setting] = [
    # --- Ceilings for what a platform admin can raise a per-app limit to (PATCH /admin/apps/{id}/resources) ---
    Setting("cpu_limit_max_m", "CPU ceiling", "Highest CPU request/limit an admin can give one app.", G_CEIL, "int", "m",
            _res("CPU_MAX_M"), 100, config.NODE_CPU_CAP_M,
            "can't exceed what one node can schedule after system reservations"),
    Setting("mem_limit_max_mi", "Memory ceiling", "Highest memory request/limit an admin can give one app.", G_CEIL, "int", "MiB",
            _res("MEM_MAX_MI"), 128, config.NODE_MEM_CAP_MI,
            "can't exceed what one node can schedule after system reservations"),
    Setting("override_db_connections_max", "Database connections ceiling", "Highest per-app database connection cap.",
            G_CEIL, "int", "connections", _c("OVERRIDE_DB_CONNECTIONS_MAX"), 1, 100),
    Setting("override_ephemeral_max_mi", "Scratch disk ceiling", "Highest per-app ephemeral (scratch) disk.", G_CEIL, "int", "MiB",
            _c("OVERRIDE_EPHEMERAL_MAX_MI"), 64, 20480),
    Setting("override_db_quota_max_mb", "Database size ceiling", "Highest per-app database size quota.", G_CEIL, "int", "MB",
            _c("OVERRIDE_DB_QUOTA_MAX_MB"), 1, 20480),
    Setting("override_storage_quota_max_mb", "Blob storage ceiling", "Highest per-app blob storage quota.", G_CEIL, "int", "MB",
            _c("OVERRIDE_STORAGE_QUOTA_MAX_MB"), 1, 512000),
    Setting("override_image_size_max_mb", "Image size ceiling", "Highest per-app image size cap.", G_CEIL, "int", "MB",
            _c("OVERRIDE_IMAGE_SIZE_MAX_MB"), 1, 20480),
    Setting("override_egress_max_mbit", "Egress bandwidth ceiling", "Highest per-app outbound bandwidth cap.", G_CEIL, "int", "Mbit/s",
            _c("OVERRIDE_EGRESS_MAX_MBIT"), 1, 1000),
    Setting("override_volume_max_gi", "Volume size ceiling (admin)", "Highest persistent volume size an admin can grow an app's volume to. "
            "The user volume ceiling can't exceed this.", G_CEIL, "int", "GiB", _c("OVERRIDE_VOLUME_MAX_GI"), 1, 1000),
    Setting("max_replicas", "Replicas ceiling", "Highest replica count an admin can set for one app.", G_CEIL, "int", "replicas",
            _c("MAX_REPLICAS"), 1, 20),
    # --- What every app gets unless an admin overrides it. Each default is capped by its ceiling above. ---
    Setting("default_cpu_limit_m", "CPU default", "CPU limit every app gets.", G_APPDEF, "int", "m", _c("DEFAULT_CPU_LIMIT_M"), 10, None),
    Setting("default_mem_limit_mi", "Memory default", "Memory limit every app gets.", G_APPDEF, "int", "MiB", _c("DEFAULT_MEM_LIMIT_MI"), 32, None),
    Setting("default_ephemeral_mi", "Scratch disk default", "Ephemeral (scratch) disk every app gets.", G_APPDEF, "int", "MiB",
            _c("EPHEMERAL_LIMIT_MI"), 64, None),
    Setting("default_image_size_mb", "Image size default", "Largest image an app can deploy.", G_APPDEF, "int", "MB",
            lambda: config.MAX_IMAGE_SIZE_BYTES // _MIB, 1, None),
    Setting("default_db_quota_mb", "Database size default", "Database size quota every app gets.", G_APPDEF, "int", "MB",
            lambda: config.DB_QUOTA_BYTES // _MIB, 1, None),
    Setting("default_storage_quota_mb", "Blob storage default", "Bucket quota every app gets.", G_APPDEF, "int", "MB",
            lambda: config.STORAGE_QUOTA_BYTES // _MIB, 1, None),
    Setting("default_egress_mbit", "Egress bandwidth default", "Outbound bandwidth cap every app gets.", G_APPDEF, "int", "Mbit/s",
            _c("EGRESS_BANDWIDTH_MBIT"), 1, None),
    # --- Defaults and platform-wide quotas ---
    Setting("volume_max_gi", "Volume size ceiling (users)", "Largest volume an app owner can request without an admin. Not above the admin ceiling.",
            G_DEF, "int", "GiB", _c("VOLUME_MAX_GI"), 1, 1000),
    Setting("volumes_max_total", "Volumes platform-wide", "Most apps with a persistent volume at once (the cluster attach limit is lower).",
            G_DEF, "int", "volumes", _c("VOLUMES_MAX_TOTAL"), 0, 100),
    Setting("max_apps_per_user", "Apps per account (hard max)", "Most active apps one account can have, admins included. "
            "A user's own limit can be lower, never higher.", G_DEF, "int", "apps", _c("MAX_APPS_PER_USER"), 1, 100),
    Setting("default_app_limit", "Default app limit for new accounts", "App limit given to a newly registered account. Not above the hard max.",
            G_DEF, "int", "apps", _c("DEFAULT_APP_LIMIT"), 0, 100),
    # --- Backups (registered here; enforcement reads them in app/backups.py) ---
    Setting("backup_max_bytes_total", "Backup storage, platform total", "Ceiling for all backups together.", G_BAK, "int", "bytes",
            _c("BACKUP_MAX_BYTES_TOTAL"), _MIB, 10 * 1024**4),
    Setting("backup_max_bytes_per_app", "Backup storage, per app", "Default ceiling for one app's backups (not above the platform total).",
            G_BAK, "int", "bytes", _c("BACKUP_MAX_BYTES_PER_APP"), _MIB, 10 * 1024**4),
    Setting("backup_max_held_per_app", "Backups kept per app", "Most backup packages kept for one app.", G_BAK, "int", "backups",
            lambda: 14, 1, 365),
    Setting("backup_manual_per_hour", "On-demand backups per app per hour", "How many manual backups one app can start per hour.",
            G_BAK, "int", "per hour", lambda: 1, 1, 60),
    Setting("backup_export_max_per_hour", "Backup downloads per app per hour", "How many download packages one app can prepare per hour.",
            G_BAK, "int", "per hour", _c("BACKUP_EXPORT_MAX_PER_HOUR"), 1, 100),
    Setting("backups_enabled_default", "Scheduled backups on for new apps", "New apps start with scheduled backups turned on.",
            G_BAK, "bool", "", _c("BACKUP_DEFAULT_ENABLED")),
]
BY_KEY: Dict[str, Setting] = {s.key: s for s in REGISTRY}

# Per-app default setting -> the ceiling setting that bounds it (and per-app overrides).
DEFAULT_CEILINGS = {
    "default_cpu_limit_m": "cpu_limit_max_m", "default_mem_limit_mi": "mem_limit_max_mi",
    "default_ephemeral_mi": "override_ephemeral_max_mi", "default_image_size_mb": "override_image_size_max_mb",
    "default_db_quota_mb": "override_db_quota_max_mb", "default_storage_quota_mb": "override_storage_quota_max_mb",
    "default_egress_mbit": "override_egress_max_mbit",
}
for _d, _c_ in DEFAULT_CEILINGS.items():  # a default may be set up to its ceiling
    BY_KEY[_d] = Setting(**{**BY_KEY[_d].__dict__, "max": BY_KEY[_c_].max})
REGISTRY[:] = [BY_KEY.get(s_.key, s_) for s_ in REGISTRY]

# Per-app override field (PATCH /admin/apps/{id}/resources) -> the setting holding its ceiling.
RESOURCE_CEILINGS = {
    "cpu_request_m": "cpu_limit_max_m", "cpu_limit_m": "cpu_limit_max_m",
    "mem_request_mi": "mem_limit_max_mi", "mem_limit_mi": "mem_limit_max_mi",
    "db_max_connections": "override_db_connections_max", "ephemeral_limit_mi": "override_ephemeral_max_mi",
    "db_quota_mb": "override_db_quota_max_mb", "storage_quota_mb": "override_storage_quota_max_mb",
    "image_size_mb": "override_image_size_max_mb", "egress_mbit": "override_egress_max_mbit",
    "volume_size_gi": "override_volume_max_gi",
}

# --- cache: {id(bind): (bind, loaded_at, {key: raw})}; the bind is held so its id can't be reused by another engine ---
_cache: Dict[int, tuple] = {}


def invalidate() -> None:
    _cache.clear()


def _overrides(db: Session) -> Dict[str, str]:
    bind = db.get_bind()
    hit = _cache.get(id(bind))
    now = time.monotonic()
    if hit is not None and hit[0] is bind and now - hit[1] < CACHE_TTL_SECONDS:
        return hit[2]
    try:
        raw = {r.key: r.value for r in db.query(models.PlatformSetting).all()}
    except Exception:  # noqa: BLE001 - e.g. migration not applied yet: behave as if nothing is overridden
        db.rollback()
        raw = {}
    _cache[id(bind)] = (bind, now, raw)
    return raw


def parse(s: Setting, raw: str) -> Any:
    if s.type == "bool":
        return raw == "1"
    return int(raw)


def serialize(s: Setting, value: Any) -> str:
    return ("1" if value else "0") if s.type == "bool" else str(int(value))


def default(key: str) -> Any:
    return BY_KEY[key].default()


def get(db: Optional[Session], key: str) -> Any:
    """Effective value of a setting: the admin override if set, else the default (env/config). Unknown key = KeyError.
    With db=None (no session available) returns the default."""
    s = BY_KEY[key]
    if db is not None:
        raw = _overrides(db).get(key)
        if raw is not None:
            try:
                return parse(s, raw)
            except ValueError:
                pass
    return s.default()


def effective_volume_max_gi(db: Optional[Session]) -> int:
    """The user volume ceiling, never above the admin ceiling."""
    return min(get(db, "volume_max_gi"), get(db, "override_volume_max_gi"))


def validate_value(s: Setting, value: Any) -> Optional[str]:
    """Error message for a bad value, else None."""
    if s.type == "bool":
        return None if isinstance(value, bool) else "must be true or false"
    if isinstance(value, bool) or not isinstance(value, int):
        return "must be a whole number"
    if s.min is not None and value < s.min:
        return f"must be at least {s.min}"
    if s.max is not None and value > s.max:
        return f"must be at most {s.max} {s.unit}" + (f" ({s.max_reason})" if s.max_reason else "")
    return None


def _invariants(vals: Dict[str, Any]) -> List[str]:
    errs = []
    for d, c in DEFAULT_CEILINGS.items():
        if vals[d] > vals[c]:
            errs.append(f"{d}: can't exceed {c} (its ceiling, {vals[c]} {BY_KEY[c].unit})")
    if vals["volume_max_gi"] > vals["override_volume_max_gi"]:
        errs.append("volume_max_gi: can't exceed override_volume_max_gi (the admin volume ceiling)")
    if vals["default_app_limit"] > vals["max_apps_per_user"]:
        errs.append("default_app_limit: can't exceed max_apps_per_user")
    if vals["backup_max_bytes_per_app"] > vals["backup_max_bytes_total"]:
        errs.append("backup_max_bytes_per_app: can't exceed backup_max_bytes_total")
    return errs


def update(db: Session, changes: Dict[str, Any], actor_email: Optional[str]) -> List[str]:
    """Apply a partial update (value None = reset to default). Returns error messages (nothing saved) or [] on success."""
    from datetime import datetime, timezone
    errs = []
    for k, v in changes.items():
        s = BY_KEY.get(k)
        if s is None:
            errs.append(f"{k}: unknown setting")
        elif v is not None and (m := validate_value(s, v)):
            errs.append(f"{k}: {m}")
    if errs:
        return errs
    proposed = {s.key: get(db, s.key) for s in REGISTRY}
    for k, v in changes.items():
        proposed[k] = BY_KEY[k].default() if v is None else v
    errs = _invariants(proposed)
    if errs:
        return errs
    rows = {r.key: r for r in db.query(models.PlatformSetting).all()}
    now = datetime.now(timezone.utc)
    for k, v in changes.items():
        row = rows.get(k)
        if v is None:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(models.PlatformSetting(key=k, value=serialize(BY_KEY[k], v), updated_at=now, updated_by=actor_email))
        else:
            row.value, row.updated_at, row.updated_by = serialize(BY_KEY[k], v), now, actor_email
    db.commit()
    invalidate()
    return []


def describe_all(db: Session) -> List[dict]:
    rows = {r.key: r for r in db.query(models.PlatformSetting).all()}
    groups: Dict[str, List[dict]] = {}
    for s in REGISTRY:
        row = rows.get(s.key)
        overridden = row is not None
        groups.setdefault(s.group, []).append({
            "key": s.key, "label": s.label, "description": s.description, "type": s.type, "unit": s.unit,
            "value": get(db, s.key), "default": s.default(), "overridden": overridden,
            "min": s.min, "max": s.max, "ceiling_key": DEFAULT_CEILINGS.get(s.key),
            "updated_at": row.updated_at.isoformat() if row is not None and row.updated_at else None,
            "updated_by": row.updated_by if row is not None else None,
        })
    return [{"name": g, "settings": items} for g, items in groups.items()]
