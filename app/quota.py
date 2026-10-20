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


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def _bounce_app(app: models.App | None) -> None:
    """Roll the app's pod (same mechanism as the restart endpoint) so any database session we could not
    kill is closed when the old pod terminates. Caller commits."""
    if app is None or app.status in ("suspended", "pending"):
        return
    import uuid

    app.restart_nonce = uuid.uuid4().hex[:16]
    app.status = "pending"
    app.reset_retry()


def _poll_db(session: Session, row: models.AppDatabase, admin: MySQLAdmin) -> bool:
    """Returns True if the DB is over its limit.

    REVOKE alone does NOT stop an already-open session (MySQL applies database-level privilege changes only
    on the next USE), so the provider also kills the app user's sessions. While the DB stays over its cap we
    re-run the enforcement every poll: it is cheap, and it catches sessions that reconnected, or a kill that
    failed earlier. If the sessions could not be killed (no privilege, or invisible to the admin account),
    the app's pod is rolled once, on the flip, which closes its connections."""
    size = admin.db_size_bytes(row.app_id)
    over = size > DB_QUOTA_BYTES
    result: dict | None = None
    flipped = over != row.over_quota
    if flipped:
        result = admin.set_read_only(row.app_id, over)
        row.over_quota = over
        log.warning(
            "quota: app %s db write access %s (%s, limit %d MB)",
            row.app_id, "REVOKED" if over else "restored", _mb(size), DB_QUOTA_BYTES // 1024 // 1024,
        )
    elif over:
        result = admin.set_read_only(row.app_id, True)
    if over and result is not None:
        killed, failed = result.get("killed", 0), result.get("failed", 0)
        unverified = bool(result.get("list_error")) or (result.get("listed", 0) == 0)
        if flipped or killed or failed:
            log.warning(
                "quota: app %s db sessions: killed %d, failed %d%s",
                row.app_id, killed, failed, ", listing failed" if result.get("list_error") else "",
            )
        if flipped and (failed or unverified):
            log.warning("quota: app %s: could not confirm open db sessions were killed; rolling the app", row.app_id)
            _bounce_app(row.app)
    row.size_bytes = size
    session.commit()
    return over


def apply_storage_state(session: Session, row: models.AppStorage, store: ObjectStore, size: int) -> bool:
    """Record `size`, and flip the bucket key read-only / read-write if the over-cap state changed.
    Returns True if over. Does NOT revoke old keys (see `revoke_stale_keys`)."""
    over = size > STORAGE_QUOTA_BYTES
    new_creds = None
    if over != row.over_quota:
        log.warning(
            "quota: app %s storage write access %s (%s, limit %d MB)",
            row.app_id, "REVOKED" if over else "restored", _mb(size), STORAGE_QUOTA_BYTES // 1024 // 1024,
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
    return over


def revoke_stale_keys(row: models.AppStorage, store: ObjectStore) -> None:
    """Delete every key but the stored one, once the app is no longer rolling out.

    Ordering choice: a swapped key is NOT revoked in the same poll. The running pod still holds the old key
    until the reconciler has rolled it to the new one; deleting it immediately made reads AND writes fail with
    InvalidAccessKeyId for the whole rollout (seen live). Instead we wait until the app is out of "pending".
    Cost: when a bucket goes over cap, the old read-write key (and so writes) survives until the rollout
    finishes plus at most one storage poll (<= STORAGE_QUOTA_POLL_SECONDS); the overshoot is bounded and
    storage is cheap to overshoot, whereas breaking reads for a running app is a visible outage. Also runs
    on every poll, so a revoke that failed earlier is retried."""
    if row.app is not None and row.app.status == "pending":
        return
    try:
        store.revoke_other_keys(row.app_id, row.access_key)
    except Exception:
        log.warning("could not revoke old storage key for app %s", row.app_id)


def _poll_storage(session: Session, row: models.AppStorage, store: ObjectStore) -> bool:
    size = store.bucket_size_bytes(row.app_id)
    before = row.access_key
    over = apply_storage_state(session, row, store, size)
    if row.access_key == before:  # nothing was swapped now; clean up leftovers of an earlier swap
        revoke_stale_keys(row, store)
    return over


def poll_quotas(
    session: Session,
    *,
    mysql_admin_factory: Callable[[Any], MySQLAdmin],
    object_store: ObjectStore,
    databases: bool = True,
    storage: bool = True,
) -> None:
    dbs = buckets = over_n = 0
    if databases:
        for row in session.query(models.AppDatabase).all():
            app_id = row.app_id
            try:
                over_n += bool(_poll_db(session, row, mysql_admin_factory(row.server)))
                dbs += 1
            except Exception as exc:
                session.rollback()
                log.warning("quota: db check/enforcement failed for app %s: %s", app_id, _summary(exc))
    if storage:
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
