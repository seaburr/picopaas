"""Moves apps from 'pending' toward 'running' (or 'error'), and cleans up
soft-deleted apps.

Dependency-injected (kube/mysql_admin_factory/object_store/settings are all
passed in, never imported as real providers here), so this module is
provider-agnostic and unit-testable against the fakes in app/providers/fake.py.

Nothing calls this yet — no APScheduler loop wires `reconcile_all` into a
background job, and `app/providers/kube.py`/`spaces.py` are still
NotImplementedError stubs (see docs/LEDGER.md Now/Next). TODO(backend-dev):
add an APScheduler job calling `reconcile_all` with the real providers once
`kube.py`/`spaces.py` land.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import ipaddress
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app import config, crypto, env_refs, image_check, k8s_manifests, models, resources
from app.db_placement import NoDbCapacity, choose_server
from app.providers.base import KubeProvider, MySQLAdmin, ObjectStore

log = logging.getLogger(__name__)

MySQLAdminFactory = Callable[[models.DbServer], MySQLAdmin]

DEFAULT_MYSQL_CIDR = "0.0.0.0/32"  # TODO: real per-DbServer/VPC CIDR once infra wires it


def _get(settings: Any, key: str, default: Any) -> Any:
    if isinstance(settings, dict):
        return settings.get(key, default)
    return getattr(settings, key, default)


def _mysql_cidr(settings: Any) -> str | None:
    """None means "in-cluster MySQL" (MYSQL_EGRESS_MODE=in-cluster); else a CIDR."""
    if _get(settings, "mysql_egress_mode", "cidr") == "in-cluster":
        return None
    return _get(settings, "mysql_cidr", None) or DEFAULT_MYSQL_CIDR


def _db_refs(app: models.App) -> dict[str, str]:
    """Build the `db.*` ref values from an already-provisioned AppDatabase."""
    row = app.database
    server = row.server
    password = crypto.decrypt(row.password_encrypted)
    return {
        "db.host": server.host,
        "db.port": str(server.port),
        "db.name": row.db_name,
        "db.user": row.db_user,
        "db.password": password,
        "db.url": f"mysql://{row.db_user}:{password}@{server.host}:{server.port}/{row.db_name}",
        "db.ca_cert": server.ca_cert or "",
        "db.tls": "required" if server.ca_cert else "off",
    }


def _storage_refs(app: models.App) -> dict[str, str]:
    """Build the `storage.*` ref values from an already-provisioned AppStorage."""
    row = app.storage
    endpoint = row.endpoint
    if endpoint and "://" not in endpoint:  # older rows were stored without a scheme; boto3 needs one
        endpoint = f"https://{endpoint}"
    return {
        "storage.endpoint": endpoint,
        "storage.region": row.region,
        "storage.bucket": row.bucket,
        "storage.access_key": row.access_key,
        "storage.secret_key": crypto.decrypt(row.secret_key_encrypted),
    }


_STORAGE_IP_TTL = 300.0
_storage_ip_cache: dict[str, tuple[float, list[str]]] = {}


def _private_storage_ips(endpoint: str) -> list[str]:
    """Private-range IPv4s the storage endpoint host resolves to (cached ~5 min); [] on DNS failure.

    Inside the DO VPC Spaces resolves to a private IP that the tenant "internet" egress rule excludes.
    """
    host = endpoint.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if not host:
        return []
    now = time.monotonic()
    hit = _storage_ip_cache.get(host)
    if hit and now - hit[0] < _STORAGE_IP_TTL:
        return hit[1]
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        log.warning("storage endpoint %s: DNS resolution failed (%s); no extra egress policy", host, type(exc).__name__)
        return hit[1] if hit else []  # keep the last good answer rather than flapping the policy
    nets = [ipaddress.ip_network(c) for c in k8s_manifests.PRIVATE_CIDRS]
    ips = sorted({i[4][0] for i in infos if any(ipaddress.ip_address(i[4][0]) in n for n in nets)})
    _storage_ip_cache[host] = (now, ips)
    return ips


UNREADABLE_DETAIL = "stored secrets unreadable (platform key changed); re-enter secrets or rotate credentials"
SUSPENDED_APPLIED = "scaled to zero"

# Rollout that isn't ready after this long fails (retryable) instead of sitting in "deploying" forever.
DEPLOY_TIMEOUT = timedelta(minutes=config.DEPLOY_TIMEOUT_MINUTES)

# status_reason code -> human status_detail (fixed strings: what happened + what to do; never provider text).
# Placeholders: {port}, {mem}, {path}, {minutes}.
STATUS_MESSAGES = {
    "image_pull": "Can't pull the image. Check the name and tag, and that it's public.",
    "crash_loop": "Your app starts and then exits. Check the logs, that it listens on port {port}, and that required env vars are set.",
    "oom": "Your app ran out of memory (limit {mem}Mi).",
    "disk": "Your app used more than {disk} MB of local disk (files, /tmp or logs) and was restarted. Store files in the storage bucket instead.",
    "not_ready": "Port {port} isn't answering. Is that the port your app listens on?",
    "not_ready_health": "Your app is running, but its health check on {path} isn't returning 2xx/3xx. Fix the path or turn the check off.",
    "root_image": "Your image runs as root, which isn't allowed. Add USER 10001 to your Dockerfile, rebuild, push, and save the app again.",
    "config": "The container couldn't be configured. Check the image's USER (must be non-root) and your env vars.",
    "quota": "The cluster refused to start a pod because this app's resource quota is used up. We'll retry automatically; if it keeps failing, ask an administrator.",
    "replica_failure": "The cluster couldn't create a pod for your app. We'll retry automatically; if it keeps failing, ask an administrator.",
    "unreadable_secrets": UNREADABLE_DETAIL,
    "timeout_not_ready": "Your app didn't become ready within {minutes} minutes: it is running but port {port} isn't answering. Is that the port your app listens on? We'll retry automatically.",
    "timeout_not_ready_health": "Your app didn't become ready within {minutes} minutes: it is running, but its health check on {path} isn't returning 2xx/3xx. Fix the path or turn the check off. We'll retry automatically.",
    "timeout_waiting": "Your app didn't become ready within {minutes} minutes: its pod is still waiting to start. We'll retry automatically; check the image and that the cluster has room.",
    "timeout_none": "Your app didn't become ready within {minutes} minutes and no pod was started. We'll retry automatically; if it keeps failing, ask an administrator.",
    "deploy_failed": "Couldn't deploy to the cluster. We'll retry automatically. If this keeps happening, edit and save the app to retry now, or ask an administrator.",
    "env_invalid": "One of your env vars can't be read or used. Open Edit, re-enter the value, and save.",
    "db_capacity": "No database space is free right now. Nothing you did wrong. We'll keep retrying; ask an administrator if it lasts more than an hour.",
    "db_failed": "Couldn't create your database. We'll retry automatically. If it keeps failing, ask an administrator.",
    "storage_failed": "Couldn't create your storage bucket. We'll retry automatically. If it keeps failing, ask an administrator.",
}
# Message key -> the status_reason code exposed on AppOut.
_REASON_OF = {
    "not_ready_health": "not_ready", "timeout_not_ready": "timeout", "timeout_not_ready_health": "timeout", "timeout_waiting": "timeout",
    "timeout_none": "timeout", "deploy_failed": "unknown", "replica_failure": "unknown",
}


def status_message(key: str, app: models.App) -> str:
    eff = resources.effective(app)
    return STATUS_MESSAGES[key].format(
        port=app.port, mem=eff["mem_limit_mi"], disk=config.EPHEMERAL_LIMIT_MI, path=app.health_check_path, minutes=config.DEPLOY_TIMEOUT_MINUTES
    )[:255]


def _kube_error_summary(exc: Exception) -> str:
    """`ApiException` -> "422 Unprocessable Entity: <k8s Status message>" (truncated); else the exception type."""
    status = getattr(exc, "status", None)
    if status is None:
        return type(exc).__name__
    message = ""
    try:
        message = json.loads(getattr(exc, "body", "") or "{}").get("message", "")
    except (ValueError, AttributeError):
        pass
    return f"{status} {getattr(exc, 'reason', '')}: {message}"[:500]


_SECRET_RES = (
    re.compile(r"(?i)(password|passwd|pwd|secret|token)\s*[=:]\s*\S+"),
    re.compile(r"://[^/\s@]*@"),
)


def _provider_error_summary(exc: Exception) -> str:
    """Operator-log summary: class name + pymysql code/message or botocore error Code; scrubbed, <=300 chars."""
    name = type(exc).__name__
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and isinstance(response.get("Error"), dict):
        text = f"{name} {response['Error'].get('Code', '')}"
    elif len(exc.args) >= 2 and isinstance(exc.args[0], int):
        text = f"{name} {exc.args[0]}: {exc.args[1]}"
    else:
        text = f"{name}: {exc}" if str(exc) else name
    text = _SECRET_RES[0].sub(r"\1=***", text)
    text = _SECRET_RES[1].sub("://***@", text)
    return text.strip()[:300]


BACKOFF = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=60)]


def _utcnow() -> datetime:
    """Naive UTC (SQLite/MySQL DATETIME columns are naive)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def backoff_for(error_count: int) -> timedelta:
    return BACKOFF[min(max(error_count, 1), len(BACKOFF)) - 1]


