"""Real `DbAdmin` for managed PostgreSQL (DigitalOcean) -- docs/requests/0006-postgresql-support.md.

Constructed per `DbServer` with engine == 'pg'. One database + one LOGIN role per app, both named `app_<id>`.

Facts confirmed by a live `doadmin` spike (design rules this module follows):
- `doadmin` is NOT a superuser but has CREATEROLE/CREATEDB, and gets ADMIN OPTION on roles it creates, so no extra
  GRANTs are needed to manage the app roles.
- EVERY admin statement is run while connected to `defaultdb`. Connecting to an app database that is in
  default_transaction_read_only mode makes doadmin's own session read-only and breaks ALTER/GRANT/REVOKE. The only
  exception is the one-time `ALTER SCHEMA public OWNER TO app_<id>` at creation (the DB is not read-only yet).
- PG17: the `public` schema is owned by pg_database_owner, so the app role cannot CREATE TABLE until the schema is
  handed over to it.
- `REVOKE CONNECT, TEMP ON DATABASE defaultdb FROM PUBLIC` keeps app roles out of defaultdb (run on every create;
  idempotent).
- Port is 25060, TLS only. Per-role CONNECTION LIMIT is ~5 (config.PG_MAX_DB_USER_CONNECTIONS): the 1 GB node has
  ~22 usable connections in total. (A temp_file_limit idea was dropped: not settable by a non-superuser.)

Quota enforcement is two-stage (Postgres has no per-DB size cap):
- stage 1 (`set_read_only(..., True)`): `ALTER DATABASE .. SET default_transaction_read_only = on` + terminate the
  role's sessions. A speed-bump only: a tenant can `SET default_transaction_read_only = off` in a session and keep
  writing. Accepted and documented.
- stage 2 (`hard=True`, used by app/quota.py once size > DB_QUOTA_BYTES * PG_HARD_CAP_RATIO): additionally
  `REVOKE CONNECT` from the role + terminate. A real lock-out; it is cleared by `set_read_only(.., False)` (GRANT CONNECT)
  and, since the tenant can no longer shrink the DB, normally needs admin intervention.

Identifiers are only ever built with psycopg.sql.Identifier (never string-formatted); `app_<int>` is also validated.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from typing import Any, Optional

import psycopg
from psycopg import sql

from app import config
from app.crypto import decrypt
from app.env_refs import db_url
from app.providers.base import DbAdmin, DbAdminError, generate_password

log = logging.getLogger(__name__)

ADMIN_DB = "defaultdb"
_IDENT_RE = re.compile(r"^app_[0-9]+$")
_ca_files: dict[str, str] = {}


def _ident(app_id: int) -> str:
    name = f"app_{int(app_id)}"
    if not _IDENT_RE.fullmatch(name):  # unreachable; defensive
        raise DbAdminError(f"refusing to use invalid generated identifier: {name!r}")
    return name


def _ca_path(pem: str) -> str:
    """psycopg/libpq needs the CA as a file: write the PEM once per distinct cert to a private temp file."""
    key = hashlib.sha256(pem.encode()).hexdigest()
    path = _ca_files.get(key)
    if path is None or not os.path.exists(path):
        fd, path = tempfile.mkstemp(prefix="pp-pg-ca-", suffix=".pem")
        with os.fdopen(fd, "w") as f:
            f.write(pem)
        _ca_files[key] = path
    return path


class PostgresAdmin(DbAdmin):
    def __init__(self, server: Any) -> None:
        self._server = server
        self._host = server.host
        self._port = server.port
        self._admin_user = server.admin_user
        self._admin_password = decrypt(server.admin_password_encrypted)
        self._ca_cert = getattr(server, "ca_cert", None)

    # -- connection -------------------------------------------------------------------------------------------
    def _connect(self, dbname: str = ADMIN_DB) -> psycopg.Connection:
        kwargs: dict[str, Any] = {}
        if self._ca_cert:
            kwargs.update(sslmode="verify-ca", sslrootcert=_ca_path(self._ca_cert))
        else:
            kwargs.update(sslmode="require")
        try:
            return psycopg.connect(
                host=self._host, port=self._port, user=self._admin_user, password=self._admin_password,
                dbname=dbname, autocommit=True, connect_timeout=10, **kwargs,
            )
        except psycopg.Error as exc:
            raise DbAdminError(f"could not connect to {self._host}:{self._port}: {type(exc).__name__}") from exc

    def _creds(self, app_id: int, password: str) -> dict[str, Any]:
        name = _ident(app_id)
        return {
            "host": self._host, "port": self._port, "name": name, "user": name, "password": password,
            "url": db_url("pg", name, password, self._host, self._port, name, tls=True),
        }

    # -- lifecycle --------------------------------------------------------------------------------------------
    def init_server(self) -> None:
        """One-time (idempotent) per-server hardening: keep app roles out of defaultdb."""
        try:
            with self._connect() as conn:
                conn.execute(sql.SQL("REVOKE CONNECT, TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(ADMIN_DB)))
        except psycopg.Error as exc:
            raise DbAdminError(f"init_server failed: {type(exc).__name__}") from exc

    def create_db_user(self, app_id: int) -> dict[str, Any]:
        name = _ident(app_id)
        ident = sql.Identifier(name)
        password = generate_password()
        pw = sql.Literal(password)
        limit = sql.Literal(int(config.PG_MAX_DB_USER_CONNECTIONS))
        try:
            with self._connect() as conn:
                try:
                    self._exec_init(conn)
                except psycopg.Error:
                    log.warning("pg: defaultdb PUBLIC revoke failed (continuing)")
                # Idempotent against a half-finished earlier attempt.
                if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone():
                    conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {} CONNECTION LIMIT {}").format(ident, pw, limit))
                else:
                    conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} CONNECTION LIMIT {}").format(ident, pw, limit))
                if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                    conn.execute(sql.SQL("CREATE DATABASE {}").format(ident))  # owner stays doadmin
                conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(ident))
                conn.execute(sql.SQL("GRANT CONNECT, TEMP ON DATABASE {} TO {}").format(ident, ident))
                for param, value in (
                    ("statement_timeout", sql.Literal(f"{int(config.PG_STATEMENT_TIMEOUT_MS)}ms")),
                    ("idle_in_transaction_session_timeout", sql.Literal(f"{int(config.PG_IDLE_TX_TIMEOUT_MS)}ms")),
                    ("search_path", sql.Literal("public")),
                ):
                    conn.execute(sql.SQL("ALTER ROLE {} SET {} = {}").format(ident, sql.Identifier(param), value))
            # PG17: hand the public schema to the app role (one of the few statements run inside the app DB).
            with self._connect(name) as conn:
                conn.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(ident))
        except psycopg.Error as exc:
            raise DbAdminError(f"create_db_user({app_id}) failed: {type(exc).__name__}: {exc.diag.message_primary or ''}") from exc
        return self._creds(app_id, password)

    @staticmethod
    def _exec_init(conn: psycopg.Connection) -> None:
        conn.execute(sql.SQL("REVOKE CONNECT, TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(ADMIN_DB)))

    def drop(self, app_id: int) -> None:
        ident = sql.Identifier(_ident(app_id))
        try:
            with self._connect() as conn:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(ident))
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(ident))
        except psycopg.Error as exc:
            raise DbAdminError(f"drop({app_id}) failed: {type(exc).__name__}") from exc

    def set_user_connection_limit(self, app_id: int, limit: Optional[int] = None) -> None:
        ident = sql.Identifier(_ident(app_id))
        try:
            with self._connect() as conn:
                conn.execute(sql.SQL("ALTER ROLE {} CONNECTION LIMIT {}").format(
                    ident, sql.Literal(int(limit or config.PG_MAX_DB_USER_CONNECTIONS))))
        except psycopg.Error as exc:
            raise DbAdminError(f"set_user_connection_limit({app_id}) failed: {type(exc).__name__}") from exc

    def set_password(self, app_id: int, password: str) -> dict[str, Any]:
        ident = sql.Identifier(_ident(app_id))
        try:
            with self._connect() as conn:
                conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(ident, sql.Literal(password)))
        except psycopg.Error as exc:
            raise DbAdminError(f"set_password({app_id}) failed: {type(exc).__name__}") from exc
        return self._creds(app_id, password)

    # -- quota ------------------------------------------------------------------------------------------------
    def db_size_bytes(self, app_id: int) -> int:
        name = _ident(app_id)
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT pg_database_size(%s)", (name,)).fetchone()
        except psycopg.Error as exc:
            raise DbAdminError(f"db_size_bytes({app_id}) failed: {type(exc).__name__}") from exc
        return int(row[0]) if row and row[0] is not None else 0

    def set_read_only(
        self, app_id: int, read_only: bool, app_password: Optional[str] = None, hard: bool = False
    ) -> Optional[dict[str, Any]]:
        """Stage 1 (read_only): default_transaction_read_only=on + kill sessions. `hard=True` (stage 2) also
        REVOKEs CONNECT. Restore: read_only=off and GRANT CONNECT (idempotent). All statements run from defaultdb.
        `app_password` is unused (the admin can always terminate the role's sessions)."""
        name = _ident(app_id)
        ident = sql.Identifier(name)
        try:
            with self._connect() as conn:
                if not read_only:
                    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(ident, ident))
                    conn.execute(sql.SQL("ALTER DATABASE {} SET default_transaction_read_only = off").format(ident))
                    return None
                conn.execute(sql.SQL("ALTER DATABASE {} SET default_transaction_read_only = on").format(ident))
                if hard:
                    conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(ident, ident))
                else:  # stage 1: undo a previous stage 2 (e.g. the quota was raised but the DB is still over it)
                    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(ident, ident))
                return self._terminate(conn, name)
        except psycopg.Error as exc:
            raise DbAdminError(f"set_read_only({app_id}) failed: {type(exc).__name__}") from exc

    @staticmethod
    def _terminate(conn: psycopg.Connection, role: str) -> dict[str, Any]:
        try:
            rows = conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s AND pid <> pg_backend_pid()",
                (role,),
            ).fetchall()
        except psycopg.Error as exc:
            log.warning("pg: terminating sessions of %s failed: %s", role, type(exc).__name__)
            return {"listed": 0, "killed": 0, "failed": 0, "list_error": True, "path": "pg"}
        killed = sum(1 for r in rows if r[0])
        return {"listed": len(rows), "killed": killed, "failed": len(rows) - killed, "list_error": False, "path": "pg"}

    # -- admin health -----------------------------------------------------------------------------------------
    def server_health(self) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                connected, running = conn.execute(
                    "SELECT count(*), count(*) FILTER (WHERE state = 'active') FROM pg_stat_activity "
                    "WHERE backend_type = 'client backend'").fetchone()
                max_conn = int(conn.execute("SHOW max_connections").fetchone()[0])
                size = conn.execute(
                    "SELECT coalesce(sum(pg_database_size(datname)), 0) FROM pg_database WHERE datname LIKE 'app\\_%'"
                ).fetchone()[0]
                uptime = conn.execute("SELECT extract(epoch FROM now() - pg_postmaster_start_time())").fetchone()[0]
        except psycopg.Error as exc:
            raise DbAdminError(f"server_health failed: {type(exc).__name__}") from exc
        return {"threads_connected": int(connected), "threads_running": int(running),
                "max_used_connections": int(connected), "max_connections": max_conn,
                "data_size_bytes": int(size), "uptime_seconds": int(uptime)}
