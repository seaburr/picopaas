# Administering PicoPaaS

This guide is for anyone running their own PicoPaaS: it covers what the platform needs, how to configure and scale it,
and how to look after it. It is generic. Instance-specific facts (domain, cloud account, node sizes, workflow names)
belong in your own deployment notes; the reference deployment keeps its notes in the repo under
`docs/deployment/`. App-owner docs are in the [User Guide](USER_GUIDE.md), HTTP routes in [API.md](API.md), and the
design record in [ARCHITECTURE.md](ARCHITECTURE.md). This guide contains no secret values; keep tokens, keys and
kubeconfig contents in your secret store.

## How it works

```
*.<your-domain> (wildcard DNS) -> load balancer -> ingress controller (wildcard cert from cert-manager)
   platform.<your-domain> -> platform (API + UI; 1 or more replicas; one leader runs the reconciler)
   <generated-host>.<your-domain> -> app-<id> namespace (Deployment, Service, Ingress, Secret, quota, netpols)
MySQL and/or PostgreSQL server(s)   platform database + one database and user per app
S3-compatible blob storage         one bucket + key per blob-storage app (and a platform bucket)
Container registry                  images are pulled by the cluster; the platform never builds
```

- The platform is one Python/Vue service. Users and apps are rows in the platform database; a **reconciler** turns
  those rows into Kubernetes objects (one namespace per app) and keeps them converged.
- With `RECONCILER_ENABLED=1` the process runs scheduled jobs: reconcile every 15 s, database quota poll (60 s), blob storage
  quota poll (5 min), image watcher (10 min) and a metrics sampler. A database lock (`GET_LOCK` on MySQL) lets several
  platform replicas run while only the leader acts.
- The reconciler re-applies any app whose rendered manifest changed, and recreates the namespace of any running app
  that has gone missing (for example after a cluster rebuild).
- All platform state lives in the platform database, not in the cluster. Rebuild the cluster and the apps come back.
- The first registered user becomes the platform administrator. Later signups stay `pending` until an admin approves.
- Tenants never get cluster access. Each app is one container image, optional database and optional bucket.

## Requirements

| Need | Notes |
|---|---|
| Kubernetes cluster | Network policies must be enforced (a CNI such as Cilium). Cilium's bandwidth manager is needed for the per-pod outbound cap. |
| Ingress controller | Traefik is what the manifests in `deploy/` assume (`INGRESS_CLASS`, `INGRESS_NAMESPACE`). |
| cert-manager | Wildcard certificate by DNS-01 for the generated hosts; per-host HTTP-01 certificates for custom domains. |
| metrics-server | Optional but needed for CPU/memory on the Health page. |
| SQL server(s) | MySQL 8 and/or PostgreSQL 13+, with TLS, an admin account that can create databases and users, reachable from the cluster. MySQL and PostgreSQL are equal choices; run one or both. |
| Blob storage | S3-compatible, able to create buckets and per-bucket access keys. A provider-specific key API is used for provisioning (the reference deployment uses DigitalOcean Spaces). |
| Container registry | Reachable from the nodes. Public images need nothing more. |
| DNS and a domain | A wildcard record to the load balancer and a name for the platform itself. |
| Network | Pod egress to the database port(s), the blob storage endpoint and the registry. |

Any cloud works if it provides the above. The reference deployment runs on DigitalOcean (managed Kubernetes, managed
databases, Spaces); the Terraform and manifests in the repo show one way to wire it.

## Configuration

The platform reads environment variables once at start. `app/config.py` is the single source of truth for every
default and ceiling; the tables below describe the knobs, not values. Outside `ENVIRONMENT=dev`, `FERNET_KEY` is
required.

