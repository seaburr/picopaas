# PicoPaaS Architecture (v0 draft)

Status: **proposed, needs Chas's sign-off on the hosting option** (see `docs/requests/0001-core-infra.md`).
Pricing checked against digitalocean.com on 2026-10-18.

> **2026-10-19 cap change:** Chas lowered the platform caps to bucket 100 MB (was 250), DB 50 MB (was 100), image 256 MB
> (was 512), CPU limit 100m (was 250m), RAM limit 384Mi (was 512Mi); default requests stay 64m/128Mi. `bootstrap.md` still shows the
> old numbers and is pending his update. Single source of truth: `app/config.py` (env-overridable). Older figures below were updated in place.

## 1. Hosting cost: what fits under $40/mo

| Item | Managed option | Lean option (recommended) |
|---|---|---|
| DOKS control plane (non-HA) | $0 | $0 |
| Worker node | 1 × s-2vcpu-4gb, $24 | 1 × s-2vcpu-4gb, $24 |
| Load balancer | $12 | **$0**: Traefik on hostPorts, DNS points at the node IP |
| MySQL | Managed 1 GiB, $15.15 | **In-cluster MySQL 8** StatefulSet on a 10 GiB volume, ~$1 |
| Spaces | $5 (250 GiB, 1 TiB egress, 100 buckets) | $5 |
| Container registry | GHCR (free) | GHCR (free) |
| **Total (static)** | **~$56: over budget** | **~$30** |

**Per-app marginal cost (lean):** about $0 until the node is full. Apps request 64m CPU / 128Mi
and are capped at 100m / 384Mi, so one 4 GB node holds about 8–10 idle-ish throwaway apps
next to the platform. After that, each extra s-2vcpu-4gb node costs $24 and holds about 12 more apps,
which works out to roughly **$2/app/month**. Spaces and DB add nothing per app inside the caps
(100 MB × 100 apps still fits in the 250 GiB included).

The lean option leaves about $10/mo of headroom, enough for a second node or managed MySQL later.

### Trade-offs we accept with the lean option
- **No LB:** when DOKS recycles the node, its public IP changes. A `dns-sync` CronJob (every 2 min,
  using the DO API) repoints `*.raisehellpraisedale.xyz` at the current node IP. Expect a few minutes of downtime
  during node upgrades. That's acceptable for throwaway apps.
- **Self-run MySQL:** we own the backups. A nightly `mysqldump` CronJob writes to a private platform
  Spaces bucket and keeps 7 days.
- **Single node:** no HA. This is a hobby/throwaway tier and we say so in the UI.

## 2. Components

```
               *.raisehellpraisedale.xyz (wildcard A → node IP, kept fresh by dns-sync)
                                │
                     Traefik (hostPort 80/443)
              default cert (TLSStore) = wildcard *.raisehellpraisedale.xyz (cert-manager, DNS-01 via DO)
                ┌───────────────┴──────────────────┐
     platform.raisehellpraisedale.xyz                 <pet>-<7char>.raisehellpraisedale.xyz
   ns: picopaas-system                    ns: app-<id>  (one per app)
   ┌──────────────────────┐               ┌───────────────────────────┐
   │ api (FastAPI)        │──k8s API────▶ │ Deployment (1 replica)    │
   │ ui  (Vue, built into │               │ Service / Ingress         │
   │      the api image)  │               │ Secret (env + refs)       │
   │ reconciler (in api)  │               │ ResourceQuota/LimitRange  │
   └──────────┬───────────┘               │ NetworkPolicy (deny-all + │
              │                           │  traefik + mysql)   │
   ns: picopaas-data                      └───────────────────────────┘
   MySQL 8 StatefulSet ── db `picopaas` (platform) + db `app_<id>` per app
   DO Spaces ── bucket `pp-<id>` per app, per-bucket access key
```

