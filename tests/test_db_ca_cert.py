"""db.ca_cert / db.tls refs, ca_cert migration, bootstrap seeding + backfill."""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect

from app import crypto, models, scheduler
from app.env_refs import ALLOWED_REFS
from app.reconciler import _db_refs

PEM = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"


def _app(db_session, ca):
    server = models.DbServer(name="s", host="h", port=3306, admin_user="a",
                             admin_password_encrypted=crypto.encrypt("x"), ca_cert=ca)
    db_session.add(server)
    db_session.flush()
    row = models.AppDatabase(server=server, db_name="d", db_user="u",
                             password_encrypted=crypto.encrypt("pw"))
    return type("A", (), {"database": row})()


@pytest.mark.parametrize("ca,tls", [(PEM, "required"), (None, "off")])
def test_refs(db_session, ca, tls):
    refs = _db_refs(_app(db_session, ca))
    assert refs["db.ca_cert"] == (ca or "")
    assert refs["db.tls"] == tls
    assert refs["db.url"] == "mysql://u:pw@h:3306/d"
    assert refs["db.ca_cert_body"] == ("abc" if ca else "")
    assert refs["db.ca_file"] == ("/etc/picopaas/db-ca.pem" if ca else "")
    assert {"db.ca_cert", "db.ca_cert_body", "db.ca_file", "db.tls"} <= ALLOWED_REFS


def test_pem_body_is_first_cert_on_one_line():
    from app.env_refs import pem_body
    two = ("junk\n-----BEGIN CERTIFICATE-----\r\nMIIB\n  AAAA==\n-----END CERTIFICATE-----\n"
           "-----BEGIN CERTIFICATE-----\nSECOND\n-----END CERTIFICATE-----\n")
    assert pem_body(two) == "MIIBAAAA=="
    assert pem_body("") == pem_body(None) == pem_body("not a pem") == ""


def test_migration_round_trip(tmp_path):
    url = f"sqlite:///{tmp_path}/m.db"
    root = os.path.join(os.path.dirname(__file__), "..")
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True,
                       capture_output=True)

    def cols():
        return {c["name"] for c in inspect(create_engine(url)).get_columns("db_servers")}
    alembic("upgrade", "head")
    assert "ca_cert" in cols()
    alembic("downgrade", "e5b2c8d1f3a7")
    assert "ca_cert" not in cols()
    alembic("upgrade", "head")
    assert "ca_cert" in cols()


def _env(monkeypatch, db_session, ca_path):
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    for k, v in dict(BOOTSTRAP_DB_HOST="h", BOOTSTRAP_DB_USER="u", BOOTSTRAP_DB_PASSWORD="pw").items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("MYSQL_SSL_CA", str(ca_path))


def test_bootstrap_seeds_ca(db_session, monkeypatch, tmp_path):
    f = tmp_path / "ca.crt"
    f.write_text(PEM)
    _env(monkeypatch, db_session, f)
    scheduler.bootstrap_db_server()
    assert db_session.query(models.DbServer).one().ca_cert == PEM


def test_bootstrap_backfills_and_missing_file(db_session, monkeypatch, tmp_path):
    _env(monkeypatch, db_session, tmp_path / "nope.crt")
    scheduler.bootstrap_db_server()
    assert db_session.query(models.DbServer).one().ca_cert is None
    f = tmp_path / "ca.crt"
    f.write_text(PEM)
    monkeypatch.setenv("MYSQL_SSL_CA", str(f))
    scheduler.bootstrap_db_server()
    assert db_session.query(models.DbServer).one().ca_cert == PEM


def test_bootstrap_repairs_undecryptable_admin_password(db_session, monkeypatch, tmp_path):
    from app import crypto

    _env(monkeypatch, db_session, tmp_path / "nope.crt")
    scheduler.bootstrap_db_server()
    row = db_session.query(models.DbServer).one()
    row.admin_password_encrypted = "gAAAAA-encrypted-with-a-lost-key"  # simulates a FERNET_KEY change
    db_session.commit()
    scheduler.bootstrap_db_server()
    assert crypto.decrypt(db_session.query(models.DbServer).one().admin_password_encrypted) == "pw"


def test_bootstrap_syncs_rotated_admin_password(db_session, monkeypatch, tmp_path):
    from app import crypto

    _env(monkeypatch, db_session, tmp_path / "nope.crt")
    scheduler.bootstrap_db_server()
    monkeypatch.setenv("BOOTSTRAP_DB_PASSWORD", "rotated")
    scheduler.bootstrap_db_server()
    assert crypto.decrypt(db_session.query(models.DbServer).one().admin_password_encrypted) == "rotated"


# --- the CA as a mounted file (Secret app-<id>-dbca at /etc/picopaas) ---

from app import config, k8s_manifests as k8s  # noqa: E402
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore  # noqa: E402
from app.reconciler import reconcile_all  # noqa: E402
from tests.conftest import register_and_login  # noqa: E402


def _render(**kw):
    return k8s.render_app_manifests(7, "h", "img:1", 8080, {"A": "b"}, None, **kw)


def _pod(rendered):
    return rendered["deployment"]["spec"]["template"]["spec"]


