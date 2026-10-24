# Backups for apps (REQ-9): design

Status update 2026-10-24 (backend-dev): **opt-in and owner downloads IMPLEMENTED.** (1) Scheduled backups are opt-in:
`BACKUP_DEFAULT_ENABLED` (default 0) sets `enabled` on new `app_backup_config` rows; existing rows keep their value; the scheduler only
picks enabled rows and no longer creates rows for apps itself. A manual `POST /apps/{id}/backups` works with scheduled backups off (same
409/hourly rules) and does not opt the app in. Delete: a disabled app gets no final backup (it never opted in, and the delete should not
wait or store data nobody asked for) but repos from earlier manual backups still get the 14-day retention and purge. (2) Download
(deviation from the original `GET .../download`): `POST /apps/{id}/backups/{run_id}/download` queues a `trigger=export` BackupRun
(`source_run_id`, `export_key`, migration `e5b1c7d9a2f4`); a Job with `MODE=export` (image script) `restic dump`s the snapshot to one file and uploads it
with the backup key to `exports/<app>/<export id>.<ext>`; `GET /apps/{id}/backups/exports/{export_id}` returns a 10-minute presigned URL
once ready; objects are swept after 1 hour (not the 24 h lifecycle first designed); 3 per app per hour; audit `backup.download`.
Come back later (2026-10-24): `GET /apps/{id}/backups/exports` lists non-expired exports (failed ones for an hour, `url` null); the single-export GET presigns a fresh 10-minute URL on every call until `available_until` (finish + 1 h); each run in `GET /apps/{id}/backups` has an `export` summary. Enqueue paths (manual backup, export) serialise per app with `SELECT ... FOR UPDATE` on the `AppBackupConfig` row (`backups.lock_app`), closing the double-click race.
CLI: `picopaas backup list|run|enable|disable|download`. Needs the backup image republished and `BACKUP_IMAGE` re-pinned.

Status update 2026-10-23 (backend-dev): **bucket kind and admin restore helpers IMPLEMENTED** (pulled forward from phases 3 and 2),
migration `d4a9e1b7c3f2`. Bucket: Job copies the app's objects to an emptyDir with `s5cmd` (pinned, sha256-verified), verifies the
object count, `restic backup /work/bucket` into `apps/<id>/bucket`, then forget/prune; the app's own bucket key goes in the per-run
Secret (`SRC_*`), no per-backup read-only key; scheduled with the db run, separate `bucket_*` status, counted in the caps, final backup
on delete, purge covers it; scratch bounded by `BACKUP_BUCKET_MAX_BYTES` (512 MiB; namespace ephemeral-storage quota raised to 3Gi). Restore: `POST /admin/backups/{run_id}/restore`
`{target_app_id, overwrite_source?}` plus `picopaas admin backup restore`; Job reads the SOURCE repo and writes the TARGET's DB (as its
own role, tables dropped first, DEFINERs stripped) or bucket (emptied first, count verified); source app refused unless suspended and
`overwrite_source`; audited. Verified locally against rclone-as-S3, MariaDB and PostgreSQL (sha256 of every object identical).
Still deferred: volume Jobs, UI, maintenance Job, restore into a NEW app.

Status: **phase 1a IMPLEMENTED (2026-10-23, backend-dev), flag `BACKUPS_ENABLED` off, nothing provisioned.** Done: config knobs,
migration `c2e8a4f6b1d7` (`app_backup_config`, `backup_runs`), backup image `images/backup/` (ghcr.io/seaburr/picopaas-backup),
`deploy/platform/backup.yaml` (ns `picopaas-backup`, RBAC, NetworkPolicies), DB dump Jobs via `app/backups.py` +
`app/providers/backup.py` (leader-only tick), `GET/POST/PATCH /apps/{id}/backups`, `GET /admin/backups` + Health block, failure events
and audit, final backup on delete with 14-day retention and purge, docs. Deferred to 1b: volume Jobs (app namespace), snapshot
download (presigned GET), UI Backups card, CLI, per-run `restic check`/`stats` maintenance Job, platform-DB dump. Deviations from
this document: one random password per app (shared by its kind repos); table is `backup_runs` (not `app_backups`) with
`queued` status and `trigger` scheduled|manual|final|purge; tick every 60 s (not 5 min) so results and deletes settle fast;
retention and `--prune` run at the end of every backup Job (a weekly maintenance Job is 1b); the dump uses `mariadb-dump`
(Alpine has no Oracle `mysqldump`), adding `--no-tablespaces` / `--set-gtid-purged=OFF` only when the client supports them;
purge = forget all snapshots + prune (restic cannot delete a repo; its config/key files remain). Original design status:
No code, nothing provisioned. Chas approved starting REQ-9; first user is his
WordPress blog (image + 1 GiB RWO volume + managed MySQL database + optional Spaces bucket). Background:
`docs/research/wordpress-backups.md`, `docs/research/wordpress-requirements.md` (REQ-9), `docs/requests/0007-persistent-volumes.md`,
`docs/deployment/infrastructure.md`. Paid-resource request: `docs/requests/0008-backups.md`.

