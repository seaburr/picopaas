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
from types import SimpleNamespace
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select, text

from app import config, crypto, models
from app.db import SessionLocal, engine

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
    )


def _object_store() -> Any | None:
    try:
        from app.providers.spaces import SpacesStore
    except ImportError:
        log.warning("SpacesStore not available; storage reconcile/quota skipped")
        return None
    try:
        return SpacesStore.from_env()
    except KeyError as exc:
        log.warning("Spaces not configured (missing %s); storage reconcile/quota skipped", exc)
        return None


class _NoStorage:
    """Stand-in so apps without storage still reconcile when Spaces is unavailable."""

    def __getattr__(self, name: str):
        def _unavailable(*a: Any, **k: Any):
            raise RuntimeError("object store unavailable")

        return _unavailable


def _mysql_factory(server: models.DbServer):
    from app.providers.mysql import MySQLAdminClient

    return MySQLAdminClient(server)


def reconcile_job() -> None:
    if not try_acquire_leader():
        return
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
        )
    except Exception:
        log.exception("reconcile_all failed")
    finally:
        session.close()


def quota_job() -> None:
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
        poll_quotas(session, mysql_admin_factory=_mysql_factory, object_store=store)
    except Exception:
        log.exception("poll_quotas failed")
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
        if session.execute(select(models.DbServer.id).limit(1)).first() is not None:
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


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler()
    sched.add_job(reconcile_job, "interval", seconds=15, max_instances=1, coalesce=True, id="reconcile")
    sched.add_job(quota_job, "interval", minutes=5, max_instances=1, coalesce=True, id="quota")
    # Health snapshot refresh runs on EVERY replica (each has its own in-memory cache), not just the leader.
    from app import health
    from app.dependencies import get_kube

    def _health_refresh() -> None:
        health.refresh(get_kube())

    sched.add_job(_health_refresh, "interval", seconds=health.CACHE_TTL_S, max_instances=1, coalesce=True,
                  id="health")
    health.start_background(get_kube)
    sched.start()
    return sched
