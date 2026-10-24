"""Backup Job runner: the one place that touches the cluster for backups (docs/design/backups.md).

`BackupRunner` is the small interface the scheduler logic (app/backups.py) depends on; `KubeBackupRunner` is the real
implementation (Jobs + ephemeral Secrets in ns picopaas-backup), `FakeBackupRunner` (providers/fake.py) is for tests.
The Job's pod prints its outcome as the LAST stdout line, `PICOPAAS_RESULT {json}` or `PICOPAAS_ERROR <fixed text>`
(images/backup/picopaas-backup.sh); the runner turns that into a `JobStatus`. Nothing secret is ever returned.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from app import config

log = logging.getLogger(__name__)

LABEL = "picopaas.io/backup"  # on every Job and Secret we create, so the orphan sweep only ever touches ours
RESULT_PREFIX = "PICOPAAS_RESULT "
ERROR_PREFIX = "PICOPAAS_ERROR "


_egress_applied: list[str] = []


@dataclass
class BackupJobSpec:
    name: str  # Job and Secret name, e.g. bk-12
    run_id: int
    app_id: int
    kind: str
    env: dict[str, str] = field(repr=False)  # includes credentials: never logged, only written to the per-run Secret
    scratch_mib: int = 0  # >0: mount an emptyDir of this size at /work (bucket backups and restores copy objects there)


@dataclass
class JobStatus:
    state: str  # running | succeeded | failed | missing
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None  # short fixed text from the script, or a generic reason


class BackupRunnerError(Exception):
    pass


class BackupRunner(ABC):
    @abstractmethod
    def start(self, spec: BackupJobSpec) -> None:
        """Create the per-run Secret, then the Job. Raises BackupRunnerError when it cannot."""

    @abstractmethod
    def status(self, job_name: str) -> JobStatus: ...

    @abstractmethod
    def cleanup(self, job_name: str) -> None:
        """Delete the Job (and its pods) and the Secret. Idempotent."""

    def ensure_spaces_egress(self, endpoint: str) -> None:
        """Allow the backup Jobs to reach the private-range IPs `endpoint` resolves to, on 443 (Spaces resolves to a private
        VPC IP inside DO, which the namespace's "public addresses only" rule excludes). Default: nothing to do (tests)."""

    def presign_export(self, key: str, expires_seconds: int, filename: str) -> str:
        """Presigned GET (as a download named `filename`) for an export object in the backup bucket, signed with the
        platform's backup key. Raises BackupRunnerError."""
        raise BackupRunnerError("downloads are not available")

    def sweep_exports(self, older_than_seconds: int) -> int:
        """Delete export objects older than `older_than_seconds`. Returns how many. Raises BackupRunnerError."""
        return 0

    @abstractmethod
    def sweep(self, active: set[str]) -> int:
        """Delete Secrets labelled picopaas.io/backup whose name is not in `active` (a crash between Job end and
        cleanup would leave one behind). Returns how many were deleted."""


def job_manifests(spec: BackupJobSpec) -> tuple[dict[str, Any], dict[str, Any]]:
    """(Secret, Job) for one run. Pure function; asserted on in tests."""
    ns = config.BACKUP_NAMESPACE
    labels = {LABEL: "1", "picopaas.io/backup-run": str(spec.run_id)}
    secret = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": spec.name, "namespace": ns, "labels": dict(labels)},
        "type": "Opaque",
        "stringData": dict(spec.env),
    }
    limits = {"cpu": "200m", "memory": "320Mi", "ephemeral-storage": "200Mi"}
    mounts = [{"name": "tmp", "mountPath": "/tmp"}]
    volumes: list[dict[str, Any]] = [{"name": "tmp", "emptyDir": {"sizeLimit": "200Mi"}}]
    if spec.scratch_mib:
        limits["ephemeral-storage"] = f"{200 + spec.scratch_mib}Mi"
        mounts.append({"name": "work", "mountPath": "/work"})
        volumes.append({"name": "work", "emptyDir": {"sizeLimit": f"{spec.scratch_mib}Mi"}})
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": spec.name, "namespace": ns, "labels": dict(labels)},
        "spec": {
            "backoffLimit": 0,  # a failed run is recorded and retried at the next slot, never silently re-run
            "activeDeadlineSeconds": config.BACKUP_JOB_DEADLINE_SECONDS,
            "ttlSecondsAfterFinished": 3600,  # safety net; the scheduler deletes finished Jobs itself
            "template": {
                "metadata": {"labels": dict(labels)},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "backup-job",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [{
                        "name": "backup",
                        "image": config.BACKUP_IMAGE,
                        "envFrom": [{"secretRef": {"name": spec.name}}],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "128Mi", "ephemeral-storage": "64Mi"},
                            "limits": limits,
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": mounts,
                    }],
                    "volumes": volumes,
                },
            },
        },
    }
    return secret, job


