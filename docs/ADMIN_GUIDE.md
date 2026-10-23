# Administering PicoPaaS

This guide is for platform admins (role `platform_admin`). It is the short, task-oriented version. Deep detail lives in
[OPERATIONS.md](OPERATIONS.md) (admin API reference, key rotation, disaster recovery), the manual runbook in
`deploy/README.md`, and the design record in [ARCHITECTURE.md](ARCHITECTURE.md). App-owner docs are in the
[User Guide](USER_GUIDE.md). It contains no secret values: tokens, keys and kubeconfig contents live in GitHub
secrets and Kubernetes Secrets.

## Architecture at a glance

```
*.picopaas.xyz (A records) -> DO load balancer -> Traefik (ns traefik, wildcard cert from cert-manager)
   platform.picopaas.xyz -> platform (ns picopaas-system, 2 replicas: API + UI; one leader runs the reconciler)
   <generated-host>.picopaas.xyz -> app-<id> namespace (Deployment, Service, Ingress, Secret, quota, netpols)
Managed MySQL (DO, NYC3, private VPC)       platform DB + one database/user per MySQL app
Managed PostgreSQL (DO, NYC3, private VPC)  one database + role per PostgreSQL app
Spaces (ATL1)                               one bucket + key per app, plus the platform bucket
Images: GHCR   State: Terraform Cloud (state only)
```

- The platform is one Python/Vue service. With `RECONCILER_ENABLED=1` it runs four jobs: reconcile every 15 s, database
  quota poll every 60 s, storage quota poll every 5 min, image watcher every 10 min. A lock (`GET_LOCK` on MySQL)
  keeps replicas from double-running.
- The reconciler turns app rows into Kubernetes objects and re-applies any app whose rendered manifest changed. If an
  `app-<id>` namespace disappears (cluster rebuilt) it recreates it.
- All platform state (users, apps, env, DB/bucket rows) is in the platform MySQL database, which survives the cluster.
- The first registered user is the administrator. Later signups stay `pending` until you approve them.

## The DOKS cluster

| Item | Value |
|---|---|
| Cluster | `picopaas-nyc3`, non-HA control plane (do not enable HA: +$40/mo) |
| Node pool | `picopaas-pool-default`, `s-2vcpu-4gb-amd`, 1 node, autoscaler up to `max_nodes` (2) |
| Region | NYC3 for compute and databases; Spaces stays in ATL1 |
| Upgrades | `auto_upgrade` and `surge_upgrade` on; maintenance window Sunday 04:00. Node recycling means a brief blip. |
| API access | Public by default; `control_plane_allowed_cidrs` restricts it. The reconciler runs in-cluster and needs no entry. |

Namespaces: `traefik`, `cert-manager`, `picopaas-system` (the platform), `app-<id>` (one per app), and
`picopaas-data` only for the in-cluster MySQL variant. Traefik is installed with Helm (values in `deploy/traefik/`),
and cert-manager issues the wildcard cert by DNS-01 and per-host certs for custom domains by HTTP-01.

Where things live:

| What | Where |
|---|---|
| Terraform (cluster, DBs, VPC, firewall, DNS, bucket) | `infra/` |
| Traefik, cert-manager, metrics-server values; platform manifests and RBAC | `deploy/` |
| Platform secrets | k8s Secret `picopaas-env` in `picopaas-system`; DB CAs in `picopaas-db-ca` |
| Workflows | `.github/workflows/` |

To get a kubeconfig, run `doctl kubernetes cluster kubeconfig save picopaas-nyc3` with your own DO token. Never
commit the result.

The platform ServiceAccount is deliberately narrow: only a per-namespace RoleBinding lets it write inside `app-*`, and
an admission policy blocks bindings elsewhere. Details: `deploy/README.md` (RBAC section).

## Database servers

PicoPaaS offers two engines as equal choices: managed MySQL and managed PostgreSQL. Each app picks one at creation
(`resources.db_engine`, fixed afterwards).

