# PicoPaaS Operations Guide

This guide is for the platform admin (role `platform_admin`) who runs the platform. App-owner docs are in [USER_GUIDE.md](USER_GUIDE.md), routes in [API.md](API.md),
design in [ARCHITECTURE.md](ARCHITECTURE.md), the manual runbook in [../deploy/README.md](../deploy/README.md).

## 1. Architecture at a glance

```
*.raisehellpraisedale.xyz (A records -> DO load balancer) -> Traefik (ns traefik, wildcard cert from cert-manager)
   platform.raisehellpraisedale.xyz -> platform (ns picopaas-system, 2 replicas: API + UI; one leader runs the reconciler)
   <adjective>-<animal>-<7 chars>.raisehellpraisedale.xyz -> app-<id> namespace (Deployment, Service, Ingress, Secret, quota, netpols)
Managed MySQL (DO, nyc3, private VPC): platform DB `picopaas` + one DB/user per app
Spaces (atl1): one bucket + key per app, plus the adopted platform bucket pico-paas-s3-a
State: Terraform Cloud (org seaburr, workspace picopaas, local execution = state only)
Images: GHCR (ghcr.io/seaburr/picopaas, tagged with the short commit SHA)
```

- DOKS cluster `picopaas-nyc3`, region NYC3; Spaces stays in ATL1 (`SPACES_REGION` default `atl1`).
- With `RECONCILER_ENABLED=1` the platform process runs four APScheduler jobs: reconcile every 15 s, database quota poll
  every 60 s, storage quota poll every 5 min, and the image watcher every 10 min. With MySQL, `GET_LOCK('picopaas-reconciler')`
  keeps the replicas from double-running.
- Reconciler drift sweep: running apps whose rendered manifest hash changed are re-applied; running apps whose
  `app-<id>` namespace is missing (cluster rebuilt) are recreated.
- On start, `BOOTSTRAP_DB_*` env creates DbServer `primary` if no server exists, and keeps its host, port, admin
  user, admin password and CA in sync on later starts.
- The first registered user becomes the administrator. Later signups stay `pending` until you approve them.

Key env on the platform pod (secret `picopaas-env`): `FERNET_KEY` (+ optional `FERNET_KEYS_PREVIOUS`), `DATABASE_URL`,
`DIGITALOCEAN_TOKEN` (`DO_API_TOKEN` is an alias), `SPACES_ACCESS_KEY_ID`, `SPACES_SECRET_ACCESS_KEY`,
`BOOTSTRAP_DB_*`. Config knobs: `MYSQL_EGRESS_MODE` (`in-cluster` | `cidr`), `MYSQL_EGRESS_CIDR`,
`MYSQL_SSL_CA`, `MYSQL_ADMIN_USE_PROCEDURES`, `INGRESS_CLASS` (traefik), `INGRESS_NAMESPACE` (traefik),
`APP_DOMAIN`, `MAX_NODES` (dashboard display only), `IMAGE_WATCH_MINUTES` (default 10), `DB_QUOTA_POLL_SECONDS` (60),
`STORAGE_QUOTA_POLL_SECONDS` (300).

## 2. GitHub Actions workflows

All are manual (`workflow_dispatch`) unless noted. `bootstrap` and `deploy` share a concurrency lock.

| Workflow | Use it when | Notes |
|---|---|---|
| `ci` (push/PR) | Automatic | gitleaks (no allowlist files allowed), terraform validate, kubeconform, backend tests, MySQL integration, frontend. |
| `image` (push to main) | Automatic | Builds and pushes `ghcr.io/seaburr/picopaas:<short sha>`. |
| `terraform` | Create or change DO infra (cluster, MySQL, VPC, firewall, DNS, Spaces bucket) | Input `apply` (default true; untick for plan only). Skips if `TF_API_TOKEN` is unset. Pre-checks the MySQL slug and that the node size currently has capacity in the region. Sets the TFC workspace to local execution. |
| `bootstrap` | Fresh or replaced cluster, or when platform add-ons/secrets change | Installs Traefik, cert-manager, metrics-server, upserts DNS A records `*` and `picopaas`, creates secrets and the platform DB, issuers and wildcard cert, deploys the platform, waits for health (up to about 10 min). Idempotent. Input `sha`. |
| `deploy` (also after a successful `image` run on main) | Roll a new platform version | Applies RBAC, deployment (image + env checksum), service, ingress; waits for rollout. Input `sha`. Skips if there is no token or no cluster `picopaas-nyc3`. |
| `diag` | Something is broken in-cluster | Inputs `namespace` (default `picopaas-system`), `selector`, `tail`. Prints nodes, pods, events, non-ready pod descriptions and logs with secret values masked. |
| `testapps` (push to `testapps/**`) | Automatic, or run to rebuild | Builds `hello`, `probe`, `toolbox`, `webpage` to `ghcr.io/seaburr/picopaas-testapp-*`. |

Typical order for a new environment: `terraform` -> `bootstrap` -> register the first user (becomes platform admin) ->
run `tests/e2e` and `tests/pentest` (see their READMEs).

## 3. Key management

