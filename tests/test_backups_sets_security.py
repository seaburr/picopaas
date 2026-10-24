"""Security review of backup sets and volume backups (2026-10-24). Each regression test fails without its fix."""
import json
import os
import shutil
import stat
import subprocess
import tarfile
from datetime import timedelta

import pytest

from app import backups, config, k8s_manifests as k8s, models
from app.providers.kube import KubeClientProvider
from tests import test_kube_provider as tk
from tests.test_backups import NOW, admin, on, other, runner, user  # noqa: F401
from tests.test_backups_sets import (_mk_set, _volume_app, api, app_ctx, rows, scoped, vols)  # noqa: F401

# --- leftover Secrets in tenant namespaces -----------------------------------------------------------------------

def test_tenant_secret_sweep_survives_turning_the_gate_off(on, vols, scoped, user, db_session, runner, monkeypatch):
    """A run Secret holds the backup key in a tenant namespace. Switching BACKUP_KEY_SCOPED back to 0 must not stop the sweep."""
    aid = _volume_app(user, db_session, with_db=False)
    _mk_set(db_session, aid, {"volume": "failed"}, created=NOW - timedelta(days=5))
    monkeypatch.setattr(config, "BACKUP_KEY_SCOPED", False)
    backups.tick(db_session, runner, NOW)
    assert [f"app-{aid}"] in runner.swept_namespaces


def test_tenant_secret_sweep_covers_a_month_not_two_days(on, vols, scoped, user, db_session, runner):
    aid = _volume_app(user, db_session, with_db=False)
    _mk_set(db_session, aid, {"volume": "failed"}, created=NOW - timedelta(days=20))
    backups.tick(db_session, runner, NOW)
    assert [f"app-{aid}"] in runner.swept_namespaces


# --- the backup pod is not the app ------------------------------------------------------------------------------

def _pod(name, labels, status, ns="app-7"):
    return {"metadata": {"name": name, "namespace": ns, "labels": labels, "creationTimestamp": "2026-01-01T00:00:00Z"}, "status": status}


OOM = {"phase": "Failed", "containerStatuses": [{"state": {"terminated": {"reason": "OOMKilled"}}}]}
BK = {"picopaas.io/backup": "1", "picopaas.io/backup-run": "5"}


def test_failed_backup_pod_does_not_turn_the_app_into_an_error():
    """Volume contents are tenant-controlled: a backup pod that is OOM-killed on a hostile volume must not show as the app's OOM."""
    dep = {"metadata": {"generation": 1}, "spec": {"replicas": 1},
           "status": {"readyReplicas": 1, "updatedReplicas": 1, "observedGeneration": 1}}
    pods = [_pod("app-1-x", {"app": "app-7"}, {"phase": "Running"}), _pod("bk-5-abc", BK, OOM)]
    assert KubeClientProvider(tk._dyn(pods=pods, deployments=[dep])).get_status("app-7") == "running"
    # the app's own OOM is still reported
    own = [_pod("app-1-x", {"app": "app-7"}, OOM)]
    assert KubeClientProvider(tk._dyn(pods=own, deployments=[dep])).get_status("app-7") == "error: oom"


def test_backup_pod_is_not_listed_logged_or_addressable_through_the_tenant_log_api(monkeypatch):
    pods = [_pod("app-1-x", {"app": "app-7"}, {"phase": "Running"}), _pod("bk-5-abc", BK, {"phase": "Running"})]
    prov = KubeClientProvider(tk._dyn(pods=pods))
    read = []
    monkeypatch.setattr(prov, "_pod_section", lambda ns, p, tail: read.append(p["metadata"]["name"]) or
                        __import__("app.providers.base", fromlist=["PodSection"]).PodSection(pod=p["metadata"]["name"], lines=["x"]))
    out = prov.pod_logs("app-7", 10)
    assert out.pods == ["app-1-x"] and read == ["app-1-x"]
    with pytest.raises(LookupError):
        prov.pod_logs("app-7", 10, pod="bk-5-abc")
    with pytest.raises(LookupError):
        prov.pod_events("app-7", "bk-5-abc")


# --- the tenant cannot reach or imitate the backup pod -----------------------------------------------------------

def test_app_pod_has_only_its_own_label_no_token_and_no_reference_to_backup_secrets():
    d = k8s.deployment_manifest(7, "ghcr.io/example/demo:1.0", 8080)
    tpl = d["spec"]["template"]
    assert tpl["metadata"]["labels"] == {"app": k8s._resource_name(7)}
    assert "picopaas.io/backup" not in json.dumps(tpl["metadata"])
    assert tpl["spec"]["automountServiceAccountToken"] is False
    refs = json.dumps(tpl["spec"])
    assert "bk-" not in refs  # run Secrets are named bk-<run id>; the app only references its own Secret


