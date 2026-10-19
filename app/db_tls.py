"""TLS for MySQL connections (platform metadata DB and tenant DB servers)."""
import ssl

from sqlalchemy.engine import URL, make_url


def mysql_ssl_context(cafile: str) -> ssl.SSLContext:
    """Verify the server cert chain against `cafile` and check the hostname.

    Python 3.13 turned on VERIFY_X509_STRICT by default, and DO managed MySQL's CA fails it ("Basic Constraints of CA
    cert not marked critical"). Clear only that flag; chain and hostname verification stay on.
    """
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def engine_url_and_args(database_url: str) -> tuple[URL | str, dict]:
    """Turn `mysql+pymysql://...?ssl_ca=/path` into (url without ssl_ca, connect_args with an SSLContext)."""
    if database_url.startswith("sqlite"):
        return database_url, {"check_same_thread": False}
    url = make_url(database_url)
    ca = url.query.get("ssl_ca")
    if not ca:
        return url, {}
    if isinstance(ca, tuple):
        ca = ca[0]
    return url.difference_update_query(["ssl_ca"]), {"ssl": mysql_ssl_context(ca)}