## 0. Summary of decisions

| Topic | Decision |
|---|---|
| Tool | **restic**, one static binary, client-side encrypted, deduplicated, speaks S3. One repo per app. |
| Database | Platform-run logical dump (`mysqldump --single-transaction --no-tablespaces --set-gtid-purged=OFF` / `pg_dump -Fc`) piped into `restic backup --stdin`. DO managed-cluster backups stay as the disaster layer. |
| Volume | restic file backup from a Job in the app namespace (read-only mount, podAffinity to the app pod). DO volume snapshots are an opt-in phase-3 extra, not the default. |
| Blob bucket | `s5cmd` copy to an emptyDir, then restic of the directory (implemented, nightly with the db run). Spaces versioning is not the default. |
| Where | A dedicated platform backup bucket, own scoped key, in a different region from the app buckets and from compute. |
| Execution | Leader-only scheduler job decides what is due and creates k8s Jobs; DB dumps and maintenance run in a platform namespace, volume backups must run in the app namespace. |
| Cost | Phase 1 fits inside the existing $5 Spaces subscription: marginal about $0. Snapshots (phase 3, optional) about $0.06/GiB/mo. |

**Decisions log**
- 2026-10-23 (Chas): enable backups on this instance and **reuse the existing platform Spaces credentials** for the backup bucket (no separate key). Phase 1 uses a dedicated bucket `picopaas-backups` in the existing ATL1 region (created by bootstrap, $0 extra) with prefix `backups/`; the second-region bucket (request 0008) is deferred. **Condition**: the shared full-access key must be replaced by a bucket-scoped key before volume backups run in tenant namespaces (phase 1b), because that Secret would then be readable from tenant-adjacent Jobs.

## 1. What is protected and how

RPO is the schedule interval (default daily, so up to 24 h of data loss). RTO is the operator time to run a restore; targets below
assume phase 2 endpoints exist (phase 1 is download plus the manual `WP_RESTORE` style flow).

| Resource | Layer 1 (per app, platform-owned) | Layer 2 (disaster, existing) | RPO | RTO |
|---|---|---|---|---|
| App database (MySQL or PostgreSQL) | Daily logical dump into the app's restic repo, taken with the platform's DB credentials. Restores one app without touching the shared cluster. | DO managed-cluster daily backups, 7 days, restore creates a **new cluster** (not per-app, destroyed with the cluster). Documented as "lost the cluster", never as per-app restore. | 24 h (cluster layer: up to 24 h; PITR option at DO restore time) | Layer 1: minutes (stream dump into the app DB). Layer 2: hours (new cluster, repoint, extract one DB). |
| Persistent volume | Daily restic file backup of the mount path. Core app files that are reproducible from the image are the app's choice to exclude (`backups.exclude`, phase 2). | none today (volume is `reclaimPolicy Delete`; deleting the app deletes the DO volume) | 24 h | Minutes for 1 GiB: stop app, `restic restore`, start. |
| Blob bucket (Spaces, 100 MB cap) | Implemented: nightly restic of the bucket contents (s5cmd to scratch, then restic), admin restore helper. | none | 24 h | Minutes |
| Platform DB (`picopaas`) | Daily dump into the same backup bucket (platform repo). Cheap and closes a real gap: lose the DB plus FERNET_KEY and every per-app repo password is gone. | DO managed MySQL backups | 24 h | Minutes to hours |

### 1a. Volume: restic Job vs DO snapshots

