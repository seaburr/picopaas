import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, crypto, k8s_manifests, models
from app.dependencies import get_db_server_checker
from app.main import app
from app.providers import postgres
from app.providers.db_flavor import DEFAULT_FLAVOR, FLAVORS, flavor_for
from app.providers.fake import FakeDbServerChecker
from tests.conftest import register_and_login

ROOT = Path(__file__).resolve().parent.parent
BODY = {"name": "primary", "host": "db.example.com", "port": 25060, "admin_user": "doadmin",
        "admin_password": "not-the-password", "max_databases": 10}


# -- flavor values ---------------------------------------------------------------------------------------------

def test_do_managed_is_todays_values():
    f = FLAVORS["do-managed"]
    assert (f.pg_admin_db, f.pg_port, f.mysql_port) == ("defaultdb", 25060, 3306)
    assert (f.pg_tls_mode, f.pg_tls_mode_with_ca, f.mysql_clear_strict_x509) == ("require", "verify-ca", True)
    assert f.mysql_admin_db is None
    assert postgres.ADMIN_DB == "defaultdb"


def test_other_flavors_use_stock_names_and_keep_strict_x509():
    for name in ("self-managed", "rds", "cloudsql"):
        f = FLAVORS[name]
        assert f.pg_admin_db == "postgres" and f.mysql_clear_strict_x509 is False
        assert f.default_port("pg") == 5432 and f.default_port("mysql") == 3306


def test_flavor_for_falls_back_to_do_managed():
    assert flavor_for(SimpleNamespace(flavor="rds")).name == "rds"
    for bad in (None, "", "nope"):
        assert flavor_for(SimpleNamespace(flavor=bad)).name == DEFAULT_FLAVOR
    assert flavor_for(SimpleNamespace()).name == DEFAULT_FLAVOR


def test_model_default_is_do_managed(db_session):
    s = models.DbServer(name="s", host="h", admin_user="a", admin_password_encrypted=crypto.encrypt("not-the-password"))
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    assert s.flavor == "do-managed"


def test_pg_admin_uses_flavor_db_and_tls(monkeypatch):
    seen = []
    monkeypatch.setattr(postgres.psycopg, "connect", lambda **kw: seen.append(kw) or object())
    def make(flavor):
        return postgres.PostgresAdmin(models.DbServer(
            name="s", engine="pg", host="h", port=5432, admin_user="a", flavor=flavor,
            admin_password_encrypted=crypto.encrypt("not-the-password")))
    make("do-managed")._connect()
    make("rds")._connect()
    assert (seen[0]["dbname"], seen[0]["sslmode"]) == ("defaultdb", "require")
    assert (seen[1]["dbname"], seen[1]["sslmode"]) == ("postgres", "require")


# -- migration -------------------------------------------------------------------------------------------------

def test_migration_backfills_do_managed(tmp_path):
    db = tmp_path / "m.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}"}

    def alembic(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, check=True, capture_output=True)

    alembic("upgrade", "c5e7f9a1b3d2")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO db_servers (name, host, port, admin_user, admin_password_encrypted, max_databases, status, "
                "engine, created_at) VALUES ('old','h',3306,'a','x',25,'active','mysql','2026-01-01')")
    con.commit()
    con.close()
    alembic("upgrade", "head")
    con = sqlite3.connect(db)
    assert con.execute("SELECT flavor FROM db_servers WHERE name='old'").fetchone() == ("do-managed",)
    con.close()


# -- admin API -------------------------------------------------------------------------------------------------

@pytest.fixture()
def checker():
    c = FakeDbServerChecker()
    app.dependency_overrides[get_db_server_checker] = lambda: c
    return c


@pytest.fixture()
def admin(make_client, checker):
    c = make_client()
    register_and_login(c, "admin@example.com")
    app.dependency_overrides[get_db_server_checker] = lambda: checker
    return c


def test_create_defaults_to_do_managed(admin, checker, db_session):
    r = admin.post("/admin/db-servers", json=BODY)
    assert r.status_code == 201 and r.json()["flavor"] == "do-managed"
    assert db_session.get(models.DbServer, r.json()["id"]).flavor == "do-managed"
    assert checker.calls[0]["flavor"] == "do-managed"
    assert admin.get("/admin/db-servers").json()[0]["flavor"] == "do-managed"


def test_create_with_flavor_persists_and_audits(admin, checker, db_session):
    r = admin.post("/admin/db-servers", json={**BODY, "flavor": "rds"})
    assert r.status_code == 201 and r.json()["flavor"] == "rds"
    assert db_session.get(models.DbServer, r.json()["id"]).flavor == "rds"
    assert checker.calls[0]["flavor"] == "rds"
    audit = db_session.query(models.AuditLog).filter_by(action="dbserver.create").one()
    assert "flavor=rds" in audit.detail


def test_create_rejects_unknown_flavor(admin, checker, db_session):
    r = admin.post("/admin/db-servers", json={**BODY, "flavor": "azure"})
    assert r.status_code == 422
    assert db_session.query(models.DbServer).count() == 0 and checker.calls == []