| GitHub secret | Purpose |
|---|---|
| `PICOPAAS_FERNET_KEY` | The only platform key. Encrypts every secret stored in MySQL (env-var values, app DB passwords, storage secret keys, DB server admin passwords). The JWT signing key is derived from it (an HMAC-SHA256 of a fixed label, see `app/config.py`), so rotating it also logs everyone out. An explicit `SECRET_KEY` env is still honoured but not needed. |
| `DIGITALOCEAN_TOKEN`, `SPACES_ACCESS_KEY_ID`, `SPACES_SECRET_ACCESS_KEY` | Terraform, bootstrap, and the platform's own provisioning of buckets and keys. |
| `TF_API_TOKEN` | Terraform Cloud state. |

Why the Fernet key matters: lose or change it and all stored ciphertext is unreadable. The 2026-10-19 cluster
replacement regenerated it per cluster and orphaned the data. Now `bootstrap` takes the key only from the repo
secret and fails if it is missing. GitHub secrets are write-only, so keep your own copy in a password manager
(open to-do in [QUESTIONS.md](QUESTIONS.md)).

What happens when the key is wrong: apps with unreadable values go to **Failed** ("Your saved secrets can't be read...")
without stopping the reconciler; owners re-enter secrets or rotate credentials. `bootstrap_db_server` repairs the
`primary` DB server admin password automatically. Deleting apps never needs the old ciphertext.

### Rotating the key

