"""Security regression tests for the backups feature (docs/PENTEST.md, "Backups end-to-end review").

Each test pins one finding or one verified-safe property: tenant isolation of runs/exports, download names, export
prefix safety, presigned URL scope, hostile bucket keys in the backup script, error text and audit trail.
Self-contained on purpose (own fixtures); nothing here runs restic or touches the network."""
import os
import shutil
import stat
import subprocess
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from app import backups, config, crypto, models
from app.providers.backup import BackupRunnerError, KubeBackupRunner
from app.providers.fake import FakeBackupRunner
from tests.conftest import register_and_login

APP = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}
NOW = datetime(2026, 10, 24, 12, 0, 0)


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(config, "BACKUPS_ENABLED", True)
    monkeypatch.setattr(config, "BACKUP_BUCKET", "picopaas-backups")
    monkeypatch.setattr(config, "BACKUP_REGION", "sfo3")
    monkeypatch.setattr(config, "BACKUP_ACCESS_KEY_ID", "backup-key-id")
    monkeypatch.setattr(config, "BACKUP_SECRET_ACCESS_KEY", "not-the-backup-secret")


@pytest.fixture()
def runner():
    return FakeBackupRunner()


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def alice(make_client, admin, db_session):
    c = make_client()
    register_and_login(c, "alice@example.com")
    db_session.query(models.User).filter_by(email="alice@example.com").one().app_limit = 5
    db_session.commit()
    return c


@pytest.fixture()
def bob(make_client, admin, db_session):
    c = make_client()
    register_and_login(c, "bob@example.com")
    db_session.query(models.User).filter_by(email="bob@example.com").one().app_limit = 5
    db_session.commit()
    return c


def _app(client, db, name="web", kinds=("db",)):
    r = client.post("/apps", json={**APP, "name": name, "want_db": "db" in kinds, "want_storage": "bucket" in kinds})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    if "db" in kinds:
        srv = db.query(models.DbServer).first()
        if srv is None:
            srv = models.DbServer(name="primary", engine="mysql", host="db.internal", port=25060, admin_user="admin",
                                  admin_password_encrypted=crypto.encrypt("not-the-password"))
            db.add(srv)
            db.commit()
        db.add(models.AppDatabase(app_id=aid, db_server_id=srv.id, db_name=f"app_{aid}", db_user=f"app_{aid}",
                                  password_encrypted=crypto.encrypt("not-the-db-password")))
    if "bucket" in kinds:
        db.add(models.AppStorage(app_id=aid, bucket=f"bkt-{aid}", endpoint="https://s3.example", region="atl1",
                                 access_key=f"AK{aid}", secret_key_encrypted=crypto.encrypt(f"not-the-bucket-secret-{aid}"),
                                 size_bytes=10))
    db.commit()
    return aid


def _succeeded(db, aid, kind="db", snap="aa11bb22"):
    backups.ensure_config(db, db.get(models.App, aid), NOW)
    cfg = db.get(models.AppBackupConfig, aid)
    cfg.repo_password_enc = cfg.repo_password_enc or crypto.encrypt("not-the-repo-password")
    cfg.next_backup_at = NOW + timedelta(days=30)
    run = models.BackupRun(app_id=aid, kind=kind, trigger="scheduled", status="succeeded", snapshot_id=snap,
                           created_at=NOW, finished_at=NOW)
    db.add(run)
    db.commit()
    return run.id


@pytest.fixture()
def wired(on, runner):
    from app.main import app as fastapi_app
    from app.routers.backups import get_backup_runner
    fastapi_app.dependency_overrides[get_backup_runner] = lambda: runner
    yield runner
    fastapi_app.dependency_overrides.pop(get_backup_runner, None)


def _finish_export(db, runner):
    backups.tick(db, runner, datetime.utcnow())
    runner.finish([s.name for s in runner.started][-1], {"exported": True, "bytes": 10})
    backups.tick(db, runner, datetime.utcnow())


# --- tenant isolation: runs, exports, restore ----------------------------------------------------

