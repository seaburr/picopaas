"""Prometheus metrics: GET /metrics (text exposition format).

HTTP request counters/latency are recorded by a middleware (path TEMPLATE, never the raw path, so ids don't explode the
label set). Platform gauges (users, apps, db servers by type, volumes, buckets, databases, cluster nodes) are read at
scrape time. Access: `Authorization: Bearer $METRICS_TOKEN` (for a Prometheus scraper; endpoint is 404 for tokens when
METRICS_TOKEN is unset) or a platform-admin browser session.
"""
from __future__ import annotations

import time

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import config, models

REGISTRY = CollectorRegistry()
HTTP_REQUESTS = Counter("picopaas_http_requests_total", "HTTP requests handled", ["method", "path", "status"], registry=REGISTRY)
HTTP_LATENCY = Histogram("picopaas_http_request_duration_seconds", "HTTP request latency", ["method", "path"], registry=REGISTRY,
                         buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
HTTP_INFLIGHT = Gauge("picopaas_http_requests_in_flight", "HTTP requests being handled", registry=REGISTRY)

USERS = Gauge("picopaas_users", "Accounts (not deleted)", registry=REGISTRY)
APPS = Gauge("picopaas_apps", "Apps (not deleted)", registry=REGISTRY)
DB_SERVERS = Gauge("picopaas_database_servers", "Database servers", ["type"], registry=REGISTRY)
VOLUMES = Gauge("picopaas_volumes", "Apps with a persistent volume", registry=REGISTRY)
BUCKETS = Gauge("picopaas_blob_storage_buckets", "Blob storage buckets", registry=REGISTRY)
DATABASES = Gauge("picopaas_databases", "Tenant databases", registry=REGISTRY)
NODES = Gauge("picopaas_cluster_nodes", "Cluster nodes (from the cached health snapshot)", registry=REGISTRY)
NODES_MAX = Gauge("picopaas_cluster_nodes_max", "Autoscale maximum nodes (MAX_NODES; 0 = not configured)", registry=REGISTRY)

SKIP_PATHS = ("/metrics",)
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def method_label(request) -> str:
    """Client-supplied HTTP methods are arbitrary tokens: collapse unknown ones so scanners can't grow the label set."""
    return request.method if request.method in _METHODS else "OTHER"


def path_template(request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if path else "unmatched"  # unknown URLs share one label: scanners can't create series


async def middleware(request, call_next):
    if request.url.path in SKIP_PATHS:
        return await call_next(request)
    start = time.perf_counter()
    HTTP_INFLIGHT.inc()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        HTTP_INFLIGHT.dec()
        p = path_template(request)
        m = method_label(request)
        HTTP_REQUESTS.labels(m, p, str(status)).inc()
        HTTP_LATENCY.labels(m, p).observe(time.perf_counter() - start)


def refresh_gauges(db: Session) -> None:
    A = models.App
    USERS.set(db.query(func.count(models.User.id)).filter(models.User.status != "deleted").scalar() or 0)
    APPS.set(db.query(func.count(A.id)).filter(A.deleted_at.is_(None)).scalar() or 0)
    by_type = {"mysql": 0, "psql": 0}
    for engine, n in db.query(models.DbServer.engine, func.count(models.DbServer.id)).group_by(models.DbServer.engine).all():
        by_type["psql" if engine == "pg" else "mysql"] += n
    for t, n in by_type.items():
        DB_SERVERS.labels(t).set(n)
    VOLUMES.set(db.query(func.count(A.id)).filter(A.deleted_at.is_(None), A.volume_size_gi.isnot(None)).scalar() or 0)
    BUCKETS.set(db.query(func.count(models.AppStorage.id)).join(A, A.id == models.AppStorage.app_id)
                .filter(A.deleted_at.is_(None)).scalar() or 0)
    DATABASES.set(db.query(func.count(models.AppDatabase.id)).join(A, A.id == models.AppDatabase.app_id)
                  .filter(A.deleted_at.is_(None)).scalar() or 0)
    from app import health
    cached = health._cache.get("value")
    NODES.set(len(cached["snap"].get("nodes", [])) if cached else 0)
    NODES_MAX.set(config.MAX_NODES or 0)


def render(db: Session) -> tuple[bytes, str]:
    refresh_gauges(db)
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
