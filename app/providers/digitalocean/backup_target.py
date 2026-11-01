"""Backups on DigitalOcean Spaces: the s3: restic URL, and the /32 egress hole for Spaces' private VPC IPs."""
from __future__ import annotations

from typing import Any

from app import config
from app.providers.base import BackupTarget


class DoBackupTarget(BackupTarget):
    def endpoint(self) -> str:
        return config.backup_endpoint()  # BACKUP_ENDPOINT, else https://<BACKUP_REGION>.digitaloceanspaces.com

    def repository_url(self, path: str) -> str:
        prefix = config.BACKUP_PREFIX.strip("/")
        prefix = f"{prefix}/" if prefix else ""
        return f"s3:{self.endpoint()}/{config.BACKUP_BUCKET}/{prefix}{path}"

    def ensure_egress(self, runner: Any) -> None:
        runner.ensure_spaces_egress(self.endpoint())