def retry_at(app: models.App) -> Optional[datetime]:
    """When an errored app will next be retried; None if it isn't errored or needs a user change."""
    if app.status != "error" or not app.error_retryable or app.last_attempt_at is None or app.deleted_at is not None:
        return None
    return _naive(app.last_attempt_at) + backoff_for(app.error_count)


def _record_failure(app: models.App, retryable: bool, now: Optional[datetime]) -> None:
    app.error_count = (app.error_count or 0) + 1
    app.last_attempt_at = _naive(now) if now else _utcnow()
    app.error_retryable = retryable


def _fail(
    session: Session,
    app: models.App,
    detail: str,
    *,
    retryable: bool = True,
    now: Optional[datetime] = None,
    reason: str = "unknown",
) -> None:
    """Mark an app 'error' with a short, secret-free detail and commit.

    `detail` must always be a fixed, hand-written string — never
    `str(exc)` — so a provider exception that happens to include a
    password/connection string/etc. can never end up in status_detail.
    """
    if app.status == "suspended":
        # Never lose the suspended state; retried next sweep (status_detail stays unmarked).
        app.status_detail = None
        session.commit()
        return
    app.status = "error"
    app.status_detail = detail
    app.status_reason = reason
    app.deploying_since = None
    _record_failure(app, retryable, now)
    session.commit()


