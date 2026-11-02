"""Backups on any S3 endpoint (Garage in the generic target): the s3: restic URL and the egress hole.

The endpoint is BACKUP_ENDPOINT, else OBJECT_STORE_ENDPOINT. Set BACKUP_ENDPOINT as well for the rest of the backup
machinery (`config.backup_enabled`, the export presign/sweep client read `config.backup_endpoint()`).

Egress: the endpoint may be in-cluster or private (Garage Service ClusterIP). That is exactly the case the existing
runner helper handles, so `ensure_egress` reuses `runner.ensure_spaces_egress` (resolve the host, allow its
private-range IPv4s as /32 in the backup namespace). The helper opens the port in the endpoint URL
(443 for https, 80 for http, else explicit, e.g. :3900). The export presign/sweep client uses path-style
(`s3_addressing_style`). Caveat: a ClusterIP is DNAT-ed before most CNIs evaluate policy, so for an in-cluster Garage
the /32 may need to be the pod IP; verify on the local cluster.
"""
from __future__ import annotations

import os
from typing import Any

from app import config
from app.providers.base import BackupTarget


class GenericBackupTarget(BackupTarget):
    def endpoint(self) -> str:
        return (config.BACKUP_ENDPOINT or os.environ.get("OBJECT_STORE_ENDPOINT", "")).rstrip("/")

    def repository_url(self, path: str) -> str:
        prefix = config.BACKUP_PREFIX.strip("/")
        prefix = f"{prefix}/" if prefix else ""
        return f"s3:{self.endpoint()}/{config.BACKUP_BUCKET}/{prefix}{path}"

    def s3_addressing_style(self) -> str:
        return "path"  # Garage serves virtual-host style only with root_domain set

    def ensure_egress(self, runner: Any) -> None:
        runner.ensure_spaces_egress(self.endpoint())