def parse_pod_output(lines: list[str]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """(result, error) from a pod's last log lines: the newest PICOPAAS_RESULT json / PICOPAAS_ERROR text."""
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    for line in lines:
        if line.startswith(RESULT_PREFIX):
            try:
                parsed = json.loads(line[len(RESULT_PREFIX):])
            except ValueError:
                continue
            result = parsed if isinstance(parsed, dict) else None
        elif line.startswith(ERROR_PREFIX):
            error = line[len(ERROR_PREFIX):].strip()
    return result, error


class KubeBackupRunner(BackupRunner):
    def __init__(self, dynamic_client: Any | None = None, core_api: Any | None = None) -> None:
        from app.providers.kube import KubeClientProvider

        self._kube = KubeClientProvider(dynamic_client, core_api)

    def _res(self, api_version: str, kind: str) -> Any:
        return self._kube.dyn.resources.get(api_version=api_version, kind=kind)

    def start(self, spec: BackupJobSpec) -> None:
        secret, job = job_manifests(spec)
        ns = config.BACKUP_NAMESPACE
        try:
            self._res("v1", "Secret").create(body=secret, namespace=ns)
            try:
                self._res("batch/v1", "Job").create(body=job, namespace=ns)
            except Exception:
                self._delete("v1", "Secret", spec.name)  # never leave credentials behind a Job that did not start
                raise
        except Exception as exc:  # noqa: BLE001 - the k8s error text can echo manifest content; keep only the type
            raise BackupRunnerError(f"could not create the backup job ({type(exc).__name__})") from None

    def status(self, job_name: str) -> JobStatus:
        from app.providers.kube import _as_dict
        from kubernetes.dynamic.exceptions import NotFoundError

        ns = config.BACKUP_NAMESPACE
        try:
            job = _as_dict(self._res("batch/v1", "Job").get(name=job_name, namespace=ns))
        except NotFoundError:
            return JobStatus("missing")
        st = job.get("status") or {}
        if st.get("succeeded"):
            result, _ = self._pod_output(job_name)
            return JobStatus("succeeded", result=result)
        failed_cond = any(c.get("type") == "Failed" and c.get("status") == "True" for c in st.get("conditions") or [])
        if failed_cond:
            _, error = self._pod_output(job_name)
            reason = next((c.get("reason") for c in st.get("conditions") or [] if c.get("type") == "Failed"), None)
            if not error:
                error = "the backup took too long and was stopped" if reason == "DeadlineExceeded" else "the backup job failed"
            return JobStatus("failed", error=error)
        return JobStatus("running")

    def _pod_output(self, job_name: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        ns = config.BACKUP_NAMESPACE
        try:
            pods = self._kube.core.list_namespaced_pod(ns, label_selector=f"job-name={job_name}").items
            pods = sorted(pods, key=lambda p: p.metadata.creation_timestamp, reverse=True)
            if not pods:
                return None, None
            text = self._kube.core.read_namespaced_pod_log(pods[0].metadata.name, ns, tail_lines=20)
        except Exception as exc:  # noqa: BLE001
            log.warning("backup %s: reading pod output failed (%s)", job_name, type(exc).__name__)
            return None, None
        return parse_pod_output(str(text).splitlines())

    def _delete(self, api_version: str, kind: str, name: str) -> None:
        from kubernetes.dynamic.exceptions import NotFoundError

        try:
            kw: dict[str, Any] = {"name": name, "namespace": config.BACKUP_NAMESPACE}
            if kind == "Job":
                kw["body"] = {"propagationPolicy": "Background"}
            self._res(api_version, kind).delete(**kw)
        except NotFoundError:
            pass

    def cleanup(self, job_name: str) -> None:
        errors = []
        for api_version, kind in (("batch/v1", "Job"), ("v1", "Secret")):
            try:
                self._delete(api_version, kind, job_name)
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)
        if errors:
            raise BackupRunnerError(f"cleanup of {job_name} failed ({', '.join(errors)})")

    def ensure_spaces_egress(self, endpoint: str) -> None:
        from app.reconciler import _private_storage_ips  # same resolution as the tenant allow-egress-storage policy

        ips = _private_storage_ips(endpoint)
        if not ips or ips == _egress_applied:  # the runner is rebuilt every tick: remember the last applied answer
            return
        policy = {
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "allow-egress-spaces", "namespace": config.BACKUP_NAMESPACE},
            "spec": {"podSelector": {}, "policyTypes": ["Egress"],
                     "egress": [{"to": [{"ipBlock": {"cidr": f"{ip}/32"}} for ip in sorted(set(ips))],
                                 "ports": [{"protocol": "TCP", "port": 443}]}]},
        }
        self._kube.apply([policy])
        _egress_applied[:] = ips

    def _s3(self) -> Any:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3", region_name=config.BACKUP_REGION or "us-east-1", endpoint_url=config.backup_endpoint(),
            aws_access_key_id=config.BACKUP_ACCESS_KEY_ID, aws_secret_access_key=config.BACKUP_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))

    def presign_export(self, key: str, expires_seconds: int, filename: str) -> str:
        try:
            return self._s3().generate_presigned_url(
                "get_object", ExpiresIn=expires_seconds, HttpMethod="GET",
                Params={"Bucket": config.BACKUP_BUCKET, "Key": key,
                        "ResponseContentDisposition": f'attachment; filename="{filename}"'})
        except Exception as exc:  # noqa: BLE001 - never echo the error text (it can contain the endpoint)
            raise BackupRunnerError(f"could not sign the download ({type(exc).__name__})") from None

    def sweep_exports(self, older_than_seconds: int) -> int:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        try:
            s3 = self._s3()
            old: list[str] = []
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=config.BACKUP_BUCKET, Prefix=config.BACKUP_EXPORT_PREFIX):
                old += [o["Key"] for o in page.get("Contents") or [] if o["LastModified"] < cutoff]
            for i in range(0, len(old), 500):
                s3.delete_objects(Bucket=config.BACKUP_BUCKET, Delete={"Objects": [{"Key": k} for k in old[i:i + 500]], "Quiet": True})
            return len(old)
        except Exception as exc:  # noqa: BLE001
            raise BackupRunnerError(f"could not sweep exports ({type(exc).__name__})") from None

    def sweep(self, active: set[str]) -> int:
        from app.providers.kube import _as_dict

        res = self._res("v1", "Secret")
        items = _as_dict(res.get(namespace=config.BACKUP_NAMESPACE, label_selector=LABEL)).get("items", [])
        n = 0
        for it in items:
            name = (it.get("metadata") or {}).get("name")
            if name and name not in active:
                self._delete("v1", "Secret", name)
                n += 1
        return n