def test_manifests_carry_secret_volume_and_mount_only_with_a_ca():
    r = _render(db_ca_pem=PEM)
    sec = r["db_ca_secret"]
    assert sec["metadata"] == {"name": "app-7-dbca", "namespace": "app-7", "labels": {"app": "app-7"}}
    assert sec["stringData"] == {"db-ca.pem": PEM}
    assert {"name": "db-ca", "secret": {"secretName": "app-7-dbca", "defaultMode": 0o444}} in _pod(r)["volumes"]
    mounts = _pod(r)["containers"][0]["volumeMounts"]
    assert mounts == [{"name": "db-ca", "mountPath": "/etc/picopaas", "readOnly": True}]  # directory, no subPath
    assert sec in k8s.flatten_manifests(r)
    bare = _render()
    assert bare["db_ca_secret"] is None and "volumes" not in _pod(bare)
    assert "volumeMounts" not in _pod(bare)["containers"][0]
    assert all(m["metadata"]["name"] != "app-7-dbca" for m in k8s.flatten_manifests(bare))


def test_ca_mount_coexists_with_the_data_volume():
    r = _render(db_ca_pem=PEM, volume={"size_gi": 1, "mount_path": "/data"})
    assert [v["name"] for v in _pod(r)["volumes"]] == ["data", "db-ca"]
    assert [m["mountPath"] for m in _pod(r)["containers"][0]["volumeMounts"]] == ["/data", config.DB_CA_MOUNT_DIR]


@pytest.mark.parametrize("path", ["/etc/picopaas", "/etc/picopaas/x", "//etc/picopaas/", "/etc"])
def test_user_volume_cannot_mount_over_the_ca_dir(path):
    from app.volumes import validate_mount_path
    with pytest.raises(ValueError):
        validate_mount_path(path)


def _sweep(db, kube):
    reconcile_all(db, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(),
                  object_store=FakeObjectStore(), settings={})
    db.expire_all()


def _applied(kube, aid, kind):
    return [m for m in kube.applied.get(f"app-{aid}", []) if m["kind"] == kind]


def test_reconciler_renders_ca_for_db_app_resolves_ref_and_prunes_when_ca_removed(make_client, db_session):
    server = models.DbServer(name="s", host="db.internal", port=3306, admin_user="a",
                             admin_password_encrypted=crypto.encrypt("x"), max_databases=5, ca_cert=PEM)
    db_session.add(server)
    db_session.commit()
    c = make_client()
    register_and_login(c, "ca@example.com")
    body = {"name": "dbapp", "image": "ghcr.io/example/demo:1.0", "port": 8080, "want_db": True,
            "env": [{"key": "CA", "value": "sslrootcert=${{ db.ca_file }}"}]}
    aid = c.post("/apps", json=body).json()["id"]
    plain = c.post("/apps", json={**body, "name": "plain", "want_db": False, "env": []}).json()["id"]
    kube = FakeKubeProvider()
    _sweep(db_session, kube)
    sec = _applied(kube, aid, "Secret")
    assert {s["metadata"]["name"] for s in sec} == {f"app-{aid}-env", f"app-{aid}-dbca"}
    env = next(s for s in sec if s["metadata"]["name"].endswith("-env"))
    assert env["stringData"]["CA"] == "sslrootcert=/etc/picopaas/db-ca.pem"
    dbca = next(s for s in sec if s["metadata"]["name"].endswith("-dbca"))
    assert dbca["stringData"]["db-ca.pem"] == PEM
    dep = _applied(kube, aid, "Deployment")[-1]
    assert dep["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0]["mountPath"] == "/etc/picopaas"
    assert [s["metadata"]["name"] for s in _applied(kube, plain, "Secret")] == [f"app-{plain}-env"]
    assert "volumes" not in _applied(kube, plain, "Deployment")[-1]["spec"]["template"]["spec"]

    # The server loses its CA: the next render has no mount, no ref value, and the Secret is pruned.
    db_session.get(models.DbServer, server.id).ca_cert = None
    app = db_session.get(models.App, aid)
    app.rendered_hash = None
    db_session.commit()
    _sweep(db_session, kube)
    assert [s["metadata"]["name"] for s in _applied(kube, aid, "Secret")][-1:] != [f"app-{aid}-dbca"]
    assert all(s["metadata"]["name"] != f"app-{aid}-dbca" for s in _applied(kube, aid, "Secret"))
    assert "volumes" not in _applied(kube, aid, "Deployment")[-1]["spec"]["template"]["spec"]
    env = [s for s in _applied(kube, aid, "Secret") if s["metadata"]["name"].endswith("-env")][-1]  # fake appends
    assert env["stringData"]["CA"] == "sslrootcert="


def test_suspended_db_app_keeps_the_ca_mount(make_client, db_session):
    db_session.add(models.DbServer(name="s", host="db.internal", port=3306, admin_user="a",
                                   admin_password_encrypted=crypto.encrypt("x"), max_databases=5, ca_cert=PEM))
    db_session.commit()
    c = make_client()
    register_and_login(c, "ca2@example.com")
    aid = c.post("/apps", json={"name": "dbapp", "image": "ghcr.io/example/demo:1.0", "port": 8080,
                                "want_db": True}).json()["id"]
    kube = FakeKubeProvider()
    _sweep(db_session, kube)
    db_session.get(models.App, aid).status = "suspended"
    db_session.commit()
    _sweep(db_session, kube)
    dep = _applied(kube, aid, "Deployment")[-1]
    assert dep["spec"]["replicas"] == 0
    assert dep["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0]["name"] == "db-ca"
