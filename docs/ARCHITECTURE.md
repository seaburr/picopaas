# PicoPaaS Architecture

Status: hosting approved (managed variant, see `docs/requests/0001-core-infra.md`). This is a design record: §1 and `docs/OPERATIONS.md` have the current numbers.
Cost figures in §1 were checked on 2026-10-20.

> **2026-10-19 cap change:** Chas lowered the platform caps to bucket 100 MB (was 250), DB 50 MB (was 100), image 256 MB
> (was 512; later restored to 512 MB so a persistent-uploads Ghost fits), CPU limit 200m (was 250m, briefly 100m — raised back to 200m 2026-10-20 after the Ghost load test showed Node SSR is CPU-bound), RAM limit 384Mi (was 512Mi); default requests stay 64m/128Mi. `bootstrap.md` still shows the
> old numbers and is pending his update. Single source of truth: `app/config.py` (env-overridable). Older figures below were updated in place.

## 1. Hosting cost

Prices checked 2026-10-20 (list prices, USD, before tax). History: this section used to plan a "lean" ~$30/mo setup
(no load balancer, in-cluster MySQL). Chas approved the managed variant instead (request 0001, 2026-10-19), so the
numbers below are what actually runs. `infra/variables.tf` is the source of truth for sizes.

| Item | What runs | Monthly |
|---|---|---|
| DOKS control plane | non-HA, NYC3 | $0 |
| Worker node | 1 × `s-2vcpu-4gb-amd` (Premium AMD, 2 vCPU / 4 GB) | $28 (*) |
| Managed MySQL | `db-s-1vcpu-1gb`, MySQL 8.4, single node, NYC3 | $15.15 |
| Load balancer | 1 × regional DO LB (created by the Traefik Service) | $12 |
| Spaces | 1 subscription (bucket `pico-paas-s3-a` in ATL1 plus one bucket per storage-enabled app) | $5 |
| DNS, VPC, GHCR (public images), Terraform Cloud (state only), GitHub Actions (public repo), Let's Encrypt | | $0 |
| **Baseline** | | **about $60.15** |
| **Worst case** (autoscaler at `max_nodes = 2`) | + 1 node while it is up | **about $88.15** |

(*) Confirmed by Chas from the DO bill (2026-10-20); DO's public pages don't list this size.

### What drives cost up
| Driver | Effect |
|---|---|
| Second node (autoscaler, up to `max_nodes = 2`) | +$28/mo while it exists (billed per second, so less if it scales back down) |
| Spaces storage beyond 250 GiB | +$0.02/GiB/mo. Unlikely: 100 MB × 100 buckets is about 10 GB |
| Spaces egress beyond 1,024 GiB | +$0.01/GiB. Unlikely at these caps |
| Spaces bucket count | Not a cost: DO's account limit is 100 buckets (see §3), so about 100 apps with storage is the wall. Raising it is a support request |
| Bigger MySQL | Standard plans: 2 GiB $30.45, 4 GiB $60.90 (+$15.30 and +$45.75 over today). Extra disk $0.215/GiB. A second 1 GiB server adds $15.15 |
| Load balancer | Flat $12. No bandwidth charge on regional LBs |

### Per-app marginal cost
About $0 until the node is full. An app requests 64m CPU / 128Mi and is capped at 200m / 384Mi, so one 4 GB node holds
roughly 8 to 12 apps next to the platform. After that each extra node ($28) holds about 8 to 12 more, which is
**about $2.50 to $3.50 per app per month**. A database adds nothing (about 25 app DBs fit one MySQL server). A bucket
adds nothing inside the Spaces allowance.