| | MySQL | PostgreSQL |
|---|---|---|
| Server | DO managed, `db-s-1vcpu-1gb`, 8.4 | DO managed, `db-s-1vcpu-1gb`, 17 |
| Per-app cap | `MAX_USER_CONNECTIONS` 10 (`MAX_DB_USER_CONNECTIONS`) | role `CONNECTION LIMIT` 5 (`PG_MAX_DB_USER_CONNECTIONS`) |
| Capacity | about 76 connections total; 25 databases per server | about 22 usable connections; 25 databases per server |
| Quota (50 MB) | write privileges revoked, sessions killed | stage 1 read-only default, stage 2 `REVOKE CONNECT` at 2x (`PG_HARD_CAP_RATIO`) |
| TLS | required, CA verified | required, CA verified |

Add a server in **Admin > Servers > Add server** (or `POST /admin/db-servers`): name, host, port, admin user, admin
password, `max_databases`, and the CA PEM. The engine is set when the server is created. A new paid server needs a
request in `docs/requests/`. The two managed servers are registered automatically at start from `BOOTSTRAP_DB_*` and
`BOOTSTRAP_PG_*` (written by the `bootstrap` workflow), with CAs from the `picopaas-db-ca` Secret.

- **Placement:** a new app database goes to the active server of its engine with the most free slots. With none free,
  the app goes to **Failed** ("No database space is free") and retries by itself.
- **Drain:** set a server to **Draining** to stop new placements. Existing databases stay; moving them is not built.
- **Capacity:** the Health page flags a server at 80% of `max_databases`, and shows live connections and data size.
- **Egress:** the tenant NetworkPolicy allows the database VPC CIDR (`MYSQL_EGRESS_CIDR`, `PG_EGRESS_CIDR`) on the
  server's port (25060 on DO), not only the app's own server (see Accepted risks).
- **Backups:** DO takes daily backups of both managed clusters (restore from the DO control panel, new cluster). The
  in-cluster MySQL variant uses a nightly dump (see `deploy/README.md`, "MySQL restore").
- **MySQL note:** DO enforces `sql_require_primary_key=ON`, so apps whose migrations create tables without a key
  (for example Wiki.js) fail on MySQL. We keep that default.

## Spaces

One Spaces subscription covers the platform bucket and one bucket plus key per storage-enabled app (ATL1). DO limits an
account to 100 buckets, so roughly 100 storage apps is the wall. The storage quota poll runs every 5 min: over the
cap, the app's key is swapped for a read-only one and the app restarts; owners free space through the platform
(**Free up space**). Credentials for provisioning (`SPACES_ACCESS_KEY_ID`, `SPACES_SECRET_ACCESS_KEY`) are in
`picopaas-env`.

## Deploys and CI

All workflows live in `.github/workflows/`. Most are manual (`workflow_dispatch`).

| Workflow | Use |
|---|---|
| `ci` | Automatic: gitleaks, terraform validate, kubeconform, tests, frontend. |
| `image` | Automatic on main: builds `ghcr.io/seaburr/picopaas:<short sha>`. |
| `deploy` | Rolls a platform version (input `sha`); also runs after a successful `image`. |
| `terraform` | Plan/apply DO infra (untick `apply` for plan only). |
| `bootstrap` | New or rebuilt cluster, or changed add-ons/secrets. Idempotent. |
| `diag` | Prints nodes, pods, events and masked logs for a namespace. |

Order for a new environment: `terraform`, `bootstrap`, register the first user, run the E2E and pentest suites.

Rollback: re-run `deploy` with an earlier `sha` (every image is tagged by commit). Database migrations run at start
and only move forward, so prefer a forward fix when a migration shipped. For a one-off, `kubectl -n picopaas-system
rollout undo deploy/picopaas` also works.

## The admin UI

Open **Admin** (tabs: Users, Apps, Servers, Audit) and **Health** from the nav bar.

- **Users:** approve `pending` signups, suspend or unsuspend (suspending scales all their apps to zero), reset a
  password (shown once; the user must change it), delete, and set the per-user app limit. The platform-wide hard max is
  5 apps per account (`MAX_APPS_PER_USER`). The admin account cannot be suspended or deleted.
