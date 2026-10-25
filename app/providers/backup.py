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
    namespace: Optional[str] = None  # None = the platform backup namespace; a volume backup runs in the app's own namespace
    volume: Optional[dict[str, Any]] = None  # volume backup only, see volume_job_spec() in app/backups.py
    deadline_seconds: Optional[int] = None  # None = config.BACKUP_JOB_DEADLINE_SECONDS (a volume "stat" Job is much shorter)


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
    def status(self, job_name: str, namespace: Optional[str] = None) -> JobStatus: ...

    @abstractmethod
    def cleanup(self, job_name: str, namespace: Optional[str] = None) -> None:
        """Delete the Job (and its pods) and the Secret. Idempotent. `namespace` None = the platform backup namespace."""

    def ensure_spaces_egress(self, endpoint: str) -> None:
        """Allow the backup Jobs to reach the private-range IPs `endpoint` resolves to, on 443 (Spaces resolves to a private
        VPC IP inside DO, which the namespace's "public addresses only" rule excludes). Default: nothing to do (tests)."""

    def presign_export(self, key: str, expires_seconds: int, filename: str) -> str:
        """Presigned GET (as a download named `filename`) for an export object in the backup bucket, signed with the
        platform's backup key. Raises BackupRunnerError."""
        raise BackupRunnerError("downloads are not available")

    def sweep_exports(self, older_than_seconds: int) -> list[str]:
        """Delete export objects older than `older_than_seconds`. Returns the deleted keys. Raises BackupRunnerError."""
        return []

    def delete_exports(self, keys: list[str]) -> None:
        """Delete these export objects now (a backup is being deleted). Raises BackupRunnerError. Default: nothing to do."""

    @abstractmethod
    def sweep(self, active: set[str]) -> int:
        """Delete Secrets labelled picopaas.io/backup whose name is not in `active` (a crash between Job end and
        cleanup would leave one behind). Returns how many were deleted."""

    def sweep_namespaces(self, namespaces: list[str], active: set[str]) -> int:
        """The same sweep inside these (app) namespaces, where volume backups keep their per-run Secret. Default: nothing."""
        return 0

    def sweep_orphans(self, namespaces: list[str], active: set[str], min_age_seconds: int, limit: int) -> dict[str, int]:
        """Delete labelled (picopaas.io/backup) Jobs, pods and Secrets in these namespaces whose name/Job is not in `active` AND
        that are older than `min_age_seconds` (a Job being started has no committed row yet). At most `limit` objects.
        Returns {"jobs": n, "pods": n, "secrets": n}. A namespace that is gone or unreadable is skipped. Default: nothing."""
        return {"jobs": 0, "pods": 0, "secrets": 0}

    def abort_stale_uploads(self, older_than_seconds: int, limit: int) -> int:
        """Abort incomplete multipart uploads (from killed Jobs) older than this, under the export and repository prefixes.
        Returns how many. Raises BackupRunnerError. Default: nothing."""
        return 0

    def repo_app_ids(self) -> set[int]:
        """App ids that have a repository prefix (`<BACKUP_PREFIX>apps/<id>/`) in the backup bucket. Raises BackupRunnerError."""
        return set()