def test_other_tenants_runs_and_exports_are_unreachable(wired, alice, bob, admin, db_session):
    a = _app(alice, db_session, "a-app")
    b = _app(bob, db_session, "b-app")
    run_a, run_b = _succeeded(db_session, a), _succeeded(db_session, b)
    ex_a = alice.post(f"/apps/{a}/backups/{run_a}/download").json()["export_id"]
    # bob cannot use alice's app path at all, and cannot reach her run or export through his own app path either
    assert bob.post(f"/apps/{a}/backups/{run_a}/download").status_code == 404
    assert bob.get(f"/apps/{a}/backups/exports/{ex_a}").status_code == 404
    assert bob.get(f"/apps/{b}/backups/exports/{ex_a}").status_code == 404  # export id under another app id
    assert bob.post(f"/apps/{b}/backups/{run_a}/download").status_code == 404  # run id of another app
    assert alice.post(f"/apps/{a}/backups/{run_b}/download").status_code == 404
    for m, p in (("get", f"/apps/{a}/backups"), ("post", f"/apps/{a}/backups"), ("patch", f"/apps/{a}/backups")):
        assert getattr(bob, m)(p, **({} if m == "get" else {"json": {}})).status_code == 404
    # no existence leak: a run that does not exist answers like one that belongs to someone else
    assert alice.post(f"/apps/{a}/backups/999999/download").status_code == 404
    assert alice.get(f"/apps/{a}/backups/exports/999999").status_code == 404
    # nothing was queued for bob's attempts
    assert db_session.query(models.BackupRun).filter_by(trigger="export").count() == 1


def test_export_endpoint_only_serves_export_rows_never_other_run_types(wired, alice, db_session):
    a = _app(alice, db_session)
    run = _succeeded(db_session, a)
    restore = models.BackupRun(app_id=a, kind="db", trigger="restore", status="succeeded", created_at=NOW, source_run_id=run)
    db_session.add(restore)
    db_session.commit()
    for rid in (run, restore.id):
        assert alice.get(f"/apps/{a}/backups/exports/{rid}").status_code == 404
    # a restore or export run cannot itself be exported (would read through another run's identity)
    assert alice.post(f"/apps/{a}/backups/{restore.id}/download").status_code == 422


def test_deleted_app_and_suspended_user_cannot_download(wired, alice, admin, db_session):
    a = _app(alice, db_session)
    run = _succeeded(db_session, a)
    ex = alice.post(f"/apps/{a}/backups/{run}/download").json()["export_id"]
    _finish_export(db_session, wired)
    assert alice.get(f"/apps/{a}/backups/exports/{ex}").json()["url"]
    u = db_session.query(models.User).filter_by(email="alice@example.com").one()
    u.status = "suspended"
    db_session.commit()
    assert alice.get(f"/apps/{a}/backups/exports/{ex}").status_code == 403
    assert alice.post(f"/apps/{a}/backups/{run}/download").status_code == 403
    u.status = "active"
    db_session.get(models.App, a).deleted_at = NOW
    db_session.commit()
    assert alice.get(f"/apps/{a}/backups/exports/{ex}").status_code == 404
    assert alice.post(f"/apps/{a}/backups/{run}/download").status_code == 404


def test_queued_export_of_app_deleted_meanwhile_never_starts(wired, alice, db_session):
    a = _app(alice, db_session)
    run = _succeeded(db_session, a)
    alice.post(f"/apps/{a}/backups/{run}/download")
    db_session.get(models.App, a).deleted_at = NOW
    db_session.commit()
    backups.tick(db_session, wired, datetime.utcnow())
    assert wired.started == []
    assert db_session.query(models.BackupRun).filter_by(trigger="export").one().status == "failed"


