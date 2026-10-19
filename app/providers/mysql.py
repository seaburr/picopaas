"""Real `MySQLAdmin` — NOT YET IMPLEMENTED.

TODO (docs/LEDGER.md "Now / Next"): needs a PyMySQL connection to the
in-cluster MySQL 8 StatefulSet (ARCHITECTURE.md §2 "DB") with platform-admin
creds, `CREATE DATABASE`/`CREATE USER`/`GRANT ALL ON app_<id>.* ... WITH
max_user_connections=10`, and an `information_schema.TABLES` size poll for
`db_size_bytes`. Use `app.providers.fake.FakeMySQLAdmin` in tests until then.
"""
from __future__ import annotations

from typing import Any

from app.providers.base import MySQLAdmin


class MySQLAdminClient(MySQLAdmin):
    def create_db_user(self, app_id: int) -> dict[str, Any]:
        raise NotImplementedError("TODO: MySQL admin connection not wired yet")

    def drop(self, app_id: int) -> None:
        raise NotImplementedError("TODO: MySQL admin connection not wired yet")

    def db_size_bytes(self, app_id: int) -> int:
        raise NotImplementedError("TODO: MySQL admin connection not wired yet")

    def set_read_only(self, app_id: int, read_only: bool) -> None:
        raise NotImplementedError("TODO: MySQL admin connection not wired yet")
