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
S3-compatible storage               one bucket + key per storage-enabled app (and a platform bucket)
Container registry                  images are pulled by the cluster; the platform never builds
```

- The platform is one Python/Vue service. Users and apps are rows in the platform database; a **reconciler** turns
  those rows into Kubernetes objects (one namespace per app) and keeps them converged.
- With `RECONCILER_ENABLED=1` the process runs scheduled jobs: reconcile every 15 s, database quota poll (60 s), storage
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
| Object storage | S3-compatible, able to create buckets and per-bucket access keys. A provider-specific key API is used for provisioning (the reference deployment uses DigitalOcean Spaces). |
| Container registry | Reachable from the nodes. Public images need nothing more. |
| DNS and a domain | A wildcard record to the load balancer and a name for the platform itself. |
| Network | Pod egress to the database port(s), the storage endpoint and the registry. |

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
| `FERNET_KEY` | Encrypts every stored secret. The JWT signing key is derived from it. |
| `FERNET_KEYS_PREVIOUS` | Comma-separated older keys, for rotation. |
| `SECRET_KEY` | Optional explicit JWT key; normally derived from `FERNET_KEY`. |
| `RECONCILER_ENABLED` | `1` starts the scheduled jobs. Off in tests. |
| `RATE_LIMIT_ENABLED` | Login and signup rate limiting; on by default. |

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

**Storage provisioning**

`SPACES_ACCESS_KEY_ID`, `SPACES_SECRET_ACCESS_KEY` and `SPACES_REGION` identify the S3 account that creates buckets;
`DIGITALOCEAN_TOKEN` is needed on that provider to mint per-bucket keys. Other providers need a matching provider
module.

**Caps and polling**

| Variable | Purpose |
|---|---|
| `DB_QUOTA_BYTES`, `STORAGE_QUOTA_BYTES` | Default database and bucket quota per app. |
| `MAX_IMAGE_SIZE_BYTES` | Largest image accepted. |
| `DEFAULT_CPU_LIMIT_M`, `DEFAULT_MEM_LIMIT_MI`, `EPHEMERAL_LIMIT_MI` | Default per-pod CPU, memory and scratch disk. |
| `EGRESS_BANDWIDTH_MBIT` | Default per-pod outbound cap. |
| `MAX_APPS_PER_USER`, `MAX_REPLICAS` | Hard ceilings for apps per account and replicas per app. |
| `DB_QUOTA_POLL_SECONDS`, `STORAGE_QUOTA_POLL_SECONDS`, `IMAGE_WATCH_MINUTES`, `METRICS_SAMPLE_SECONDS`, `METRICS_RETENTION_POINTS` | Job cadences and history depth. |
| `MAX_NODES` | Display only: the node ceiling shown on the Health page. Set it to your autoscaler maximum. |

After changing caps, running apps pick up new container limits, LimitRange and ResourceQuota on the next reconcile
(pods restart once). Existing databases and buckets over a lowered quota are limited at the next poll; images over a
lowered cap keep running but cannot be redeployed until they shrink.

## Secrets and keys

| Secret | Purpose |
|---|---|
| `FERNET_KEY` | The one platform key. Encrypts env var values, app database passwords, bucket secret keys and database-server admin passwords. |
| Storage and cloud credentials | Used by the platform to provision buckets and keys. |
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

## Object storage

One bucket and one access key per storage-enabled app. Check your provider's bucket-per-account limit; it is the
ceiling for storage apps. The storage poll lists each bucket: over its quota, the app's key is swapped for a read-only
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
  log as CSV or JSON, and each download is logged too (`audit.export`).

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

Clear a field to return to the default. Changing CPU, memory, scratch disk or outbound bandwidth rolls the app's pods.
Changing a quota runs that app's check immediately, so raising it restores a read-only database or bucket at once.
Connection caps apply immediately and on each reconcile sweep (not in MySQL procedure mode).

**Replicas** are an admin setting. The ResourceQuota is sized for the replicas plus one rolling-update surge pod; each
replica gets the full per-pod limits and all replicas share the app's one database and bucket, not local disk. An
extra NetworkPolicy lets an app's own pods talk to each other. Replicas must be stateless: an app has one ClusterIP
Service and no per-pod addressing, so clustered or gossip apps that need named peers are not supported.

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
- **Object storage:** buckets are not backed up by the platform; tell owners.
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
- Storage credential theft is not a goal of the platform's isolation.