def test_anonymous_and_read_scope_keys(wired, alice, make_client, db_session):
    a = _app(alice, db_session)
    run = _succeeded(db_session, a)
    anon = make_client()
    assert anon.get(f"/apps/{a}/backups").status_code == 401
    assert anon.post(f"/apps/{a}/backups/{run}/download").status_code == 401
    assert anon.get(f"/apps/{a}/backups/exports/1").status_code == 401
    key = alice.post("/api/keys", json={"scope": "read"}).json()["key"]
    ro = make_client()
    ro.headers["Authorization"] = f"Bearer {key}"
    assert ro.get(f"/apps/{a}/backups").status_code == 200
    assert ro.post(f"/apps/{a}/backups/{run}/download").status_code == 403  # starting an export is a write
    assert ro.post(f"/apps/{a}/backups", json={}).status_code == 403
    assert ro.patch(f"/apps/{a}/backups", json={"enabled": True}).status_code == 403
    assert db_session.query(models.BackupRun).filter_by(trigger="export").count() == 0


def test_admin_endpoints_deny_tenants_and_validate_ids(wired, alice, admin, db_session):
    a = _app(alice, db_session)
    run = _succeeded(db_session, a)
    assert alice.post(f"/admin/backups/{run}/restore", json={"target_app_id": a}).status_code == 403
    assert alice.get("/admin/backups").status_code == 403
    assert admin.post("/admin/backups/999999/restore", json={"target_app_id": a}).status_code == 404
    assert admin.post(f"/admin/backups/{run}/restore", json={"target_app_id": 999999}).status_code == 404
    assert admin.post(f"/admin/backups/{run}/restore", json={"target_app_id": a, "x": 1}).status_code == 422  # extra=forbid
    assert db_session.query(models.BackupRun).filter_by(trigger="restore").count() == 0


def test_restore_never_overwrites_source_while_running_or_a_deleted_target(wired, alice, bob, admin, db_session):
    a = _app(alice, db_session, "a-app")
    b = _app(bob, db_session, "b-app")
    run = _succeeded(db_session, a)
    r = admin.post(f"/admin/backups/{run}/restore", json={"target_app_id": a})
    assert r.status_code == 409  # the source app is the default refusal
    r = admin.post(f"/admin/backups/{run}/restore", json={"target_app_id": a, "overwrite_source": True})
    assert r.status_code == 409  # ... and the flag alone is not enough: the app must be suspended
    db_session.get(models.App, b).deleted_at = NOW
    db_session.commit()
    assert admin.post(f"/admin/backups/{run}/restore", json={"target_app_id": b}).status_code == 404
    assert db_session.query(models.BackupRun).filter_by(trigger="restore").count() == 0


def test_restore_job_uses_target_credentials_for_target_only(wired, alice, bob, admin, db_session):
    a = _app(alice, db_session, "a-app", ("db", "bucket"))
    b = _app(bob, db_session, "b-app", ("db", "bucket"))
    run_db, run_bk = _succeeded(db_session, a, "db"), _succeeded(db_session, a, "bucket", "bb22cc33")
    for rid in (run_db, run_bk):
        assert admin.post(f"/admin/backups/{rid}/restore", json={"target_app_id": b}).status_code == 202
        backups.tick(db_session, wired, NOW)
        spec = wired.started[-1]
        wired.finish(spec.name, {"restored": True})
        backups.tick(db_session, wired, NOW)
        assert spec.app_id == b and spec.env["MODE"] == "restore"
        assert spec.env["RESTIC_REPOSITORY"].endswith(f"/apps/{a}/{spec.kind}")  # reads A's repo ...
        joined = " ".join(spec.env.values())
        assert f"app_{b}" in joined or f"bkt-{b}" in joined  # ... writes into B only
        assert f"app_{a}" not in joined.replace(f"/apps/{a}/", "") and f"bkt-{a}" not in joined
        assert "not-the-bucket-secret-%d" % a not in joined and "not-the-db-password" not in "".join(
            v for k, v in spec.env.items() if k != "DB_PASSWORD")


# --- download names (Content-Disposition, CLI local path) -----------------------------------------

