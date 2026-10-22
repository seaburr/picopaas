"""Picks the DbAdmin implementation for a DbServer by its `engine` ('mysql' | 'pg')."""
from __future__ import annotations

from typing import Any

from app.providers.base import DbAdmin


def db_admin_for(server: Any) -> DbAdmin:
    if getattr(server, "engine", "mysql") == "pg":
        from app.providers.postgres import PostgresAdmin

        return PostgresAdmin(server)
    from app.providers.mysql import MySQLAdminClient

    return MySQLAdminClient(server)