**Core and secrets**

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | `dev` (default) allows insecure fixed keys and SQLite. Use anything else in production. |
| `DATABASE_URL` | The platform's own metadata database (MySQL in production). |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | Connections the platform keeps open to its metadata database per process (default 5) and extra connections allowed under burst (default 5). Ignored for SQLite. Size so that (pool size + overflow) x API replicas (+1 replica for rollouts, +1 pinned connection each) stays well under the database server's `max_connections`, leaving room for tenant databases if they share the server. |
| `DB_POOL_TIMEOUT`, `DB_POOL_RECYCLE` | Seconds a request waits for a free connection before failing (default 10), and the age after which a connection is replaced (default 1800, keep below the server's `wait_timeout`). |
| `FERNET_KEY` | Encrypts every stored secret. The JWT signing key is derived from it. |
| `FERNET_KEYS_PREVIOUS` | Comma-separated older keys, for rotation. |
| `SECRET_KEY` | Optional explicit JWT key; normally derived from `FERNET_KEY`. |
| `RECONCILER_ENABLED` | `1` starts the scheduled jobs. Off in tests. |
| `RATE_LIMIT_ENABLED` | Login and signup rate limiting; on by default. |

**Persistent volumes** (off by default; a new paid resource, see `docs/requests/0007-persistent-volumes.md`)

| Variable | Purpose |
|---|---|
| `VOLUMES_ENABLED` | `1` lets apps request a persistent volume (one `ReadWriteOnce` PVC each). Off: the API answers 422 "Persistent volumes aren't enabled on this platform." and the reconciler never renders a PVC. Turning it off later stops mounting volumes into existing apps; it does not delete a claim or its data. |
| `VOLUME_STORAGE_CLASS` | StorageClass for the claims (default `do-block-storage`). It must have `reclaimPolicy: Delete` and allow volume expansion. |
| `VOLUME_MAX_GI` | Largest volume a user can ask for (default 1, the provider minimum). Clamped to `OVERRIDE_VOLUME_MAX_GI` at startup, with a log warning. |
| `OVERRIDE_VOLUME_MAX_GI` | Largest volume a platform admin can grant (default 10 GiB). Enforced on create, spec, PATCH and the admin resources endpoint, and shown to the admin dialog via `/auth/me` `limits.volume_override_max_gi`. |
| `VOLUMES_MAX_TOTAL` | Platform-wide cap on live apps with a volume (default 10). Adding one beyond it is a 409. Keep it under the per-node attach limit of your provider (15 on DigitalOcean). The admin health page shows the count as `workloads.with_volume`. |

**Names and domains**

| Variable | Purpose |
|---|---|
| `APP_DOMAIN` | Base domain for generated app hosts. Set it to your own. |
| `PLATFORM_HOST`, `APEX_HOST`, `LEGACY_HOSTS` | Host of the platform UI/API, the bare domain, and extra hosts that redirect to the platform. |
| `CUSTOM_DOMAINS_ENABLED` | Allow owners to attach their own domains. |
| `CUSTOM_DOMAIN_CNAME_TARGET`, `CUSTOM_DOMAIN_CHALLENGE_LABEL`, `CUSTOM_DOMAIN_ISSUER` | The CNAME owners point at, the TXT label for verification, and the cert-manager issuer used for per-host certificates. |
| `INGRESS_CLASS`, `INGRESS_NAMESPACE` | Which ingress controller apps attach to. |

**Database servers**

| Variable | Purpose |
|---|---|
| `BOOTSTRAP_DB_HOST`, `_PORT`, `_USER`, `_PASSWORD` | If all are set and no MySQL server is registered, a server named `primary` is created and kept in sync on later starts. |
| `BOOTSTRAP_PG_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `BOOTSTRAP_PG_CA` | The same for a PostgreSQL server. The CA is a file path to a PEM. |
| `MYSQL_SSL_CA` | CA file path for the bootstrapped MySQL server. |
| `MYSQL_EGRESS_MODE`, `MYSQL_EGRESS_CIDR`, `PG_EGRESS_CIDR` | How tenant NetworkPolicies reach the database: `in-cluster` (a pod in the data namespace) or `cidr` (the network that holds a managed server). |
| `MYSQL_ADMIN_USE_PROCEDURES` | `1` when the MySQL admin user works through stored procedures (in-cluster variant). |
| `MAX_DB_USER_CONNECTIONS`, `PG_MAX_DB_USER_CONNECTIONS` | Per-app connection cap for each engine. |
| `PG_HARD_CAP_RATIO`, `PG_STATEMENT_TIMEOUT_MS`, `PG_IDLE_TX_TIMEOUT_MS` | PostgreSQL second-stage quota multiple and session timeouts. |
| `DB_METRICS_ENABLED`, `DB_METRICS_PORT`, `DB_METRICS_CA`, `MANAGED_DB_DISK_MB` | Optional host metrics scrape (off by default) and disk size for the Health page percentage. |

**Blob storage provisioning**

`SPACES_ACCESS_KEY_ID`, `SPACES_SECRET_ACCESS_KEY` and `SPACES_REGION` identify the S3 account that creates buckets;
`DIGITALOCEAN_TOKEN` is needed on that provider to mint per-bucket keys. Other providers need a matching provider
module.

**Caps and polling**

| Variable | Purpose |
|---|---|
| `DB_QUOTA_BYTES`, `STORAGE_QUOTA_BYTES` | Default database and blob storage bucket quota per app. |
| `MAX_IMAGE_SIZE_BYTES` | Largest image accepted. |
| `DEFAULT_CPU_LIMIT_M`, `DEFAULT_MEM_LIMIT_MI`, `EPHEMERAL_LIMIT_MI` | Default per-pod CPU, memory and scratch disk. |
| `EGRESS_BANDWIDTH_MBIT` | Default per-pod outbound cap. |
| `MAX_APPS_PER_USER`, `MAX_REPLICAS` | Hard ceilings for apps per account and replicas per app. |
| `AUDIT_RETENTION_DAYS` | Audit log retention in days (default 90). The leader deletes older rows daily and records one `audit.purge` entry (actor `system`) when it removed any. `0` or negative keeps everything. |
| `DB_QUOTA_POLL_SECONDS`, `STORAGE_QUOTA_POLL_SECONDS`, `IMAGE_WATCH_MINUTES`, `METRICS_SAMPLE_SECONDS`, `METRICS_RETENTION_POINTS` | Job cadences and history depth. |
| `MAX_NODES` | Display only: the node ceiling shown on the Health page. Set it to your autoscaler maximum. |

After changing caps, running apps pick up new container limits, LimitRange and ResourceQuota on the next reconcile
(pods restart once). Existing databases and buckets over a lowered quota are limited at the next poll; images over a
lowered cap keep running but cannot be redeployed until they shrink.

## Secrets and keys

| Secret | Purpose |
|---|---|
| `FERNET_KEY` | The one platform key. Encrypts env var values, app database passwords, bucket secret keys and database-server admin passwords. |
| Blob storage and cloud credentials | Used by the platform to provision buckets and keys. |
| Database admin credentials | One per registered server, stored encrypted. |

Keep an off-cluster copy of `FERNET_KEY`. Lose it and all stored ciphertext is unreadable. A wrong key does not stop
the reconciler: affected apps go to **Failed** ("Your saved secrets can't be read") and owners re-enter secrets or
rotate credentials. Deleting apps never needs the old ciphertext.

**Rotate `FERNET_KEY`:**

1. Generate a key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and
   store it.
2. Put the old key in `FERNET_KEYS_PREVIOUS` (comma-separated) and the new one in `FERNET_KEY`.
3. Restart the platform. It decrypts with the new key, then the previous ones, and re-encrypts every stored value to
   the new key. The log line `fernet rotation: rotated=... unchanged=... skipped_undecryptable=...` shows counts only.
4. When a later start reports `rotated=0` and nothing skipped, drop the previous keys.

Rotating also signs everyone out, because the JWT key is derived from it. Rotate a single app's database password and
bucket keys from the app's Settings (**Rotate credentials**); the owner can do it.

## Database servers

Each app picks one engine at creation (`db_engine`), fixed afterwards. Both are first-class.

| | MySQL | PostgreSQL |
|---|---|---|
| Per-app connection cap | `MAX_USER_CONNECTIONS` (`MAX_DB_USER_CONNECTIONS`) | role `CONNECTION LIMIT` (`PG_MAX_DB_USER_CONNECTIONS`) |
| Quota over the cap | write privileges revoked, sessions killed | stage 1 read-only default, stage 2 `REVOKE CONNECT` at `PG_HARD_CAP_RATIO` times the quota |
| TLS | required, CA verified | required, CA verified |

Register a server in **Admin > Servers > Add server** (or `POST /admin/db-servers`): name, host, port, admin user, admin
password, `max_databases` and the CA PEM. The engine is set when the server is created. A server is also registered
automatically at start from the `BOOTSTRAP_*` variables.

- **Connection check:** on create (and when the admin password or CA changes) the platform connects with the given
  credentials and runs `SELECT 1` (5 s timeout) before saving. A failure returns a short reason (authentication failed,
  timed out, refused, TLS handshake failed, host not found) and nothing is saved.
- **Rotate the admin password:** change it on the database server first, then use the lock button on the server row
  (Admin > Database servers) or `PATCH /admin/db-servers/{id}` with `{"admin_password": "..."}`. The platform verifies
  the new password and only then stores it (encrypted); if the check fails the old password stays. Running apps are
  unaffected (they use their own per-app credentials); the reconciler reads the stored password on every use.
- **Placement:** a new app database goes to the active server of its engine with the most free slots. With none free
  the app goes to **Failed** ("No database space is free"), and retries by itself once you add or free space.
- **Drain:** set a server to **Draining** to stop new placements. Existing databases stay; moving a database between
  servers is not built.
- **Capacity:** size `max_databases` to the server's connection budget (per-app caps times databases must fit the
  server's `max_connections`). The Health page flags a server at 80% of `max_databases` and shows live connections and
  data size.
- **Egress:** tenant NetworkPolicies allow the network of the database server(s) on the server port (see Security).
- **Managed-provider quirks:** some providers force `sql_require_primary_key=ON` on MySQL, so apps whose migrations
  create keyless tables fail there. Choose PostgreSQL for those apps.
- **In-cluster MySQL:** supported (`MYSQL_EGRESS_MODE=in-cluster`, `MYSQL_ADMIN_USE_PROCEDURES=1`); the manifests are in
  `deploy/mysql/`, including a nightly dump job. Prefer a managed server if your provider has one.

## Blob storage

One bucket and one access key per blob-storage app. Check your provider's bucket-per-account limit; it is the
ceiling for blob-storage apps. The blob storage poll lists each bucket: over its quota, the app's key is swapped for a read-only
one and the app restarts, and owners free space with **Free up space**. Raising the quota restores the app.

## The admin UI

Open **Admin** (tabs: Users, Apps, Servers, Audit) and **Health** from the nav bar.

- **Users:** approve `pending` signups; suspend or unsuspend (suspending scales all the user's apps to zero); reset a
  password (shown once, the user must change it); delete; set the per-user app limit. The hard max is
  `MAX_APPS_PER_USER` active apps per account, administrators included. The per-user `app_limit` defaults to 1 and
  can be raised up to that max, never above. The platform administrator cannot be suspended or deleted.
- **Apps:** suspend, unsuspend, delete, open the app, change limits (sliders icon, below) and set replicas.
- **Platform host:** give an app a fixed short address `<label>.<your-domain>` besides its generated one. It is served
  by the wildcard certificate. Reserved labels (`platform`, `www`, `api`, `lb`, `admin`, ...) are refused and a label
  can belong to one app.
- **Servers:** register, drain or update database servers (above).
- **Audit:** newest first. It records registrations, failed logins, password changes and resets, API key changes, app
  create/update/delete/restart/rotate, image updates, limit and replica changes, user approvals, suspensions and
  deletions, and server changes. It never stores secrets; `detail` is capped at 255 characters. Action names:
  `user.register`, `user.register_duplicate`, `login.failed`, `user.change_password`, `user.reset_password`,
  `user.update`, `user.approve`, `user.suspend`, `user.unsuspend`, `user.delete`, `apikey.create`, `apikey.revoke`,
  `app.create`, `app.update`, `app.delete`, `app.rotate`, `app.restart`, `app.image_updated`, `app.resources`,
  `app.replicas`, `app.storage.delete`, `app.suspend`, `app.unsuspend`, `dbserver.create`, `dbserver.update`.
- **Health:** nodes, per-app readiness, restarts, CPU and memory against capacity (needs metrics-server), platform
  pods, and database server capacity. Managed databases usually do not expose host CPU or disk externally; use your
  provider's console for those.

Every admin action is also an HTTP route under `/admin`; see [API.md](API.md).

## Admins and recovery

The first account to register becomes an administrator. Keep at least two: one person locked out of their account
shouldn't lock you out of the platform.

- **Promote**: **Admin → Users → Make admin** on any active user. Pending or suspended users must be approved or
  unsuspended first. Administrators have no app limit.
- **Demote**: **Remove admin** returns an administrator to a normal account. You can demote yourself only while
  another active administrator exists.
- **Last-admin protection**: the platform refuses (409) to demote, suspend or delete the last active administrator.
  Administrators can't be suspended or deleted directly; demote them first.
- **Audit**: promotions and demotions are logged (`user.promote`, `user.demote`). **Admin → Audit** downloads the full
  log (up to 50,000 newest rows) as CSV or JSON, streamed, and each download is logged too (`audit.export`).
  Rows older than `AUDIT_RETENTION_DAYS` (default 90) are deleted daily (`audit.purge`); download the export
  regularly if you need history beyond that window.

### Break-glass recovery

If no administrator can log in (lost password, departed admin), use the recovery CLI inside an API pod. It talks to
the platform database directly, so it needs cluster access, not a login:

```bash
manage() {
  kubectl -n <platform-namespace> exec -it deploy/<api-deployment> -- python -m app.manage "$@"
}
manage list-admins
manage grant-admin you@example.com
manage reset-password you@example.com
```

- `list-admins` prints each administrator's id, email and status.
- `grant-admin <email>` makes an existing user an administrator and activates them if pending or suspended.
- `reset-password <email>` prints a one-time temporary password; the user must change it at next login.

Each command records an audit entry with actor `manage-cli`. Whoever holds cluster credentials can make themselves
an administrator this way (and can read the signing and encryption keys from the pod environment), so treat
kubeconfig access as full platform control.

## Quotas and enforcement

Defaults come from `app/config.py`; an admin can override most per app.

| Resource | Enforced by |
|---|---|
| CPU, memory, scratch disk | Pod limits, a LimitRange and a ResourceQuota in the app namespace. A pod over its scratch disk is evicted. |
| Image size | Checked at deploy and by the image watcher. A larger image already running is unaffected. |
| Database size | Quota poll (60 s). See the engine table above. |
| Bucket size | Quota poll (5 min). The key is swapped for a read-only one. |
| Outbound bandwidth | Per-pod cap through the `kubernetes.io/egress-bandwidth` annotation and Cilium. It covers everything the pod sends, HTTP responses included. |
| Apps per account | `MAX_APPS_PER_USER` hard max, with a lower per-user `app_limit`. |

**Per-app overrides** (Apps tab, sliders icon, or `PATCH /admin/apps/{id}/resources`). Ceilings are constants in
`app/config.py` (`OVERRIDE_*`):

| Setting | Range |
|---|---|
| CPU / memory (per pod) | CPU 10 to 1000m, memory 32 to 2048Mi |
| Replicas | 1 to `MAX_REPLICAS` |
| DB connections | 1 to 25 |
| Scratch disk | 64 to 2048 MiB |
| DB quota | 1 to 1024 MB |
| Bucket quota | 1 to 51200 MB |
| Image size | 1 to 2048 MB |
| Outbound bandwidth | 1 to 160 Mbit/s |
| Persistent volume | 1 to 10 GiB (`OVERRIDE_VOLUME_MAX_GI`), grow-only (the app must already have one) |

Clear a field to return to the default. Changing CPU, memory, scratch disk or outbound bandwidth rolls the app's pods.
Changing a quota runs that app's check immediately, so raising it restores a read-only database or bucket at once.
Connection caps apply immediately and on each reconcile sweep (not in MySQL procedure mode).

**Replicas** are an admin setting. The ResourceQuota is sized for the replicas plus one rolling-update surge pod; each
replica gets the full per-pod limits and all replicas share the app's one database and bucket, not local disk. An
extra NetworkPolicy lets an app's own pods talk to each other. Replicas must be stateless: an app has one ClusterIP
Service and no per-pod addressing, so clustered or gossip apps that need named peers are not supported.

## Persistent volumes

Enable with `VOLUMES_ENABLED=1` after the cost is approved. Persistent volumes are block storage, billed per GiB per month for as long as
the volume exists, including while the app is stopped, so cost is (sum of volume sizes) x the provider's per-GiB price;
the count and size caps above bound it. What the platform does for an app with a volume:
- Renders a PVC `app-<id>-data` (RWO, `VOLUME_STORAGE_CLASS`), mounts it at the app's mount path, and sets the pod
  `fsGroup` 10001 so the non-root user can write.
- Uses the `Recreate` strategy and exactly one replica (setting replicas above 1 is refused with 409). Every deploy,
  restart and node drain is a short outage while the volume detaches and reattaches.
- Adds `persistentvolumeclaims: 1` and `requests.storage: <size>` to the namespace ResourceQuota.
- Never deletes the claim on restart or redeploy. It goes with the app: deleting the namespace deletes the claim and, with
  `reclaimPolicy: Delete`, the CSI driver removes the volume. Check once with
  `kubectl get storageclass <class> -o jsonpath='{.reclaimPolicy}'` (DigitalOcean's `do-block-storage` is `Delete`; its
  `-retain` variant is not suitable). If a claim ever leaves an orphaned PV, delete the PV by hand; the platform has no
  PersistentVolume permission on purpose.
- Grow with `PATCH /admin/apps/{id}/resources {"volume_size_gi": N}` (1 to `OVERRIDE_VOLUME_MAX_GI`, default 10, only up). The claim is expanded online: the API supports growing a volume after creation (DigitalOcean's CSI driver expands the filesystem online), but this is not yet tested live on this platform, so plan the size up front.
  The storage class cannot be changed on an existing claim.
- Users can't detach a volume; deleting the app is the only way to drop it.
- Not backed up yet (volume backups are phase 1b of App backups below): add snapshots before relying on it for data that cannot be recreated.

Apply `deploy/platform/rbac.yaml` before enabling (it adds namespaced `persistentvolumeclaims` rights to `picopaas-app-ns`).

## App backups

Off by default (`BACKUPS_ENABLED=0`): with the flag off nothing runs, no namespace exists and every backup endpoint says so.
Backs up **databases** (MySQL and PostgreSQL) and **blob storage buckets** of apps that have them, and gives admins a restore
helper (below); volume backups and snapshot download come next. Design: `docs/design/backups.md`; paid-resource request: `docs/requests/0008-backups.md`.

How it works: a leader-only scheduler job (`BACKUP_TICK_SECONDS`, default 60) queues a run per app at its daily slot
(`BACKUP_SCHEDULE_HOUR` UTC plus a per-app minute offset) and starts a Kubernetes Job in namespace `picopaas-backup`
(at most `BACKUP_MAX_CONCURRENT`, one dump per DB server at a time). The Job gets an ephemeral Secret (the app's own DB
user, repo URL and password, the bucket key), runs `mariadb-dump` or `pg_dump` into `restic backup --stdin`, then
`restic forget --prune` with the retention below, and the platform deletes the Job and Secret once it records the result.
Each app has its own restic repository per kind (`<BACKUP_PREFIX>apps/<app id>/db` and `.../bucket`) and a random password stored encrypted in the
platform database (rotated with `FERNET_KEY`; if you lose the key and the database the backups are unreadable).

Enable:
1. Create the bucket and a key limited to it (request 0008; keep it separate from the tenant Spaces key and the platform
   admin key). Put these in `picopaas-env`: `BACKUPS_ENABLED=1`, `BACKUP_BUCKET`, `BACKUP_REGION` (or `BACKUP_ENDPOINT`),
   `BACKUP_ACCESS_KEY_ID`, `BACKUP_SECRET_ACCESS_KEY`. With any of bucket, endpoint or key missing the feature is
   "misconfigured": the API answers 503, nothing runs, and the Health and `/admin/backups` pages say so. The tenant
   `SPACES_*` key is never used as a fallback.
2. Make the image pullable: `ghcr.io/seaburr/picopaas-backup` is built by `.github/workflows/backup-image.yml` from
   `images/backup/`. A new GHCR package is private; set it to **Public** once (GitHub > Packages > picopaas-backup > Package
   settings > Change visibility) so the cluster can pull it anonymously. Pin by digest with `BACKUP_IMAGE` in production.
3. Deploy: `deploy.yml` applies `deploy/platform/backup.yaml` (namespace, quota, minimal RBAC, NetworkPolicies limited to
   the DB CIDR and HTTPS to public addresses) only while `BACKUPS_ENABLED=1` in `picopaas-env`; it reads the DB CIDR from
   `MYSQL_EGRESS_CIDR` / `PG_EGRESS_CIDR`. Apply it by hand the same way if you deploy manually.

**Blob storage (`bucket` kind).** Scheduled in the same nightly window as the database dump, as its own run with its own
`bucket_last_*` status (an app is `failing` if either kind's last run failed). The Job copies every object of the app's bucket
into an emptyDir at `/work` with `s5cmd` (pinned, checksum-verified in `images/backup/Dockerfile`), checks that the number of
files copied equals the number of objects listed, runs `restic backup /work/bucket`, deletes the scratch copy, then forget/prune.
The app's own bucket key (the platform holds it; there is no separate read-only key per backup) travels in the per-run Secret as
`SRC_*` and reaches only the `s5cmd` process environment, never an argument; the backup bucket's key is a different pair. The
emptyDir size is `BACKUP_BUCKET_MAX_BYTES` (default 512 MiB, plus headroom; the namespace quota in `deploy/platform/backup.yaml` allows two such Jobs at once, so raise both together): a bucket whose last measured size is larger fails the
run with a clear error rather than being half-copied. Bucket repo bytes count toward the per-app and platform caps, and a deleted
app gets a final bucket backup too. Note that `restic forget --keep-daily` keeps the newest snapshot of each day, so a second
backup on the same day (for example a manual one) replaces the earlier one when it is pruned.

**Restore helper (admin only).** `POST /admin/backups/{run_id}/restore` with `{"target_app_id": N}` (CLI:
`picopaas admin backup restore <run_id> --to <app>`) restores a succeeded database or bucket run into the target app. It
**replaces** what is there: for a database the Job runs as the target app's own DB role, drops every table and view, then loads the
dump (DEFINER clauses are stripped so views and routines are created as the target role); for a bucket it empties the target
bucket and uploads the snapshot's files, then checks the object count. The run row belongs to the target app (trigger `restore`,
`source_run_id` points at the backup), so a target can have only one backup or restore active, and the Job/Secret lifecycle is the
same as a backup's. Restoring over the app the backup came from is refused unless the body has `"overwrite_source": true`
**and** the app is suspended (`POST /admin/apps/{id}/suspend` first; checked again when the Job starts). The source may be a
deleted app whose repository is still within its 14 days. Audit: `backup.restore` (the admin, with run ids), then
`backup.restore_succeeded` / `backup.restore_failed` (actor system). Restores do not touch the target's own backup summary or
send the owner an event. Watch progress in `GET /apps/{target}/backups`. Restoring a database snapshot taken on one engine into an app
on the other fails with "wrong engine". Run ids come from `GET /apps/{id}/backups` (`runs[]`, status `succeeded`). A snapshot
that retention already pruned fails with "restoring the snapshot from the repository failed".

**A backup Job that shows Running and prints nothing.** Inside the DO VPC the Spaces endpoint resolves to a private IP, which the
namespace's "public addresses only" rule excludes; restic then retries the connection silently for ~15 minutes per command. The
platform now keeps a NetworkPolicy `allow-egress-spaces` (the endpoint's private /32s on 443) current from the scheduler tick
(the Role in `backup.yaml` grants that), the script prints `PICOPAAS_STEP ...` progress lines (read them with
`kubectl -n picopaas-backup logs job/bk-<run id>` or the diag workflow), every network step runs under `timeout`
(`QUICK_TIMEOUT` 120 s, `LONG_TIMEOUT` 1500 s), `mariadb-dump --connect-timeout=20` / `PGCONNECT_TIMEOUT=20`, Jobs have
`backoffLimit: 0` and `activeDeadlineSeconds` (`BACKUP_JOB_DEADLINE_SECONDS`, 1800), and the scheduler fails a run two minutes
after that deadline even if Kubernetes never reports it, freeing its DB-server slot.

Knobs (defaults in `app/config.py`): retention `BACKUP_KEEP_DAILY` / `_WEEKLY` / `_MONTHLY` = 1 / 1 / 1 (Chas, 2026-10-23: owners archive anything older themselves); per-app cap
`BACKUP_MAX_BYTES_PER_APP` = 2 GiB; platform cap `BACKUP_MAX_BYTES_TOTAL` = 100 GiB (warning at 80%); `BACKUP_UPLOAD_LIMIT_KIB`
(2048 per Job); `BACKUP_JOB_DEADLINE_SECONDS` (1800); `BACKUP_DELETED_RETENTION_DAYS` (14). Backup bytes do not count against
the 50 MB database or 100 MB storage caps. Over a cap, the backup is skipped with a failed run and an app event, never silently.

Monitoring: `GET /admin/backups` and the `backups` block of `GET /admin/health` give ok / pending / stale (no success in
36 hours, 8 days for weekly) / failing counts, repo bytes against the cap and per-app detail. A failed run also adds a
`backup_failed` event to the app and an audit row (`backup.failed`, actor system).

Deleting an app: the platform takes one final backup (the delete waits for it, at most two hours, and a failure does not block
it), keeps the repositories for 14 days, then purges them (`backup.retained` is audited). Purge forgets all snapshots and prunes the
data; restic's small config and key files stay in the bucket. DigitalOcean's own managed-database backups remain the
disaster layer underneath.

Restore is manual until phase 2: read the snapshot with restic using the repo URL and the decrypted password (an admin
tool for this ships with phase 1b).

## Scaling

- **More compute:** add nodes (or raise the autoscaler maximum and set `MAX_NODES` to match). Apps request a small
  CPU/memory slice and are capped higher, so density is set by requests: node allocatable divided by the default
  request, minus the platform and add-ons. Plan for a handful of apps per small node; check the Health page, since
  capacity is reported against the limits.
- **More databases:** register another server of the same engine and raise capacity by `max_databases`. Keep
  per-app connection caps times databases within each server's connection limit; the connection budget, not disk, is
  usually the first wall (especially on small PostgreSQL servers).
- **More buckets:** limited by your provider's bucket-per-account cap.
- **Platform replicas:** run two or more for availability. Only the lock holder runs the reconciler and quota jobs, so
  extra replicas add API capacity and failover, not reconcile speed. Run a PodDisruptionBudget once you have two nodes.
- **Single node:** recycling a node (provider upgrades, autoscaling) causes a brief blip, and all platform replicas
  share that node. Spread across nodes for real availability.
- **Costs and caps:** every new paid resource should be budgeted before you add it.

## Backups and upgrades

- **Platform database:** the most important data. Use your provider's daily backups or your own dumps and test a
  restore. The in-cluster MySQL manifests include a nightly dump job.
- **App databases:** same servers, same backups. A restore brings back every tenant database on that server.
- **Blob storage:** buckets are not backed up by the platform; tell owners.
- **Key copy:** keep `FERNET_KEY` and the provider credentials in a secret manager.
- **Upgrades:** build and publish a new platform image, then roll the Deployment. Migrations (Alembic) run at start and
  only move forward, so prefer a forward fix when a migration has shipped. `kubectl rollout undo` works for a rollback
  that has no migration. Cluster upgrades recycle nodes; pick a quiet window.
- **Migrations:** `alembic upgrade head` with the same environment as the platform, or let the start-up do it.

## Runbooks

**Stuck deploy.** Open the app's page for its status message and logs. A rollout not ready after 10 minutes fails with a
reason, then retries after 1, 5, 15, then every 60 minutes. For platform problems read the platform pod logs and
events. Typical causes: image pull failure, wrong port, a health check path that never returns 2xx, no free database
slot, unreadable secrets (wrong Fernet key). **Restart**, or saving the app, retries at once.

**Over-quota app.** The app shows an over-cap badge. The owner frees space (`TRUNCATE`/`DROP`, **Free up space** for
buckets). To unblock them yourself, raise the quota in the app's limits. A PostgreSQL app past the stage-2 multiple has
had `CONNECT` revoked; it returns once the size falls below the cap or the quota is raised.

**Unknown hosts and suspended apps.** An unknown host under your domain hits a low-priority catch-all Ingress and gets
the platform's 404 page. A suspended app has its Ingress removed (the Deployment stays at zero replicas), so its host
falls through to the same catch-all, which answers 503 with a suspended page.

**Teardown and rebuild.** Deleting an app removes its namespace, database and bucket. After rebuilding a lost cluster,
install the add-ons, point DNS at the new load balancer and start the platform with the same database and keys: the
reconciler recreates every running app from the database. Delete a stale load balancer left by the old cluster if
names clash. If a database server was lost, restore it from backup first. Verify with `/healthz`, a login, the Health
page and one app.

## Security model

- **Tenant isolation:** each app has its own namespace with default-deny NetworkPolicies, then allows ingress from the
  ingress controller, DNS, the public internet and the database port. The internet allowance excludes private,
  link-local and reserved ranges and common abuse ports.
- **Pods:** run non-root with no privilege escalation; images that name a user are forced to a fixed UID.
- **Platform ServiceAccount:** writes inside `app-*` namespaces only through per-namespace RoleBindings, with an
  admission policy blocking bindings elsewhere. See `deploy/README.md` for the RBAC details.
- **Secrets:** Fernet-encrypted at rest; shown to their owner only. The audit log never stores them.
- **Accounts:** signups need admin approval; login and signup are rate limited; passwords are hashed.
- **Databases:** each app gets its own database and user with a connection cap, TLS required and the CA verified.

**Accepted risks.** Review these for your own threat model; the test notes are in [PENTEST.md](PENTEST.md).

- A single node means node recycling causes brief downtime.
- The platform ServiceAccount can create, patch and delete Namespaces cluster-wide, and can read Secrets cluster-wide.
- If database servers share a network, tenant egress allows that whole network on the database port. Valid credentials
  are still needed; narrowing to each server's address means pinning managed IPs.
- On PostgreSQL, a tenant owns schema `public`, so trusted extensions such as `dblink` and `postgres_fdw` can open
  outbound TCP from the database server. They still need credentials on the target. Tenants can also read other apps'
  database and role names (not data or password hashes).
- A failed database or bucket delete during app deletion is logged and ignored, so provider-side orphans are possible.
- A public Kubernetes API unless your provider lets you restrict it to known networks.
- Blob storage credential theft is not a goal of the platform's isolation.
