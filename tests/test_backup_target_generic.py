"""Backup egress port, S3 addressing style, endpoint fallback; restic against a real Garage (skipped without it)."""
import json
import os
import shutil
import subprocess
import uuid

import boto3
import httpx
import pytest
from botocore.config import Config

from app import config, k8s_manifests
from app.providers import backup as pb
from app.providers import cloud
from app.providers.base import endpoint_port
from app.providers.digitalocean.backup_target import DoBackupTarget
from app.providers.generic.backup_target import GenericBackupTarget

REAL_ENV = ("GARAGE_TEST_ADMIN_URL", "GARAGE_TEST_ADMIN_TOKEN", "GARAGE_TEST_S3_ENDPOINT",
            "GARAGE_TEST_ACCESS_KEY_ID", "GARAGE_TEST_SECRET_ACCESS_KEY")
HAVE_GARAGE = all(os.environ.get(n) for n in REAL_ENV)


@pytest.mark.parametrize("url,port", [
    ("https://atl1.digitaloceanspaces.com", 443), ("http://garage:3900", 3900), ("http://garage", 80),
    ("https://s3.example.test:9000/", 9000), ("garage.svc:3900", 3900), ("", 443)])
def test_endpoint_port(url, port):
    assert endpoint_port(url) == port


def test_do_target_is_443_and_virtual_host():
    t = DoBackupTarget()
    assert t.egress_port() == 443 and t.s3_addressing_style() == "virtual"


