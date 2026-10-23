import pytest

from app import crypto, models
from app.dependencies import get_db_server_checker
from app.main import app
from app.providers.fake import FakeDbServerChecker
from tests.conftest import register_and_login

BODY = {"name": "primary", "host": "db.example.com", "port": 25060, "admin_user": "doadmin",
        "admin_password": "not-the-password", "max_databases": 10}


@pytest.fixture()
def checker():
    c = FakeDbServerChecker()
    app.dependency_overrides[get_db_server_checker] = lambda: c
    return c


@pytest.fixture()
def admin(make_client, checker):
    c = make_client()
    register_and_login(c, "admin@example.com")
    app.dependency_overrides[get_db_server_checker] = lambda: checker  # make_client reset happens before
    return c


def _server(db_session, sid):
    db_session.expire_all()
    return db_session.get(models.DbServer, sid)


def test_create_ok_checks_connection(admin, checker, db_session):
    r = admin.post("/admin/db-servers", json=BODY)
    assert r.status_code == 201
    assert checker.calls[0]["host"] == "db.example.com" and checker.calls[0]["password"] == "not-the-password"
    assert "admin_password" not in r.json()


def test_create_fails_422_saves_nothing(admin, checker, db_session):
    checker.error = "authentication failed"
    r = admin.post("/admin/db-servers", json=BODY)
    assert r.status_code == 422
    assert r.json()["detail"] == "Couldn't connect to db.example.com:25060 as doadmin: authentication failed"
    assert "not-the-password" not in r.text
    assert db_session.query(models.DbServer).count() == 0


def test_patch_password_ok(admin, checker, db_session):
    sid = admin.post("/admin/db-servers", json=BODY).json()["id"]
    r = admin.patch(f"/admin/db-servers/{sid}", json={"admin_password": "new-not-a-password"})
    assert r.status_code == 200 and "admin_password" not in r.json()
    assert "new-not-a-password" not in r.text
    assert checker.calls[-1]["password"] == "new-not-a-password"
    row = _server(db_session, sid)
    assert row.admin_password_encrypted != "new-not-a-password"
    assert crypto.decrypt(row.admin_password_encrypted) == "new-not-a-password"
    audit = admin.get("/admin/audit").json()
    detail = next(a["detail"] for a in audit if a["action"] == "dbserver.update")
    assert "admin_password=changed" in detail and "new-not-a-password" not in detail


def test_patch_password_fails_keeps_old(admin, checker, db_session):
    sid = admin.post("/admin/db-servers", json=BODY).json()["id"]
    checker.error = "connection timed out"
    r = admin.patch(f"/admin/db-servers/{sid}", json={"admin_password": "new-not-a-password", "max_databases": 99})
    assert r.status_code == 422 and "timed out" in r.json()["detail"]
    row = _server(db_session, sid)
    assert crypto.decrypt(row.admin_password_encrypted) == "not-the-password"
    assert row.max_databases == 10


def test_patch_invalidates_health_cache(admin, checker):
    from app import health
    sid = admin.post("/admin/db-servers", json=BODY).json()["id"]
    health._db_cache[sid] = (0.0, {"error": "stale"})
    admin.patch(f"/admin/db-servers/{sid}", json={"admin_password": "new-not-a-password"})
    assert sid not in health._db_cache


def test_ca_change_rechecks_but_status_does_not(admin, checker, db_session):
    sid = admin.post("/admin/db-servers", json=BODY).json()["id"]
    n = len(checker.calls)
    admin.patch(f"/admin/db-servers/{sid}", json={"status": "draining"})
    assert len(checker.calls) == n
    from cryptography import x509  # noqa: F401
    pem = _make_pem()
    r = admin.patch(f"/admin/db-servers/{sid}", json={"ca_cert": pem})
    assert r.status_code == 200 and len(checker.calls) == n + 1
    assert checker.calls[-1]["ca_cert"] == pem
    assert checker.calls[-1]["password"] == "not-the-password"  # existing password, decrypted
    checker.error = "TLS handshake failed"
    assert admin.patch(f"/admin/db-servers/{sid}", json={"ca_cert": ""}).status_code == 422
    assert _server(db_session, sid).ca_cert == pem


def _make_pem():
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_non_admin_403(admin, make_client, checker):
    sid = admin.post("/admin/db-servers", json=BODY).json()["id"]
    u = make_client()
    register_and_login(u, "user@example.com")
    assert u.post("/admin/db-servers", json={**BODY, "name": "x"}).status_code == 403
    assert u.patch(f"/admin/db-servers/{sid}", json={"admin_password": "new-not-a-password"}).status_code == 403


def test_read_scope_key_403_full_key_allowed(admin, make_client, checker):
    sid = admin.post("/admin/db-servers", json=BODY).json()["id"]
    read = admin.post("/api/keys", json={"scope": "read"}).json()["key"]
    full = admin.post("/api/keys", json={"scope": "full"}).json()["key"]
    k = make_client()
    r = k.patch(f"/admin/db-servers/{sid}", json={"admin_password": "new-not-a-password"},
                headers={"Authorization": f"Bearer {read}"})
    assert r.status_code == 403
    r = k.patch(f"/admin/db-servers/{sid}", json={"admin_password": "new-not-a-password"},
                headers={"Authorization": f"Bearer {full}"})
    assert r.status_code == 200


def test_classify_never_leaks():
    from app.providers.dbcheck import RealDbServerChecker, DbServerCheckError
    c = RealDbServerChecker()
    c._mysql = staticmethod(lambda *a: (_ for _ in ()).throw(Exception("Access denied for user 'x' password=secret1")))
    with pytest.raises(DbServerCheckError) as e:
        c.check(engine="mysql", host="h", port=1, user="u", password="secret1", ca_cert=None)
    assert str(e.value) == "Couldn't connect to h:1 as u: authentication failed" and "secret1" not in str(e.value)
    c._mysql = staticmethod(lambda *a: (_ for _ in ()).throw(TimeoutError("timed out")))
    with pytest.raises(DbServerCheckError, match="connection timed out"):
        c.check(engine="mysql", host="h", port=1, user="u", password="p", ca_cert=None)
