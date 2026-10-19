"""Admin health dashboard payload (GET /admin/health). Cached in-process for CACHE_TTL_S."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app import config, models
from app.k8s_manifests import QUOTA_LIMITS_CPU, QUOTA_LIMITS_MEMORY, namespace_name
from app.providers.base import KubeProvider

CACHE_TTL_S = 15
PLATFORM_NAMESPACES = ("picopaas-system", "traefik", "cert-manager")

_cache: dict[str, Any] = {"at": None, "value": None}


def reset_cache() -> None:
    _cache["at"] = _cache["value"] = None


def _limit_cpu_m() -> int:
    return int(QUOTA_LIMITS_CPU[:-1]) if QUOTA_LIMITS_CPU.endswith("m") else int(float(QUOTA_LIMITS_CPU) * 1000)


def _limit_mem_mi() -> int:
    return int(QUOTA_LIMITS_MEMORY[:-2])  # "512Mi"


def _sum(vals: list) -> Any:
    known = [v for v in vals if v is not None]
    return sum(known) if known else None


def build(db: Session, kube: KubeProvider, server_outs: list[dict]) -> dict[str, Any]:
    errors: list[str] = []
    try:
        snap = kube.cluster_snapshot()
    except Exception as exc:  # noqa: BLE001 - never leak exception text (could echo URLs/creds)
        snap = {"nodes": [], "pods": [], "metrics_available": False, "errors": []}
        errors.append(f"cluster snapshot failed ({type(exc).__name__})")
    errors.extend(snap.get("errors", []))
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
        apps.append({
            "id": app_.id, "name": app_.name, "owner_email": email, "status": app_.status, "namespace": ns,
            "pods_ready": sum(1 for p in ps if p["ready"]), "pods_total": len(ps),
            "restarts": sum(p["restarts"] for p in ps),
            "cpu_used_m": _sum([p["cpu_used_m"] for p in ps]), "mem_used_mi": _sum([p["mem_used_mi"] for p in ps]),
            "cpu_limit_m": _limit_cpu_m(), "mem_limit_mi": _limit_mem_mi(),
        })

    platform = [
        {"name": p["name"], "namespace": p["namespace"], "ready": p["ready"], "restarts": p["restarts"],
         "cpu_used_m": p["cpu_used_m"], "mem_used_mi": p["mem_used_mi"]}
        for ns in PLATFORM_NAMESPACES for p in by_ns.get(ns, [])
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics_available": bool(snap.get("metrics_available", False)),
        "errors": errors,
        "cluster": {"node_count": len(nodes), "max_nodes": config.MAX_NODES, "pods_total": len(pods)},
        "nodes": nodes,
        "apps": apps,
        "platform": platform,
        "db_servers": server_outs,
    }


def get_cached(builder) -> dict[str, Any]:
    now = time.monotonic()
    if _cache["value"] is not None and now - _cache["at"] < CACHE_TTL_S:
        return _cache["value"]
    value = builder()
    _cache["at"], _cache["value"] = now, value
    return value
