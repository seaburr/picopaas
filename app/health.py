"""Admin health dashboard payload (GET /admin/health). Cached in-process for CACHE_TTL_S."""
from __future__ import annotations

import json
import logging
import os
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


def invalidate_db_server(server_id: int) -> None:
    """Forget cached per-server metrics (e.g. after the admin password or CA changed)."""
    with _lock:
        _db_cache.pop(server_id, None)
        _host_cache.pop(server_id, None)


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


# DO host metrics (cpu/mem/disk %): separate, slower cache so a DO failure never affects the SQL metrics.
HOST_METRICS_TTL_S = 45
_host_cache: dict[int, tuple[float, dict[str, Any]]] = {}


def _host_metrics(server_id: int, host: str) -> dict[str, Any]:
    """{"host_metrics": {...}|None, "host_metrics_error": str|None}. Never raises."""
    from app import db_host_metrics as hm
    if not config.DB_METRICS_ENABLED or not os.environ.get("DIGITALOCEAN_TOKEN"):
        return {"host_metrics": None, "host_metrics_error": None}
    now = time.monotonic()
    with _lock:
        hit = _host_cache.get(server_id)
    if hit and now - hit[0] < HOST_METRICS_TTL_S:
        return hit[1]
    try:
        out = {"host_metrics": hm.fetch_host_metrics(host), "host_metrics_error": None}
    except hm.HostMetricsError as exc:
        out = {"host_metrics": None, "host_metrics_error": str(exc)[:120]}
    except Exception as exc:  # noqa: BLE001
        out = {"host_metrics": None, "host_metrics_error": f"metrics failed ({type(exc).__name__})"}
    with _lock:
        _host_cache[server_id] = (now, out)
    return out


def reset_cache() -> None:
    global _background
    _db_cache.clear()
    _host_cache.clear()
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
            so.update(_host_metrics(so["id"], so.get("host", "")))
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
        replicas = max(1, app_.replicas or 1)
        slots = max(replicas, len(ps))  # a rollout surge pod can briefly exceed `replicas`
        apps.append({
            "id": app_.id, "name": app_.name, "owner_email": email, "status": app_.status, "namespace": ns,
            "pods_ready": sum(1 for p in ps if p["ready"]), "pods_total": len(ps),
            "restarts": sum(p["restarts"] for p in ps),
            "cpu_used_m": _sum([p["cpu_used_m"] for p in ps]), "mem_used_mi": _sum([p["mem_used_mi"] for p in ps]),
            "cpu_limit_m": eff["cpu_limit_m"], "mem_limit_mi": eff["mem_limit_mi"],  # per pod
            # usage above is summed over pods: compare it to the aggregate capacity, not the per-pod limit
            "replicas": replicas,
            "cpu_capacity_m": slots * eff["cpu_limit_m"], "mem_capacity_mi": slots * eff["mem_limit_mi"],
            "pod_usage": [{"name": p["name"], "ready": p["ready"], "restarts": p["restarts"],
                           "cpu_used_m": p["cpu_used_m"], "mem_used_mi": p["mem_used_mi"]}
                          for p in sorted(ps, key=lambda p: p["name"])],
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


def sample_app_metrics(db: Session, kube: KubeProvider) -> int:
    """Insert one app_metric_samples row per app that has usage in the cached snapshot (the same metrics.k8s.io
    data the Health page shows; no extra metrics-server call), then prune to METRICS_RETENTION_POINTS per app.
    Returns rows inserted. Skips suspended apps and apps with no readings."""
    cached = _get_snapshot(kube)
    if cached is None or not cached["snap"].get("metrics_available"):
        return 0
    by_ns: dict[str, list[dict]] = {}
    for p in cached["snap"].get("pods", []):
        by_ns.setdefault(p["namespace"], []).append(p)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    inserted = 0
    for app_ in db.query(models.App).filter(models.App.deleted_at.is_(None), models.App.status != "suspended").all():
        ps = by_ns.get(namespace_name(app_.id), [])
        cpu = _sum([p["cpu_used_m"] for p in ps])
        mem = _sum([p["mem_used_mi"] for p in ps])
        if cpu is None or mem is None:
            continue
        per_pod = {p["name"]: [p["cpu_used_m"], p["mem_used_mi"]] for p in ps
                   if p["cpu_used_m"] is not None and p["mem_used_mi"] is not None}
        db.add(models.AppMetricSample(app_id=app_.id, ts=now, cpu_m=int(cpu), mem_mi=int(mem),
                                      pods=max(1, len(ps)), per_pod=json.dumps(per_pod)))
        inserted += 1
        db.flush()
        cutoff = (
            db.query(models.AppMetricSample.id)
            .filter(models.AppMetricSample.app_id == app_.id)
            .order_by(models.AppMetricSample.id.desc())
            .offset(config.METRICS_RETENTION_POINTS).limit(1).scalar()
        )
        if cutoff is not None:
            db.query(models.AppMetricSample).filter(
                models.AppMetricSample.app_id == app_.id, models.AppMetricSample.id <= cutoff
            ).delete(synchronize_session=False)
    db.commit()
    return inserted


WORKLOAD_STATUSES = ("running", "deploying", "pending", "error", "suspended")


def db_counts(db: Session) -> dict[str, Any]:
    """Cheap DB-only platform counts; computed per request (never cached) so approvals show up immediately."""
    from sqlalchemy import func

    U, A = models.User, models.App
    # Every non-deleted account (admins included), so the numbers match the Admin users list; `admins` is a subset.
    users = {"total": 0, "active": 0, "pending": 0, "suspended": 0, "admins": 0}
    users["admins"] = db.query(func.count(U.id)).filter(U.role == "platform_admin", U.status != "deleted").scalar() or 0
    for status, n in (db.query(U.status, func.count(U.id)).filter(U.status != "deleted")
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
            "with_volume": db.query(func.count(A.id)).filter(live, A.volume_size_gi.isnot(None)).scalar() or 0,
            "by_db_engine": {
                "mysql": db.query(func.count(A.id)).filter(live, A.want_db.is_(True), A.db_engine == "mysql").scalar() or 0,
                "postgres": db.query(func.count(A.id)).filter(live, A.want_db.is_(True), A.db_engine == "pg").scalar() or 0,
            },
        },
    }