def test_generic_target_uses_endpoint_port_and_path_style(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_ENDPOINT", "http://garage.test:3900")
    t = GenericBackupTarget()
    assert t.egress_port() == 3900 and t.s3_addressing_style() == "path"


def test_backup_endpoint_falls_back_to_object_store_only_for_generic(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_ENDPOINT", "")
    monkeypatch.setattr(config, "BACKUP_REGION", "atl1")
    monkeypatch.setenv("OBJECT_STORE_ENDPOINT", "http://garage.test:3900/")
    assert config.backup_endpoint() == "https://atl1.digitaloceanspaces.com"  # DO unchanged
    monkeypatch.setattr(config, "CLOUD_PROVIDER", "generic")
    assert config.backup_endpoint() == "http://garage.test:3900"
    monkeypatch.setattr(config, "BACKUP_ENDPOINT", "https://explicit.test/")
    assert config.backup_endpoint() == "https://explicit.test"


def test_backup_s3_region(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_REGION", "")
    assert config.backup_s3_region() == "us-east-1"
    monkeypatch.setattr(config, "CLOUD_PROVIDER", "generic")
    monkeypatch.delenv("OBJECT_STORE_REGION", raising=False)
    assert config.backup_s3_region() == "garage"
    monkeypatch.setenv("OBJECT_STORE_REGION", "eu-test")
    assert config.backup_s3_region() == "eu-test"
    monkeypatch.setattr(config, "BACKUP_REGION", "atl1")
    assert config.backup_s3_region() == "atl1"


def _runner(monkeypatch, ips):
    applied = []

    class Kube:
        def apply(self, manifests):
            applied.extend(manifests)

    monkeypatch.setattr("app.reconciler._private_storage_ips", lambda endpoint: ips)
    monkeypatch.setattr(pb, "_egress_applied", [])
    r = pb.KubeBackupRunner(dynamic_client=object(), core_api=object())
    r._kube = Kube()
    return r, applied


def test_runner_policy_opens_the_endpoint_port(monkeypatch):
    r, applied = _runner(monkeypatch, ["10.0.0.5"])
    r.ensure_spaces_egress("http://garage.test:3900")
    r.ensure_spaces_egress("http://garage.test:3900")
    assert len(applied) == 1
    assert applied[0]["spec"]["egress"][0]["ports"] == [{"protocol": "TCP", "port": 3900}]
    r.ensure_spaces_egress("https://garage.test")  # same IPs, new port: re-applied
    assert len(applied) == 2 and applied[1]["spec"]["egress"][0]["ports"][0]["port"] == 443


def test_runner_s3_client_addressing_follows_target(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_ENDPOINT", "http://garage.test:3900")
    r = pb.KubeBackupRunner(dynamic_client=object(), core_api=object())
    assert r._s3().meta.config.s3["addressing_style"] == "virtual"  # DO default
    monkeypatch.setattr(config, "CLOUD_PROVIDER", "generic")
    assert r._s3().meta.config.s3["addressing_style"] == "path"


def test_do_volume_backup_policy_is_unchanged_default():
    pols = k8s_manifests.network_policies(7, 8080, None, backup_ips=["10.1.1.1"])
    pol = next(p for p in pols if p["metadata"]["name"] == k8s_manifests.BACKUP_EGRESS_POLICY)
    assert len(pol["spec"]["egress"]) == 1
    assert pol["spec"]["egress"][0]["ports"] == [{"protocol": "TCP", "port": 443}]
    assert {"ipBlock": {"cidr": "10.1.1.1/32"}} in pol["spec"]["egress"][0]["to"]
    same = k8s_manifests.network_policies(7, 8080, None, backup_ips=["10.1.1.1"], backup_port=443, storage_port=443)
    assert same == pols


def test_generic_volume_backup_policy_adds_the_port_for_endpoint_ips_only():
    pols = k8s_manifests.network_policies(7, 8080, None, storage_ips=["10.1.1.2"], backup_ips=["10.1.1.1"],
                                          backup_port=3900, storage_port=3900)
    pol = next(p for p in pols if p["metadata"]["name"] == k8s_manifests.BACKUP_EGRESS_POLICY)
    assert pol["spec"]["egress"][0]["ports"] == [{"protocol": "TCP", "port": 443}]
    assert pol["spec"]["egress"][1] == {"to": [{"ipBlock": {"cidr": "10.1.1.1/32"}}],
                                        "ports": [{"protocol": "TCP", "port": 3900}]}
    storage = next(p for p in pols if p["metadata"]["name"] == "allow-egress-storage")
    assert storage["spec"]["egress"][0]["ports"] == [{"protocol": "TCP", "port": 3900}]


# --- restic against a real Garage ------------------------------------------------------------------------

def _restic(args, env, container, stdin=None):
    """Local restic if installed, else the restic docker image sharing Garage's network namespace (127.0.0.1:3900)."""
    if shutil.which("restic"):
        return subprocess.run(["restic", *args], env={**os.environ, **env}, capture_output=True, text=True, timeout=180, input=stdin)
    flags = [x for k, v in env.items() for x in ("-e", f"{k}={v}")]
    return subprocess.run(["docker", "run", "--rm", "-i", "--network", f"container:{container}", *flags,
                           "--entrypoint", "restic", "restic/restic", *args],
                          capture_output=True, text=True, timeout=180, input=stdin)


@pytest.mark.skipif(not HAVE_GARAGE, reason="set GARAGE_TEST_* (scripts/dev/garage-up.sh)")
def test_restic_init_backup_snapshots_and_export_presign_on_garage(monkeypatch):
    dump = "select 1;\n" * 100  # streamed on stdin: no bind mount needed (and none works on some Docker Desktops)
    container = os.environ.get("GARAGE_TEST_CONTAINER", "picopaas-garage")
    if not shutil.which("restic") and not shutil.which("docker"):
        pytest.skip("neither restic nor docker available")
    admin, tok = os.environ["GARAGE_TEST_ADMIN_URL"], {"Authorization": f"Bearer {os.environ['GARAGE_TEST_ADMIN_TOKEN']}"}
    key_id, secret = os.environ["GARAGE_TEST_ACCESS_KEY_ID"], os.environ["GARAGE_TEST_SECRET_ACCESS_KEY"]
    endpoint = os.environ["GARAGE_TEST_S3_ENDPOINT"]
    bucket = f"pp-bk-{uuid.uuid4().hex[:8]}"
    bid = httpx.post(f"{admin}/v2/CreateBucket", headers=tok, json={"globalAlias": bucket}).json()["id"]
    httpx.post(f"{admin}/v2/AllowBucketKey", headers=tok, json={
        "bucketId": bid, "accessKeyId": key_id, "permissions": {"read": True, "write": True}}).raise_for_status()
    s3 = boto3.client("s3", region_name="garage", endpoint_url=endpoint, aws_access_key_id=key_id,
                      aws_secret_access_key=secret, config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    monkeypatch.setattr(config, "CLOUD_PROVIDER", "generic")
    monkeypatch.setattr(config, "BACKUP_ENDPOINT", endpoint)
    monkeypatch.setattr(config, "BACKUP_BUCKET", bucket)
    monkeypatch.setattr(config, "BACKUP_PREFIX", "pp/")
    monkeypatch.setattr(config, "BACKUP_ACCESS_KEY_ID", key_id)
    monkeypatch.setattr(config, "BACKUP_SECRET_ACCESS_KEY", secret)
    try:
        repo = cloud.backup_target().repository_url("apps/9/db")
        assert repo == f"s3:{endpoint}/{bucket}/pp/apps/9/db"
        env = {"AWS_ACCESS_KEY_ID": key_id, "AWS_SECRET_ACCESS_KEY": secret, "AWS_DEFAULT_REGION": "garage",
               "RESTIC_PASSWORD": "not-the-password", "RESTIC_REPOSITORY": repo}
        for args in (["init"], ["backup", "--stdin", "--stdin-filename", "dump.sql"]):
            r = _restic(args, env, container, dump)
            if r.returncode and "docker" in r.stderr.lower() and "restic" not in r.stdout:
                pytest.skip(f"cannot run restic: {r.stderr[:200]}")
            assert r.returncode == 0, r.stderr[-500:]
        snaps = _restic(["snapshots", "--json"], env, container)
        assert snaps.returncode == 0, snaps.stderr[-500:]
        assert len(json.loads(snaps.stdout)) == 1

        # export presign through the runner's own client (path-style on generic), then a real GET of that object
        s3.put_object(Bucket=bucket, Key="exports/x.txt", Body=b"export-bytes")
        url = pb.KubeBackupRunner(dynamic_client=object(), core_api=object()).presign_export("exports/x.txt", 120, "x.txt")
        assert url.startswith(f"{endpoint}/{bucket}/exports/x.txt?")
        resp = httpx.get(url)
        assert resp.status_code == 200 and resp.content == b"export-bytes"
        assert "attachment" in resp.headers.get("content-disposition", "")
    finally:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objs, "Quiet": True})
        httpx.post(f"{admin}/v2/DeleteBucket", headers=tok, params={"id": bid})
