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
    # Settings page layout, filled in by ROWS below: one row per tunable, with a Default and/or Ceiling column.
    row: str = ""
    row_label: str = ""
    column: str = "default"  # "default" | "ceiling"


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
    Setting("max_apps_per_user", "Apps per user, ceiling", "Most active apps one regular account can have. "
            "A user's own limit can be lower, never higher.", G_DEF, "int", "apps", _c("MAX_APPS_PER_USER"), 1, 100),
    Setting("default_app_limit", "Apps per user, default", "App limit given to a newly registered account. Not above the user ceiling.",
            G_DEF, "int", "apps", _c("DEFAULT_APP_LIMIT"), 0, 100),
    Setting("max_apps_per_admin", "Apps per platform admin, ceiling", "Most active apps one platform admin can have. "
            "An admin's own limit can be raised up to this, never above.", G_DEF, "int", "apps", _c("MAX_APPS_PER_ADMIN"), 1, 100),
    Setting("admin_default_app_limit", "Apps per platform admin, default", "Active apps every platform admin gets at least "
            "(raise one admin's own limit under Users). Not above the admin ceiling.", G_DEF, "int", "apps",
            _c("ADMIN_DEFAULT_APP_LIMIT"), 0, 100),
    # --- Backups (registered here; enforcement reads them in app/backups.py) ---
    Setting("backup_max_bytes_total", "Backup storage, platform total", "Ceiling for all backups together.", G_BAK, "int", "bytes",
            _c("BACKUP_MAX_BYTES_TOTAL"), _MIB, 10 * 1024**4),
    Setting("backup_max_bytes_per_app", "Backup storage, per app", "What one app's backups can use unless an admin sets its own ceiling "
            "(not above the platform total).", G_BAK, "int", "bytes", _c("BACKUP_MAX_BYTES_PER_APP"), _MIB, 10 * 1024**4),
    Setting("backup_override_max_bytes_per_app", "Backup storage, per-app override ceiling", "Highest backup storage an admin can give one app "
            "(Apps > Backups). Also bounded by the platform total.", G_BAK, "int", "bytes",
            _c("BACKUP_OVERRIDE_MAX_BYTES_PER_APP"), _MIB, 10 * 1024**4),
    Setting("backup_max_held_per_app", "Backups kept per app", "Most backup packages kept for one app.", G_BAK, "int", "backups",
            lambda: 14, 1, 365),
    Setting("backup_manual_per_hour", "On-demand backups per app per hour", "How many manual backups one app can start per hour.",
            G_BAK, "int", "per hour", lambda: 1, 1, 60),
    Setting("backup_export_max_per_hour", "Backup downloads per app per hour", "How many download packages one app can prepare per hour.",
            G_BAK, "int", "per hour", _c("BACKUP_EXPORT_MAX_PER_HOUR"), 1, 100),
    Setting("backups_enabled_default", "Scheduled backups on for new apps", "New apps start with scheduled backups turned on.",
            G_BAK, "bool", "", _c("BACKUP_DEFAULT_ENABLED")),
    # --- Features ---
    Setting("kaisen_enabled", "Kaisen (Preview)", "Let users build apps with Kaisen. Also needs KAISEN_ENABLED on the platform.",
            "Features", "bool", "", _c("KAISEN_ENABLED")),
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

# Settings page layout: key -> (group, row id, row label, column). One row per tunable; a pair shares a row id.
# Single-value settings sit in the Default column (what everyone gets) or the Ceiling column (a pure upper bound).
G_LIM, G_ACC = "Per-app limits", "Accounts and volumes"
_PAIRS = [  # (group, row id, row label, default key, ceiling key)
    (G_LIM, "cpu", "CPU", "default_cpu_limit_m", "cpu_limit_max_m"),
    (G_LIM, "mem", "Memory", "default_mem_limit_mi", "mem_limit_max_mi"),
    (G_LIM, "ephemeral", "Scratch disk", "default_ephemeral_mi", "override_ephemeral_max_mi"),
    (G_LIM, "image", "Image size", "default_image_size_mb", "override_image_size_max_mb"),
    (G_LIM, "db", "Database size", "default_db_quota_mb", "override_db_quota_max_mb"),
    (G_LIM, "storage", "Blob storage", "default_storage_quota_mb", "override_storage_quota_max_mb"),
    (G_LIM, "egress", "Egress bandwidth", "default_egress_mbit", "override_egress_max_mbit"),
    (G_ACC, "apps_user", "Apps per user", "default_app_limit", "max_apps_per_user"),
    (G_ACC, "apps_admin", "Apps per platform admin", "admin_default_app_limit", "max_apps_per_admin"),
    (G_ACC, "volume_size", "Volume size (users)", None, "volume_max_gi"),
    (G_BAK, "backup_app", "Backup storage per app", "backup_max_bytes_per_app", "backup_override_max_bytes_per_app"),
]
_SINGLES = [  # (group, row id, row label, key, column)
    (G_LIM, "db_conn", "Database connections", "override_db_connections_max", "ceiling"),
    (G_LIM, "replicas", "Replicas", "max_replicas", "ceiling"),
    (G_ACC, "volume_admin", "Volume size (admins)", "override_volume_max_gi", "ceiling"),
    (G_ACC, "volumes_total", "Volumes platform-wide", "volumes_max_total", "ceiling"),
    (G_BAK, "backup_total", "Backup storage, platform total", "backup_max_bytes_total", "ceiling"),
    (G_BAK, "backup_held", "Backups kept per app", "backup_max_held_per_app", "default"),
    (G_BAK, "backup_manual", "On-demand backups per app per hour", "backup_manual_per_hour", "default"),
    (G_BAK, "backup_export", "Backup downloads per app per hour", "backup_export_max_per_hour", "default"),
    (G_BAK, "backup_default_on", "Scheduled backups on for new apps", "backups_enabled_default", "default"),    (G_ACC, "kaisen", "Kaisen (Preview)", "kaisen_enabled", "default"),
]
_LAYOUT = {}
for _g, _r, _l, _dk, _ck in _PAIRS:
    if _dk:
        _LAYOUT[_dk] = (_g, _r, _l, "default")
    _LAYOUT[_ck] = (_g, _r, _l, "ceiling")
