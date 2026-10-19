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
import re
from typing import Any, Callable

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app import config, crypto, env_refs, k8s_manifests, models
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


SUSPENDED_APPLIED = "scaled to zero"


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


def _fail(session: Session, app: models.App, detail: str) -> None:
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
    session.commit()


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
            _fail(session, app, "no database capacity")
            return
        try:
            creds = mysql_admin_factory(server).create_db_user(app.id)
        except Exception as exc:
            log.warning("app %s: db provisioning failed: %s", app.id, _provider_error_summary(exc))
            _fail(session, app, "database provisioning failed")
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
            _fail(session, app, "storage provisioning failed")
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
    if app.want_db and app.database is not None:
        refs.update(_db_refs(app))
    if app.want_storage and app.storage is not None:
        refs.update(_storage_refs(app))

    env_vars = {e.key: crypto.decrypt(e.value_encrypted) for e in app.env_vars}
    try:
        resolved_env = env_refs.resolve(env_vars, refs)
    except env_refs.EnvValidationError:
        if sweep:
            return
        _fail(session, app, "environment configuration invalid")
        return

    # 4. Render + apply manifests.
    manifests = k8s_manifests.render_app_manifests(
        app_id=app.id,
        hostname=app.hostname,
        image=app.image,
        port=app.port,
        env=resolved_env,
        mysql_cidr=_mysql_cidr(settings),
        mysql_port=app.database.server.port if app.want_db and app.database is not None else None,
        replicas=0 if app.status == "suspended" else 1,
        health_check_enabled=app.health_check_enabled,
        health_check_path=app.health_check_path,
    )
    suspended = app.status == "suspended"
    flat = k8s_manifests.flatten_manifests(manifests)
    # Only the digest is stored (the manifests include the resolved Secret).
    new_hash = hmac.new(
        config.SECRET_KEY.encode(), json.dumps(flat, sort_keys=True, default=str).encode(), hashlib.sha256
    ).hexdigest()  # keyed: unguessable offline
    if sweep and not force and app.rendered_hash == new_hash:
        return

    if not suspended and not sweep:
        app.status = "deploying"
        app.status_detail = None
        session.commit()

    namespace = k8s_manifests.namespace_name(app.id)
    try:
        kube.apply(flat)
        status = kube.get_status(namespace)
    except Exception as exc:
        # Operator-only log. k8s Status code/reason/message describe the failure without echoing Secret data.
        log.warning("app %s: kube apply failed: %s", app.id, _kube_error_summary(exc))
        if sweep:
            return
        _fail(session, app, "deployment failed")
        return

    app.rendered_hash = new_hash
    if suspended:
        # Scaled to zero; DB/storage kept. Marker stops the sweep from re-applying every tick.
        app.status_detail = SUSPENDED_APPLIED
    elif status.startswith("error:"):
        app.status = "error"
        app.status_detail = status[len("error:"):].strip()[:255] or None
    else:
        app.status = status
        app.status_detail = None
    session.commit()


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

    if app.database is not None:
        try:
            mysql_admin_factory(app.database.server).drop(app.id)
        except Exception:
            pass
        session.delete(app.database)

    if app.storage is not None:
        try:
            object_store.delete(app.id)
        except Exception:
            pass
        session.delete(app.storage)

    app.status = "deleted"
    app.status_detail = None
    session.commit()


def reconcile_all(
    session: Session,
    *,
    kube: KubeProvider,
    mysql_admin_factory: MySQLAdminFactory,
    object_store: ObjectStore,
    settings: Any,
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
    for app in pending_or_deploying:
        reconcile_app(
            session,
            app,
            kube=kube,
            mysql_admin_factory=mysql_admin_factory,
            object_store=object_store,
            settings=settings,
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
