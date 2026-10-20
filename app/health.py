"""Admin health dashboard payload (GET /admin/health). Cached in-process for CACHE_TTL_S."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app import config, models
from app import resources
from app.k8s_manifests import namespace_name
from app.providers.base import KubeProvider

log = logging.getLogger(__name__)

CACHE_TTL_S = 15
PLATFORM_NAMESPACES = ("picopaas-system", "traefik", "cert-manager")

# Cache of the (slow) k8s snapshot only; DB-derived data is built per request.
# {"at": monotonic secs, "generated_at": iso str, "snap": dict, "errors": [str]}
_cache: dict[str, Any] = {"at": None, "value": None}
_lock = threading.Lock()
_background = False  # True once start_background() ran: requests then never block on k8s


# Per-DbServer SQL metrics, keyed by server id: {id: (monotonic_at, metrics_dict)}. Same TTL as the k8s cache,
# so rapid Health refreshes don't hit the DB servers (errors are cached too, to avoid hammering a down server).
_db_cache: dict[int, tuple[float, dict[str, Any]]] = {}


def _server_metrics(db: Session, server_id: int, factory) -> dict[str, Any]:
    now = time.monotonic()
    with _lock:
        hit = _db_cache.get(server_id)
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    out: dict[str, Any] = {"connections": None, "data_size_bytes": None, "uptime_seconds": None, "error": None}
    try:
        server = db.get(models.DbServer, server_id)
        h = factory(server).server_health()
        out["connections"] = {"used": h["threads_connected"], "running": h["threads_running"],
                              "max": h["max_connections"], "max_used": h["max_used_connections"]}
        out["data_size_bytes"] = h["data_size_bytes"]
        out["uptime_seconds"] = h.get("uptime_seconds")
    except Exception as exc:  # noqa: BLE001 - scrubbed: only the type and (for MySQLAdminError) its code-only text
        from app.providers.base import MySQLAdminError
        out["error"] = str(exc)[:120] if isinstance(exc, MySQLAdminError) and "mysql error" in str(exc) \
            else f"unreachable or query failed ({type(exc).__name__})"
    with _lock:
        _db_cache[server_id] = (now, out)
    return out


def reset_cache() -> None:
    global _background
    _db_cache.clear()
    _cache["at"] = _cache["value"] = None
    _background = False


def refresh(kube: KubeProvider) -> None:
    """Fetch a fresh cluster snapshot into the cache. Never raises; no exception text is kept."""
    errors: list[str] = []
    try:
        snap = kube.cluster_snapshot()
    except Exception as exc:  # noqa: BLE001 - never leak exception text (could echo URLs/creds)
        snap = {"nodes": [], "pods": [], "metrics_available": False, "errors": []}
        errors.append(f"cluster snapshot failed ({type(exc).__name__})")
    errors.extend(snap.get("errors", []))
    value = {"snap": snap, "errors": errors, "generated_at": datetime.now(timezone.utc).isoformat()}
    with _lock:
        _cache["at"], _cache["value"] = time.monotonic(), value


def start_background(kube_getter) -> None:
    """Mark the cache as background-refreshed (requests never block) and warm it in a thread.
    The caller schedules `refresh` every CACHE_TTL_S (see app/scheduler.py)."""
    global _background
    _background = True

    def _warm():
        try:
            refresh(kube_getter())
        except Exception:  # noqa: BLE001
            log.exception("health warm-up failed")

    threading.Thread(target=_warm, name="health-warmup", daemon=True).start()


def _get_snapshot(kube: KubeProvider) -> dict[str, Any] | None:
    """Cached snapshot. None = still warming (background mode, nothing cached yet).
    Without a background refresher (dev/tests) falls back to a synchronous refresh every CACHE_TTL_S."""
    with _lock:
        value, at = _cache["value"], _cache["at"]
    if _background:
        return value
    if value is None or time.monotonic() - at >= CACHE_TTL_S:
        refresh(kube)
        value = _cache["value"]
    return value


def _sum(vals: list) -> Any:
    known = [v for v in vals if v is not None]
    return sum(known) if known else None


def build(db: Session, kube: KubeProvider, server_outs: list[dict], mysql_factory=None) -> dict[str, Any]:
    if mysql_factory is not None:
        for so in server_outs:
            so.update(_server_metrics(db, so["id"], mysql_factory))
        for so in server_outs:
            so["disk_mb"] = config.MANAGED_DB_DISK_MB
    cached = _get_snapshot(kube)
    warming = cached is None
    if warming:
        snap, errors, generated_at = {"nodes": [], "pods": [], "metrics_available": False}, [], None
    else:
        snap, errors, generated_at = cached["snap"], list(cached["errors"]), cached["generated_at"]
    nodes, pods = snap.get("nodes", []), snap.get("pods", [])

    by_ns: dict[str, list[dict]] = {}
    for p in pods:
        by_ns.setdefault(p["namespace"], []).append(p)

    apps = []
    q = (
        db.query(models.App, models.User.email)
        .join(models.User, models.User.id == models.App.owner_id)
        .filter(models.App.deleted_at.is_(None))
        .order_by(models.App.id)
    )
    for app_, email in q.all():
        ns = namespace_name(app_.id)
        ps = by_ns.get(ns, [])
        eff = resources.effective(app_)
        apps.append({
            "id": app_.id, "name": app_.name, "owner_email": email, "status": app_.status, "namespace": ns,
            "pods_ready": sum(1 for p in ps if p["ready"]), "pods_total": len(ps),
            "restarts": sum(p["restarts"] for p in ps),
            "cpu_used_m": _sum([p["cpu_used_m"] for p in ps]), "mem_used_mi": _sum([p["mem_used_mi"] for p in ps]),
            "cpu_limit_m": eff["cpu_limit_m"], "mem_limit_mi": eff["mem_limit_mi"],
        })

    platform = [
        {"name": p["name"], "namespace": p["namespace"], "ready": p["ready"], "restarts": p["restarts"],
         "cpu_used_m": p["cpu_used_m"], "mem_used_mi": p["mem_used_mi"]}
        for ns in PLATFORM_NAMESPACES for p in by_ns.get(ns, [])
    ]

    return {
        "generated_at": generated_at,  # when the k8s snapshot was taken (null while warming)
        "warming": warming,
        "metrics_available": bool(snap.get("metrics_available", False)),
        "errors": errors,
        "cluster": {"node_count": len(nodes), "max_nodes": config.MAX_NODES, "pods_total": len(pods)},
        "nodes": nodes,
        "apps": apps,
        "platform": platform,
        "db_servers": server_outs,
    }


WORKLOAD_STATUSES = ("running", "deploying", "pending", "error", "suspended")


def db_counts(db: Session) -> dict[str, Any]:
    """Cheap DB-only platform counts; computed per request (never cached) so approvals show up immediately."""
    from sqlalchemy import func

    U, A = models.User, models.App
    users = {"total": 0, "active": 0, "pending": 0, "suspended": 0}
    for status, n in (db.query(U.status, func.count(U.id)).filter(U.role == "user", U.status != "deleted")
                      .group_by(U.status).all()):
        users["total"] += n
        if status in users:
            users[status] = n
    pending = (db.query(U).filter(U.role == "user", U.status == "pending")
               .order_by(U.created_at, U.id).limit(20).all())
    by_status = {k: 0 for k in WORKLOAD_STATUSES}
    total = 0
    for status, n in db.query(A.status, func.count(A.id)).filter(A.deleted_at.is_(None)).group_by(A.status).all():
        total += n
        if status in by_status:
            by_status[status] = n
    live = A.deleted_at.is_(None)
    return {
        "users": users,
        "pending_users": [{"id": u.id, "email": u.email, "created_at": u.created_at.isoformat()} for u in pending],
        "workloads": {
            "apps_total": total, "by_status": by_status,
            "with_db": db.query(func.count(A.id)).filter(live, A.want_db.is_(True)).scalar() or 0,
            "with_storage": db.query(func.count(A.id)).filter(live, A.want_storage.is_(True)).scalar() or 0,
        },
    }