### Platform service (Python + Vue, modeled on seaburr/tribal)
- FastAPI, SQLAlchemy 2, Alembic, PyMySQL, bcrypt + JWT session cookie, plus API keys for CLI/YAML use.
- Vue 3 + Vite + Pinia + Tailwind, built into the same image (same layout as tribal).
- Tests use pytest with the real login flow and no auth mocks. K8s, MySQL-admin, and Spaces clients sit
  behind thin interfaces with fake implementations for unit tests. CI also runs a MySQL integration job.
- A single replica handles the API and also runs an in-process reconcile loop (APScheduler), with no
  separate operator. Desired state lives in the platform DB, and the loop converges k8s, MySQL, and Spaces toward it.

### Tenancy and isolation
- **Compute:** one namespace per app with a `ResourceQuota` (limits.cpu=200m, limits.memory=768Mi (2x the 100m/384Mi per-pod limits), pods=2
  to allow rolling updates; also `ephemeral-storage` requests/limits.ephemeral-storage at 2x the per-pod **250 MB scratch-disk** cap
  (`EPHEMERAL_LIMIT_MI`, request 64Mi; a pod over it is evicted -> `status_reason=disk`, retryable)). A `LimitRange` sets defaults. PodSecurity `restricted` is enforced.
  `automountServiceAccountToken: false`. Default-deny NetworkPolicy: ingress only from the Traefik namespace, egress
  to the internet and to MySQL on 3306, and no traffic to other app namespaces (DOKS uses Cilium, which
  enforces NetworkPolicy).
- **No root images (decided by Chas, 2026-10-18):** pods run with `runAsNonRoot: true`, so an image whose
  default user is root won't start. The deploy API should check the image config's `User` field ahead of
  time and reject root images with a clear 422 instead of letting the pod fail. Docs should show a
  `USER` line example. A named non-root user (kubelet can't verify it) is accepted and forced to
  `runAsUser`/`runAsGroup` = `NAMED_USER_RUN_AS_UID` (10001, `apps.run_as_uid`). Every app namespace also
  carries Pod Security Admission `enforce: restricted` (+ `enforce-version: latest`) as a backstop.
- **DB:** one MySQL database plus one user per app (`GRANT ALL ON app_<id>.*`), `max_user_connections=10`,
  and a random 32-char password. MySQL has no per-database quota, so the reconciler polls
  `information_schema.TABLES` and, above 50 MB, revokes INSERT/UPDATE/CREATE (leaving SELECT/DELETE so the
  user can clean up) and flags the app in the UI. DB is polled every 60s (`DB_QUOTA_POLL_SECONDS`) and
  re-enforced on every poll while over, because REVOKE does not touch open sessions: the provider also KILLs the
  user's sessions (in-cluster MySQL: `picopaas_ops.kill_app_sessions`), and if that can't be confirmed the app's pod
  is rolled once, which closes its connections.
- **Storage:** one Spaces bucket per app with a per-bucket read/write key. Spaces has no quotas, so the
  reconciler sums object sizes (every 5 min), and above 100 MB it swaps the key to read-only and flags the app.
  The old key is revoked only after the app has rolled to the new one. An over-cap app can't delete with a read-only
  key, so `POST /apps/{id}/storage/delete` deletes with the platform's credentials and restores the key at once.
  ⚠ Spike needed: per-bucket keys were UI-only at GA (Jan 2025). Confirm the DO API (`/v2/spaces/keys`) supports them now.
  Fallback: a shared bucket with a platform-side presigned-URL proxy.
- **Image size:** before deploying, the API reads the registry manifest and rejects the image if the summed
  compressed layer size exceeds 256 MB. v1 supports public images only; private pull creds come later.

### Public exposure (Chas, 2026-10-18)
Goal: the **DB server and Spaces** shouldn't be reachable from the public internet. The Kubernetes API
staying public is fine for now. `control_plane_allowed_cidrs` exists in Terraform but is off by default.
- **MySQL: private.** In-cluster ClusterIP only, or managed MySQL with trusted sources = the cluster only.
  The DO firewall refuses everything else, so the public hostname is unusable from outside.
- **Spaces: as private as DO allows on this budget.** The endpoint is inherently public (DO has no VPC
  endpoint for Spaces). Buckets are private and keys are per-bucket. Restricting by source IP would need
  bucket policies, which DO doesn't support with per-bucket keys, *and* a static egress IP (a DO NAT gateway,
  extra monthly cost). Revisit if DO ships VPC-only Spaces access. Credential theft is out of scope
  (request 0002, option A), and there's a credential-rotation action in the UI.