def test_tenant_env_cannot_set_pod_labels_or_the_secret_the_deployment_references():
    env = {"picopaas.io/backup": "1", "LABELS": "picopaas.io/backup=1"}
    d = k8s.deployment_manifest(7, "ghcr.io/example/demo:1.0", 8080, env_checksum="x")
    sec = k8s.secret_manifest(7, env)
    assert "picopaas.io/backup" not in json.dumps(d["spec"]["template"]["metadata"])
    assert "picopaas.io/backup" not in json.dumps(sec["metadata"])


def test_policies_keep_every_allow_rule_on_the_app_and_never_on_the_backup_pod():
    pols = k8s.network_policies(7, 8080, "10.9.0.0/24", storage_ips=["10.1.1.1"], custom_domain=True, replicas=2, backup_ips=["10.2.2.2"])
    for p in pols:
        sel = p["spec"]["podSelector"]
        name = p["metadata"]["name"]
        if name in ("default-deny", "allow-egress-dns"):
            assert sel == {}
        elif name == "allow-egress-backup":
            assert sel == {"matchLabels": {"picopaas.io/backup": "1"}}
        elif name == "allow-acme-solver":  # selects only cert-manager's solver pods, which the backup pod is not
            assert sel == {"matchLabels": {"acme.cert-manager.io/http01-solver": "true"}}
        else:  # every other allow rule must exclude the backup pod, none may select it
            assert sel == {"matchExpressions": [{"key": "picopaas.io/backup", "operator": "DoesNotExist"}]}, name
    bk = next(p for p in pols if p["metadata"]["name"] == "allow-egress-backup")
    (rule,) = bk["spec"]["egress"]
    assert bk["spec"]["policyTypes"] == ["Egress"] and "ingress" not in bk["spec"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]
    assert not any("namespaceSelector" in t or "podSelector" in t for t in rule["to"])  # no namespace or pod peers
    blocked = set(k8s.EGRESS_EXCEPT_CIDRS)
    assert {"169.254.0.0/16", "10.0.0.0/8", "100.64.0.0/10"} <= blocked
    assert set(rule["to"][0]["ipBlock"]["except"]) == blocked
    assert [t["ipBlock"]["cidr"] for t in rule["to"][1:]] == ["10.2.2.2/32"]  # only the bucket endpoint, never a range


def test_gate_off_renders_exactly_the_old_manifests():
    """BACKUP_KEY_SCOPED=0: no volume_backup input, so policies, quota and everything else are byte for byte what they were."""
    kw = dict(app_id=7, image="ghcr.io/example/demo:1.0", port=8080, hostname="x.example.invalid", mysql_cidr=None, env={"A": "b"}, volume={"size_gi": 1, "mount_path": "/data"})
    base = k8s.render_app_manifests(**kw)
    off = k8s.render_app_manifests(**kw, volume_backup=None)
    assert json.dumps(base, sort_keys=True) == json.dumps(off, sort_keys=True)
    flat = json.dumps(base)
    assert "allow-egress-backup" not in flat and "picopaas.io/backup" not in flat
    assert not [m for m in k8s.flatten_manifests(base) if m["kind"] in ("Job",)]


def test_gate_off_creates_no_tenant_job_or_secret_and_no_extra_quota(on, vols, user, db_session, runner):
    from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
    from app.reconciler import reconcile_all

    aid = _volume_app(user, db_session, with_db=False)  # no `scoped` fixture: BACKUP_KEY_SCOPED=0
    user.post(f"/apps/{aid}/backups")
    backups.tick(db_session, runner, NOW)
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    assert not [s for s in runner.started if s.namespace is not None or s.volume is not None]
    assert not any(n.startswith("bk-") and runner.secrets.get(n, {}).get("KIND") == "volume" for n in runner.secrets)
    kube = FakeKubeProvider()
    reconcile_all(db_session, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=FakeObjectStore(), settings={})
    applied = kube.applied.get(f"app-{aid}", [])
    assert not [m for m in applied if m["kind"] in ("Job", "Secret") and m["metadata"]["name"].startswith("bk-")]
    assert next(m for m in applied if m["kind"] == "ResourceQuota")["spec"]["hard"]["pods"] == "2"
    assert "picopaas.io/backup" not in json.dumps(applied)


