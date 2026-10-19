"""Quota polling (ARCHITECTURE.md: DB 100MB, bucket 250MB).

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
from app.providers.base import MySQLAdmin, ObjectStore

log = logging.getLogger(__name__)

DB_QUOTA_BYTES = 100 * 1024 * 1024
STORAGE_QUOTA_BYTES = 250 * 1024 * 1024
CAPACITY_WARN_PCT = 80


def _poll_db(session: Session, row: models.AppDatabase, admin: MySQLAdmin) -> None:
    size = admin.db_size_bytes(row.app_id)
    over = size > DB_QUOTA_BYTES
    if over != row.over_quota:
        admin.set_read_only(row.app_id, over)
        row.over_quota = over
    row.size_bytes = size
    session.commit()


def _poll_storage(session: Session, row: models.AppStorage, store: ObjectStore) -> None:
    size = store.bucket_size_bytes(row.app_id)
    over = size > STORAGE_QUOTA_BYTES
    if over != row.over_quota:
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
    session.commit()


def poll_quotas(
    session: Session,
    *,
    mysql_admin_factory: Callable[[Any], MySQLAdmin],
    object_store: ObjectStore,
) -> None:
    for row in session.query(models.AppDatabase).all():
        try:
            _poll_db(session, row, mysql_admin_factory(row.server))
        except Exception:
            session.rollback()
            log.exception("db quota poll failed for app %s", row.app_id)
    for srow in session.query(models.AppStorage).all():
        try:
            _poll_storage(session, srow, object_store)
        except Exception:
            session.rollback()
            log.exception("storage quota poll failed for app %s", srow.app_id)


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
