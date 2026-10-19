"""Quota polling (ARCHITECTURE.md: caps live in app/config.py).

`poll_quotas` refreshes sizes and flips resources read-only when they cross
their cap (and back when they drop under it). A failure on one resource is
logged and skipped; the over_quota flag only changes after the provider call
succeeded, so a failed flip is retried on the next poll.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from app import crypto, models
from app.config import DB_QUOTA_BYTES, STORAGE_QUOTA_BYTES  # noqa: F401
from app.providers.base import MySQLAdmin, ObjectStore

log = logging.getLogger(__name__)

CAPACITY_WARN_PCT = 80


def _summary(exc: Exception) -> str:
    from app.reconciler import _provider_error_summary

    return _provider_error_summary(exc)


def _poll_db(session: Session, row: models.AppDatabase, admin: MySQLAdmin) -> bool:
    """Returns True if the DB is over its limit. Enforcement via REVOKE applies to new
    connections (open ones are killed best-effort by the provider)."""
    size = admin.db_size_bytes(row.app_id)
    over = size > DB_QUOTA_BYTES
    if over != row.over_quota:
        admin.set_read_only(row.app_id, over)
        row.over_quota = over
        log.info(
            "quota: app %s db write access %s (%.1f MB, limit %d MB)",
            row.app_id, "REVOKED" if over else "restored", size / 1024 / 1024, DB_QUOTA_BYTES // 1024 // 1024,
        )
    row.size_bytes = size
    session.commit()
    return over


def _poll_storage(session: Session, row: models.AppStorage, store: ObjectStore) -> bool:
    size = store.bucket_size_bytes(row.app_id)
    over = size > STORAGE_QUOTA_BYTES
    new_creds = None
    if over != row.over_quota:
        log.info(
            "quota: app %s storage write access %s (%.1f MB, limit %d MB)",
            row.app_id, "REVOKED" if over else "restored", size / 1024 / 1024, STORAGE_QUOTA_BYTES // 1024 // 1024,
        )
        new_creds = store.set_read_only(row.app_id, over)
        row.over_quota = over
        if new_creds:
            # Spaces grants are immutable, so the key was replaced: persist it and
            # have the reconciler re-render the Secret and roll the pod.
            row.access_key = new_creds["access_key"]
            row.secret_key_encrypted = crypto.encrypt(new_creds["secret_key"])
            if row.app is not None and row.app.status != "error":
                row.app.status = "pending"
    row.size_bytes = size
    try:
        session.commit()
    except Exception:
        session.rollback()
        if new_creds:  # stored (old) key keeps working; drop the unpersisted new one
            try:
                store.delete_key(row.app_id, new_creds["access_key"])
            except Exception:
                log.warning("could not discard unpersisted key for app %s", row.app_id)
        raise
    # Only after the new creds are committed: revoke old keys. Also run every poll so a
    # revoke that failed earlier (leaving a stale read-write key) is retried.
    try:
        store.revoke_other_keys(row.app_id, row.access_key)
    except Exception:
        log.warning("could not revoke old storage key for app %s", row.app_id)
    return over


def poll_quotas(
    session: Session,
    *,
    mysql_admin_factory: Callable[[Any], MySQLAdmin],
    object_store: ObjectStore,
) -> None:
    dbs = buckets = over_n = 0
    for row in session.query(models.AppDatabase).all():
        app_id = row.app_id
        try:
            over_n += bool(_poll_db(session, row, mysql_admin_factory(row.server)))
            dbs += 1
        except Exception as exc:
            session.rollback()
            log.warning("quota: db check/enforcement failed for app %s: %s", app_id, _summary(exc))
    for srow in session.query(models.AppStorage).all():
        app_id = srow.app_id
        try:
            over_n += bool(_poll_storage(session, srow, object_store))
            buckets += 1
        except Exception as exc:
            session.rollback()
            log.warning("quota: storage check/enforcement failed for app %s: %s", app_id, _summary(exc))
    log.info("quota poll: %d dbs, %d buckets checked, %d over limit", dbs, buckets, over_n)


def capacity_summary(session: Session) -> list[dict]:
    out = []
    for server in session.query(models.DbServer).order_by(models.DbServer.id).all():
        used = session.query(models.AppDatabase).filter_by(db_server_id=server.id).count()
        pct = round(100 * used / server.max_databases, 1) if server.max_databases else 100.0
        out.append(
            {
                "name": server.name,
                "status": server.status,
                "used": used,
                "max_databases": server.max_databases,
                "pct": pct,
                "warn": pct >= CAPACITY_WARN_PCT,
            }
        )
    return out
