"""Background reconcile + quota jobs. Started from the FastAPI lifespan only
when RECONCILER_ENABLED=1 (never in tests).

Leader election: on MySQL we hold `GET_LOCK('picopaas-reconciler', 0)` on a
dedicated connection; replicas that fail to get it skip the tick. On sqlite
there is no lock (single process).
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select, text

from app import config, crypto, models
from app.db import SessionLocal, engine
from app.providers.base import ObjectStoreError

log = logging.getLogger(__name__)

LOCK_NAME = "picopaas-reconciler"
_lock_conn: Any = None
# The reconcile and quota jobs run on separate APScheduler threads; one MySQL connection must not be used
# concurrently (it corrupts the TLS stream: "bad record mac").
_lock_guard = threading.Lock()


def try_acquire_leader() -> bool:
    """True if this process is (or just became) the leader. MySQL named locks are
    per-connection, so we keep one connection open for the process lifetime."""
    if engine.dialect.name != "mysql":
        return True
    with _lock_guard:
        return _try_acquire_leader_locked()


def _try_acquire_leader_locked() -> bool:
    global _lock_conn
    try:
        if _lock_conn is None:
            _lock_conn = engine.connect()
        got = _lock_conn.execute(text("SELECT GET_LOCK(:n, 0)"), {"n": LOCK_NAME}).scalar()
        _lock_conn.commit()
        return got == 1
    except Exception:
        log.exception("leader lock check failed")
        if _lock_conn is not None:
            try:
                _lock_conn.close()
            except Exception:
                pass
            _lock_conn = None
        return False


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        mysql_egress_mode=config.MYSQL_EGRESS_MODE,
        mysql_cidr=config.MYSQL_EGRESS_CIDR or None,
        pg_cidr=config.PG_EGRESS_CIDR or None,
    )


def _object_store() -> Any | None:
    try:
        from app.providers.spaces import SpacesStore
    except ImportError:
        log.warning("SpacesStore not available; storage reconcile/quota skipped")
        return None
    try:
        return SpacesStore.from_env()
    except (KeyError, ObjectStoreError) as exc:
        # SpacesStore.from_env() raises ObjectStoreError for a missing env var. Degrade (apps without storage still
        # reconcile); apps that DO need storage fail per-app with a clear error via _NoStorage / the reconciler.
        log.warning("Spaces not configured (%s); storage reconcile/quota skipped", exc)
        return None


class _NoStorage:
    """Stand-in so apps without storage still reconcile when Spaces is unavailable."""

    def __getattr__(self, name: str):
        def _unavailable(*a: Any, **k: Any):
            raise RuntimeError("object store unavailable")

        return _unavailable


def _mysql_factory(server: models.DbServer):
    from app.providers.factory import db_admin_for

    return db_admin_for(server)


def reconcile_job() -> None:
    if not try_acquire_leader():
        return
    from app import backups
    from app.providers.kube import KubeClient
    from app.reconciler import reconcile_all

    session = SessionLocal()
    try:
        reconcile_all(
            session,
            kube=KubeClient(),
            mysql_admin_factory=_mysql_factory,
            object_store=_object_store() or _NoStorage(),
            settings=_settings(),
            backups_active=backups.usable(),
        )
    except Exception:
        log.exception("reconcile_all failed")
    finally:
        session.close()


def _quota_run(*, databases: bool, storage: bool) -> None:
    if not try_acquire_leader():
        return
    try:
        from app.quota import poll_quotas
    except ImportError:
        log.warning("app.quota missing; quota poll skipped")
        return
    # Without Spaces, still enforce DB quotas (storage polls then fail per-row and are logged).
    store = _object_store() or _NoStorage()
    session = SessionLocal()
    try:
        poll_quotas(session, mysql_admin_factory=_mysql_factory, object_store=store,
                    databases=databases, storage=storage)
    except Exception:
        log.exception("poll_quotas failed")
    finally:
        session.close()


def db_quota_job() -> None:
    _quota_run(databases=True, storage=False)


def storage_quota_job() -> None:
    _quota_run(databases=False, storage=True)


def quota_job() -> None:
    """Both polls in one (kept for callers/tests)."""
    _quota_run(databases=True, storage=True)


def image_watch_job() -> None:
    """Re-resolve floating tags; roll out new images that pass the size/non-root checks."""
    if not try_acquire_leader():
        return
    from app import image_check
    from app.dependencies import get_registry

    session = SessionLocal()
    try:
        counts = image_check.watch_all(session, get_registry())
        if counts.get("updated") or counts.get("rejected"):
            log.info("image watch: %s", counts)
    except Exception:
        log.exception("image watch failed")
    finally:
        session.close()


def metrics_sample_job() -> None:
    """Leader-only: persist one CPU/memory point per app from the cached health snapshot."""
    if not try_acquire_leader():
        return
    from app import health
    from app.dependencies import get_kube

    session = SessionLocal()
    try:
        health.sample_app_metrics(session, get_kube())
    except Exception:
        session.rollback()
        log.exception("metrics sampling failed")
    finally:
        session.close()


def audit_purge_job() -> None:
    """Leader-only daily: delete audit rows older than AUDIT_RETENTION_DAYS (0 = keep forever)."""
    if config.AUDIT_RETENTION_DAYS <= 0 or not try_acquire_leader():
        return
    from app import audit

    session = SessionLocal()
    try:
        n = audit.purge_old(session, config.AUDIT_RETENTION_DAYS)
        if n:
            log.info("audit purge: deleted %d rows", n)
    except Exception:
        session.rollback()
        log.exception("audit purge failed")
    finally:
        session.close()


def backup_job() -> None:
    """Leader-only: poll running backup Jobs, queue/start due ones, purge expired repos (app/backups.py). Inert unless
    BACKUPS_ENABLED and the backup bucket/key are configured."""
    from app import backups

    if not backups.usable() or not try_acquire_leader():
        return
    from app.providers.backup import KubeBackupRunner

    session = SessionLocal()
    try:
        backups.tick(session, KubeBackupRunner())
    except Exception:
        session.rollback()
        log.exception("backup tick failed")
    finally:
        session.close()


def backup_sweep_job() -> None:
    """Leader-only: reconcile stuck backup runs and delete orphaned Jobs/Secrets/blobs/rows (app/backup_sweeper.py)."""
    from app import backup_sweeper, backups

    if not backups.usable() or not try_acquire_leader():
        return
    from app.providers.backup import KubeBackupRunner

    session = SessionLocal()
    try:
        backup_sweeper.sweep(session, KubeBackupRunner())
    except Exception:
        session.rollback()
        log.exception("backup sweep failed")
    finally:
        session.close()


def _read_ca() -> str | None:
    """PEM text from the file at MYSQL_SSL_CA, or None if unset/unreadable."""
    path = os.environ.get("MYSQL_SSL_CA") or config.MYSQL_SSL_CA
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() + "\n" or None
    except OSError:
        return None


def rotate_encrypted_data() -> None:
    """Re-encrypt all stored secrets to the current FERNET_KEY (idempotent; counts logged only)."""
    session = SessionLocal()
    try:
        crypto.rotate_all(session)
    except Exception:  # noqa: BLE001 - never block startup on rotation
        session.rollback()
        log.exception("fernet rotation failed")
    finally:
        session.close()


def bootstrap_db_server() -> None:
    """Insert DbServer 'primary' from BOOTSTRAP_DB_* env if none exists; otherwise keep its connection details
    and admin credentials in sync with the env (bootstrap.yml reads them from DO each run). That also repairs a
    row whose password can no longer be decrypted after a FERNET_KEY change."""
    host = os.environ.get("BOOTSTRAP_DB_HOST")
    user = os.environ.get("BOOTSTRAP_DB_USER")
    password = os.environ.get("BOOTSTRAP_DB_PASSWORD")
    if not (host and user and password):
        return
    ca_cert = _read_ca()
    session = SessionLocal()
    try:
        if session.execute(select(models.DbServer.id).where(models.DbServer.engine == "mysql").limit(1)).first() is not None:
            primary = session.execute(
                select(models.DbServer).where(models.DbServer.name == "primary")
            ).scalar_one_or_none()
            if primary is None:
                return
            changed = []
            if primary.ca_cert is None and ca_cert:
                primary.ca_cert = ca_cert
                changed.append("ca_cert")
            port = int(os.environ.get("BOOTSTRAP_DB_PORT", "3306"))
            if (primary.host, primary.port, primary.admin_user) != (host, port, user):
                primary.host, primary.port, primary.admin_user = host, port, user
                changed.append("connection")
            try:
                current = crypto.decrypt(primary.admin_password_encrypted)
            except ValueError:
                current = None
            if current != password:
                primary.admin_password_encrypted = crypto.encrypt(password)
                changed.append("admin password")
            if changed:
                session.commit()
                log.info("updated DbServer 'primary': %s", ", ".join(changed))
            return
        session.add(
            models.DbServer(
                name="primary",
                host=host,
                port=int(os.environ.get("BOOTSTRAP_DB_PORT", "3306")),
                admin_user=user,
                admin_password_encrypted=crypto.encrypt(password),
                ca_cert=ca_cert,
            )
        )
        session.commit()
        log.info("bootstrapped DbServer 'primary'")
    finally:
        session.close()


def bootstrap_pg_server() -> None:
    """Insert (or keep in sync) DbServer 'primary-pg' (engine=pg) from BOOTSTRAP_PG_HOST/PORT/USER/PASSWORD
    (+ BOOTSTRAP_PG_CA = path to the PEM CA) when all of host/user/password are set. Independent of the MySQL
    bootstrap: an existing MySQL 'primary' does not stop it. No network calls (the per-server `REVOKE .. ON
    defaultdb FROM PUBLIC` runs in PostgresAdmin.create_db_user)."""
    host = os.environ.get("BOOTSTRAP_PG_HOST")
    user = os.environ.get("BOOTSTRAP_PG_USER")
    password = os.environ.get("BOOTSTRAP_PG_PASSWORD")
    if not (host and user and password):
        return
    port = int(os.environ.get("BOOTSTRAP_PG_PORT", "25060"))
    ca_cert = None
    ca_path = os.environ.get("BOOTSTRAP_PG_CA") or config.PG_BOOTSTRAP_CA
    if ca_path:
        try:
            with open(ca_path, encoding="utf-8") as f:
                ca_cert = f.read().strip() + "\n"
        except OSError:
            ca_cert = None
    session = SessionLocal()
    try:
        row = session.execute(
            select(models.DbServer).where(models.DbServer.name == "primary-pg")
        ).scalar_one_or_none()
        if row is None:
            session.add(models.DbServer(
                name="primary-pg", engine="pg", host=host, port=port, admin_user=user,
                admin_password_encrypted=crypto.encrypt(password), ca_cert=ca_cert, max_databases=10,
            ))
            session.commit()
            log.info("bootstrapped DbServer 'primary-pg'")
            return
        changed = []
        if ca_cert and row.ca_cert != ca_cert:
            row.ca_cert = ca_cert
            changed.append("ca_cert")
        if (row.host, row.port, row.admin_user) != (host, port, user):
            row.host, row.port, row.admin_user = host, port, user
            changed.append("connection")
        try:
            current = crypto.decrypt(row.admin_password_encrypted)
        except ValueError:
            current = None
        if current != password:
            row.admin_password_encrypted = crypto.encrypt(password)
            changed.append("admin password")
        if changed:
            session.commit()
            log.info("updated DbServer 'primary-pg': %s", ", ".join(changed))
    finally:
        session.close()


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler()
    sched.add_job(reconcile_job, "interval", seconds=15, max_instances=1, coalesce=True, id="reconcile")
    # next_run_time=now: every new pod would otherwise wait a full interval before its first check (the platform
    # is redeployed often, which kept pushing the first run out and let a runaway writer go unnoticed).
    now = datetime.now(timezone.utc)
    sched.add_job(db_quota_job, "interval", seconds=config.DB_QUOTA_POLL_SECONDS, max_instances=1, coalesce=True,
                  id="quota_db", next_run_time=now)
    sched.add_job(storage_quota_job, "interval", seconds=config.STORAGE_QUOTA_POLL_SECONDS, max_instances=1,
                  coalesce=True, id="quota_storage", next_run_time=now)
    # Same reason; a minute after start so a deploy's pod churn and leader hand-over settle first.
    sched.add_job(image_watch_job, "interval", minutes=config.IMAGE_WATCH_MINUTES, max_instances=1, coalesce=True,
                  id="image_watch", next_run_time=now + timedelta(minutes=1))
    # Health snapshot refresh runs on EVERY replica (each has its own in-memory cache), not just the leader.
    from app import health
    from app.dependencies import get_kube

    def _health_refresh() -> None:
        health.refresh(get_kube())

    sched.add_job(_health_refresh, "interval", seconds=health.CACHE_TTL_S, max_instances=1, coalesce=True,
                  id="health")
    sched.add_job(metrics_sample_job, "interval", seconds=config.METRICS_SAMPLE_SECONDS, max_instances=1,
                  coalesce=True, id="metrics_sample", next_run_time=now + timedelta(seconds=45))
    sched.add_job(audit_purge_job, "interval", hours=24, max_instances=1, coalesce=True, id="audit_purge",
                  next_run_time=now + timedelta(minutes=5))
    if config.BACKUPS_ENABLED:
        sched.add_job(backup_job, "interval", seconds=config.BACKUP_TICK_SECONDS, max_instances=1, coalesce=True,
                      id="backup", next_run_time=now + timedelta(seconds=30))
        sched.add_job(backup_sweep_job, "interval", minutes=config.BACKUP_SWEEP_MINUTES, max_instances=1, coalesce=True,
                      id="backup_sweep", next_run_time=now + timedelta(minutes=5))
    health.start_background(get_kube)
    sched.start()
    return sched
