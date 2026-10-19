"""MySQLAdminClient passes TLS kwargs only when MYSQL_SSL_CA is set."""
from types import SimpleNamespace

import pymysql

from app import config
from app.crypto import encrypt
from app.providers.mysql import MySQLAdminClient


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


def test_ssl_kwargs_passed_when_ca_set(monkeypatch):
    kw = _client(monkeypatch, "/etc/picopaas/db-ca/ca.crt")
    assert kw["ssl"] == {"ca": "/etc/picopaas/db-ca/ca.crt"}
    assert kw["ssl_verify_cert"] is True and kw["ssl_verify_identity"] is True


def test_ssl_omitted_when_ca_empty(monkeypatch):
    kw = _client(monkeypatch, "")
    assert "ssl" not in kw and "ssl_verify_cert" not in kw
