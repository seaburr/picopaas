"""Picks which `DbServer` a new `AppDatabase` lands on (ARCHITECTURE.md "Scaling").

Pure SQLAlchemy — no provider/network calls — so it's unit-testable against
plain SQLite, same as the rest of the model layer.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AppDatabase, DbServer


class NoDbCapacity(Exception):
    """Raised when no 'active' DbServer has a free slot."""


def choose_server(session: Session) -> DbServer:
    """Return the 'active' DbServer with the most free slots
    (`max_databases` minus its current `AppDatabase` count), tie-broken by
    the lowest id. Raises `NoDbCapacity` if no active server qualifies."""
    counts = dict(
        session.execute(
            select(AppDatabase.db_server_id, func.count(AppDatabase.id)).group_by(
                AppDatabase.db_server_id
            )
        ).all()
    )

    active_servers = (
        session.execute(select(DbServer).where(DbServer.status == "active")).scalars().all()
    )

    best: DbServer | None = None
    best_free = -1
    for server in active_servers:
        free = server.max_databases - counts.get(server.id, 0)
        if free <= 0:
            continue
        if free > best_free or (free == best_free and (best is None or server.id < best.id)):
            best = server
            best_free = free

    if best is None:
        raise NoDbCapacity("no active DbServer has a free slot")
    return best