for _g, _r, _l, _k, _col in _SINGLES:
    _LAYOUT[_k] = (_g, _r, _l, _col)
for _k, (_g, _r, _l, _col) in _LAYOUT.items():
    BY_KEY[_k] = Setting(**{**BY_KEY[_k].__dict__, "group": _g, "row": _r, "row_label": _l, "column": _col})
REGISTRY[:] = [BY_KEY[s_.key] for s_ in REGISTRY]
# Pair partner: key -> (default key, ceiling key), both directions.
PAIRS = {}
for _g, _r, _l, _dk, _ck in _PAIRS:
    if _dk:
        PAIRS[_dk] = PAIRS[_ck] = (_dk, _ck)
# Default -> ceiling bound for every pair (per-app defaults keep the older DEFAULT_CEILINGS map for the max clamp).
CEILING_OF = {dk: ck for (dk, ck) in PAIRS.values()}

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


def kaisen_on(db: Optional[Session]) -> bool:
    """Kaisen is available: the deploy-time env switch AND the admin setting."""
    return bool(config.KAISEN_ENABLED and get(db, "kaisen_enabled"))


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


def _invariants(vals: Dict[str, Any], changed: Any = None) -> List[str]:
    errs = []
    for d, c in DEFAULT_CEILINGS.items():
        if vals[d] > vals[c]:
            errs.append(f"{d}: can't exceed {c} (its ceiling, {vals[c]} {BY_KEY[c].unit})")
    if vals["volume_max_gi"] > vals["override_volume_max_gi"]:
        errs.append("volume_max_gi: can't exceed override_volume_max_gi (the admin volume ceiling)")
    if vals["default_app_limit"] > vals["max_apps_per_user"]:
        errs.append("default_app_limit: can't exceed max_apps_per_user")
    if vals["admin_default_app_limit"] > vals["max_apps_per_admin"]:
        errs.append("admin_default_app_limit: can't exceed max_apps_per_admin")
    if vals["backup_max_bytes_per_app"] > vals["backup_max_bytes_total"]:
        errs.append("backup_max_bytes_per_app: can't exceed backup_max_bytes_total")
    # The override ceiling is clamped to the platform total wherever it's used, so only a change to it is checked against the total.
    if changed and "backup_override_max_bytes_per_app" in changed and vals["backup_override_max_bytes_per_app"] > vals["backup_max_bytes_total"]:
        errs.append("backup_override_max_bytes_per_app: can't exceed backup_max_bytes_total")
    if vals["backup_max_bytes_per_app"] > vals["backup_override_max_bytes_per_app"]:
        errs.append("backup_max_bytes_per_app: can't exceed backup_override_max_bytes_per_app (its ceiling)")
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
    before_user_ceiling = proposed["max_apps_per_user"]
    for k, v in changes.items():
        proposed[k] = BY_KEY[k].default() if v is None else v
    # Raising the user app ceiling lifts the admin pair to at least that ceiling (admins never get fewer than users).
    new_ceiling = proposed["max_apps_per_user"]
    if new_ceiling > before_user_ceiling:
        changes = dict(changes)
        for k in ("max_apps_per_admin", "admin_default_app_limit"):
            if proposed[k] < new_ceiling:
                proposed[k] = changes[k] = new_ceiling
    errs = _invariants(proposed, {k for k, v in changes.items() if v is not None})
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
            "min": s.min, "max": s.max, "ceiling_key": CEILING_OF.get(s.key), "default_key": PAIRS[s.key][0] if s.key in PAIRS else None,
            "row": s.row, "row_label": s.row_label, "column": s.column,
            "updated_at": row.updated_at.isoformat() if row is not None and row.updated_at else None,
            "updated_by": row.updated_by if row is not None else None,
        })
    return [{"name": g, "settings": items} for g, items in groups.items()]