@pytest.mark.parametrize("name", ['a"b;c', "../../etc/cron.d/x", "x\r\nSet-Cookie: y=1", "ünï/cødé", "..", "a b"])
def test_export_filename_is_a_safe_ascii_basename(wired, alice, db_session, name):
    a = _app(alice, db_session, name)
    run = _succeeded(db_session, a)
    ex = alice.post(f"/apps/{a}/backups/{run}/download").json()["export_id"]
    _finish_export(db_session, wired)
    got = alice.get(f"/apps/{a}/backups/exports/{ex}").json()
    fn = got["filename"]
    assert fn.isascii() and all(c.isalnum() or c in "._-" for c in fn), fn
    assert not fn.startswith(".") and "/" not in fn and ".." not in fn.replace(".sql.gz", "")
    assert wired.presigned[-1][2] == fn  # the same sanitised name goes into the signed Content-Disposition
    assert fn.endswith(f"-db-backup{run}.sql.gz")


def test_cli_ignores_path_components_in_server_filename():
    src = open(os.path.join(os.path.dirname(__file__), "..", "cli", "picopaas.py")).read()
    assert 'os.path.basename(e.get("filename") or "")' in src


# --- export staging prefix vs repositories --------------------------------------------------------

@pytest.mark.parametrize("repo_prefix,export_prefix,ok", [
    ("backups/", "exports/", True), ("", "exports/", True), ("exports/", "exports/", False),
    ("backups/", "", False), ("backups/", "backups/", False), ("backups/", "backups/apps/", False),
    ("", "apps/", False), ("backups/", "/exports/", False), ("backups/", "exports", False)])
def test_export_prefix_must_not_overlap_repositories(monkeypatch, repo_prefix, export_prefix, ok):
    monkeypatch.setattr(config, "BACKUP_PREFIX", repo_prefix)
    monkeypatch.setattr(config, "BACKUP_EXPORT_PREFIX", export_prefix)
    assert config.backup_export_prefix_ok() is ok


def test_unsafe_prefix_blocks_sweep_and_new_downloads(wired, alice, db_session, monkeypatch):
    a = _app(alice, db_session)
    run = _succeeded(db_session, a)
    monkeypatch.setattr(config, "BACKUP_PREFIX", "exports/")  # misconfiguration: repos under the swept prefix
    assert alice.post(f"/apps/{a}/backups/{run}/download").status_code == 503
    deleted = []

    class S3:
        def get_paginator(self, _):
            raise AssertionError("must not even list")

        def delete_objects(self, **kw):
            deleted.append(kw)

    r = KubeBackupRunner(dynamic_client=object(), core_api=object())
    monkeypatch.setattr(r, "_s3", lambda: S3())
    with pytest.raises(BackupRunnerError):
        r.sweep_exports(3600)
    assert deleted == []


def test_sweep_only_deletes_old_objects_under_exports(monkeypatch):
    from datetime import timezone
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    new = datetime.now(timezone.utc)
    seen, deleted = [], []

    class Pager:
        def paginate(self, **kw):
            seen.append(kw)
            return [{"Contents": [{"Key": "exports/1/1.sql.gz", "LastModified": old},
                                  {"Key": "exports/1/2.sql.gz", "LastModified": new}]}]

    class S3:
        def get_paginator(self, _):
            return Pager()

        def delete_objects(self, **kw):
            deleted.extend(o["Key"] for o in kw["Delete"]["Objects"])

    monkeypatch.setattr(config, "BACKUP_BUCKET", "picopaas-backups")
    r = KubeBackupRunner(dynamic_client=object(), core_api=object())
    monkeypatch.setattr(r, "_s3", lambda: S3())
    assert r.sweep_exports(3600) == 1
    assert seen[0]["Prefix"] == "exports/" and deleted == ["exports/1/1.sql.gz"]


# --- presigned URL scope ---------------------------------------------------------------------------

