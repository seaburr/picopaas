"""TLS to MySQL: MySQLAdminClient and the app engine pass a verifying SSLContext only when a CA is configured."""
import datetime
import ssl
from types import SimpleNamespace

import pymysql
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app import config
from app.crypto import encrypt
from app.db_tls import engine_url_and_args, mysql_ssl_context
from app.providers.mysql import MySQLAdminClient


def _ca_file(tmp_path):
    # Like DO's CA: Basic Constraints present but NOT critical, which Python 3.13's strict mode rejects.
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=False)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "ca.crt"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(path)


def _client(monkeypatch, ca):
    monkeypatch.setattr(config, "MYSQL_SSL_CA", ca)
    server = SimpleNamespace(
        host="db.example", port=25060, admin_user="admin",
        admin_password_encrypted=encrypt("pw"),
    )
    captured = {}
    monkeypatch.setattr(pymysql, "connect", lambda **kw: captured.update(kw) or object())
    MySQLAdminClient(server)._connect()
    return captured


def test_context_verifies_but_is_not_strict(tmp_path):
    ctx = mysql_ssl_context(_ca_file(tmp_path))
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert not ctx.verify_flags & ssl.VERIFY_X509_STRICT


def test_ssl_context_passed_when_ca_set(monkeypatch, tmp_path):
    kw = _client(monkeypatch, _ca_file(tmp_path))
    assert isinstance(kw["ssl"], ssl.SSLContext) and kw["ssl"].verify_mode == ssl.CERT_REQUIRED


def test_ssl_omitted_when_ca_empty(monkeypatch):
    kw = _client(monkeypatch, "")
    assert "ssl" not in kw


def test_engine_url_moves_ssl_ca_into_context(tmp_path):
    ca = _ca_file(tmp_path)
    url, args = engine_url_and_args(f"mysql+pymysql://u:p@db.example:25060/picopaas?ssl_ca={ca}&charset=utf8mb4")
    assert "ssl_ca" not in url.query and url.query["charset"] == "utf8mb4"
    assert isinstance(args["ssl"], ssl.SSLContext)


def test_engine_url_without_ca_or_sqlite():
    url, args = engine_url_and_args("mysql+pymysql://u:p@h/db")
    assert args == {}
    assert engine_url_and_args("sqlite://") == ("sqlite://", {"check_same_thread": False})