| | restic Job (chosen) | DO volume snapshot (CSI VolumeSnapshot or DO API) |
|---|---|---|
| Consistency | File level while the app runs: not atomic. Fine for WordPress/Ghost media/themes. Apps that keep a SQLite file or LSM store on the volume can tear; they need an app-level quiesce or `backups.mode: stop` (phase 2, brief downtime). | Crash-consistent block snapshot (the app would recover as after a power cut). Better for in-volume databases. |
| Off-site/region | Yes: a different region and separate credentials. | Same DO account and region, not downloadable; a lost account or region loses both. |
| Encryption | Client side, platform-held key. | DO-managed at rest only. |
| Retention/dedup | `forget --keep-*` and dedup: a 1 GiB volume with 20 MB of change per day costs about 1.1 GiB total. | Each snapshot bills its used size ($0.06/GiB/mo); retention is our own code. |
| Download/migrate | Yes (any machine with restic and the password). | No (restore only to a DO volume). |
| Restore target | Any volume, including a new app. | New PVC from snapshot (a new volume, new PVC, switch the Deployment): more k8s plumbing (snapshot CRDs, VolumeSnapshotClass). |
| Cost | $0 marginal | $0.06/GiB/mo, new paid resource, request needed |
| Complexity | Job in tenant ns, co-scheduling | VolumeSnapshot controller wiring |

Reasoning: the main failure modes we care about are "I broke the site", "the app or volume was deleted", and "the DO account or
region has a problem". Only restic covers the last two and gives download. Snapshots win only for fast pre-upgrade rollback and
crash-consistency, so they ship as an opt-in extra (phase 3), including an automatic one before an image or WordPress core
upgrade.

**Co-scheduling.** The PVC is RWO: another pod can mount it only on the node where it is already attached.
- App running: the Job gets `requiredDuringScheduling` podAffinity on the app pod's labels (`kubernetes.io/hostname` topology). RWO allows several pods on one node.
- App stopped (replicas 0): the volume is detached, no affinity (a required affinity with no matching pod would be unschedulable). The reconciler decides at Job creation time; if the app starts mid-run the Job may fail once and is retried next cycle.
- The Job mounts the volume `readOnly: true`. Never use `ReadWriteOncePod`.
- Pod-level `fsGroup` matches the app's `VOLUME_FS_GROUP`; the Job runs as the same non-root UID.

### 1b. Database
Dump with the app's own database user (already provisioned, full rights on its own DB only, per-app connection caps), not the
admin account: least privilege, no admin credential leaves `picopaas-system`. Flags: MySQL `--single-transaction --quick
--no-tablespaces --set-gtid-purged=OFF --routines --triggers --events`; PostgreSQL `pg_dump -Fc --no-owner --no-acl` (so a restore
into a new `app_<id>` role works). The dump is streamed (no scratch file), compressed by restic. The Job fails if the client exits
non-zero, and `restic backup --stdin` uses a named stdin file (`db.sql` / `db.pgdump`) so snapshots are self-describing.
The managed-DB per-user connection cap is respected (the dump uses one connection). DO's `sql_require_primary_key` is irrelevant to
dumps but matters on restore (a dump from a keyless app would not have loaded in the first place).

### 1c. Blob bucket
Recommended: **restic**, not versioning.
- Versioning: DO Spaces supports S3 versioning and lifecycle `noncurrent_version_expiration` (DO docs "How to Enable Spaces Versioning"; enable only via the API; once enabled a bucket can only be suspended, never reverted to unversioned). I could not find an official statement on whether noncurrent versions count against the 250 GiB allowance or the overage rate, and our own quota poll (current objects) would not see them, so the 100 MB cap would silently stop meaning anything. It also protects against delete/overwrite only, not against account/region loss.
- restic: a phase-3 Job copies the bucket to an `emptyDir` (the cap is 100 MB, scratch is 250 MB, so it fits; refuse and alert if the bucket is over 200 MB) and runs `restic backup`. Same repo, retention, restore tooling and download as the rest.
- Read access: the Job runs in `picopaas-backup` with a freshly issued read-only key for that one app bucket (same scoped-key mechanism as `SpacesStore`), revoked after the run; the platform admin key never enters a Job.

## 2. Where backups live

