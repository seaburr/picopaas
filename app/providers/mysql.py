"""Real `MySQLAdmin` implementation (ARCHITECTURE.md §2 "DB" / "Scaling").

Constructed per `DbServer` (host/port/admin creds come from the model row,
chosen via `app/db_placement.py::choose_server` — never passed per call, see
`app/providers/base.py::MySQLAdmin`). Manages one database + user per app:
`CREATE DATABASE app_<id>`, `CREATE USER 'app_<id>'@'%'` with a random
32-char password and `MAX_USER_CONNECTIONS` (config.MAX_DB_USER_CONNECTIONS), `GRANT ALL` scoped to that
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

import logging
import re
import secrets
import string
from typing import Any

import pymysql
import pymysql.cursors

from app import config
from app.crypto import decrypt
from app.db_tls import mysql_ssl_context
from app.providers.base import MySQLAdmin, MySQLAdminError

log = logging.getLogger(__name__)

# We only ever generate `app_<int>` ourselves (see _db_name/_db_user below),
# but validate before interpolating into SQL anyway since MySQL identifiers
# can't be bound as query parameters.
_IDENT_RE = re.compile(r"^app_[0-9]+$")

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
        self._use_procs = config.MYSQL_ADMIN_USE_PROCEDURES
        self._ssl_ca = config.MYSQL_SSL_CA

    def _connect(self) -> pymysql.connections.Connection:
        kwargs: dict[str, Any] = {}
        if self._ssl_ca:
            # Managed MySQL requires TLS: verify the server cert against the CA and hostname.
            kwargs.update(ssl=mysql_ssl_context(self._ssl_ca))
        try:
            return pymysql.connect(
                **kwargs,
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
                    if self._use_procs:
                        cur.execute("CALL picopaas_ops.provision_app(%s, %s)", (app_id, password))
                        return self._creds(app_id, password)
                    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}`")
                    # Idempotent: a user left over from a failed previous
                    # attempt shouldn't block retrying with a fresh password.
                    cur.execute(f"DROP USER IF EXISTS '{db_user}'@'%'")
                    cur.execute(
                        f"CREATE USER '{db_user}'@'%%' IDENTIFIED BY %s "
                        f"WITH MAX_USER_CONNECTIONS {int(config.MAX_DB_USER_CONNECTIONS)}",
                        (password,),
                    )
                    cur.execute(f"GRANT ALL ON `{db_name}`.* TO '{db_user}'@'%'")
                    cur.execute("FLUSH PRIVILEGES")
            finally:
                conn.close()
        except pymysql.MySQLError as exc:
            raise MySQLAdminError(f"create_db_user({app_id}) failed: {exc}") from exc

        return self._creds(app_id, password)

    def set_user_connection_limit(self, app_id: int) -> None:
        """Idempotent ALTER USER ... WITH MAX_USER_CONNECTIONS <config value> so existing app users pick up a raised
        cap. No-op in procedure mode (the proc set the limit at provision time)."""
        db_user = _validate_identifier(_db_user(app_id))
        if self._use_procs:
            return
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"ALTER USER '{db_user}'@'%' WITH MAX_USER_CONNECTIONS {int(config.MAX_DB_USER_CONNECTIONS)}"
                    )
            finally:
                conn.close()
        except pymysql.MySQLError as exc:
            raise MySQLAdminError(f"set_user_connection_limit({app_id}) failed: {exc}") from exc

    def _creds(self, app_id: int, password: str) -> dict[str, Any]:
        db_name = _db_name(app_id)
        db_user = _db_user(app_id)
        return {
            "host": self._host,
            "port": self._port,
            "name": db_name,
            "user": db_user,
            "password": password,
            "url": f"mysql://{db_user}:{password}@{self._host}:{self._port}/{db_name}",
        }

    def drop(self, app_id: int) -> None:
        db_name = _validate_identifier(_db_name(app_id))
        db_user = _validate_identifier(_db_user(app_id))
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    if self._use_procs:
                        cur.execute("CALL picopaas_ops.drop_app(%s)", (app_id,))
                        return
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
                    if self._use_procs:
                        cur.execute("CALL picopaas_ops.app_size(%s)", (app_id,))
                        (total,) = cur.fetchone()
                        return int(total)
                    # MySQL 8 caches information_schema.TABLES sizes for 24h
                    # (information_schema_stats_expiry=86400); force fresh stats.
                    cur.execute("SET SESSION information_schema_stats_expiry = 0")
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

    def server_health(self) -> dict[str, Any]:
        """Server-wide, SQL-derived health over the admin connection (3 cheap queries). Never includes
        credentials; failures raise MySQLAdminError carrying only the MySQL error code."""
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SHOW GLOBAL STATUS WHERE Variable_name IN "
                        "('Threads_connected','Threads_running','Max_used_connections','Uptime')"
                    )
                    status = {str(k).lower(): v for k, v in cur.fetchall()}
                    cur.execute("SHOW VARIABLES LIKE 'max_connections'")
                    row = cur.fetchone()
                    max_conn = int(row[1]) if row else None
                    # information_schema sizes are cached 24h on MySQL 8; force fresh stats.
                    cur.execute("SET SESSION information_schema_stats_expiry = 0")
                    cur.execute(
                        "SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables"
                    )
                    (total,) = cur.fetchone()
            finally:
                conn.close()
        except pymysql.MySQLError as exc:
            code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
            raise MySQLAdminError(f"server_health failed (mysql error {code})" if code else "server_health failed") from exc

        def _i(key: str) -> int | None:
            return int(status[key]) if key in status else None

        return {
            "threads_connected": _i("threads_connected"),
            "threads_running": _i("threads_running"),
            "max_used_connections": _i("max_used_connections"),
            "max_connections": max_conn,
            "data_size_bytes": int(total),
            "uptime_seconds": _i("uptime"),
        }

    def set_read_only(
        self, app_id: int, read_only: bool, app_password: str | None = None
    ) -> dict[str, Any] | None:
        """Revoke/restore write privileges. When revoking, also kill the app user's open sessions and return
        {"listed", "killed", "failed", "list_error"} (None when restoring).

        The kill matters: MySQL applies database-level privilege changes to an open session only on its next
        `USE`, so a long-lived connection (a pool, or one big loader) keeps writing after the REVOKE. Prod
        showed one connection writing 300MB under a 50MB cap.

        Kill order: (1) as the app's own user (`app_password`): MySQL always lets a user kill its own threads,
        and managed `doadmin` gets 1095 "not owner of thread" for them; (2) the admin/procedure path. The result
        has a "path" key naming which one produced it. If neither verifies, the caller rolls the app pod."""
        db_name = _validate_identifier(_db_name(app_id))
        db_user = _validate_identifier(_db_user(app_id))
        verb, preposition = ("REVOKE", "FROM") if read_only else ("GRANT", "TO")
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    if self._use_procs:
                        cur.execute("CALL picopaas_ops.set_app_read_only(%s, %s)", (app_id, int(read_only)))
                        if not read_only:
                            return None
                        own = self._kill_as_app_user(app_id, db_user, app_password)
                        if own is not None:
                            return own
                        return self._kill_via_proc(cur, app_id)
                    cur.execute(
                        f"{verb} {_WRITE_PRIVILEGES} ON `{db_name}`.* {preposition} '{db_user}'@'%'"
                    )
                    # No FLUSH PRIVILEGES: GRANT/REVOKE take effect without it, and it needs RELOAD,
                    # which managed MySQL admins may lack.
                    if not read_only:
                        return None
                    own = self._kill_as_app_user(app_id, db_user, app_password)
                    if own is not None:
                        return own
                    return self._kill_connections(cur, db_user)
            finally:
                conn.close()
        except pymysql.MySQLError as exc:
            raise MySQLAdminError(f"set_read_only({app_id}, {read_only}) failed: {exc}") from exc

    def _kill_as_app_user(self, app_id: int, db_user: str, password: str | None) -> dict[str, Any] | None:
        """Connect AS the app user (CONNECT survives the REVOKE) and kill all of its other threads. Without
        PROCESS, SHOW PROCESSLIST lists only the user's own threads, and a user may always KILL its own.
        Returns None when this path can't be used or didn't fully work (no password, connection refused, e.g.
        MAX_USER_CONNECTIONS reached, list error, any failed kill) so the caller falls back. The password is
        never logged."""
        if not password:
            return None
        kwargs: dict[str, Any] = {}
        if self._ssl_ca:
            kwargs.update(ssl=mysql_ssl_context(self._ssl_ca))
        try:
            conn = pymysql.connect(
                **kwargs,
                host=self._host,
                port=self._port,
                user=db_user,
                password=password,
                autocommit=True,
                cursorclass=pymysql.cursors.Cursor,
                connect_timeout=10,
            )
        except pymysql.MySQLError as exc:
            log.warning("app-user session kill for app %s: could not connect as %s: %s", app_id, db_user, exc)
            return None
        result = {"listed": 0, "killed": 0, "failed": 0, "list_error": False, "path": "app-user"}
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT CONNECTION_ID()")
                own_id = int(cur.fetchone()[0])
                cur.execute("SHOW PROCESSLIST")
                ids = [int(row[0]) for row in cur.fetchall() if int(row[0]) != own_id]
                result["listed"] = len(ids)
                for cid in ids:
                    try:
                        cur.execute("KILL %s", (cid,))
                        result["killed"] += 1
                    except pymysql.err.OperationalError as exc:
                        if exc.args and exc.args[0] == 1094:  # already gone
                            result["killed"] += 1
                            continue
                        result["failed"] += 1
                        log.warning("could not kill connection %s of %s as itself: %s", cid, db_user, exc)
                    except pymysql.MySQLError as exc:
                        result["failed"] += 1
                        log.warning("could not kill connection %s of %s as itself: %s", cid, db_user, exc)
        except pymysql.MySQLError as exc:
            log.warning("app-user session kill for app %s failed: %s", app_id, exc)
            return None
        finally:
            try:
                conn.close()
            except pymysql.MySQLError:
                pass
        if result["failed"]:
            return None
        return result

    @staticmethod
    def _kill_via_proc(cur: Any, app_id: int) -> dict[str, Any]:
        """In-cluster MySQL: the admin has no KILL right, a root-owned procedure does it."""
        result = {"listed": 0, "killed": 0, "failed": 0, "list_error": False, "path": "admin-proc"}
        try:
            cur.execute("CALL picopaas_ops.kill_app_sessions(%s)", (app_id,))
            row = cur.fetchone()
            result["killed"] = result["listed"] = int(row[0]) if row else 0
        except pymysql.MySQLError as exc:
            # e.g. 1305 PROCEDURE does not exist: the data volume predates the procedure.
            log.warning("kill_app_sessions failed for app %s: %s", app_id, exc)
            result["list_error"] = True
        return result

    @staticmethod
    def _kill_connections(cur: Any, db_user: str) -> dict[str, Any]:
        """Kill the app user's open sessions (needs PROCESS to see them and CONNECTION_ADMIN/SUPER to kill
        another user's thread). Never raises; the caller gets counts and decides on a fallback.

        Managed-MySQL caveat (not verifiable from here): without PROCESS, information_schema.PROCESSLIST
        silently shows only the admin's OWN threads, so `listed` is 0 even if the app is connected. The caller
        treats listed == 0 as "unverified" and falls back to rolling the app's pod."""
        result = {"listed": 0, "killed": 0, "failed": 0, "list_error": False, "path": "admin"}
        try:
            cur.execute("SELECT id FROM information_schema.PROCESSLIST WHERE user = %s", (db_user,))
            ids = [row[0] for row in cur.fetchall()]
        except pymysql.MySQLError as exc:
            log.warning("could not list connections of %s: %s", db_user, exc)
            result["list_error"] = True
            return result
        result["listed"] = len(ids)
        for cid in ids:
            try:
                cur.execute("KILL %s", (int(cid),))
                result["killed"] += 1
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] == 1094:  # unknown thread id: it already ended
                    result["killed"] += 1
                    continue
                result["failed"] += 1
                log.warning("could not kill connection %s of %s: %s", cid, db_user, exc)
            except pymysql.MySQLError as exc:
                result["failed"] += 1
                log.warning("could not kill connection %s of %s: %s", cid, db_user, exc)
        return result

    def set_password(self, app_id: int, password: str) -> dict[str, Any]:
        db_name = _validate_identifier(_db_name(app_id))
        db_user = _validate_identifier(_db_user(app_id))
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    if self._use_procs:
                        cur.execute("CALL picopaas_ops.set_app_password(%s, %s)", (app_id, password))
                        return self._creds(app_id, password)
                    cur.execute(f"ALTER USER '{db_user}'@'%%' IDENTIFIED BY %s", (password,))
                    cur.execute("FLUSH PRIVILEGES")
            finally:
                conn.close()
        except pymysql.MySQLError as exc:
            raise MySQLAdminError(f"set_password({app_id}) failed: {exc}") from exc
        return self._creds(app_id, password)
