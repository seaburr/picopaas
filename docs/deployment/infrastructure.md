# Infrastructure

Instance layout for `picopaas.xyz`. Terraform in `infra/` is the source of truth for sizes
(`infra/variables.tf`); the cost table and sources are in [ARCHITECTURE.md section 1](../ARCHITECTURE.md#1-hosting-cost).
For the concepts, see the [Admin guide](../ADMIN_GUIDE.md).

## Layout

```
*.picopaas.xyz (A records -> DO load balancer) -> Traefik (ns traefik, wildcard cert from cert-manager)
   platform.picopaas.xyz -> platform (ns picopaas-system, 2 replicas: API + UI; one leader runs the reconciler)
   <adjective>-<animal>-<7 chars>.picopaas.xyz -> app-<id> namespace (Deployment, Service, Ingress, Secret, quota, netpols)
Managed MySQL (DO, nyc3, private VPC): platform DB `picopaas` + one DB/user per MySQL app
Managed PostgreSQL (DO, nyc3, private VPC, `picopaas-pg-nyc3`): one database + `app_<id>` role per PostgreSQL app
Spaces (atl1): one bucket + key per app, plus the adopted platform bucket pico-paas-s3-a
State: Terraform Cloud (org seaburr, workspace picopaas, local execution = state only)
Images: GHCR (ghcr.io/seaburr/picopaas, tagged with the short commit SHA)
```

Compute and databases are in NYC3 (ATL1 was out of capacity for the node size); Spaces stays in ATL1
(`SPACES_REGION` default `atl1`).

## DOKS cluster

| Item | Value |
|---|---|
| Cluster | `picopaas-nyc3`, non-HA control plane (do not enable HA: +$40/mo) |
| Node pool | `picopaas-pool-default`, `s-2vcpu-4gb-amd`, 1 node, autoscaler up to `max_nodes` (2) |
| Region | NYC3 for compute and databases; Spaces stays in ATL1 |
| Upgrades | `auto_upgrade` and `surge_upgrade` on; maintenance window Sunday 04:00. Node recycling means a brief blip. |
| API access | Public by default; `control_plane_allowed_cidrs` restricts it. The reconciler runs in-cluster and needs no entry. |

Namespaces: `traefik`, `cert-manager`, `picopaas-system` (the platform), `app-<id>` (one per app), and
`picopaas-data` only for the in-cluster MySQL variant. Traefik is installed with Helm (values in `deploy/traefik/`);
cert-manager issues the wildcard cert by DNS-01 and per-host certs for custom domains by HTTP-01.

| What | Where |
|---|---|
| Terraform (cluster, DBs, VPC, firewall, DNS, bucket) | `infra/` |
| Traefik, cert-manager, metrics-server values; platform manifests and RBAC | `deploy/` |
| Platform secrets | k8s Secret `picopaas-env` in `picopaas-system`; DB CAs in `picopaas-db-ca` |
| Workflows | `.github/workflows/` |

Kubeconfig: `doctl kubernetes cluster kubeconfig save picopaas-nyc3` with your own DO token. Never commit the result.

## Managed databases

| | MySQL | PostgreSQL |
|---|---|---|
| Server | DO managed, `db-s-1vcpu-1gb`, 8.4 | `picopaas-pg-nyc3`, DO managed, `db-s-1vcpu-1gb`, 17 |
| Capacity | about 76 connections total; 25 databases per server | about 22 usable connections; 25 databases per server |
| Per-app cap | `MAX_USER_CONNECTIONS` 10 | role `CONNECTION LIMIT` 5 |
| Quota | 50 MB | 50 MB; stage 2 at 2x |

Both servers are registered automatically at start from `BOOTSTRAP_DB_*` and `BOOTSTRAP_PG_*` (written by the `bootstrap`
workflow), with CAs from the `picopaas-db-ca` Secret (`pg-ca.crt` for PostgreSQL, `BOOTSTRAP_PG_CA=/etc/picopaas/db-ca/pg-ca.crt`).
The MySQL one is named `primary` and bootstrap keeps its host, port, admin user, password and CA in sync. Tenant egress
allows the database VPC CIDR (`MYSQL_EGRESS_CIDR`, `PG_EGRESS_CIDR`) on port 25060. DO takes daily backups of both
clusters (restore from the DO control panel into a new cluster). DO enforces `sql_require_primary_key=ON` on MySQL, so
apps whose migrations create keyless tables (for example Wiki.js) fail on MySQL; we keep that default. DO does not
expose host CPU or disk for managed databases externally; use the control panel Insights tab. `DB_METRICS_ENABLED` stays
off (a live scrape fails for MySQL); a tabled feature request tracks it.

Platform DB pool (`DB_POOL_SIZE=5`, `DB_MAX_OVERFLOW=5`, `DB_POOL_TIMEOUT=10`, defaults; not overridden in the manifests):
the platform database `picopaas` lives on the same managed MySQL as the tenant databases, so its connections compete
with them for about 76 total. Each API replica may hold 10 (5 steady, 5 burst) plus one pinned `GET_LOCK` connection;
2 replicas plus 1 surge pod during a rollout is at most about 33, leaving roughly 43 for tenants (each tenant user is
capped at 10). The previous SQLAlchemy defaults (5 + 10, 30 s wait) allowed 45 at peak and turned a burst into
30-second hangs; a 10 s timeout fails fast instead. If tenant DB count grows or replicas are added, lower the pool or
move the platform database to its own server. Per-app connection caps are applied once and recorded
(`app_databases.db_conn_limit_applied`), not re-sent every sweep. In `MYSQL_ADMIN_USE_PROCEDURES=1` mode the cap call is a
no-op (the procedure sets the default at creation), so admin overrides are not applied there; managed MySQL here uses direct
`ALTER USER`, so overrides apply.

## Spaces

One Spaces subscription covers the platform bucket and one bucket plus key per blob-storage app (ATL1). DO limits an
account to 100 buckets, so roughly 100 blob-storage apps is the wall (raising it is a support request). Credentials for
provisioning (`SPACES_ACCESS_KEY_ID`, `SPACES_SECRET_ACCESS_KEY`) live in `picopaas-env`. Credential theft from Spaces is
out of scope (request 0002).

Backups (request 0008 phase 1, enabled 2026-10-23): a dedicated bucket `picopaas-backups` in the same ATL1 Spaces subscription ($0 extra; counts toward the 100-bucket limit), created by bootstrap if missing, restic repos under `backups/`. The second-region bucket from request 0008 is deferred, so a regional ATL1 outage or loss of the Spaces account takes backups too. **Shared key decision (2026-10-23)**: backups reuse the platform Spaces key (`BACKUP_ACCESS_KEY_*` = `SPACES_*`), so a leaked backup Job credential can reach every tenant bucket. Acceptable only while Jobs run in the platform namespace (DB dumps); it must be replaced with a bucket-scoped key before volume backups run in tenant namespaces.

## DNS and domain

The domain `picopaas.xyz` is a DO DNS domain. `bootstrap` upserts only the A records `*` and `picopaas` to the load
balancer IP; TXT and every other record is never touched (leave Terraform `wildcard_lb_ip` empty). Platform host:
`platform.picopaas.xyz`; the Ghost blog app uses the fixed host `blog.picopaas.xyz`. The old domain
`raisehellpraisedale.xyz` was retired on 2026-10-21 (request 0004). The bootstrap check
`doctl compute domain records list picopaas.xyz` fails unless the domain is added in DO under Networking > Domains.

## Cost

Baseline about $75.30/mo: node $28, MySQL $15.15, PostgreSQL $15.15, load balancer $12, Spaces $5. The autoscaler's
second node adds $28 while it exists (about $103.30 worst case). Apps cost about nothing for the first 8 to 12 per node.
Capacity: roughly 8 to 12 apps per node; about 25 app databases per server (PostgreSQL is limited by about 22 usable
connections against the per-app limit of 5); 100 Spaces buckets per account. The project budget target and any
new paid resource go through `docs/requests/`.

`MAX_NODES` (2) is set at bootstrap and is display-only; the real cap is `max_nodes` in Terraform.

## Persistent volumes (request 0007, enabled 2026-10-23)

`VOLUMES_ENABLED=1` is set at bootstrap in `picopaas-env`. Apps that ask for `resources.volume` get one RWO PVC on
StorageClass `do-block-storage` (DOKS default, `dobs.csi.digitalocean.com`, reclaimPolicy Delete, allowVolumeExpansion
true; verified by Chas and re-checked on every bootstrap). Deleting an app deletes its PVC and the DO volume. Caps:
1 GiB per volume for users, 10 GiB admin override (`OVERRIDE_VOLUME_MAX_GI`), `VOLUMES_MAX_TOTAL=10`; at most 15 attached per node. Volumes are
grow-only and not backed up. Cost: $0 static, $0.10/GiB/mo per volume while it exists (worst case with defaults about
$1/mo), not included in the $75.30 baseline.