1. Generate a key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`; save it in your password manager.
2. Set it as the repo secret `PICOPAAS_FERNET_KEY`.
3. Run the `bootstrap` workflow. It reads the current `FERNET_KEY` from the existing `picopaas-env` Secret and, if it
   differs, stores it (plus earlier previous keys, deduped, max 3) in `FERNET_KEYS_PREVIOUS`. If the existing Secret
   cannot be read (anything but NotFound) bootstrap fails rather than lose the old key.
4. On startup the platform decrypts with the new key then the previous keys (`MultiFernet`) and re-encrypts every
   stored ciphertext to the new key (log line `fernet rotation: rotated=... unchanged=... skipped_undecryptable=...`,
   counts only). Rows no key can decrypt are skipped and logged.
5. Once `rotated` is 0 on a later start and nothing is skipped, previous keys are no longer needed.

## 4. Disaster recovery: replacing the cluster

What we did on 2026-10-19 and the lessons baked in since:

1. `terraform` workflow: creates the new DOKS cluster (and, if needed, MySQL/VPC). If it fails with
   "insufficient capacity", the node size is not currently offered in the region; the workflow's pre-check
   catches that early, so pick a size from the region's list.
2. `bootstrap` workflow. Things that went wrong the first time and are now handled:
   - **Orphaned load balancer.** The old cluster's LB keeps the name `picopaas` (set in `deploy/traefik/values.yaml`),
     so the new Traefik Service could not claim it. Bootstrap now deletes any LB named `picopaas` that is not tagged
     for the current cluster and not on the live Service IP, before installing Traefik. If you see a name clash,
     delete the stale LB in the DO panel and re-run.
   - **Keys.** The new cluster used freshly generated keys, so old ciphertext was unreadable. Keys now come from the
     `PICOPAAS_*` repo secrets, so they survive. Do not delete them.
   - **DB server creds.** The `primary` DbServer row is kept in sync with what bootstrap reads from DO each run.
3. Platform starts; the reconciler recreates every `running` app whose namespace is missing (the DR sweep), and
   re-provisions from the DB: apps, env vars and DB/bucket rows live in the platform database, which is on managed
   MySQL and survives the cluster.
4. Verify: `/healthz`, log in, check `/admin/health`, open an app. Use `diag` for logs.
5. If the managed MySQL was also lost, restore from DO backups. The in-cluster variant uses the nightly dump
   described in `deploy/README.md` ("MySQL restore").

DNS: bootstrap upserts only the A records `*` and `picopaas`; TXT and everything else is never touched.

## 5. Database servers

Open **Admin**, then **Database servers**, or use the API (`/admin/db-servers`).

- **Add:** click **Add server**, or `POST /admin/db-servers` with `name, host, port, admin_user, admin_password,
  max_databases` and an optional `ca_cert` (PEM). A new paid server needs an approved request in `docs/requests/`.
  Servers start `active`. The `primary` server gets its CA from `MYSQL_SSL_CA` at bootstrap.
- **Placement:** a new app DB goes to the `active` server with the most free slots. If none has room, the app goes to
  **Failed** with a "No database space is free" message and is retried automatically. Users also can't tick **Database**
  on new apps. The dashboard flags servers at 80% or more of `max_databases`
  (default 25 per server).
- **Drain:** set the server's status to **Draining** in the UI, or `PATCH /admin/db-servers/{id}` with
  `{"status": "draining"}`. This stops new placements; existing databases stay. Moving databases between servers is
  not implemented. `full` is a legacy status that also stops placements; the UI hides it.
- **In-cluster MySQL:** set `MYSQL_ADMIN_USE_PROCEDURES=1` (the `picopaas_admin` user works through stored
  procedures, issue #1).

## 6. Approvals, suspension, audit

- **New users:** the **Admin** page (or `GET /admin/users`) lists `pending` users. Approve them there, or with
  `POST /admin/users/{id}/approve`. The Health page also lists pending approvals.
- **Forgotten password:** use the key icon on the user's row (`POST /admin/users/{id}/reset-password`). It shows a temporary
  password once. Give it to the user out of band. They are prompted to change it.
- **App limit:** edit the limit on the user's row, or `PATCH /admin/users/{id}` with `{"app_limit": N}`. Default 1.
- **App resources:** the sliders icon on an app's row (`PATCH /admin/apps/{id}/resources`) overrides its CPU and memory.
- **Suspend user:** account `suspended`, all their apps scaled to zero. Unsuspend returns apps that were suspended
  with the user to `pending`; apps you suspended individually stay suspended.
- **Suspend/delete app:** `POST /admin/apps/{id}/suspend|unsuspend`, `DELETE /admin/apps/{id}`. Deleting a user
  deletes their apps and revokes their API keys. The administrator account can't be suspended or deleted.
- **Audit log:** `GET /admin/audit?limit=N` (default 100, max 1000), newest first; also shown in the admin UI. It
  records registrations, failed logins, password changes and resets, API key create and revoke, app create, update, delete,
  restart, rotate, image updates, resource overrides and storage deletes, user approve, suspend, unsuspend, delete and
  app-limit changes, app suspend and unsuspend, and database server changes. The full list of action names is in
  [API.md](API.md).

## 7. Health dashboard

`/admin/health` in the UI (the administrator can reach it from the nav bar), backed by `GET /admin/health` (cached 15 s). It shows nodes,
per-app pod readiness, restarts and CPU/memory against the 100m/384Mi limits, platform pods
(`picopaas-system`, `traefik`, `cert-manager`), and DB server capacity. CPU/memory need metrics-server (installed
by bootstrap); without it usage cells show a dash and `metrics_available` is false. The node ceiling shown comes
from `MAX_NODES` (2, set at bootstrap); the real cap is `max_nodes` in Terraform.

## 8. Cost

Baseline about $60.15/mo (node $28 + MySQL $15.15 + LB $12 + Spaces $5), about $88.15 while the autoscaler runs the
second node. The full table, cost drivers, per-app marginal cost, and sources are in
[ARCHITECTURE.md §1](ARCHITECTURE.md#1-hosting-cost). `infra/variables.tf` is the source of truth; update §1 if you
change a size. Any new paid resource needs a request in `docs/requests/`.

Capacity: roughly 8 to 12 apps per node; about 25 app databases per MySQL server; Spaces has a 100 buckets per
account limit.

## 9. Known limitations and open issues

Open GitHub issues (`gh issue list`):

| # | Title |
|---|---|
| 10 | Move the API under `/api/` (SPA and API share paths; see API.md) |
| 11 | In-cluster calls to an app's public URL time out (load balancer hairpin) |
| 9 | `status_detail` "deployment failed" is too vague for customers (messages have since been rewritten; may be ready to close) |

Other known gaps:

- One node: node recycling causes brief downtime. The platform runs two replicas, but both share that node.
- The platform's ClusterRole can read Secrets in all namespaces (see `deploy/README.md`, "Known RBAC exposure").
- A failed MySQL `DROP` or bucket delete during app deletion is logged-and-ignored and the DB/bucket rows are
  removed anyway, so such resources can be orphaned on the provider side.
- Old keys stay in `FERNET_KEYS_PREVIOUS` until bootstrap trims them (max 3); there is no automatic removal.
- PodDisruptionBudget for the platform is deferred until 2+ nodes.
- Spaces credential theft is out of scope (request 0002).

## Unknown app hosts and suspended apps

An unknown `*.raisehellpraisedale.xyz` host matches the low-priority catch-all Ingress
(`deploy/platform/ingress.yaml`, `router.priority: "1"`) and gets the platform's custom 404 page
(`static/404.html`, `noindex`, `no-store`).

Suspended apps: the reconciler deletes their Ingress (Deployment stays at 0 replicas; unsuspend re-renders it), so
the host falls through to the same catch-all. The platform looks the hostname up (cached 10s) and serves
`static/suspended.html` with 503 if the app is suspended, otherwise the 404 page.

## Platform caps (changed 2026-10-19)
Caps are defined once in `app/config.py` (env overrides: `DB_QUOTA_BYTES`, `STORAGE_QUOTA_BYTES`, `MAX_IMAGE_SIZE_BYTES`,
`DEFAULT_CPU_LIMIT_M`, `DEFAULT_MEM_LIMIT_MI`): DB 50 MB, bucket 100 MB, image 256 MB, CPU 100m, RAM 384Mi. Existing apps pick up
the new container limits, LimitRange and ResourceQuota through the rendered-hash sweep on the next reconcile (pods restart once).
Existing databases over 50 MB are made read-only at the next quota poll (within a minute) and buckets over 100 MB within 5 minutes. Owners must
free space to get writes back (see the User Guide, section 10). Existing images over 256 MB keep running but cannot be redeployed or updated until shrunk.
