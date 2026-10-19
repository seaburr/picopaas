"""Real `MySQLAdmin` implementation (ARCHITECTURE.md §2 "DB" / "Scaling").

Constructed per `DbServer` (host/port/admin creds come from the model row,
chosen via `app/db_placement.py::choose_server` — never passed per call, see
`app/providers/base.py::MySQLAdmin`). Manages one database + user per app:
`CREATE DATABASE app_<id>`, `CREATE USER 'app_<id>'@'%'` with a random
32-char password and `MAX_USER_CONNECTIONS 5`, `GRANT ALL` scoped to that
one database. `set_read_only` revokes/restores INSERT/UPDATE/CREATE/ALTER
only — SELECT/DELETE/DROP always stay granted so a user can read and clean
up their own data even once their quota's enforced as read-only.

Identifiers (`app_<id>`) are always of our own generation, never taken from
user input, but MySQL has no parameterized-identifier support, so they're
still validated against a strict regex before being interpolated into SQL.
Passwords go through query parameters (PyMySQL escapes client-side) wherever
MySQL accepts a literal there (e.g. `IDENTIFIED BY %s`); use
`pymysql.escape_string` for creating an inline string literal if a code path
ever needs one instead.
"""
from __future__ import annotations

import re
import secrets
import string
from typing import Any

import pymysql
import pymysql.cursors

from app.crypto import decrypt
from app.providers.base import MySQLAdmin, MySQLAdminError

# We only ever generate `app_<int>` ourselves (see _db_name/_db_user below),
# but validate before interpolating into SQL anyway since MySQL identifiers
# can't be bound as query parameters.
_IDENT_RE = re.compile(r"^app_[0-9]+$")

_MAX_USER_CONNECTIONS = 5
_PASSWORD_LENGTH = 32
_PASSWORD_ALPHABET = string.ascii_letters + string.digits

# Revoked/restored by set_read_only(). SELECT, DELETE, and DROP are never
# touched so a quota-exceeding app can still read and delete its own data.
_WRITE_PRIVILEGES = "INSERT, UPDATE, CREATE, ALTER"


def _db_name(app_id: int) -> str:
    return f"app_{app_id}"


def _db_user(app_id: int) -> str:
    return f"app_{app_id}"


def _validate_identifier(identifier: str) -> str:
    if not _IDENT_RE.fullmatch(identifier):
        # Should be unreachable (app_id is always an int we generated the
        # string from) — defensive check against SQL injection via identifiers.
        raise MySQLAdminError(f"refusing to use invalid generated identifier: {identifier!r}")
    return identifier


def _generate_password(length: int = _PASSWORD_LENGTH) -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


class MySQLAdminClient(MySQLAdmin):
    def __init__(self, server: Any) -> None:
        """`server` is a `DbServer` row (or anything with the same
        host/port/admin_user/admin_password_encrypted attributes)."""
        self._server = server
        self._host = server.host
        self._port = server.port
        self._admin_user = server.admin_user
        self._admin_password = decrypt(server.admin_password_encrypted)

    def _connect(self) -> pymysql.connections.Connection:
        try:
            return pymysql.connect(
                host=self._host,
                port=self._port,
                user=self._admin_user,
                password=self._admin_password,
                autocommit=True,
                cursorclass=pymysql.cursors.Cursor,
                connect_timeout=10,
            )
        except pymysql.MySQLError as exc:
            raise MySQLAdminError(f"could not connect to {self._host}:{self._port}: {exc}") from exc

    def create_db_user(self, app_id: int) -> dict[str, Any]:
        db_name = _validate_identifier(_db_name(app_id))
        db_user = _validate_identifier(_db_user(app_id))
        password = _generate_password()

        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}`")
                    # Idempotent: a user left over from a failed previous
                    # attempt shouldn't block retrying with a fresh password.
                    cur.execute(f"DROP USER IF EXISTS '{db_user}'@'%'")
                    cur.execute(
                        f"CREATE USER '{db_user}'@'%%' IDENTIFIED BY %s "
                        f"WITH MAX_USER_CONNECTIONS {_MAX_USER_CONNECTIONS}",
                        (password,),
                    )
                    cur.execute(f"GRANT ALL ON `{db_name}`.* TO '{db_user}'@'%'")
                    cur.execute("FLUSH PRIVILEGES")
            finally:
                conn.close()
        except pymysql.MySQLError as exc:
            raise MySQLAdminError(f"create_db_user({app_id}) failed: {exc}") from exc

        creds = {
            "host": self._host,
            "port": self._port,
            "name": db_name,
            "user": db_user,
            "password": password,
        }
        creds["url"] = f"mysql://{db_user}:{password}@{self._host}:{self._port}/{db_name}"
        return creds

    def drop(self, app_id: int) -> None:
        db_name = _validate_identifier(_db_name(app_id))
        db_user = _validate_identifier(_db_user(app_id))
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
                    cur.execute(f"DROP USER IF EXISTS '{db_user}'@'%'")
                    cur.execute("FLUSH PRIVILEGES")
            finally:
                conn.close()
        except pymysql.MySQLError as exc:
            raise MySQLAdminError(f"drop({app_id}) failed: {exc}") from exc

    def db_size_bytes(self, app_id: int) -> int:
        db_name = _validate_identifier(_db_name(app_id))
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COALESCE(SUM(data_length + index_length), 0) "
                        "FROM information_schema.TABLES WHERE table_schema = %s",
                        (db_name,),
                    )
                    (total,) = cur.fetchone()
                    return int(total)
            finally:
                conn.close()
        except pymysql.MySQLError as exc:
            raise MySQLAdminError(f"db_size_bytes({app_id}) failed: {exc}") from exc

    def set_read_only(self, app_id: int, read_only: bool) -> None:
        db_name = _validate_identifier(_db_name(app_id))
        db_user = _validate_identifier(_db_user(app_id))
        verb, preposition = ("REVOKE", "FROM") if read_only else ("GRANT", "TO")
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"{verb} {_WRITE_PRIVILEGES} ON `{db_name}`.* {preposition} '{db_user}'@'%'"
                    )
                    cur.execute("FLUSH PRIVILEGES")
            finally:
                conn.close()
        except pymysql.MySQLError as exc:
            raise MySQLAdminError(f"set_read_only({app_id}, {read_only}) failed: {exc}") from exc

    def set_password(self, app_id: int, password: str) -> dict[str, Any]:
        db_name = _validate_identifier(_db_name(app_id))
        db_user = _validate_identifier(_db_user(app_id))
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"ALTER USER '{db_user}'@'%%' IDENTIFIED BY %s", (password,))
                    cur.execute("FLUSH PRIVILEGES")
            finally:
                conn.close()
        except pymysql.MySQLError as exc:
            raise MySQLAdminError(f"set_password({app_id}) failed: {exc}") from exc
        creds = {
            "host": self._host,
            "port": self._port,
            "name": db_name,
            "user": db_user,
            "password": password,
        }
        creds["url"] = f"mysql://{db_user}:{password}@{self._host}:{self._port}/{db_name}"
        return creds