- **Bucket**: `picopaas-backups` (name configurable, `BACKUP_BUCKET`), separate from `pico-paas-s3-a` and from every tenant bucket, with its own scoped Spaces key (read/write on that one bucket; DO limited-access keys are per bucket, not per prefix). Key lives only in `picopaas-env` (platform) and in ephemeral per-run Secrets (section 6).
- **Region**: not ATL1 (app buckets) and not NYC3 (cluster and databases). Candidate: **SFO3** (or TOR1/AMS3). Disaster isolation from a single-region outage in either NYC3 or ATL1. Spaces region list to confirm at provisioning time (docs list nyc3, sfo2/sfo3, ams3, sgp1, fra1, syd1, atl1, blr1, tor1 and others); `BACKUP_REGION` is a variable. Transfer from NYC3 to SFO3 is inbound to Spaces, free; restores read out of Spaces and count against the 1 TiB allowance.
- **Layout**: `s3:<endpoint>/picopaas-backups/apps/<app_id>/db` and `.../vol` and `.../bucket` (one restic repo per kind, so a corrupt repo or a forgotten password affects one thing; prune locks are independent), `platform/db` for the platform DB, `exports/` (24 h lifecycle) for download staging.
- **Encryption**: per-repo restic password. **Decision: a random 32-byte password per app, stored Fernet-encrypted in the platform DB** (`app_backup_config.repo_password_enc`), reusing the existing `rotate_encrypted_data` path. Rejected: deriving it from FERNET_KEY (`HMAC(FERNET_KEY, app_id)`), because a FERNET_KEY rotation would orphan every repo (restic cannot re-key without `key add/remove`, so we would have to do it for all repos at rotation time), and one leaked derivation key would unlock all repos. A random per-app password keeps tenant blast radius to one repo. Escrow: the platform DB dump is itself in the bucket but encrypted by a separate **platform repo password held as a platform secret** in `picopaas-env` (`BACKUP_PLATFORM_REPO_PASSWORD`) and printed once by bootstrap for Chas to store offline; admin docs say "if you lose FERNET_KEY and the DB, backups are gone".
- **Retention default**: CHANGED 2026-10-23 (Chas): 1 daily, 1 weekly, 1 monthly per repo (env-overridable); owners archive anything older by downloading (download endpoint still to build). Originally 7 daily, 4 weekly, 6 monthly per repo (`restic forget --prune`, weekly maintenance Job; `restic check --read-data-subset=5%` weekly). Admin-overridable per platform (`BACKUP_KEEP_*`), per-app override phase 2. Manual and pre-restore snapshots are tagged and kept 14 days regardless.
- **Quota/accounting**: backup bytes do **not** count against the 100 MB blob cap or the 50 MB DB cap (different purpose; counting them would punish users for backing up and would let backup growth disable their bucket via quota enforcement). They are metered separately: `repo_bytes` per kind from `restic stats --mode raw-data`, with a per-app backup cap **2 GiB** (`BACKUP_MAX_BYTES_PER_APP`, covers a 1 GiB volume plus history; admin override) and a platform total cap (default 100 GiB, 40% of the 250 GiB allowance, health warning at 80%). Over the cap: new backups are skipped with an app event and a failed status, never silent. Volume-app size is already capped (1 GiB user, 10 GiB admin), so the arithmetic is bounded: 10 volumes at the admin cap is already unrealistic.
- **Cost** (DO pricing as recorded in the repo: Spaces $5/mo includes 250 GiB storage + 1 TiB transfer, $0.02/GiB-mo over): realistic load is a handful of apps, roughly 10 MB to 1.2 GiB each after dedup; ~5 GiB total, about 2% of the allowance. Marginal cost about $0. Assumption to verify in the DO control panel before provisioning: the allowance is account-wide across regions, so a bucket in a second region does not need a second subscription (stated in request 0008). Volume snapshots if chosen: about $0.06/GiB/mo; one 1 GiB volume, 7 retained about $0.42/mo worst case.

## 3. Scheduling and execution

**Control plane**: a new leader-only APScheduler job `backup_job` (every 5 minutes, `max_instances=1`, same pattern as `quota_job` in `app/scheduler.py`; follows the leader lock so replicas do not double-run) that:
1. selects apps with `backups.enabled` and `next_backup_at <= now` (staggered by `hash(app_id)` within a 02:00-05:00 UTC window; `next_backup_at` is stored, not recomputed from a cron string, so a missed window runs at the next tick),
2. creates at most `BACKUP_MAX_CONCURRENT` (default 2) Jobs, queueing the rest (the node is 2 vCPU/4 GB shared with tenants; the managed DB has about 76/22 connections total, so dumps must be sequential per server, 1 at a time per DB server),
3. polls Job status (kube provider) each tick and writes `app_backups` rows (`running -> succeeded|failed`), updates `last_success_at`, emits app events, and deletes finished Jobs (also `ttlSecondsAfterFinished: 3600` as a safety net).