def test_patch_flavor_and_validation(admin, db_session):
    sid = admin.post("/admin/db-servers", json=BODY).json()["id"]
    assert admin.patch(f"/admin/db-servers/{sid}", json={"flavor": "bogus"}).status_code == 422
    r = admin.patch(f"/admin/db-servers/{sid}", json={"flavor": "cloudsql"})
    assert r.status_code == 200 and r.json()["flavor"] == "cloudsql"
    db_session.expire_all()
    assert db_session.get(models.DbServer, sid).flavor == "cloudsql"
    audit = db_session.query(models.AuditLog).filter_by(action="dbserver.update").one()
    assert "flavor" in audit.detail and "flavor=cloudsql" in audit.detail


def test_flavor_routes_anonymous_and_non_admin(admin, make_client):
    sid = admin.post("/admin/db-servers", json=BODY).json()["id"]
    anon = make_client()
    assert anon.post("/admin/db-servers", json=BODY).status_code == 401
    assert anon.patch(f"/admin/db-servers/{sid}", json={"flavor": "rds"}).status_code == 401
    user = make_client()
    register_and_login(user, "user@example.com")
    assert user.post("/admin/db-servers", json={**BODY, "name": "x", "flavor": "rds"}).status_code == 403
    assert user.patch(f"/admin/db-servers/{sid}", json={"flavor": "rds"}).status_code == 403


# -- tenant CIDRs ------------------------------------------------------------------------------------------------

SNAP = json.loads((Path(__file__).parent / "snapshots" / "network_policies_default.json").read_text())
VARIANTS = {
    "plain": dict(mysql_cidr=None),
    "mysql": dict(mysql_cidr="10.0.0.0/24"),
    "pg": dict(mysql_cidr="10.11.0.0/20", mysql_port=25060),
    "full": dict(mysql_cidr="10.9.0.0/24", storage_ips=["10.1.1.1"], custom_domain=True, replicas=2, backup_ips=["10.2.2.2"]),
}


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_default_network_policies_match_pre_change_snapshot(name):
    # Snapshot taken before P1b (CIDRs moved to config): default config must render identical policies.
    got = json.loads(json.dumps(k8s_manifests.network_policies(7, 8080, **VARIANTS[name])))
    assert got == SNAP[name]


def test_default_cidr_constants_match_snapshot():
    assert k8s_manifests.PRIVATE_CIDRS == SNAP["consts"]["PRIVATE_CIDRS"]
    assert k8s_manifests.RESERVED_CIDRS == SNAP["consts"]["RESERVED_CIDRS"]
    assert k8s_manifests.EGRESS_EXCEPT_CIDRS == SNAP["consts"]["EGRESS_EXCEPT_CIDRS"]


def test_parse_extra_cidrs():
    assert config.parse_extra_cidrs("X", None) == []
    assert config.parse_extra_cidrs("X", "  ") == []
    assert config.parse_extra_cidrs("X", "100.64.0.0/10, 172.20.0.0/14") == ["100.64.0.0/10", "172.20.0.0/14"]


@pytest.mark.parametrize("raw", ["not-a-cidr", "10.0.0.0/8,bogus", "10.0.0.1/8", "0.0.0.0/0", "fd00::/8"])
def test_parse_extra_cidrs_rejects_bad_values(raw):
    with pytest.raises(ValueError, match="X"):
        config.parse_extra_cidrs("X", raw)


def test_merge_keeps_defaults_and_dedupes():
    d = config.DEFAULT_TENANT_PRIVATE_CIDRS
    assert config.merge_cidrs(d, []) == list(d)
    assert config.merge_cidrs(d, ["10.0.0.0/8", "100.64.0.0/10", "100.64.0.0/10"]) == list(d) + ["100.64.0.0/10"]


@pytest.mark.parametrize("var", ["TENANT_EXTRA_PRIVATE_CIDRS", "TENANT_EXTRA_RESERVED_CIDRS"])
def test_invalid_env_fails_clearly_at_startup(var):
    r = subprocess.run([sys.executable, "-c", "import app.config"], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, var: "10.0.0.0/8,nonsense"})
    assert r.returncode != 0 and f"{var} has an invalid network 'nonsense'" in r.stderr


def test_old_replace_style_vars_are_ignored():
    code = "from app import k8s_manifests as k; print(k.PRIVATE_CIDRS)"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "TENANT_PRIVATE_CIDRS": "100.64.0.0/10"})
    assert r.returncode == 0 and r.stdout.strip() == str(SNAP["consts"]["PRIVATE_CIDRS"])


def test_extra_env_only_adds_never_removes():
    code = ("from app import k8s_manifests as k; import json; "
            "print(json.dumps([k.PRIVATE_CIDRS, k.RESERVED_CIDRS, k.EGRESS_EXCEPT_CIDRS, "
            "k.network_policies(7, 8080, mysql_cidr=None)]))")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "TENANT_EXTRA_PRIVATE_CIDRS": "100.96.0.0/11, 169.254.0.0/16",
                            "TENANT_EXTRA_RESERVED_CIDRS": "203.0.113.0/24"})
    assert r.returncode == 0, r.stderr
    priv, res, exc, pols = json.loads(r.stdout)
    assert priv == SNAP["consts"]["PRIVATE_CIDRS"] + ["100.96.0.0/11"]
    assert "169.254.0.0/16" in priv and "10.0.0.0/8" in priv
    assert res == SNAP["consts"]["RESERVED_CIDRS"] + ["203.0.113.0/24"]
    assert exc == priv + res
    assert set(SNAP["consts"]["EGRESS_EXCEPT_CIDRS"]) <= set(exc)
    assert "203.0.113.0/24" in json.dumps(pols)