def test_presigned_url_is_one_key_get_only_and_short_lived(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_BUCKET", "picopaas-backups")
    monkeypatch.setattr(config, "BACKUP_REGION", "sfo3")
    monkeypatch.setattr(config, "BACKUP_ACCESS_KEY_ID", "backup-key-id")
    monkeypatch.setattr(config, "BACKUP_SECRET_ACCESS_KEY", "not-the-backup-secret")
    r = KubeBackupRunner(dynamic_client=object(), core_api=object())
    url = r.presign_export("exports/7/9.sql.gz", config.BACKUP_EXPORT_URL_SECONDS, "web-db-backup3.sql.gz")
    u = urlparse(url)
    q = parse_qs(u.query)
    assert u.scheme == "https" and u.path == "/exports/7/9.sql.gz"  # exactly one object, no bucket-level (list) URL
    assert u.hostname.startswith("picopaas-backups.")
    assert q["X-Amz-Expires"] == ["600"] and q["X-Amz-SignedHeaders"] == ["host"]  # <= 10 minutes
    assert q["response-content-disposition"] == ['attachment; filename="web-db-backup3.sql.gz"']
    assert "not-the-backup-secret" not in url
    assert "X-Amz-Security-Token" not in q


def test_export_object_key_is_built_from_integers_only(wired, alice, db_session):
    a = _app(alice, db_session, 'evil/../name"')
    run = _succeeded(db_session, a)
    ex = alice.post(f"/apps/{a}/backups/{run}/download").json()["export_id"]
    row = db_session.get(models.BackupRun, ex)
    assert row.export_key == f"exports/{a}/{ex}.sql.gz"
    backups.tick(db_session, wired, datetime.utcnow())
    env = wired.started[-1].env
    assert env["EXPORT_KEY"] == row.export_key and wired.started[-1].name == f"bk-{ex}"


# --- no leaks ----------------------------------------------------------------------------------------

def test_api_output_never_contains_repo_password_or_backup_key(wired, alice, db_session):
    a = _app(alice, db_session)
    run = _succeeded(db_session, a)
    ex = alice.post(f"/apps/{a}/backups/{run}/download")
    _finish_export(db_session, wired)
    bodies = [ex.text, alice.get(f"/apps/{a}/backups").text, alice.get(f"/apps/{a}/backups/exports/{ex.json()['export_id']}").text]
    for secret in ("not-the-repo-password", "not-the-backup-secret", "not-the-db-password", "backup-key-id"):
        assert all(secret not in b for b in bodies), secret
    assert "digitaloceanspaces" not in "".join(bodies) and "repo_password" not in "".join(bodies)


def test_unreadable_repo_password_error_is_generic(wired, alice, db_session):
    a = _app(alice, db_session)
    run = _succeeded(db_session, a)
    db_session.get(models.AppBackupConfig, a).repo_password_enc = "not-a-fernet-token"
    db_session.commit()
    ex = alice.post(f"/apps/{a}/backups/{run}/download").json()["export_id"]
    backups.tick(db_session, wired, datetime.utcnow())
    got = alice.get(f"/apps/{a}/backups/exports/{ex}").json()
    assert got["status"] == "failed" and got["error"] == "the stored backup password can't be read (platform key changed)"
    assert wired.started == []


def test_job_error_text_is_redacted_and_bounded(wired, alice, db_session):
    a = _app(alice, db_session)
    run = _succeeded(db_session, a)
    ex = alice.post(f"/apps/{a}/backups/{run}/download").json()["export_id"]
    backups.tick(db_session, wired, datetime.utcnow())
    wired.fail(wired.started[-1].name, "boom secret_key=abcd1234 " + "x" * 400)
    backups.tick(db_session, wired, datetime.utcnow())
    err = alice.get(f"/apps/{a}/backups/exports/{ex}").json()["error"]
    assert len(err) <= 255 and "abcd1234" not in err


def test_presign_failure_message_does_not_leak(wired, alice, db_session):
    a = _app(alice, db_session)
    run = _succeeded(db_session, a)
    ex = alice.post(f"/apps/{a}/backups/{run}/download").json()["export_id"]
    _finish_export(db_session, wired)
    wired.fail_presign = True
    r = alice.get(f"/apps/{a}/backups/exports/{ex}")
    assert r.status_code == 503 and "FakeError" not in r.text and "sfo3" not in r.text


# --- rate limits ---------------------------------------------------------------------------------------

def test_download_rate_limit_counts_failed_attempts_and_per_app(wired, alice, db_session):
    a = _app(alice, db_session, "one", ("db", "bucket"))
    runs = [_succeeded(db_session, a, "db", f"{i:08x}") for i in range(5)]
    seen = []
    for rid in runs:
        r = alice.post(f"/apps/{a}/backups/{rid}/download")
        seen.append(r.status_code)
        if r.status_code == 202:
            backups.tick(db_session, wired, datetime.utcnow())
            wired.fail(wired.started[-1].name, "failed")  # failures still count against the hourly budget
            backups.tick(db_session, wired, datetime.utcnow())
    assert seen == [202, 202, 202, 429, 429]
    assert alice.post(f"/apps/{a}/backups/{runs[3]}/download").headers["Retry-After"].isdigit()


def test_idempotent_download_does_not_consume_budget_or_audit_twice(wired, alice, db_session):
    a = _app(alice, db_session)
    run = _succeeded(db_session, a)
    ids = {alice.post(f"/apps/{a}/backups/{run}/download").json()["export_id"] for _ in range(6)}
    assert len(ids) == 1
    assert db_session.query(models.AuditLog).filter_by(action="backup.download").count() == 1


# --- audit -----------------------------------------------------------------------------------------------

def test_audit_trail_for_download_restore_and_config(wired, alice, admin, db_session):
    a = _app(alice, db_session, "a-app")
    b = _app(alice, db_session, "b-app")
    run = _succeeded(db_session, a)
    alice.post(f"/apps/{a}/backups/{run}/download")
    alice.patch(f"/apps/{a}/backups", json={"enabled": True})
    admin.post(f"/admin/backups/{run}/restore", json={"target_app_id": b})
    actions = {r.action: r.detail for r in db_session.query(models.AuditLog)}
    assert f"run={run}" in actions["backup.download"] and "export=" in actions["backup.download"]
    assert "enabled=True" in actions["backup.config_changed"]
    assert f"from_app={a}" in actions["backup.restore"] and f"from_run={run}" in actions["backup.restore"]
    assert "not-the-" not in "".join(actions.values())


# --- the Job: manifest, namespace policy ---------------------------------------------------------------------

def test_job_pod_is_locked_down_and_secret_only_via_envfrom():
    from app.providers.backup import BackupJobSpec, job_manifests
    secret, job = job_manifests(BackupJobSpec(name="bk-1", run_id=1, app_id=1, kind="db",
                                              env={"RESTIC_PASSWORD": "not-the-password"}, scratch_mib=64))
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False and pod["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert "args" not in c and "command" not in c and "env" not in c and "not-the-password" not in repr(job)
    assert not pod.get("hostNetwork") and not pod.get("hostPID") and pod["restartPolicy"] == "Never"


def test_backup_namespace_manifest_denies_by_default_and_limits_rbac():
    import yaml
    path = os.path.join(os.path.dirname(__file__), "..", "deploy", "platform", "backup.yaml")
    docs = [d for d in yaml.safe_load_all(open(path).read().replace("__DB_EGRESS_CIDR__", "10.0.0.0/16")
                                           .replace("__PG_EGRESS_CIDR__", "10.0.0.0/16")) if d]
    by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    deny = by[("NetworkPolicy", "default-deny")]["spec"]
    assert deny["policyTypes"] == ["Ingress", "Egress"] and deny["ingress"] == [] and deny["egress"] == []
    assert not [d for d in docs if d["kind"] == "NetworkPolicy" and "Ingress" in d["spec"].get("policyTypes", [])
                and d["metadata"]["name"] != "default-deny"]  # nothing opens ingress
    ns = by[("Namespace", "picopaas-backup")]["metadata"]["labels"]
    assert ns["pod-security.kubernetes.io/enforce"] == "restricted"
    role = by[("Role", "picopaas-backup-runner")]
    assert all(r["verbs"] and "*" not in r["verbs"] and "*" not in r["resources"] for r in role["rules"])
    assert not [d for d in docs if d["kind"] in ("ClusterRole", "ClusterRoleBinding")]
    bucket = by[("NetworkPolicy", "allow-egress-bucket")]["spec"]["egress"][0]
    assert bucket["ports"] == [{"protocol": "TCP", "port": 443}]
    assert {"10.0.0.0/8", "169.254.0.0/16", "192.168.0.0/16", "172.16.0.0/12"} <= set(bucket["to"][0]["ipBlock"]["except"])


# --- the script: hostile object names in a tenant bucket -----------------------------------------------------------

def _script_env(tmp_path, keys):
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq not installed")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"

    def stub(name, body):
        p = bindir / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    (bindir / "jq").symlink_to(jq)
    stub("restic", f'echo "restic $@" >> {calls}\nfor a in "$@"; do case "$a" in backup) echo \'{{"message_type":"summary","snapshot_id":"cafe0123456789","data_added":1}}\';; stats) echo \'{{"total_size":1}}\';; esac; done\nexit 0\n')
    listing = "\n".join('{"key":"s3://b/%s","size":1}' % k for k in keys)
    stub("s5cmd", f'echo "s5cmd $@" >> {calls}\n'
         f'for a in "$@"; do [ "$a" = ls ] && cat <<\'EOF\'\n{listing}\nEOF\n[ "$a" = ls ] && exit 0; done\n'
         f'for a in "$@"; do [ "$a" = cp ] && touch {tmp_path}/ESCAPED; done\nexit 0\n')
    work, scratch = tmp_path / "work", tmp_path / "tmp"
    work.mkdir()
    scratch.mkdir()
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "RESTIC_REPOSITORY": "s3:x/y", "RESTIC_PASSWORD": "not-the-password",
           "AWS_ACCESS_KEY_ID": "backup-key-id", "AWS_SECRET_ACCESS_KEY": "not-the-backup-secret", "KIND": "bucket",
           "MODE": "backup", "SRC_ENDPOINT": "https://s3.example", "SRC_BUCKET": "b", "SRC_REGION": "atl1",
           "SRC_ACCESS_KEY": "app-key-id", "SRC_SECRET_KEY": "not-the-app-secret", "TAG": "scheduled",
           "WORK_DIR": str(work), "TMP_DIR": str(scratch)}
    return env, calls


def _run(env, tmp_path):
    script = os.path.join(os.path.dirname(__file__), "..", "images", "backup", "picopaas-backup.sh")
    return subprocess.run(["sh", script], env=env, capture_output=True, text=True, cwd=tmp_path)


@pytest.mark.parametrize("key", ["../../tmp/x", "a/../../b", "a/b/..", "..", "x/../y"])
def test_script_refuses_bucket_with_dotdot_object_names_before_copying(tmp_path, key):
    env, calls = _script_env(tmp_path, ["ok.txt", key])
    r = _run(env, tmp_path)
    assert r.returncode == 1, r.stdout
    assert "PICOPAAS_ERROR the bucket has object names that can't be backed up safely" in r.stdout
    log = calls.read_text()
    assert " cp " not in log and " backup " not in log  # nothing copied, no snapshot
    assert not (tmp_path / "ESCAPED").exists()
    assert key not in r.stdout + r.stderr  # object names are not echoed into the run's log either


@pytest.mark.parametrize("key", ["a..b", "..hidden", "dir/..data", "v1.2..3/x", "normal/path.txt", "sp ace/é.txt"])
def test_script_still_backs_up_names_that_merely_contain_dots(tmp_path, key):
    env, calls = _script_env(tmp_path, [key])
    r = _run(env, tmp_path)
    assert "can't be backed up safely" not in r.stdout
    assert " cp " in calls.read_text()
