# 0008: Per-app backups (status: PENDING, awaiting Chas)

Design: `docs/design/backups.md`. Nothing here is built or provisioned. The feature ships disabled (`BACKUPS_ENABLED` unset), so
merging code costs nothing.

1) **What is being deployed.** A dedicated platform backup bucket (`picopaas-backups`) in a Spaces region other than ATL1 and NYC3
   (proposed SFO3) with one bucket-scoped access key, holding encrypted restic repositories of app databases, persistent volumes
   and (later) blob buckets. Optional, phase 3 only and separately confirmed: DO volume snapshots for fast pre-upgrade rollback.
2) **Why.** Stateful apps (first: Chas's WordPress blog) need off-site, restorable, retention-managed backups, because a deleted or
   corrupted volume or database has no other recovery path (managed-DB backups restore only a whole new cluster).
3) **Cost.**
   - **Static: about $0.** The bucket lives in the existing Spaces subscription ($5/mo, already in the $75.30 baseline), which
     includes 250 GiB storage and 1 TiB transfer; overage is $0.02/GiB-mo. Expected usage is a few GiB (dedup, 7 daily/4 weekly/6
     monthly), about 2% of the allowance. To confirm in the DO control panel: the allowance is account-wide across regions.
     It takes one of the 100 buckets per account.
   - **Per app deployed: about $0**; a typical app adds 10 MB to about 1.2 GiB of backup data, about $0.02/GiB-mo only if the
     allowance were exceeded. Per-app cap 2 GiB, platform cap 100 GiB.
   - **Snapshots (phase 3, only if enabled): $0.06/GiB/mo** per retained snapshot; one 1 GiB volume with 7 retained is about
     $0.42/mo worst case.

**Default if unanswered:** phase 1 uses only the existing Spaces allowance: backups go under `backups/` in the adopted platform
bucket `pico-paas-s3-a` with a bucket-scoped key (disaster isolation from the ATL1 app buckets is deferred; switching later is a
config change plus `restic copy`).