def test_volume_job_secret_carries_only_the_backup_key_and_this_apps_repo_password(app_ctx, user, db_session, runner):
    user.post(f"/apps/{app_ctx}/backups")
    backups.tick(db_session, runner, NOW)
    for s in list(runner.started):
        runner.finish(s.name)
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    spec = next(s for s in runner.started if s.kind == "volume")
    assert set(spec.env) <= {"RESTIC_REPOSITORY", "RESTIC_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION",
                             "LIMIT_UPLOAD_KIB", "MODE", "KIND", "KEEP_DAILY", "KEEP_WEEKLY", "KEEP_MONTHLY", "TAG", "VOLUME_DIR"}
    assert spec.env["RESTIC_REPOSITORY"].endswith(f"/apps/{app_ctx}/volume")
    _, job = __import__("app.providers.backup", fromlist=["job_manifests"]).job_manifests(spec)
    c = job["spec"]["template"]["spec"]["containers"][0]
    assert c["envFrom"] == [{"secretRef": {"name": spec.name}}]
    assert [e["name"] for e in c.get("env", [])] == ["GOMEMLIMIT"]  # no credential is ever put inline in the Job spec


# --- set APIs: IDOR and scope --------------------------------------------------------------------------------------

def test_set_of_another_app_is_a_404_even_for_the_same_owner(app_ctx, user, db_session):
    other_app = user.post("/apps", json={"name": "second", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}).json()["id"]
    sid, _ = _mk_set(db_session, app_ctx, {"volume": "succeeded"})
    assert user.post(f"/apps/{other_app}/backups/sets/{sid}/download").status_code == 404
    assert user.delete(f"/apps/{other_app}/backups/sets/{sid}").status_code == 404
    assert user.post(f"/apps/{app_ctx}/backups/sets/999999/download").status_code == 404


def test_other_users_get_404_and_nothing_is_queued_or_audited(app_ctx, user, other, db_session):
    sid, _ = _mk_set(db_session, app_ctx, {"volume": "succeeded", "db": "succeeded"})
    assert other.post(f"/apps/{app_ctx}/backups/sets/{sid}/download").status_code == 404
    assert other.delete(f"/apps/{app_ctx}/backups/sets/{sid}").status_code == 404
    db_session.expire_all()
    assert db_session.query(models.BackupRun).filter_by(trigger="export").count() == 0
    assert db_session.query(models.BackupRun).filter_by(trigger="delete").count() == 0
    assert not rows(db_session, "backup.download") and not rows(db_session, "backup.delete")


def test_anonymous_gets_401_on_the_set_routes(app_ctx, make_client):
    anon = make_client()
    assert anon.post(f"/apps/{app_ctx}/backups/sets/1/download").status_code in (401, 403)
    assert anon.delete(f"/apps/{app_ctx}/backups/sets/1").status_code in (401, 403)


def test_set_download_and_delete_are_audited_with_the_set_id(app_ctx, user, db_session, runner):
    sid, _ = _mk_set(db_session, app_ctx, {"volume": "succeeded"})
    assert user.post(f"/apps/{app_ctx}/backups/sets/{sid}/download").status_code == 202
    assert f"set={sid}" in rows(db_session, "backup.download")[0].detail
    st2, _ = _mk_set(db_session, app_ctx, {"db": "succeeded"}, created=NOW - timedelta(days=1))
    assert user.delete(f"/apps/{app_ctx}/backups/sets/{st2}").status_code == 202
    assert f"set={st2}" in rows(db_session, "backup.delete")[0].detail


# --- deleting one part must also take away the package that holds it ----------------------------------------------

def _ready_package(db, aid, sid, runner):
    run = models.BackupRun(app_id=aid, kind="package", trigger="export", status="succeeded", created_at=NOW, finished_at=NOW,
                           set_id=sid, export_key=f"exports/{aid}/900.tar.gz")
    db.add(run)
    db.commit()
    return run.id


def test_deleting_one_part_expires_the_ready_package_of_its_set(app_ctx, user, db_session, runner, monkeypatch):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sid, ids = _mk_set(db_session, app_ctx, {"volume": "succeeded", "db": "succeeded"}, created=now)
    pid = _ready_package(db_session, app_ctx, sid, runner)
    run = db_session.get(models.BackupRun, pid)
    run.finished_at = now
    db_session.commit()
    assert user.delete(f"/apps/{app_ctx}/backups/{ids['volume']}").status_code == 202
    db_session.expire_all()
    assert runner.deleted_exports == [f"exports/{app_ctx}/900.tar.gz"]
    assert db_session.get(models.BackupRun, pid).status == "expired"