Why scheduler-created Jobs and not a per-app CronJob: the schedule, concurrency limits, quota, on-demand runs, and "last backup" state all live in the platform DB; a CronJob in the tenant namespace adds a second source of truth, cannot be globally throttled, and would sit in a namespace the tenant's quota governs. Downside: no backups while the platform is down; acceptable (the platform is a 2 replica Deployment and a missed day is caught by the stale alarm).

**Where each Job runs**

| Job | Namespace | Why | Credentials in the pod |
|---|---|---|---|
| DB dump | `picopaas-backup` (new platform namespace, own ResourceQuota, default-deny NetworkPolicy; egress only to DB VPC CIDR port 25060 and Spaces 443) | Needs no tenant volume. Uses no tenant quota. Tenant netpols are per app and irrelevant here. | Ephemeral Secret per run: that app's DB creds + backup key + repo password |
| Volume backup | `app-<id>` (PVC is namespaced; a PVC in another namespace cannot be mounted) | Only place the PVC is mountable | Ephemeral Secret per run: backup key + repo password for this app's repo |
| Maintenance (`forget --prune`, `check`, `stats`) | `picopaas-backup` | No volume | Ephemeral Secret |
| Bucket copy (phase 3) | `picopaas-backup` | emptyDir only | read-only app key + backup key |