### Sources
- Load balancer $12: https://docs.digitalocean.com/products/networking/load-balancers/details/pricing/ and https://www.digitalocean.com/pricing/kubernetes (2026-10-20).
- Spaces $5, 250 GiB, 1,024 GiB egress, $0.02/GiB and $0.01/GiB overage: https://docs.digitalocean.com/products/spaces/details/pricing/ (2026-10-20).
- MySQL 1 GiB $15.15, 2 GiB $30.45, 4 GiB $60.90: https://www.digitalocean.com/pricing/managed-databases (2026-10-20). DO's docs page
  (https://docs.digitalocean.com/products/databases/mysql/details/pricing/) says "from $15.00"; we use the pricing page.
  Neither page prices the exact slug `db-s-1vcpu-1gb` separately, so treat $15.15 as the Regular 1 GiB plan.
- DOKS control plane free (HA $40): https://www.digitalocean.com/pricing/kubernetes (2026-10-20).
- Node $28: confirmed by Chas from the DO bill (2026-10-20). DO's public pages don't list Premium AMD 2 vCPU / 4 GB; the
  same-size Regular Droplet is $24 (https://www.digitalocean.com/pricing/droplets).
- Droplet transfer allowance for the node is not counted; app egress is small at these caps.

CLAUDE.md's hard rule still says "under $40/mo". The approved setup is about $60, tracked in `docs/QUESTIONS.md`.

## 2. Components

```
               *.raisehellpraisedale.xyz (wildcard A → DigitalOcean load balancer)
                                │
                     Traefik (behind the load balancer, ports 80/443)
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
- Two replicas serve the API; the one holding a MySQL `GET_LOCK` also runs the in-process reconcile
  loop (APScheduler), with no separate operator. Desired state lives in the platform DB, and the loop converges k8s, MySQL, and Spaces toward it.

### Tenancy and isolation
- **Compute:** one namespace per app with a `ResourceQuota` (limits.cpu=400m, limits.memory=768Mi (2x the 200m/384Mi per-pod limits), pods=2
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
- **DB:** one MySQL database plus one user per app (`GRANT ALL ON app_<id>.*`), `MAX_USER_CONNECTIONS` = `MAX_DB_USER_CONNECTIONS` (default 10, app/config.py; re-applied to every existing app user by `ALTER USER` on each reconciler sweep, so raising it self-heals),
  and a random 32-char password. MySQL has no per-database quota, so the reconciler polls
  `information_schema.TABLES` and, above 50 MB, revokes INSERT/UPDATE/CREATE/ALTER (leaving SELECT/DELETE/TRUNCATE/DROP so the
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
  compressed layer size exceeds 512 MB. v1 supports public images only; private pull creds come later.

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
- **Nodes:** no inbound except 80/443, which come through the load balancer.
- Pentest items 8 and 10 (use of stolen creds) are informational, not blockers.

### Abuse & moderation (Chas, 2026-10-19)
- Audience is an internal team, not the public.
- Signups are inert until the platform admin approves them.
- The platform admin can suspend or delete users and apps. An audit log records these actions.
- SMTP egress (ports 25/465/587) is blocked by NetworkPolicy.
- Apps and the platform live on the separate throwaway domain `raisehellpraisedale.xyz`, which protects seaburr.io's reputation.
- `hostUsers: false` (user namespaces) was considered and not adopted.

### Reference variables
If an app requests a DB or storage, the platform exposes reference variables the user maps onto their
own env names, e.g. `DATABASE_URL: ${{ db.url }}`:
- `db.host`, `db.port`, `db.name`, `db.user`, `db.password`, `db.url`, `db.ca_cert`, `db.tls`
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
- The first registered user becomes `platform_admin`. Everyone after that is `user`.
- `user.app_limit` defaults to 1. The platform admin can raise it per user in the admin UI, up to the hard max.
- **Hard per-account app cap** (`MAX_APPS_PER_USER`, default 5, app/config.py) applies to EVERY account including platform admins. Effective limit = `min(app_limit, max)` for users, `max` for admins (`resources.effective_app_limit`). Enforced at create only (existing apps are never deleted); `PATCH /admin/users/{id}` rejects `app_limit` above it (422). `/auth/me` returns `max_apps` and `app_limit_effective`.

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

Principle: **the platform tracks capacity and tells the platform admin when more is needed, but never buys
infrastructure by itself.** Every new paid resource still goes through a `docs/requests/` approval
(bootstrap budget rule). What the platform does automate is *placement*: putting each new app on a
server that has room.

### What runs out first

| Resource | Unit we add | Capacity per unit | Ceiling signal |
|---|---|---|---|
| Compute | +1 node (`s-2vcpu-4gb-amd`, $28) | ~12 apps (requests 64m/128Mi, limits 200m/384Mi) | Pending pods / node allocatable |
| MySQL | +1 DB server (managed 1 GiB, ~$15) | **~25 app DBs**, limited by connections: 76 max ÷ ~10 per app user (cap is a ceiling, not a reservation), oversubscribed. Disk alone would fit ~200 × 50 MB | allocated DBs ≥ 80% of `max_databases` |
| Spaces | the account limit, not money | **100 buckets per account**: a hard wall at ~100 apps with storage | bucket count ≥ 80 |

### Compute
The DOKS node pool gets `auto_scale = true` with `min_nodes = 1` and `max_nodes` set by Chas. That's
the spending ceiling, built into Terraform. The cluster autoscaler adds a node when app pods can't be
scheduled and removes it when nodes are idle. This is the only automatic scale-out, and it's bounded by an approved `max_nodes`.

### Database: many servers, one placement table
- `db_servers` table: each registered MySQL server (host, port, encrypted admin creds, `max_databases`,
  status `active | draining`; `full` is a legacy value that behaves like `draining`). The first server comes from Terraform. More are added by the
  platform admin in the admin UI after a request is approved, or by Terraform outputs.
- `app_databases` table: which server each app's DB lives on (`db_server_id`), plus its size and quota flag.
  The reconciler builds the reference variables (`db.host`, `db.url`, …) from that row, so apps on different
  servers simply get different hosts.
- **Placement** (`app/db_placement.py`): a new DB goes on the `active` server with the most free slots. If
  there's no room, the deploy fails with a clear "no database capacity" error and the platform admin is alerted.
- **Tracking:** the reconciler's quota poll (it already measures per-DB size for the 50 MB cap) also
  rolls up per-server counts and bytes. When a server reaches 80%, the admin dashboard shows a banner and
  the platform logs a "capacity request" containing the text of a new `docs/requests/` entry
  (what / why / cost) to send to Chas.
- **Draining:** setting a server to `draining` stops new placements. Moving existing DBs between servers
  (dump → restore → swap secret → restart app) is a later admin action, not v1.
- Every server is the same kind of thing to the platform. In-cluster MySQL (the earlier, cheaper option) and managed MySQL
  can coexist, so mixing them later costs nothing.

### Spaces
The 100-bucket account limit hits before cost does. Before ~80 storage-enabled apps, either ask DO
support for a higher limit (free), or switch to one shared bucket with per-app prefixes and access
through platform-signed URLs. The `ObjectStore` interface hides which one is in use, so apps don't change.

### Platform itself
Two replicas run (HA, 2026-10-19), so deploys and node drains don't take the API down. The reconciler uses a MySQL
`GET_LOCK`, so only one replica runs the loops.

## 4. Decisions on earlier open questions
New questions go in `docs/QUESTIONS.md`. Decided so far:
1. Hosting variant: managed MySQL and a load balancer (approved 2026-10-19), not the in-cluster "lean" setup.
2. Domain: the throwaway `raisehellpraisedale.xyz` (request 0003); never touch TXT records.
3. Region: NYC3 for compute and MySQL (ATL1 had no DOKS capacity, 2026-10-19); Spaces stays in ATL1. The node is
   `s-2vcpu-4gb-amd` ($28/mo, confirmed from the DO bill 2026-10-20).
4. Autoscaler `max_nodes = 2` (approved), so the worst case is +$28/mo over the static setup.

## Key management (2026-10-19)
`FERNET_KEY` encrypts every secret stored in MySQL (env-var secrets, app DB passwords, storage keys, DB server admin
passwords). The session signing key is derived from it unless `SECRET_KEY` is set. The key lives in the GitHub repo
secret `PICOPAAS_FERNET_KEY`; `bootstrap.yml` writes it into `picopaas-env`. It must never be generated per cluster:
a cluster replacement on 2026-10-19 did that and orphaned the encrypted data. Rotation (MultiFernet re-encrypt) is
implemented; see `docs/OPERATIONS.md` §3.

## Auth throttling and security headers
- `/auth/login` and `/auth/register` are throttled by `app/ratelimit.py` using the `auth_throttle` table (fixed windows, hashed keys), so the limit holds across replicas. Login: 10 failures per email / 15 min (primary control, no IP dependence; applies identically to unknown emails; success clears it) and 100 per client IP. Register: 40 attempts per client IP / hour. 429 + `Retry-After`. Store errors fail open (logged). API-key traffic is not throttled here.
- Client IP = rightmost `X-Forwarded-For` entry (Traefik discards client-supplied XFF and writes the peer it saw). Behind the DO LB in TCP mode without PROXY protocol the peer may be the LB, so IP buckets are effectively shared platform-wide; the per-IP limits are therefore a generous floodwall (not per-client) and the per-email lockout is the real control. Enable PROXY protocol on LB+Traefik to make the IP buckets precise, then tighten them.
- Every response carries HSTS, nosniff, X-Frame-Options DENY and an enforcing CSP (`/docs`, `/redoc` exempt from CSP: they load a CDN).

## Production hardening (deferred 2026-10-20)
This is a hobby/dev deployment. Before any real production use (tracked in docs/QUESTIONS.md):
- **Scope the cloud credentials.** The API pod's `picopaas-env` secret currently holds an account-wide DigitalOcean API token and a full-access Spaces key, so a code-execution bug in the API reaches the whole DO account. Replace with a DO token scoped to only what bootstrap/the reconciler need and a Spaces key limited as far as DO allows; update the GitHub/k8s secrets and re-run bootstrap.
- **CI/CD:** protect `main` (require PRs + checks) and move the Actions secrets into a protected GitHub Environment rather than repo-level. (Left open now because the repo is private and single-maintainer.)
- The 2026-10-20 pentest pass (SSRF/rebinding, registry content verification, auth rate limiting, security headers, per-namespace RBAC, CI hardening) is already fixed and deployed; these two are the remaining account-level items.