- **App limits (Apps tab, sliders icon):** override an app's resources within these ceilings.

  | Setting | Ceiling |
  |---|---|
  | CPU / memory (per pod) | CPU 10 to 1000m, memory 32 to 2048Mi |
  | Replicas | 1 to 3 (`MAX_REPLICAS`) |
  | DB connections | up to 25 |
  | Scratch disk | up to 2 GB |
  | DB quota | up to 1 GB |
  | Bucket quota | up to 50 GB |
  | Image size | up to 2 GB |

  Changing CPU, memory or scratch disk restarts the app's pods. Raising a quota restores an over-quota app at the next
  poll. Replicas must be stateless: each app has one ClusterIP Service and no per-pod addressing, so clustered apps
  are not supported.
- **App actions:** suspend, unsuspend, delete, and a fixed short address (`<label>.picopaas.xyz`, set on the app's
  Settings page).
- **Audit:** newest first, recording registrations, failed logins, approvals, suspensions, app changes, limit
  overrides and server changes. It never stores secrets.
- **Health page:** nodes, per-app readiness, restarts and CPU/memory against capacity, platform pods, and database
  server capacity. It needs metrics-server (installed by `bootstrap`). DO does not expose host CPU/disk for managed
  databases externally; use the DO control panel Insights tab.

## Runbooks

**Stuck deploy.** Open the app's page for the status message and logs. A rollout not ready after 10 minutes fails
with a reason, then retries after 1, 5, 15, then every 60 minutes. For platform problems run `diag`
(`namespace`, `selector`). Typical causes: image pull failure, wrong port, a health check path that never returns 2xx,
no free database slot, unreadable secrets (wrong Fernet key). **Restart** or saving the app retries immediately.

**Over-quota app.** The app shows an over-cap badge. The owner frees space (`TRUNCATE`/`DROP`, **Free up space**
for buckets). To unblock them yourself, raise the quota in the app limits (it restores the app at the next poll, within
a minute for databases). A PostgreSQL app past 2x the cap has had `CONNECT` revoked; it comes back once the size falls
below the cap or the quota is raised.

**Rotate platform secrets.**
- `FERNET_KEY`: set the new value as repo secret `PICOPAAS_FERNET_KEY`, then run `bootstrap`. The old key is kept in
  `FERNET_KEYS_PREVIOUS` and every stored ciphertext is re-encrypted at start. It also logs everyone out (the JWT key
  is derived from it). Keep your own copy: GitHub secrets are write-only. Procedure: OPERATIONS.md, "Key management".
- DO token or Spaces keys: update the GitHub secret, then run `bootstrap` (rewrites `picopaas-env`).
- A single app's DB password or bucket keys: the owner uses **Rotate credentials** in the Danger zone of Settings.

**Teardown and restore.** Deleting an app removes its namespace, database and bucket. To rebuild a lost cluster run
`terraform`, then `bootstrap`; the platform recreates every running app from the database (the namespace sweep). A
leftover load balancer from the old cluster is deleted by `bootstrap`. If a managed database was lost, restore it from
DO backups first. Full steps: OPERATIONS.md, "Disaster recovery".

## Cost model

Baseline about $75.30 per month: node $28, MySQL $15.15, PostgreSQL $15.15, load balancer $12, Spaces $5. The
autoscaler's second node adds $28 while it exists (about $103.30 worst case). Apps cost about nothing for the first 8 to
12 per node. `infra/variables.tf` is the source of truth for sizes; the full table is in ARCHITECTURE.md section 1.
Any new paid resource needs a request in `docs/requests/` with what, why and cost.

## Accepted risks

- One node: recycling causes brief downtime, and both platform replicas share it.
- The platform ServiceAccount can still create, patch and delete Namespaces cluster-wide.
- Shared database subnet: tenant egress allows the whole VPC CIDR on the database port. Valid credentials are still
  needed to get in.
- PostgreSQL trusted extensions (`dblink`, `postgres_fdw`) can open outbound TCP from the database server, but need
  credentials on the target. Tenants can also read other apps' database and role names on PostgreSQL (not data).
- A failed database or bucket delete during app deletion is logged and ignored, so provider-side orphans are possible.
- Public Kubernetes API unless `control_plane_allowed_cidrs` is set.

The list is tracked in OPERATIONS.md section 9 and `docs/PENTEST.md`.