- **Nodes:** no inbound except 80/443 (no LB) or from the LB only.
- Pentest items 8 and 10 (use of stolen creds) are informational, not blockers.

### Abuse & moderation (Chas, 2026-10-19)
- Audience is an internal team, not the public.
- Signups are inert until the uberadmin approves them.
- The uberadmin can suspend or delete users and apps. An audit log records these actions.
- SMTP egress (ports 25/465/587) is blocked by NetworkPolicy.
- Apps and the platform live on the separate throwaway domain `raisehellpraisedale.xyz`, which protects seaburr.io's reputation.
- `hostUsers: false` (user namespaces) was considered and not adopted.

### Reference variables
If an app requests a DB or storage, the platform exposes reference variables the user maps onto their
own env names, e.g. `DATABASE_URL: ${{ db.url }}`:
- `db.host`, `db.port`, `db.name`, `db.user`, `db.password`, `db.url`
- `storage.endpoint`, `storage.region`, `storage.bucket`, `storage.access_key`, `storage.secret_key` (`storage.endpoint` is a full URL, e.g. `https://atl1.digitaloceanspaces.com`)

User secrets and resolved refs go into the app namespace's k8s Secret. In the platform DB they are
encrypted at rest with Fernet, keyed by a platform secret.

### YAML spec (v1)
```yaml
apiVersion: picopaas/v1
name: my-app              # display name only; hostname is generated
image: ghcr.io/me/app:1.2
port: 8080
resources: { db: true, storage: false }
env:
  LOG_LEVEL: info
  DATABASE_URL: ${{ db.url }}
secrets:                  # values supplied separately (UI or `--secret K=V`), never in the file
  - API_TOKEN
```

### Users and roles
- The first registered user becomes `uberadmin`. Everyone after that is `user`.
- `user.app_limit` defaults to 1. The uberadmin can raise it per user later (the column exists from day one,
  but the admin UI for it is deferred).

### DNS and TLS
- Hostname format: `<adjective>-<animal>-<7 chars of [a-z0-9]>.raisehellpraisedale.xyz`, with a uniqueness check in the DB.
- One wildcard A record `*.raisehellpraisedale.xyz`. Explicit records that already exist still take precedence.
- One wildcard cert from cert-manager + Let's Encrypt, DNS-01 via the DO solver. It's the ingress default
  cert, so there's no per-app issuance and no LE rate-limit risk.

### IaC
- Terraform (DO provider) handles the cluster, node pool, firewall rule (80/443 to nodes), Spaces, and DNS.
  State and runs live in Terraform Cloud (org `seaburr`, workspace `picopaas`), the same as Chas's other projects.
- Helm/manifests in `deploy/` cover Traefik, cert-manager, MySQL, and the platform itself.

**Decision (Chas, 2026-10-19):** ingress-nginx is retired upstream, so the ingress controller is **Traefik**
(official chart, ns `traefik`, IngressClass `traefik` as default) using the standard Kubernetes Ingress API only (no
Gateway API, no IngressRoute). Traefik's CRDs are installed solely for the `default` TLSStore, which serves the
cert-manager wildcard cert (secret in ns `traefik`) to per-app Ingresses that have hosts-only `tls`.

## 3. Scaling

Principle: **the platform tracks capacity and tells the uberadmin when more is needed, but never buys
infrastructure by itself.** Every new paid resource still goes through a `docs/requests/` approval
(bootstrap budget rule). What the platform does automate is *placement*: putting each new app on a
server that has room.

### What runs out first

