"""Connection check for admin-registered database servers (create / admin-password rotation).

`DbServerChecker.check(...)` connects as the admin user and runs `SELECT 1`. On failure it raises
`DbServerCheckError` with a short, classified, non-leaky message (never the password or driver text).
TLS matches provisioning: MySQL verifies against config.MYSQL_SSL_CA when set (app/providers/mysql.py), PostgreSQL uses
verify-ca with the server's ca_cert, else sslmode=require (app/providers/postgres.py).
"""
from __future__ import annotations

import socket
import ssl
from typing import Optional

from app import config
from app.providers.db_flavor import DEFAULT_FLAVOR, FLAVORS, DbFlavor

CHECK_TIMEOUT_S = 5


class DbServerCheckError(Exception):
    """`str(exc)` is safe to show to the admin."""


class DbServerChecker:
    def check(self, *, engine: str, host: str, port: int, user: str, password: str, ca_cert: Optional[str],
              flavor: str = DEFAULT_FLAVOR) -> None:
        raise NotImplementedError


def _classify(exc: BaseException) -> str:
    text = str(exc).lower()
    if isinstance(exc, ssl.SSLError) or "ssl" in text or "tls" in text or "certificate" in text:
        return "TLS handshake failed (check the CA certificate and that the server requires TLS)"
    if "authentication failed" in text or "access denied" in text or "(1045" in text or "password" in text:
        return "authentication failed"
    if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in text or "timeout" in text or "(2003" in text:
        return "connection timed out" if "refused" not in text else "connection refused"
    if "refused" in text:
        return "connection refused"
    if isinstance(exc, socket.gaierror) or "name or service" in text or "translate host" in text or "getaddrinfo" in text \
            or "nodename" in text:
        return "host name could not be resolved"
    return f"connection failed ({type(exc).__name__})"


class RealDbServerChecker(DbServerChecker):
    def check(self, *, engine: str, host: str, port: int, user: str, password: str, ca_cert: Optional[str],
              flavor: str = DEFAULT_FLAVOR) -> None:
        fl = FLAVORS.get(flavor, FLAVORS[DEFAULT_FLAVOR])
        try:
            if engine == "pg":
                self._pg(host, port, user, password, ca_cert, fl)
            else:
                self._mysql(host, port, user, password, fl)
        except DbServerCheckError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified; driver text and the password are never echoed
            raise DbServerCheckError(f"Couldn't connect to {host}:{port} as {user}: {_classify(exc)}") from None

    @staticmethod
    def _mysql(host: str, port: int, user: str, password: str, fl: DbFlavor = FLAVORS[DEFAULT_FLAVOR]) -> None:
        import pymysql
        from app.db_tls import mysql_ssl_context

        kwargs = {"ssl": mysql_ssl_context(config.MYSQL_SSL_CA, fl.mysql_clear_strict_x509)} if config.MYSQL_SSL_CA else {}
        conn = pymysql.connect(host=host, port=port, user=user, password=password, connect_timeout=CHECK_TIMEOUT_S,
                               read_timeout=CHECK_TIMEOUT_S, write_timeout=CHECK_TIMEOUT_S, **kwargs)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        finally:
            conn.close()

    @staticmethod
    def _pg(host: str, port: int, user: str, password: str, ca_cert: Optional[str],
            fl: DbFlavor = FLAVORS[DEFAULT_FLAVOR]) -> None:
        import psycopg
        from app.providers.postgres import _ca_path

        ssl_kw = ({"sslmode": fl.pg_tls_mode_with_ca, "sslrootcert": _ca_path(ca_cert)} if ca_cert
                  else {"sslmode": fl.pg_tls_mode})
        with psycopg.connect(host=host, port=port, user=user, password=password, dbname=fl.pg_admin_db,
                             connect_timeout=CHECK_TIMEOUT_S, **ssl_kw) as conn:
            conn.execute("SELECT 1")