def job_manifests(spec: BackupJobSpec) -> tuple[dict[str, Any], dict[str, Any]]:
    """(Secret, Job) for one run. Pure function; asserted on in tests. A volume backup (spec.volume) is the one Job that runs in
    a tenant namespace: it mounts the app's PVC read-only, sits next to the app pod when there is one, and carries no
    ServiceAccount (the namespace default has no token and no rights)."""
    ns = spec.namespace or config.BACKUP_NAMESPACE
    vol = spec.volume
    labels = {LABEL: "1", "picopaas.io/backup-run": str(spec.run_id)}
    secret = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": spec.name, "namespace": ns, "labels": dict(labels)},
        "type": "Opaque",
        "stringData": dict(spec.env),
    }
    limits = {"cpu": "200m", "memory": "320Mi", "ephemeral-storage": "200Mi"}
    requests = {"cpu": "50m", "memory": "128Mi", "ephemeral-storage": "64Mi"}
    tmp_size = "200Mi"
    if vol:  # the namespace LimitRange caps a container at the app's own limits, so the Job never asks for more than that
        limits = {"cpu": f"{vol['cpu_limit_m']}m", "memory": f"{vol['mem_limit_mi']}Mi", "ephemeral-storage": f"{vol['eph_limit_mi']}Mi"}
        requests = {"cpu": f"{vol['cpu_request_m']}m", "memory": f"{vol['mem_request_mi']}Mi", "ephemeral-storage": f"{vol['eph_request_mi']}Mi"}
        tmp_size = f"{vol['eph_limit_mi']}Mi"
    mounts = [{"name": "tmp", "mountPath": "/tmp"}]
    volumes: list[dict[str, Any]] = [{"name": "tmp", "emptyDir": {"sizeLimit": tmp_size}}]
    if spec.scratch_mib:
        limits["ephemeral-storage"] = f"{200 + spec.scratch_mib}Mi"
        mounts.append({"name": "work", "mountPath": "/work"})
        volumes.append({"name": "work", "emptyDir": {"sizeLimit": f"{spec.scratch_mib}Mi"}})
    pod_security: dict[str, Any] = {
        "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
    }
    container: dict[str, Any] = {
        "name": "backup",
        "image": config.BACKUP_IMAGE,
        "envFrom": [{"secretRef": {"name": spec.name}}],
        "resources": {"requests": requests, "limits": limits},
        "securityContext": {
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": mounts,
    }
    if vol:
        pod_security.update({"runAsUser": vol["uid"], "runAsGroup": vol["fs_group"], "fsGroup": vol["fs_group"]})
        # Same non-root UID/fsGroup as the app pod, so group-readable files are readable; the volume is never writable here.
        volumes.append({"name": "data", "persistentVolumeClaim": {"claimName": vol["claim"], "readOnly": True}})
        mounts.append({"name": "data", "mountPath": config.BACKUP_VOLUME_MOUNT, "readOnly": True})
        container["env"] = [{"name": "GOMEMLIMIT", "value": f"{int(vol['mem_limit_mi'] * 0.8)}MiB"}]
        if vol.get("app_pod_label"):  # RWO volume: another pod can mount it only on the node it is already attached to
            pod_spec["affinity"] = {"podAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
                "labelSelector": {"matchLabels": {"app": vol["app_pod_label"]}},
                "topologyKey": "kubernetes.io/hostname"}]}}
    else:
        pod_spec["serviceAccountName"] = "backup-job"
    pod_spec.update({"securityContext": pod_security, "containers": [container], "volumes": volumes})
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": spec.name, "namespace": ns, "labels": dict(labels)},
        "spec": {
            "backoffLimit": 0,  # a failed run is recorded and retried at the next slot, never silently re-run
            "activeDeadlineSeconds": spec.deadline_seconds or config.BACKUP_JOB_DEADLINE_SECONDS,
            "ttlSecondsAfterFinished": 3600,  # safety net; the scheduler deletes finished Jobs itself
            "template": {"metadata": {"labels": dict(labels)}, "spec": pod_spec},
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


def _parse_ts(value: Any) -> Any:
    """k8s creationTimestamp (ISO string or datetime) as an aware datetime; None when missing/unparseable."""
    from datetime import datetime, timezone

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class KubeBackupRunner(BackupRunner):
    def __init__(self, dynamic_client: Any | None = None, core_api: Any | None = None) -> None:
        from app.providers.kube import KubeClientProvider

        self._kube = KubeClientProvider(dynamic_client, core_api)

    def _res(self, api_version: str, kind: str) -> Any:
        return self._kube.dyn.resources.get(api_version=api_version, kind=kind)

    def start(self, spec: BackupJobSpec) -> None:
        secret, job = job_manifests(spec)
        ns = spec.namespace or config.BACKUP_NAMESPACE
        try:
            self._res("v1", "Secret").create(body=secret, namespace=ns)
            try:
                self._res("batch/v1", "Job").create(body=job, namespace=ns)
            except Exception:
                self._delete("v1", "Secret", spec.name, ns)  # never leave credentials behind a Job that did not start
                raise
        except Exception as exc:  # noqa: BLE001 - the k8s error text can echo manifest content; keep only the type
            raise BackupRunnerError(f"could not create the backup job ({type(exc).__name__})") from None

    def status(self, job_name: str, namespace: Optional[str] = None) -> JobStatus:
        from app.providers.kube import _as_dict
        from kubernetes.dynamic.exceptions import NotFoundError

        ns = namespace or config.BACKUP_NAMESPACE
        try:
            job = _as_dict(self._res("batch/v1", "Job").get(name=job_name, namespace=ns))
        except NotFoundError:
            return JobStatus("missing")
        st = job.get("status") or {}
        if st.get("succeeded"):
            result, _ = self._pod_output(job_name, ns)
            return JobStatus("succeeded", result=result)
        failed_cond = any(c.get("type") == "Failed" and c.get("status") == "True" for c in st.get("conditions") or [])
        if failed_cond:
            _, error = self._pod_output(job_name, ns)
            reason = next((c.get("reason") for c in st.get("conditions") or [] if c.get("type") == "Failed"), None)
            if not error:
                error = "the backup took too long and was stopped" if reason == "DeadlineExceeded" else "the backup job failed"
            return JobStatus("failed", error=error)
        return JobStatus("running")

    def _pod_output(self, job_name: str, ns: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
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

    def _delete(self, api_version: str, kind: str, name: str, namespace: Optional[str] = None) -> None:
        from kubernetes.dynamic.exceptions import NotFoundError

        try:
            kw: dict[str, Any] = {"name": name, "namespace": namespace or config.BACKUP_NAMESPACE}
            if kind == "Job":
                kw["body"] = {"propagationPolicy": "Background"}
            self._res(api_version, kind).delete(**kw)
        except NotFoundError:
            pass

    def cleanup(self, job_name: str, namespace: Optional[str] = None) -> None:
        errors = []
        for api_version, kind in (("batch/v1", "Job"), ("v1", "Secret")):
            try:
                self._delete(api_version, kind, job_name, namespace)
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

    def sweep_exports(self, older_than_seconds: int) -> list[str]:
        from datetime import datetime, timedelta, timezone

        if not config.backup_export_prefix_ok():  # a prefix overlapping the repos would delete backups
            raise BackupRunnerError("could not sweep exports (unsafe export prefix)")
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        try:
            s3 = self._s3()
            old: list[str] = []
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=config.BACKUP_BUCKET, Prefix=config.BACKUP_EXPORT_PREFIX):
                old += [o["Key"] for o in page.get("Contents") or [] if o["LastModified"] < cutoff]
            for i in range(0, len(old), 500):
                s3.delete_objects(Bucket=config.BACKUP_BUCKET, Delete={"Objects": [{"Key": k} for k in old[i:i + 500]], "Quiet": True})
            return old
        except Exception as exc:  # noqa: BLE001
            raise BackupRunnerError(f"could not sweep exports ({type(exc).__name__})") from None

    def delete_exports(self, keys: list[str]) -> None:
        if not config.backup_export_prefix_ok() or any(not k.startswith(config.BACKUP_EXPORT_PREFIX) or ".." in k for k in keys):
            raise BackupRunnerError("could not delete exports (unsafe export key)")  # never touch a repo object
        if not keys:
            return
        try:
            self._s3().delete_objects(Bucket=config.BACKUP_BUCKET, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True})
        except Exception as exc:  # noqa: BLE001
            raise BackupRunnerError(f"could not delete exports ({type(exc).__name__})") from None

    def sweep(self, active: set[str]) -> int:
        return self._sweep_ns(config.BACKUP_NAMESPACE, active)

    def sweep_namespaces(self, namespaces: list[str], active: set[str]) -> int:
        n = 0
        for ns in namespaces:
            try:
                n += self._sweep_ns(ns, active)
            except Exception as exc:  # noqa: BLE001 - a namespace that is gone or not readable must not stop the others
                if type(exc).__name__ in ("NotFoundError", "ForbiddenError") or getattr(exc, "status", None) in (403, 404):
                    log.debug("backup: namespace %s is gone or not readable, nothing to sweep", ns)  # a deleted app: not news
                    continue
                log.warning("backup: secret sweep in %s failed (%s)", ns, type(exc).__name__)
        return n

    def sweep_orphans(self, namespaces: list[str], active: set[str], min_age_seconds: int, limit: int) -> dict[str, int]:
        from datetime import datetime, timedelta, timezone

        from app.providers.kube import _as_dict

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)
        out = {"jobs": 0, "pods": 0, "secrets": 0}
        left = limit
        for ns in namespaces:
            for key, api_version, kind in (("jobs", "batch/v1", "Job"), ("secrets", "v1", "Secret"), ("pods", "v1", "Pod")):
                if left <= 0:
                    return out
                try:
                    items = _as_dict(self._res(api_version, kind).get(namespace=ns, label_selector=LABEL)).get("items", [])
                    for it in items:
                        meta = it.get("metadata") or {}
                        name = meta.get("name")
                        owner = (meta.get("labels") or {}).get("job-name") or name  # a pod belongs to its Job
                        if not name or owner in active or name in active or left <= 0:
                            continue
                        made = _parse_ts(meta.get("creationTimestamp"))
                        if made is None or made > cutoff:
                            continue
                        self._delete(api_version, kind, name, ns)
                        out[key] += 1
                        left -= 1
                except Exception as exc:  # noqa: BLE001 - gone / forbidden namespace or a kind we may not list: not news
                    if type(exc).__name__ in ("NotFoundError", "ForbiddenError") or getattr(exc, "status", None) in (403, 404):
                        continue
                    log.warning("backup: orphan sweep of %s in %s failed (%s)", kind, ns, type(exc).__name__)
        return out

    def abort_stale_uploads(self, older_than_seconds: int, limit: int) -> int:
        from datetime import datetime, timedelta, timezone

        if not config.backup_export_prefix_ok():
            raise BackupRunnerError("could not abort uploads (unsafe export prefix)")
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        n = 0
        try:
            s3 = self._s3()
            for pfx in (config.BACKUP_EXPORT_PREFIX, config.BACKUP_PREFIX):
                for page in s3.get_paginator("list_multipart_uploads").paginate(Bucket=config.BACKUP_BUCKET, Prefix=pfx):
                    for u in page.get("Uploads") or []:
                        if n >= limit:
                            return n
                        if u["Initiated"] < cutoff:
                            s3.abort_multipart_upload(Bucket=config.BACKUP_BUCKET, Key=u["Key"], UploadId=u["UploadId"])
                            n += 1
            return n
        except Exception as exc:  # noqa: BLE001
            raise BackupRunnerError(f"could not abort incomplete uploads ({type(exc).__name__})") from None

    def repo_app_ids(self) -> set[int]:
        base = f"{config.BACKUP_PREFIX}apps/"
        ids: set[int] = set()
        try:
            for page in self._s3().get_paginator("list_objects_v2").paginate(Bucket=config.BACKUP_BUCKET, Prefix=base, Delimiter="/"):
                for cp in page.get("CommonPrefixes") or []:
                    part = cp["Prefix"][len(base):].strip("/")
                    if part.isdigit():
                        ids.add(int(part))
            return ids
        except Exception as exc:  # noqa: BLE001
            raise BackupRunnerError(f"could not list repositories ({type(exc).__name__})") from None

    def _sweep_ns(self, namespace: str, active: set[str]) -> int:
        from app.providers.kube import _as_dict

        res = self._res("v1", "Secret")
        items = _as_dict(res.get(namespace=namespace, label_selector=LABEL)).get("items", [])
        n = 0
        for it in items:
            name = (it.get("metadata") or {}).get("name")
            if name and name not in active:
                self._delete("v1", "Secret", name, namespace)
                n += 1
        return n