def test_deleting_one_part_waits_for_a_package_being_prepared(app_ctx, user, db_session):
    sid, ids = _mk_set(db_session, app_ctx, {"volume": "succeeded", "db": "succeeded"})
    db_session.add(models.BackupRun(app_id=app_ctx, kind="package", trigger="export", status="running", created_at=NOW, set_id=sid))
    db_session.commit()
    r = user.delete(f"/apps/{app_ctx}/backups/{ids['volume']}")
    assert r.status_code == 409
    db_session.expire_all()
    assert db_session.get(models.BackupRun, ids["volume"]).status == "succeeded"


# --- the script: package and volume export -------------------------------------------------------------------------

def _pkg_env(tmp_path, restic_body):
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq not installed")
    bindir = tmp_path / "bin"
    bindir.mkdir()

    def stub(name, body):
        p = bindir / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    (bindir / "jq").symlink_to(jq)
    if not shutil.which("sha256sum"):
        stub("sha256sum", 'exec shasum -a 256 "$@"\n')
    stub("restic", restic_body)
    work, scratch = tmp_path / "work", tmp_path / "tmp"
    work.mkdir()
    scratch.mkdir()
    out = tmp_path / "out.tar.gz"
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "MODE": "package", "KIND": "package", "RESTIC_PASSWORD": "not-the-password",
           "AWS_ACCESS_KEY_ID": "backup-key-id", "AWS_SECRET_ACCESS_KEY": "not-the-backup-secret",
           "REPO_BASE": "s3:x/apps/1", "PARTS": "volume=abcd1234", "EXPORT_ENDPOINT": "https://s3.example",
           "EXPORT_BUCKET": "b", "EXPORT_KEY": "exports/1/2.tar.gz", "EXPORT_TO_FILE": str(out),
           "WORK_DIR": str(work), "TMP_DIR": str(scratch), "APP_NAME": "blog", "SET_ID": "3",
           "COPYFILE_DISABLE": "1"}  # macOS bsdtar would add ._ members
    return env, out


def _run(env, tmp_path):
    script = os.path.join(os.path.dirname(__file__), "..", "images", "backup", "picopaas-backup.sh")
    return subprocess.run(["sh", script], env=env, capture_output=True, text=True, cwd=tmp_path)


def test_package_refuses_a_volume_dump_that_died_half_way(tmp_path):
    """restic dump exits non-zero after some output: gzip would still produce a valid-looking, truncated volume.tar.gz."""
    env, out = _pkg_env(tmp_path, 'for a in "$@"; do case "$a" in stats) echo \'{"total_size":1}\'; exit 0;; dump) printf partial-tar; exit 1;; esac; done\nexit 0\n')
    r = _run(env, tmp_path)
    assert r.returncode == 1 and "PICOPAAS_ERROR reading the volume snapshot failed" in r.stdout
    assert not out.exists()


def test_package_refuses_a_volume_bigger_than_the_download_cap(tmp_path):
    env, out = _pkg_env(tmp_path, 'for a in "$@"; do case "$a" in stats) echo \'{"total_size":5000}\'; exit 0;; esac; done\nexit 0\n')
    env["MAX_VOLUME_BYTES"] = "1000"
    r = _run(env, tmp_path)
    assert r.returncode == 1 and "too big to download" in r.stdout and not out.exists()


@pytest.mark.parametrize("name", ['evil"name', 'a\nb', '"},{"x":1', "back\\slash", "$(touch pwned)", "`id`", "é x"])
def test_package_manifest_is_valid_json_whatever_the_app_is_called(tmp_path, name):
    env, out = _pkg_env(tmp_path, 'for a in "$@"; do case "$a" in stats) echo \'{"total_size":1}\'; exit 0;; dump) printf data; exit 0;; esac; done\nexit 0\n')
    env["APP_NAME"] = name
    r = _run(env, tmp_path)
    assert r.returncode == 0, r.stdout
    with tarfile.open(out) as t:
        assert sorted(t.getnames()) == ["MANIFEST.json", "volume.tar.gz"]  # fixed member names only
        for m in t.getmembers():
            assert m.isreg() and "/" not in m.name and not m.name.startswith(".")
        manifest = json.load(t.extractfile("MANIFEST.json"))
    assert manifest["app"] == name and manifest["set_id"] == 3 and [p["kind"] for p in manifest["parts"]] == ["volume"]
    assert not (tmp_path / "pwned").exists()


@pytest.mark.parametrize("parts", ["volume=abc;touch pwned", "volume=../x", "volume=abc,db=$(id)", "other=abcd", "volume="])
def test_package_rejects_hostile_part_specs(tmp_path, parts):
    env, out = _pkg_env(tmp_path, 'exit 0\n')
    env["PARTS"] = parts
    r = _run(env, tmp_path)
    assert r.returncode == 1 and "PICOPAAS_ERROR" in r.stdout and not out.exists()
    assert not (tmp_path / "pwned").exists()