def _fail_key(
    session: Session, app: models.App, key: str, *, retryable: bool = True, now: Optional[datetime] = None
) -> None:
    _fail(session, app, status_message(key, app), retryable=retryable, now=now, reason=_REASON_OF.get(key, key))


def reconcile_app(
    session: Session,
    app: models.App,
    *,
    kube: KubeProvider,
    mysql_admin_factory: MySQLAdminFactory,
    object_store: ObjectStore,
    settings: Any,
    sweep: bool = False,
    force: bool = False,
    now: Optional[datetime] = None,
) -> None:
    """Provision DB/storage (if requested and not already done), render and
    apply this app's k8s manifests, and update `app.status`/`status_detail`.

    Idempotent: an app that already has an AppDatabase/AppStorage row is not
    re-provisioned (the existing creds are reused to build the refs), so
    calling this repeatedly (e.g. from a polling reconcile_all loop) is safe.
    Soft-deleted apps are a no-op here — see `delete_app`.

    `sweep=True` is the drift check for already-running apps: a matching
    `rendered_hash` is a no-op, a stale one re-applies without flipping the
    app to 'deploying', and failures are logged and retried next tick.
    `force=True` (sweep only) re-applies even when the hash matches (namespace missing after a cluster rebuild).
    """
    if app.deleted_at is not None:
        return

    # 1. Database.
    if app.want_db and app.database is None:
        try:
            server = choose_server(session)
        except NoDbCapacity:
            _fail_key(session, app, "db_capacity", now=now)
            return
        try:
            creds = mysql_admin_factory(server).create_db_user(app.id)
        except Exception as exc:
            log.warning("app %s: db provisioning failed: %s", app.id, _provider_error_summary(exc))
            _fail_key(session, app, "db_failed", now=now)
            return
        db_row = models.AppDatabase(
            app_id=app.id,
            db_server_id=server.id,
            db_name=creds["name"],
            db_user=creds["user"],
            password_encrypted=crypto.encrypt(creds["password"]),
        )
        session.add(db_row)
        session.flush()
        app.database = db_row

    # 2. Storage.
    if app.want_storage and app.storage is None:
        try:
            creds = object_store.create_bucket_and_key(app.id)
        except Exception as exc:
            log.warning("app %s: storage provisioning failed: %s", app.id, _provider_error_summary(exc))
            _fail_key(session, app, "storage_failed", now=now)
            return
        storage_row = models.AppStorage(
            app_id=app.id,
            bucket=creds["bucket"],
            endpoint=creds["endpoint"],
            region=creds["region"],
            access_key=creds["access_key"],
            secret_key_encrypted=crypto.encrypt(creds["secret_key"]),
        )
        session.add(storage_row)
        session.flush()
        app.storage = storage_row

    # 3. Build refs, decrypt env vars, resolve.
    refs: dict[str, str] = {}
    try:
        if app.want_db and app.database is not None:
            refs.update(_db_refs(app))
        if app.want_storage and app.storage is not None:
            refs.update(_storage_refs(app))
        env_vars = {e.key: crypto.decrypt(e.value_encrypted) for e in app.env_vars}
    except ValueError:
        log.warning("app %s: stored secrets unreadable (platform key changed)", app.id)
        # Retryable with backoff: during a key-rotation rollout an old-key pod may lead the reconciler and
        # fail on rows the new pod already re-encrypted; it self-heals once the rollout finishes.
        _fail_key(session, app, "unreadable_secrets", retryable=True, now=now)
        return
    try:
        resolved_env = env_refs.resolve(env_vars, refs)
    except env_refs.EnvValidationError:
        if sweep:
            return
        _fail_key(session, app, "env_invalid", retryable=False, now=now)
        return

    # 4. Render + apply manifests.
    namespace = k8s_manifests.namespace_name(app.id)
    suspended = app.status == "suspended"
    render_kwargs = dict(
        app_id=app.id,
        hostname=app.hostname,
        image=image_check.pinned_image(app),
        image_ref=app.image,
        port=app.port,
        env=resolved_env,
        mysql_cidr=_mysql_cidr(settings),
        mysql_port=app.database.server.port if app.want_db and app.database is not None else None,
        storage_ips=(
            _private_storage_ips(refs["storage.endpoint"]) if "storage.endpoint" in refs else []
        ),
        replicas=0 if suspended else 1,
        health_check_enabled=app.health_check_enabled,
        health_check_path=app.health_check_path,
        resources=resources.effective(app),
        run_as_uid=app.run_as_uid,
        include_ingress=not suspended,
        restart_nonce=app.restart_nonce,
    )

    def _hash(flat_manifests: list[dict[str, Any]]) -> str:
        # Only the digest is stored (the manifests include the resolved Secret). Keyed: unguessable offline.
        return hmac.new(
            config.SECRET_KEY.encode(), json.dumps(flat_manifests, sort_keys=True, default=str).encode(), hashlib.sha256
        ).hexdigest()

    # Steady-state render (quota = 2x the new limits). The stored hash is this one unless the quota had to be
    # widened for an in-flight rollout, so a finished rollout makes the next sweep shrink the quota back.
    flat = k8s_manifests.flatten_manifests(k8s_manifests.render_app_manifests(**render_kwargs))
    new_hash = _hash(flat)
    if sweep and not force and app.rendered_hash == new_hash:
        return

    # Existing pods consume the quota with their own (possibly bigger) limits while the surge pod needs the NEW ones.
    eff = render_kwargs["resources"]
    try:
        used = kube.get_used_limits(namespace)
    except Exception as exc:
        log.warning("app %s: reading used limits failed: %s", app.id, _kube_error_summary(exc))
        used = None
    if used and (
        (used.get("cpu_limit_m") or 0) + eff["cpu_limit_m"] > 2 * eff["cpu_limit_m"]
        or (used.get("mem_limit_mi") or 0) + eff["mem_limit_mi"] > 2 * eff["mem_limit_mi"]
        or (used.get("ephemeral_limit_mi") or 0) + config.EPHEMERAL_LIMIT_MI > 2 * config.EPHEMERAL_LIMIT_MI
    ):
        flat = k8s_manifests.flatten_manifests(
            k8s_manifests.render_app_manifests(**render_kwargs, used_limits=used)
        )
        new_hash = _hash(flat)
        if sweep and not force and app.rendered_hash == new_hash:
            return

    if not suspended and not sweep:
        if app.status != "deploying":
            app.deploying_since = _naive(now) if now else _utcnow()
        app.status = "deploying"
        app.status_detail = None
        app.status_reason = None
        session.commit()

    try:
        kube.apply(flat)
        if suspended:
            # No Ingress while suspended: the host falls through to the wildcard catch-all (suspended page).
            kube.delete_ingress(namespace, k8s_manifests.ingress_name(app.id))
        status = kube.get_status(namespace)
    except Exception as exc:
        # Operator-only log. k8s Status code/reason/message describe the failure without echoing Secret data.
        log.warning("app %s: kube apply failed: %s", app.id, _kube_error_summary(exc))
        if sweep:
            return
        _fail_key(session, app, "deploy_failed", now=now)
        return

    app.rendered_hash = new_hash
    if suspended:
        # Scaled to zero; DB/storage kept. Marker stops the sweep from re-applying every tick.
        app.status_detail = SUSPENDED_APPLIED
    elif status.startswith("error:"):
        code = status[len("error:"):].strip() or "unknown"
        key = code if code in STATUS_MESSAGES else "replica_failure"
        app.status = "error"
        app.status_detail = status_message(key, app)
        app.status_reason = _REASON_OF.get(key, key)
        app.deploying_since = None
        _record_failure(app, True, now)
    elif status == "deploying" and not _deploy_timed_out(app, now):
        app.status = status
        app.status_detail = None
        app.status_reason = None
        if app.deploying_since is None:
            app.deploying_since = _naive(now) if now else _utcnow()
    elif status == "deploying":
        # Not ready after DEPLOY_TIMEOUT: fail (retryable) saying what we saw.
        try:
            hint = kube.pending_hint(namespace)
        except Exception:
            hint = "none"
        session.commit()  # keep rendered_hash
        key = f"timeout_{hint if hint in ('not_ready', 'waiting') else 'none'}"
        if key == "timeout_not_ready" and app.health_check_enabled:
            key += "_health"
        _fail_key(session, app, key, now=now)
        return
    else:
        app.status = status
        app.status_detail = None
        app.status_reason = None
        app.deploying_since = None
        app.reset_retry()
    session.commit()


