"""Per-provider managed-database quirks as data (docs/PORTABILITY_PLAN.md, Design 2 `DbFlavor`).

`DbServer.flavor` selects one of these. `do-managed` holds exactly the values that used to be hard-coded for
DigitalOcean managed databases, so existing rows (default `do-managed`) behave identically. The other flavors are
reasonable but UNVERIFIED: nobody has run them against a real RDS / Cloud SQL / self-managed server yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_FLAVOR = "do-managed"


@dataclass(frozen=True)
class DbFlavor:
    name: str
    pg_admin_db: str             # database every PG admin statement runs from
    mysql_admin_db: str | None   # MySQL admin connections select no database (None) on every flavor today
    mysql_port: int
    pg_port: int
    pg_tls_mode: str             # libpq sslmode for PG admin connections when the server has no CA cert
    pg_tls_mode_with_ca: str     # libpq sslmode when a CA cert is stored on the server
    mysql_clear_strict_x509: bool  # clear ssl.VERIFY_X509_STRICT for MySQL TLS (DO's CA fails Python 3.13's strict check)

    def default_port(self, engine: str) -> int:
        return self.pg_port if engine == "pg" else self.mysql_port


FLAVORS: dict[str, DbFlavor] = {
    # Today's values, byte for byte: defaultdb, port 25060 for PG, sslmode require / verify-ca, strict-X509 cleared.
    "do-managed": DbFlavor("do-managed", pg_admin_db="defaultdb", mysql_admin_db=None, mysql_port=3306, pg_port=25060,
                           pg_tls_mode="require", pg_tls_mode_with_ca="verify-ca", mysql_clear_strict_x509=True),
    # UNVERIFIED: a plain server you run yourself; the stock `postgres` database, standard ports, strict X509 kept.
    "self-managed": DbFlavor("self-managed", pg_admin_db="postgres", mysql_admin_db=None, mysql_port=3306, pg_port=5432,
                             pg_tls_mode="require", pg_tls_mode_with_ca="verify-ca", mysql_clear_strict_x509=False),
    # UNVERIFIED: AWS RDS (CA = the RDS global bundle, stored as the server's ca_cert).
    "rds": DbFlavor("rds", pg_admin_db="postgres", mysql_admin_db=None, mysql_port=3306, pg_port=5432,
                    pg_tls_mode="require", pg_tls_mode_with_ca="verify-ca", mysql_clear_strict_x509=False),
    # UNVERIFIED: GCP Cloud SQL (CA = the per-instance server CA, stored as the server's ca_cert).
    "cloudsql": DbFlavor("cloudsql", pg_admin_db="postgres", mysql_admin_db=None, mysql_port=3306, pg_port=5432,
                         pg_tls_mode="require", pg_tls_mode_with_ca="verify-ca", mysql_clear_strict_x509=False),
}
FLAVOR_NAMES = tuple(FLAVORS)


def flavor_for(server: Any) -> DbFlavor:
    """The flavor of a DbServer-like object; a missing, empty or unknown value falls back to do-managed."""
    return FLAVORS.get(getattr(server, "flavor", None) or DEFAULT_FLAVOR, FLAVORS[DEFAULT_FLAVOR])
