"""P2c: in-cluster egress by selector (PG, MySQL, object store, backups), PG16 SET ROLE grant, bootstrap flavors.
The default (cidr / do-managed) paths are pinned by tests/snapshots/network_policies_default.json."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, k8s_manifests, models, reconciler, scheduler
from app.providers import backup as pb
from app.providers import postgres
from tests.test_postgres import _Cur, _render, _server

SNAP = json.loads((Path(__file__).parent / "snapshots" / "network_policies_default.json").read_text())


def _pol(pols, name):
    return next(p for p in pols if p["metadata"]["name"] == name)


def _peer(ns, labels):
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ns}}, "podSelector": {"matchLabels": labels}}


# -- policy shape ---------------------------------------------------------------------------------------------

def test_default_policies_still_match_snapshot_without_new_args():
    got = json.loads(json.dumps(k8s_manifests.network_policies(7, 8080, "10.0.0.0/24")))
    assert got == SNAP["mysql"]
    got = json.loads(json.dumps(k8s_manifests.network_policies(7, 8080, None)))
    assert got == SNAP["plain"]  # MySQL in-cluster default selector (ns picopaas-data, app=mysql) is unchanged


def test_mysql_in_cluster_default_selector_unchanged():
    rule = _pol(k8s_manifests.network_policies(7, 8080, None, 3306), "allow-egress-mysql")["spec"]["egress"][0]
    assert rule["to"] == [_peer("picopaas-data", {"app": "mysql"})]
    assert rule["ports"] == [{"protocol": "TCP", "port": 3306}]


def test_pg_in_cluster_selector_and_server_port():
    peer = k8s_manifests.selector_peer("picopaas-data", {"app": "postgres"})
    rule = _pol(k8s_manifests.network_policies(7, 8080, None, 5432, db_peer=peer), "allow-egress-mysql")["spec"]["egress"][0]
    assert rule["to"] == [_peer("picopaas-data", {"app": "postgres"})]  # exactly one peer, no ipBlock
    assert rule["ports"] == [{"protocol": "TCP", "port": 5432}]


def test_storage_in_cluster_uses_selector_on_endpoint_port_and_no_ipblock():
    peer = k8s_manifests.selector_peer("picopaas-data", {"app": "garage"})
    pols = k8s_manifests.network_policies(7, 8080, None, storage_ips=[], storage_port=3900, storage_peer=peer)
    egress = _pol(pols, "allow-egress-storage")["spec"]["egress"]
    assert egress == [{"to": [_peer("picopaas-data", {"app": "garage"})], "ports": [{"protocol": "TCP", "port": 3900}]}]
    assert "ipBlock" not in json.dumps(egress)


def test_storage_policy_absent_without_ips_or_peer():
    names = [p["metadata"]["name"] for p in k8s_manifests.network_policies(7, 8080, None, storage_ips=[])]
    assert "allow-egress-storage" not in names


def test_backup_in_cluster_adds_selector_rule_keeps_public_443_rule():
    peer = k8s_manifests.selector_peer("picopaas-data", {"app": "garage"})
    pols = k8s_manifests.network_policies(7, 8080, None, backup_ips=[], backup_port=3900, backup_peer=peer)
    rules = _pol(pols, k8s_manifests.BACKUP_EGRESS_POLICY)["spec"]["egress"]
    assert rules[-1] == {"to": [peer], "ports": [{"protocol": "TCP", "port": 3900}]}
    assert rules[0]["ports"] == [{"protocol": "TCP", "port": 443}]
    assert not any("/32" in json.dumps(r) for r in rules)


def test_selector_is_exact_one_namespace_one_label_set():
    peer = k8s_manifests.selector_peer("ns1", {"app": "garage"})
    assert set(peer) == {"namespaceSelector", "podSelector"}
    assert peer["namespaceSelector"] == {"matchLabels": {"kubernetes.io/metadata.name": "ns1"}}
    assert peer["podSelector"] == {"matchLabels": {"app": "garage"}}  # no matchExpressions / empty selector


def test_render_app_manifests_threads_peers():
    kw = dict(app_id=7, hostname="h", image="i", port=8080, env={}, mysql_cidr=None, mysql_port=5432,
              storage_ips=[], storage_port=3900)
    peer = k8s_manifests.selector_peer("picopaas-data", {"app": "garage"})
    pols = k8s_manifests.render_app_manifests(**kw, storage_peer=peer)["network_policies"]
    assert _pol(pols, "allow-egress-storage")["spec"]["egress"][0]["to"] == [peer]


# -- reconciler wiring ----------------------------------------------------------------------------------------

def test_mysql_cidr_pg_in_cluster_mode_is_none_else_cidr():
    assert reconciler._mysql_cidr({"pg_egress_mode": "in-cluster"}, "pg") is None
    assert reconciler._mysql_cidr({"pg_egress_mode": "cidr", "pg_cidr": "10.11.0.0/20"}, "pg") == "10.11.0.0/20"
    # no pg_egress_mode on the settings object (older callers / SimpleNamespace): today's behaviour
    assert reconciler._mysql_cidr(SimpleNamespace(mysql_cidr="10.0.0.0/16", pg_cidr=None), "pg") == "10.0.0.0/16"


def test_db_peer_and_object_store_peer(monkeypatch):
    assert reconciler._db_peer("mysql") is None
    assert reconciler._db_peer("pg") == _peer("picopaas-data", {"app": "postgres"})
    assert reconciler._object_store_peer() is None  # default cidr
    monkeypatch.setattr(config, "OBJECT_STORE_EGRESS_MODE", "in-cluster")
    monkeypatch.setattr(config, "OBJECT_STORE_EGRESS_NAMESPACE", "data")
    monkeypatch.setattr(config, "OBJECT_STORE_EGRESS_POD_LABELS", {"app": "s3", "tier": "x"})
    assert reconciler._object_store_peer() == _peer("data", {"app": "s3", "tier": "x"})


def test_scheduler_settings_carry_pg_mode(monkeypatch):
    monkeypatch.setattr(config, "PG_EGRESS_MODE", "in-cluster")
    assert scheduler._settings().pg_egress_mode == "in-cluster"


def test_backup_runner_in_cluster_policy_has_selector_and_endpoint_port(monkeypatch):
    applied = []

    class Kube:
        def apply(self, manifests):
            applied.extend(manifests)

    monkeypatch.setattr(config, "OBJECT_STORE_EGRESS_MODE", "in-cluster")
    monkeypatch.setattr("app.reconciler._private_storage_ips", lambda e: pytest.fail("no DNS in in-cluster mode"))
    monkeypatch.setattr(pb, "_egress_applied", [])
    r = pb.KubeBackupRunner(dynamic_client=object(), core_api=object())
    r._kube = Kube()
    r.ensure_spaces_egress("http://garage.picopaas-data.svc:3900")
    r.ensure_spaces_egress("http://garage.picopaas-data.svc:3900")  # unchanged: applied once
    assert len(applied) == 1
    assert applied[0]["metadata"]["name"] == "allow-egress-spaces"
    assert applied[0]["spec"]["egress"] == [
        {"to": [_peer("picopaas-data", {"app": "garage"})], "ports": [{"protocol": "TCP", "port": 3900}]}]


# -- config validation ----------------------------------------------------------------------------------------

def test_parse_pod_labels():
    assert config.parse_pod_labels("X", "app=a, tier=b") == {"app": "a", "tier": "b"}
    for bad in ("", "app", "=v", "app=", "app=a,", "app=a b", "app=-x", "a/b/c=v"):
        with pytest.raises(ValueError, match="X"):
            config.parse_pod_labels("X", bad)


def test_egress_mode_env_validated(monkeypatch):
    monkeypatch.setenv("PG_EGRESS_MODE", "in-cluster")
    assert config._egress_mode("PG_EGRESS_MODE") == "in-cluster"
    monkeypatch.setenv("PG_EGRESS_MODE", "wide-open")
    with pytest.raises(ValueError, match="PG_EGRESS_MODE"):
        config._egress_mode("PG_EGRESS_MODE")


def test_defaults_are_cidr():
    assert config.PG_EGRESS_MODE == "cidr" and config.OBJECT_STORE_EGRESS_MODE == "cidr"
    assert config.MYSQL_EGRESS_MODE == "cidr"


# -- PG16 SET ROLE grant --------------------------------------------------------------------------------------

class _Conn:
    log: list[str] = []
    version = "160004"
    has_set = False
    fail_grant = False

    def __init__(self, dbname):
        self.dbname = dbname

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        text = _render(query) if not isinstance(query, str) else query
        _Conn.log.append(text)
        if text == "SHOW server_version_num":
            return _Cur([(_Conn.version,)])
        if "pg_has_role" in text:
            return _Cur([(_Conn.has_set,)])
        if text.startswith("SELECT 1 FROM"):
            return _Cur([])
        if text.startswith("GRANT") and "CURRENT_USER" in text and _Conn.fail_grant:
            raise postgres.psycopg.Error("nope")
        return _Cur([])


@pytest.fixture()
def pgadmin(db_session, monkeypatch):
    from psycopg import sql
    _Conn.log, _Conn.version, _Conn.has_set, _Conn.fail_grant = [], "160004", False, False
    monkeypatch.setattr(postgres.psycopg, "connect", lambda **kw: _Conn(kw["dbname"]))
    monkeypatch.setattr(sql.Composable, "as_string", lambda self, ctx=None: _render(self), raising=False)

    def make(flavor):
        return postgres.PostgresAdmin(_server(db_session, f"p-{flavor}", "pg", flavor=flavor))
    return make


GRANT = 'GRANT "app_7" TO CURRENT_USER WITH INHERIT TRUE, SET TRUE'


def test_pg16_self_managed_grants_set_role_after_create_role(pgadmin):
    pgadmin("self-managed").create_db_user(7)
    assert GRANT in _Conn.log
    assert _Conn.log.index(GRANT) > next(i for i, s in enumerate(_Conn.log) if s.startswith('CREATE ROLE "app_7"'))
    assert _Conn.log.index(GRANT) < _Conn.log.index('CREATE DATABASE "app_7"')


def test_pg16_grant_skipped_when_already_effective(pgadmin):
    _Conn.has_set = True
    pgadmin("self-managed").create_db_user(7)
    assert GRANT not in _Conn.log


def test_pre_pg16_never_grants(pgadmin):
    _Conn.version = "150009"
    pgadmin("self-managed").create_db_user(7)
    assert GRANT not in _Conn.log


def test_do_managed_runs_no_extra_statements(pgadmin):
    pgadmin("do-managed").create_db_user(7)
    assert GRANT not in _Conn.log
    assert "SHOW server_version_num" not in _Conn.log and not any("pg_has_role" in s for s in _Conn.log)


def test_grant_failure_is_logged_not_raised(pgadmin):
    _Conn.fail_grant = True
    assert pgadmin("rds").create_db_user(7)["name"] == "app_7"


# -- bootstrap flavor -----------------------------------------------------------------------------------------

def _boot_env(monkeypatch, db_session, prefix):
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    for k, v in dict(HOST="h", USER="u", PASSWORD="not-the-password").items():
        monkeypatch.setenv(f"BOOTSTRAP_{prefix}_{k}", v)
    monkeypatch.delenv(f"BOOTSTRAP_{prefix}_FLAVOR", raising=False)
    monkeypatch.delenv("MYSQL_SSL_CA", raising=False)


@pytest.mark.parametrize("prefix,fn,name", [("DB", "bootstrap_db_server", "primary"), ("PG", "bootstrap_pg_server", "primary-pg")])
def test_bootstrap_flavor_default_explicit_and_no_clobber(db_session, monkeypatch, prefix, fn, name):
    _boot_env(monkeypatch, db_session, prefix)
    getattr(scheduler, fn)()
    row = db_session.query(models.DbServer).filter_by(name=name).one()
    assert row.flavor == "do-managed"
    monkeypatch.setenv(f"BOOTSTRAP_{prefix}_FLAVOR", "self-managed")
    getattr(scheduler, fn)()
    db_session.refresh(row)
    assert row.flavor == "self-managed"
    monkeypatch.delenv(f"BOOTSTRAP_{prefix}_FLAVOR")
    row.flavor = "rds"  # an admin's PATCH survives a restart without the env var
    db_session.commit()
    getattr(scheduler, fn)()
    db_session.refresh(row)
    assert row.flavor == "rds"


@pytest.mark.parametrize("prefix,fn", [("DB", "bootstrap_db_server"), ("PG", "bootstrap_pg_server")])
def test_bootstrap_flavor_seeds_new_row(db_session, monkeypatch, prefix, fn):
    _boot_env(monkeypatch, db_session, prefix)
    monkeypatch.setenv(f"BOOTSTRAP_{prefix}_FLAVOR", "self-managed")
    getattr(scheduler, fn)()
    assert db_session.query(models.DbServer).one().flavor == "self-managed"


@pytest.mark.parametrize("prefix,fn", [("DB", "bootstrap_db_server"), ("PG", "bootstrap_pg_server")])
def test_bootstrap_flavor_invalid_rejected(db_session, monkeypatch, prefix, fn):
    _boot_env(monkeypatch, db_session, prefix)
    monkeypatch.setenv(f"BOOTSTRAP_{prefix}_FLAVOR", "oracle")
    with pytest.raises(ValueError, match=f"BOOTSTRAP_{prefix}_FLAVOR"):
        getattr(scheduler, fn)()
    assert db_session.query(models.DbServer).count() == 0


def test_egress_namespace_validated(monkeypatch):
    monkeypatch.setenv("PG_EGRESS_NAMESPACE", "Bad NS")
    with pytest.raises(ValueError, match="PG_EGRESS_NAMESPACE"):
        config._egress_namespace("PG_EGRESS_NAMESPACE")
    monkeypatch.setenv("PG_EGRESS_NAMESPACE", "")
    with pytest.raises(ValueError):
        config._egress_namespace("PG_EGRESS_NAMESPACE")