def _deploy_timed_out(app: models.App, now: Optional[datetime]) -> bool:
    since = app.deploying_since
    if since is None:
        return False
    return (_naive(now) if now else _utcnow()) - _naive(since) > DEPLOY_TIMEOUT


def delete_app(
    session: Session,
    app: models.App,
    *,
    kube: KubeProvider,
    mysql_admin_factory: MySQLAdminFactory,
    object_store: ObjectStore,
) -> None:
    """Tear down a soft-deleted app's cluster resources, DB, and storage.

    Best-effort and idempotent: a failure tearing down one resource doesn't
    stop the others, and doesn't raise — a future `reconcile_all` pass will
    retry (the app stays deleted_at-set / status != 'deleted' until this
    fully succeeds).
    """
    namespace = k8s_manifests.namespace_name(app.id)
    try:
        kube.delete_namespace(namespace)
    except Exception:
        pass

    failed = False
    if app.database is not None:
        try:
            mysql_admin_factory(app.database.server).drop(app.id)
        except Exception as exc:
            if _is_not_found(exc):
                session.delete(app.database)
            else:
                failed = True
                log.warning("app %s: database drop failed (will retry): %s", app.id, _provider_error_summary(exc))
        else:
            session.delete(app.database)

    if app.storage is not None:
        try:
            object_store.delete(app.id)
        except Exception as exc:
            if _is_not_found(exc):
                session.delete(app.storage)
            else:
                failed = True
                log.warning("app %s: storage delete failed (will retry): %s", app.id, _provider_error_summary(exc))
        else:
            session.delete(app.storage)

    if failed:
        # Keep the remaining rows and the soft-deleted state; the next sweep retries.
        session.commit()
        return

    app.status = "deleted"
    app.status_detail = None
    session.commit()