| Resource | Unit we add | Capacity per unit | Ceiling signal |
|---|---|---|---|
| Compute | +1 node (s-2vcpu-4gb, $24) | ~12 apps (requests 64m/128Mi, limits 100m/384Mi) | Pending pods / node allocatable |
| MySQL | +1 DB server (managed 1 GiB, ~$15–20) | **~25 app DBs**, limited by connections: 75 max ÷ 5 per app user, oversubscribed. Disk alone would fit ~200 × 50 MB | allocated DBs ≥ 80% of `max_databases` |
| Spaces | the account limit, not money | **100 buckets per account**: a hard wall at ~100 apps with storage | bucket count ≥ 80 |

### Compute
The DOKS node pool gets `auto_scale = true` with `min_nodes = 1` and `max_nodes` set by Chas. That's
the spending ceiling, built into Terraform. The cluster autoscaler adds a node when app pods can't be
scheduled and removes it when nodes are idle. This is the only automatic scale-out, and it's bounded by an approved `max_nodes`.

### Database: many servers, one placement table
- `db_servers` table: each registered MySQL server (host, port, encrypted admin creds, `max_databases`,
  status `active | draining | full`). The first server comes from Terraform. More are added by the
  uberadmin in the admin UI after a request is approved, or by Terraform outputs.
- `app_databases` table: which server each app's DB lives on (`db_server_id`), plus its size and quota flag.
  The reconciler builds the reference variables (`db.host`, `db.url`, …) from that row, so apps on different
  servers simply get different hosts.
- **Placement** (`app/db_placement.py`): a new DB goes on the `active` server with the most free slots. If
  there's no room, the deploy fails with a clear "no database capacity" error and the uberadmin is alerted.
- **Tracking:** the reconciler's quota poll (it already measures per-DB size for the 50 MB cap) also
  rolls up per-server counts and bytes. When a server reaches 80%, the admin dashboard shows a banner and
  the platform logs a "capacity request" containing the text of a new `docs/requests/` entry
  (what / why / cost) to send to Chas.
- **Draining:** setting a server to `draining` stops new placements. Moving existing DBs between servers
  (dump → restore → swap secret → restart app) is a later admin action, not v1.
- Every server is the same kind of thing to the platform. In-cluster MySQL (lean option) and managed MySQL
  can coexist, so mixing them later costs nothing.

### Spaces
The 100-bucket account limit hits before cost does. Before ~80 storage-enabled apps, either ask DO
support for a higher limit (free), or switch to one shared bucket with per-app prefixes and access
through platform-signed URLs. The `ObjectStore` interface hides which one is in use, so apps don't change.

### Platform itself
One replica is enough at this size. The reconciler uses a DB lock row so a second replica could be added
later without two loops fighting each other.

## 4. Open questions for Chas
1. Approve the lean option (~$30/mo)? Or managed MySQL/LB once there's budget?
2. ~~Wildcard~~ → OK. Domain is now the throwaway `raisehellpraisedale.xyz` (request 0003); never touch TXT records.
3. ~~Region~~ → NYC3 for compute/MySQL (ATL1 had no DOKS capacity, 2026-10-19); Spaces stays in ATL1. Smallest SKUs for launch (Chas, 2026-10-19): s-1vcpu-2gb node, basic 1 GiB MySQL; scale later.
4. Autoscaler `max_nodes`, the compute spending ceiling. Proposed: 2 (worst case +$24/mo over static).

## Key management (2026-10-19)
`FERNET_KEY` encrypts every secret stored in MySQL (env-var secrets, app DB passwords, storage keys, DB server admin
passwords). `SECRET_KEY` signs sessions. Both live in GitHub repo secrets `PICOPAAS_FERNET_KEY` / `PICOPAAS_SECRET_KEY`
(with Chas's offline copy); `bootstrap.yml` writes them into `picopaas-env`. They must never be generated per cluster: a
cluster replacement on 2026-10-19 did that and orphaned the encrypted data. Rotation (MultiFernet re-encrypt) is TODO.