**Resource caps and tenant quota.** Backup Job pods: requests 50m/128Mi, limits 200m/320Mi, `GOMEMLIMIT=256MiB`, `restic --pack-size 8`, `GOMAXPROCS=1`, `--limit-upload` (platform default 2 MiB/s for tenant-namespace Jobs so a backup does not saturate the node's egress), ephemeral-storage limit 200Mi (restic cache on an emptyDir, never the volume). `activeDeadlineSeconds` 1800 (volume/DB) and `backoffLimit: 1`. Prune is deliberately kept out of volume Jobs (it is the memory-hungry part) and run in the platform namespace.
- The volume Job runs in the tenant namespace and therefore counts against its ResourceQuota (pods, CPU, memory, limits). The reconciler widens the app's quota by exactly one backup pod's worth only when `backups.enabled` and a volume exist (`resource_quota_manifest` already adds PVC quota conditionally). Residual risk: the tenant could use that headroom for its own pods. The clean fix is a `PriorityClass picopaas-backup` with a quota `scopeSelector` so the headroom applies only to that class (phase 2 hardening, noted not required for MVP since replicas are capped and the app is the only pod spec the tenant controls).
- Because the Job must fit under the 100m CPU tenant cap in aggregate with the app, the backup pod uses a low `PriorityClass` and the app is never throttled below its own limit by it (CPU limits are per container; the shared node, not the quota, is the real constraint).
- DB and maintenance Jobs run in `picopaas-backup` with a namespace quota (4 pods, 1 CPU, 1.5 GiB) so a bug cannot swamp the node.

**Failure alerting and status**
- Every outcome writes an app event (`backup.succeeded`, `backup.failed` with a short, redacted error summary: never the connection string, never restic stdout containing the repo URL with credentials) and an audit entry (admin-visible).
- Admin Health page: new "Backups" row. States: ok / stale (last success older than 36 h for a daily schedule, 8 days for weekly) / failing (last run failed) / disabled. Counts for fleet and repo bytes against caps. Stale transitions emit one admin-visible event, not a repeat per tick.
- API/UI: every app exposes `backup: {enabled, schedule, last_success_at, last_status, next_backup_at, repo_bytes, kinds: [db, volume]}` in the app payload; the UI shows "Last backup 3 h ago" on the app health page and settings.
- Email: not in MVP (SMTP is egress-blocked; the platform has no mailer in this design).

## 4. Restore

| Flow | MVP phase | How |
|---|---|---|
| List and download | **1** | `GET /apps/{id}/backups`; download via presigned GET. Because restic data is encrypted packs, "download" is a materialisation: a platform-namespace Job runs `restic dump <snap> db.sql` (or `restic dump <snap> / --archive tar` for the volume), gzips it into `exports/<app>/<bid>.gz` with a 24 h lifecycle rule, then the API returns a presigned GET (expiry 15 min, reuse the REQ-8 presign logic in `SpacesStore.presign`, extended to take a bucket/credential set for the backup bucket; the export credential used to sign is the platform's, never the tenant's). Works for the existing manual `WP_RESTORE` path (download then re-upload to the app bucket). |
| Restore DB into the same app | 2 | Job in `picopaas-backup` runs `restic dump | mysql/psql` using the app's user. Before it: an automatic `pre-restore` backup, then `DROP/CREATE` of objects owned by the app (MySQL: drop tables; PostgreSQL: `pg_restore --clean --if-exists`). Confirm token required, audit entry, app set to maintenance state (scale 0 optional flag `stop_app: true`, default true for volume apps). |
| Restore volume into the same app | 2 | App scaled to 0 (Recreate already means one pod); wait for detach; Job in the app namespace mounts RW, `restic restore <snap> --target <mount> --delete`; scale back to the stored replica count. `pre-restore` backup first. Phase set to `restoring`, reconciler skips scale-up while it is set. |
| Restore into a new app | 3 | Same Jobs with `target_app_id`, only if the target has a DB of the same engine and a volume at least the size of the restored data; admin-only at first (cross-tenant data). Source and target owner must match unless admin. |
| Restore a bucket | 3 | rclone sync from the restored restic tree to the app bucket, after an automatic export of the current contents. |
| Disaster (cluster loss) | doc only | DO managed DB backup to a new cluster; restic repos are independent of it. Runbook added to `docs/deployment/runbooks.md` with the monthly restore drill on a throwaway app (the REQ-9 acceptance test). |

### Deleting one backup (owner)

`DELETE /apps/{id}/backups/{run_id}` removes one snapshot. Under `backups.lock_app` the API checks that the run is a succeeded
`db`/`bucket` backup with a snapshot (422 otherwise) and that nothing is queued or running for that repo of that kind (backup,
restore into the app or from this repo, export, delete: 409), then queues a run with `trigger="delete"` and `source_run_id` = the
backup and sets the source to `deleting`. The tick starts a Job with `MODE=forget SNAPSHOT=<id>`: it checks the snapshot exists
(`restic snapshots --json <id>`), runs `restic forget <id> --prune` under `LONG_TIMEOUT` and prints
`PICOPAAS_RESULT {"forgotten":true,"repo_bytes":N}`. A snapshot that is already gone (retention pruned it first) is success
and prints a "already gone" step; any other failure is a fixed `PICOPAAS_ERROR`. No DB credentials, no per-server dump slot, no cap checks.

State model: the source is `succeeded -> deleting -> deleted` on success, or back to `succeeded` on failure (including a Job
that could not start). A `deleted` row is **kept** (not removed) and filtered out of `runs[]`, so `source_run_id` on past
restore/export/delete runs still resolves; its status makes restore, export and delete refuse it. On success the kind's
`repo_bytes` is taken from the result, and if the deleted run was the kind's latest backup the per-kind and app-wide
last-backup fields are recomputed from the remaining runs. On failure the delete run holds the error and a `backup_failed`
app event says so. Live-export rule: a finished, unexpired download of the run is deleted from the staging prefix and marked
`expired` as part of the delete (a failure to remove it aborts with 503 and changes nothing); a queued/running export is a 409.
Audit: `backup.delete` (user), `backup.export_deleted` (reason=backup_deleted), then `backup.deleted` / `backup.delete_failed` (system).
Delete runs are hidden from `runs[]`. Requires the backup image with `MODE=forget` (republish and re-pin `BACKUP_IMAGE`).

MVP means phase 1: backups exist, are listed, downloadable, and status is visible. Restores are the documented manual
download-and-reimport flow until phase 2 (honest: restores are phase 2, since an untested backup is not a backup, the drill ships with phase 2).

## 5. API, UI and CLI shape

Spec / app setting (`spec.backups`, also `PATCH /apps/{id}`):
```yaml
backups:
  enabled: true        # default true when the app has a volume or a database; false otherwise
  schedule: daily      # daily | weekly | off   (default daily, platform picks the time of day)
  # phase 2: keep: {daily: 7, weekly: 4, monthly: 6}, exclude: ["cache/*"], mode: live|stop
```
Defaults on for volume and DB apps because those are the unrecoverable resources, off for image-only and blob-only apps (blob
opt-in in phase 3). Users can turn it off; admins can disable the feature globally (`BACKUPS_ENABLED`, default off until request
0008 is answered, so merging code costs nothing).

| Endpoint | Purpose |
|---|---|
| `GET /apps/{id}/backups` | List `app_backups` rows (id, kind, status, started_at, finished_at, size, snapshot short id, trigger, error summary) and the config block |
| `POST /apps/{id}/backups` | On-demand run (`{kinds: ["db","volume"]}`), rate limit 1/hour per app, 429 otherwise; 409 if one is running |
| `GET /apps/{id}/backups/{bid}/download` | Starts the export (202 + poll) or returns presigned URL |
| `POST /apps/{id}/backups/{bid}/restore` | Phase 2: body `{kinds, confirm: "<app name>", stop_app}`; 202 with a restore job id |
| `DELETE /apps/{id}/backups/{bid}` | Phase 2: forget one snapshot (owner/admin); audit |
| `GET /admin/backups` | Fleet overview: per app last success, status, repo bytes, platform total vs cap, stale list |
| `POST /admin/backups/{app_id}/purge` | Admin: purge a (deleted) app's repos now |

Auth follows the existing model: read-scope API keys can list, write scope required for create/restore/delete, admin for `/admin/*`.
Presigned download is a read operation exempt-by-route like REQ-8 presign (it only reads).

Data model (migration): `app_backup_config(app_id pk, enabled, schedule, repo_password_enc, next_backup_at, last_success_at, last_status, db_repo_bytes, vol_repo_bytes, deleted_retention_until)`, `app_backups(id, app_id, kind, trigger, status, started_at, finished_at, snapshot_id, bytes_added, bytes_total, error_summary, job_name)`. Only summaries are stored; restic stdout is truncated and filtered for URLs/keys before storage.

CLI: `picopaas backup list [app]`, `backup create [app] [--db] [--volume]`, `backup download <backup-id> [-o file]` (calls the endpoint, polls, then reuses the CLI `storage download` presigned code), `backup restore <backup-id> [--yes]` (phase 2), `backup config [app] --schedule daily|weekly|off`. UI: a Backups card on the Settings page (list, "Back up now", download; phase 2 adds Restore with typed app-name confirmation) and the last-backup badge on the app health page.

## 6. Security review notes

1. **Backup bucket credentials never reach tenant pods.** DB dumps, maintenance and bucket copies run in `picopaas-backup` only. The one exception is the **volume** Job in the tenant namespace; it needs the backup key. Mitigations: an ephemeral Secret created for the run and deleted when the Job finishes (and swept by the reconciler if left behind), not mounted in the app Deployment, `automountServiceAccountToken: false`, no tenant kube access exists in this platform, the Job image is ours and pinned, the pod runs non-root with a read-only root filesystem and dropped capabilities, no tenant-controlled command or environment reaches it (it mounts tenant *data* only, never executes it; restic does not follow symlinks out of the mount). Residual risk: a leaked key reaches all repos (still encrypted by per-app passwords). Hardening for later: a small platform-side S3 gateway that issues per-app, per-prefix credentials (DO limited keys are per bucket, not per prefix). Chas accepted that tenant credential theft from Spaces is out of scope (request 0002); this design keeps that stance and does not widen it for tenants.
2. **restic password handling.** Random per-repo password, Fernet-encrypted in the DB, passed to Jobs via env var from an ephemeral Secret (`RESTIC_PASSWORD`), never on a command line (visible in `ps`; the old script's flaw) and never logged. The platform repo password is a platform secret and is escrowed offline by Chas. Rotating FERNET_KEY re-encrypts the stored passwords with the existing `rotate_encrypted_data`; repos need no change.
3. **Least privilege on databases.** App user, not admin, for dumps and restores (the user's own connection limit applies; dumps are sequential). Admin credentials never leave `picopaas-system`.
4. **Backup key scope**: bucket-scoped read/write on the backup bucket only; the Spaces admin key remains in `picopaas-env`. restic prune needs delete, so append-only is not possible with Spaces keys; compensate with the separate region, the platform repo password, and (phase 3) bucket versioning on the backup bucket itself with a 14 day noncurrent expiry if DO's cost treatment is confirmed.
5. **Tenant isolation of data.** One repo per app and kind; a tenant cannot name a repo path (paths are built from integer ids server-side); the download export prefix is keyed by app id and uses 15 min presigned GETs; backups are visible to owner and admins only; restore-into-another-app is admin-only.
6. **Deleting an app.** Take a final `pre-delete` backup, keep the repos **14 days** (`BACKUP_DELETED_RETENTION_DAYS`, admin overridable, `deleted_retention_until` on the config row), then the leader job purges the repos and `exports/` and records `backup.purged`. Admin can purge immediately; owner cannot extend it. The user's deletion dialog states the 14 day retention. Account deletion follows the same rule. This also bounds privacy exposure of deleted tenants' data.
7. **Audit entries**: `backup.enabled/disabled`, `backup.started` (trigger, kinds), `backup.succeeded/failed`, `backup.download`, `backup.restore.started/completed/failed`, `backup.pruned` (after `forget --prune`), `backup.purged` (success or failure), `backup.export_deleted` (1 h sweep, one per export), `backup.download_link` (each presigned URL issued; `export= run= kind= expires_in=600`), `backup.create` (`trigger=manual|scheduled|final`, one row per enqueue), `backup.config_changed`, each with actor, app id, backup id. No secrets, no URLs in details.
8. **Denial of service**: global and per-DB-server concurrency caps, per-app cap on repo size, on-demand rate limit, Job deadlines, upload throttle, a global kill switch. A tenant cannot grow backup storage faster than its own volume/DB caps allow (1 GiB volume, 50 MB DB), plus history bounded by retention.
9. **Supply chain**: the backup image is built from this repo in CI (GHCR, pinned by digest in the platform config), restic and DB clients installed from the distribution packages with versions pinned; image budget about 80 MB. The image carries no secrets.
10. **Restore safety**: confirm-by-name, automatic pre-restore backup, app stopped during volume restore, audit, rate limit. A tampered repo is detected by restic's authenticated encryption; weekly `check` surfaces corruption.

## 7. Phased plan

| Phase | Scope | Effort |
|---|---|---|
| **1 MVP** | Backup image + CI build. Backup bucket + scoped key via Terraform (request 0008) or the fallback prefix (see 8). Migration for `app_backup_config`/`app_backups`. Kube provider Job helpers; `backup_job` in the scheduler (leader-only); DB dump Job (MySQL and PostgreSQL), volume Job (affinity, readOnly), weekly maintenance Job (`forget --prune`, `check`), per-app retention defaults; platform DB dump; `GET/POST /apps/{id}/backups`, download via export + presign, last-backup status in the app payload, admin `/admin/backups` and a Health row, stale/failure events and audit entries, final-backup-on-delete with 14 day purge; spec `backups` block; CLI `backup list|create|download|config`; admin docs, user docs, runbook. Tests with fake providers (no real Jobs). | **L** (about 6-8 agent-days) |
| **2** | Restore endpoints (DB and volume, pre-restore backup, typed confirmation), UI Backups card with restore, CLI `backup restore`, per-app retention and excludes, quota PriorityClass scoping, the documented restore drill executed on a throwaway WordPress app (REQ-9 acceptance), restic `check` surfaced in health. | **M** (about 3-4 days) |
| **3** | Blob bucket backups (rclone + restic), opt-in volume snapshots (VolumeSnapshotClass, pre-upgrade automatic snapshot, restore to new PVC) with request 0008 amendment, restore into a new app, bucket versioning on the backup bucket, optional per-prefix S3 gateway. | **M** (about 4 days) |

Order dependencies: WordPress production migration is gated on phase 1 (plus a manual restore rehearsal using the phase-1 download).

## 8. New paid resources

Draft request: `docs/requests/0008-backups.md`. Summary: one extra Spaces bucket in a second region and one scoped key (inside the
existing $5 subscription, about $0 marginal at expected volumes), optional phase-3 volume snapshots (about $0.06/GiB/mo). Question
added to `docs/QUESTIONS.md`: "Approve request 0008?". **Default if unanswered**: phase 1 uses only the existing allowance, with
backups under `backups/` in the adopted platform bucket `pico-paas-s3-a` (ATL1, same region as app buckets, so the disaster-isolation
goal is deferred) with a **bucket-scoped key created for backups** (never the platform admin key in a Job). Switching to the
dedicated bucket later is a config change plus `restic copy`, not a rewrite (`BACKUP_BUCKET`, `BACKUP_REGION`).

## 9. Open points to verify before building
- Spaces allowance is account-wide across regions (control panel / billing page); otherwise the second region needs its own subscription decision.
- Spaces region list and capacity at provisioning time (as with DOKS, a region can refuse).
- Exact behaviour of a limited-access Spaces key created through the same API `SpacesStore` uses, scoped to the backup bucket with read/write/delete permission.
- restic memory on the real pod (`GOMEMLIMIT`) under the 320Mi limit with a 1 GiB volume of many small files (WordPress `uploads`); fall back to `--read-concurrency 1`.
- Whether snapshot CRDs (`VolumeSnapshotClass`) are installed on this DOKS version (phase 3 only).