def _is_not_found(exc: BaseException) -> bool:
    """MySQL unknown database/user or Spaces NoSuchBucket/404 (also via __cause__) count as already gone."""
    e: Optional[BaseException] = exc
    for _ in range(3):
        if e is None:
            break
        response = getattr(e, "response", None)
        if isinstance(response, dict) and isinstance(response.get("Error"), dict):
            if str(response["Error"].get("Code", "")) in ("NoSuchBucket", "NoSuchKey", "404", "NotFound"):
                return True
        if e.args and e.args[0] in (1008, 1049, 1396):
            return True
        e = e.__cause__
    return False


def reconcile_all(
    session: Session,
    *,
    kube: KubeProvider,
    mysql_admin_factory: MySQLAdminFactory,
    object_store: ObjectStore,
    settings: Any,
    now: Optional[datetime] = None,
) -> None:
    """One sweep: provision/apply every pending or still-deploying app, and
    clean up every soft-deleted app that isn't fully torn down yet.

    TODO(backend-dev): call this from an APScheduler interval job with the
    real providers (kube.py/mysql.py/spaces.py) once kube.py/spaces.py exist.
    """
    pending_or_deploying = (
        session.execute(
            select(models.App).where(
                models.App.deleted_at.is_(None),
                or_(
                    models.App.status.in_(["pending", "deploying"]),
                    and_(
                        models.App.status == "suspended",
                        or_(
                            models.App.status_detail.is_(None),
                            models.App.status_detail != SUSPENDED_APPLIED,
                        ),
                    ),
                ),
            )
        )
        .scalars()
        .all()
    )
    # Errored apps whose backoff has elapsed are retried; non-retryable ones wait for a user change.
    current = _naive(now) if now else _utcnow()
    errored = session.execute(
        select(models.App).where(
            models.App.deleted_at.is_(None),
            models.App.status == "error",
            models.App.error_retryable.is_(True),
        )
    ).scalars().all()
    due = [a for a in errored if a.last_attempt_at is None or current >= retry_at(a)]  # None: pre-migration row
    for app in [*pending_or_deploying, *due]:
        reconcile_app(
            session,
            app,
            kube=kube,
            mysql_admin_factory=mysql_admin_factory,
            object_store=object_store,
            settings=settings,
            now=now,
        )

    # Drift sweep: running apps whose rendered manifests changed (platform upgrade) get re-applied.
    running = session.execute(
        select(models.App).where(models.App.deleted_at.is_(None), models.App.status == "running")
    ).scalars().all()
    existing_ns: Optional[set[str]] = None
    if running:
        try:
            existing_ns = kube.list_app_namespaces()  # one call per tick
        except Exception as exc:
            log.warning("list_app_namespaces failed: %s", _kube_error_summary(exc))
    for app in running:
        reconcile_app(
            session,
            app,
            kube=kube,
            mysql_admin_factory=mysql_admin_factory,
            object_store=object_store,
            settings=settings,
            sweep=True,
            # DR: namespace gone (cluster rebuilt) -> re-apply even though the hash still matches.
            force=existing_ns is not None and k8s_manifests.namespace_name(app.id) not in existing_ns,
        )

    not_cleaned_up = (
        session.execute(
            select(models.App).where(
                models.App.deleted_at.is_not(None),
                models.App.status != "deleted",
            )
        )
        .scalars()
        .all()
    )
    for app in not_cleaned_up:
        delete_app(
            session,
            app,
            kube=kube,
            mysql_admin_factory=mysql_admin_factory,
            object_store=object_store,
        )
