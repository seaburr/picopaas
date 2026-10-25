# PicoPaaS

A tiny platform for hosting quick throwaway projects. You give it one Docker image, add env vars and secrets,
and optionally get a MySQL or PostgreSQL database (50 MB) and an S3-compatible blob storage bucket (100 MB), with nightly automatic backups of both and of an optional persistent volume. Each app gets its own HTTPS
subdomain. It runs on DigitalOcean Kubernetes for about $75/mo.

Live: https://platform.picopaas.xyz (sign up, and if your account is approved, a PicoPaaS platform admin will reach out)

Docs: [User guide](docs/USER_GUIDE.md) | [API](docs/API.md) | [CLI](docs/CLI.md) | [Admin guide](docs/ADMIN_GUIDE.md) | [Deployment](docs/deployment/README.md) | [Architecture](docs/ARCHITECTURE.md) | [Pentest notes](docs/PENTEST.md) | [Ledger](docs/LEDGER.md)

## Features

- One Docker image per app, public registries (GHCR, Docker Hub, etc.).
- Env vars and encrypted secrets. Reference values with `${{ db.url }}`, `${{ storage.bucket }}` and friends.
- Optional MySQL or PostgreSQL database and S3-compatible blob storage bucket per app, each with its own credentials.
- HTTPS subdomain per app (`<adjective>-<animal>-<id>.picopaas.xyz`) from a wildcard certificate.
- Logs (with download), restart, and live status in the UI.
- One-click templates (Ghost, WordPress, Directus, Grafana, n8n, Shlink, a Gatus status page, an nginx static site) that prefill the new-app form.
- Instant rollback: the last few image versions are kept, and one click redeploys an older one.
- Auto-rollback: a new image that fails to start is reverted to the last working version.
- Per-app CPU and memory history (sparklines, about 3 hours) on the app page.
- Digest pinning: every deploy runs a `sha256` digest. Floating tags (`:latest`) are re-checked every 10 minutes and
  auto-redeployed when they move.
- Backups: nightly, automatic, for the database, bucket and volume. Back up now, download a whole backup as one package, delete backups, per-app on/off, the newest 14 kept (failed ones cleaned first), a per-app storage cap, and a final backup kept 14 days after an app is deleted.
- Quotas on CPU, memory, scratch disk, image size, database size and bucket size.
- Administrator approval for new accounts, user and app suspend or delete, and an audit log.
- API keys, a CLI, and a YAML app spec.

## Caps

Single source of truth: [`app/config.py`](app/config.py) (env-overridable).

| What | Cap |
|---|---|
| CPU | 200m limit (64m request) |
| Memory | 384 MB limit (128 MB request) |
| Scratch disk | 250 MB (pod is evicted over it) |
| Image size | 512 MB compressed |
| MySQL or PostgreSQL database | 50 MB (writes revoked over it) |
| Blob storage bucket (S3-compatible) | 100 MB (key goes read-only over it) |
| Outbound bandwidth | 20 Mbit/s per app (about 2.5 MB/s), counting everything the app sends, HTTP responses to visitors included |
| Apps per user | 1 by default (`app_limit`; an administrator can raise it). Hard cap of 5 active apps per user account (`MAX_APPS_PER_USER`); platform admins have their own pair (default 5, cap 10) |

These are defaults. A platform administrator can override them per app, up to these ceilings: 1000m CPU, 2 GB memory, 3 replicas, 25 database connections, 2 GB scratch disk, 1 GB database, 50 GB bucket, 2 GB image and 160 Mbit/s outbound bandwidth (20 MB/s).

## Architecture

```mermaid
flowchart TB
  user["Browser, CLI or API key"]
  dns["DNS: *.picopaas.xyz"]
  lb["DigitalOcean load balancer"]
  user --> dns --> lb

  subgraph doks["DOKS cluster picopaas-nyc3 (NYC3)"]
    traefik["Traefik ingress<br/>wildcard TLS certificate"]
    plat["Platform, 2 replicas (ns picopaas-system)<br/>FastAPI API + Vue SPA<br/>reconciler, quota polls, image watcher<br/>(one leader runs the loops)"]
    app["App, one namespace each (ns app-ID)<br/>Deployment, Service, Ingress<br/>NetworkPolicies, ResourceQuota"]
    cm["cert-manager"]
  end

  lb --> traefik
  traefik --> plat
  traefik --> app
  plat -- "Kubernetes API" --> app

  subgraph data["Data (DigitalOcean)"]
    mysql[("Managed MySQL 8.4<br/>platform DB + per-app MySQL DBs")]
    pg[("Managed PostgreSQL 17<br/>per-app PostgreSQL DBs")]
    spaces[("Spaces<br/>one bucket + key per app")]
  end

  plat -- "VPC, TLS" --> mysql
  plat -- "VPC, TLS" --> pg
  plat -- "DO API + S3 API" --> spaces
  app -- "own DB user" --> mysql
  app -- "own DB role" --> pg
  app -- "own bucket key" --> spaces

  subgraph ext["External services"]
    ghcr["GHCR and other registries"]
    le["Let's Encrypt"]
  end

  plat -- "check image manifest" --> ghcr
  app -- "pull image by digest" --> ghcr
  cm -- "DNS-01 wildcard cert" --> le

  subgraph ci["GitHub"]
    gha["Actions: ci, image, deploy, bootstrap, terraform"]
    tfc["Terraform Cloud<br/>state only"]
  end

  gha -- "helm, kubectl" --> doks
  gha -- "terraform" --> tfc
  gha -- "push image" --> ghcr
```

What happens when you deploy an app:

```mermaid
sequenceDiagram
  participant U as User (UI, CLI, API)
  participant A as Platform API
  participant R as Registry
  participant D as Platform DB
  participant C as Reconciler (leader)
  participant K as Kubernetes
  U->>A: create or update app (image, env, health check)
  A->>R: read manifest (size, root user check, digest)
  R-->>A: digest and size
  A->>D: save desired state (pinned digest, encrypted secrets)
  A-->>U: saved, status pending
  loop every 15 seconds
    C->>D: apps that need work, plus a drift sweep
  end
  C->>K: apply namespace, quota, policies, Secret, Deployment, Service, Ingress
  K-->>C: rollout ready
  C->>D: status running
  U->>A: poll status
  A-->>U: running, https URL
```

## Tech stack

| Layer | Tech | Why |
|---|---|---|
| API | FastAPI, SQLAlchemy 2, Alembic, PyMySQL | Small, typed, easy to test; migrations on startup (`entrypoint.sh`) |
| Background jobs | APScheduler, in the API process | No separate operator. Reconcile every 15s, DB quota every 60s, bucket quota every 5 min, image watcher every 10 min. A MySQL `GET_LOCK` picks one leader among replicas |
| Auth | bcrypt, JWT session cookie, `pp_sk_` API keys | UI sessions plus keys for CLI and CI |
| Frontend | Vue 3, Vite, Pinia, Vue Router, Tailwind, lucide icons | Built into the API image and served as static files |
| Compute | Kubernetes on DigitalOcean (DOKS) | Namespaces, quotas and NetworkPolicy give per-app isolation; free control plane |
| Ingress | Traefik (standard Ingress API) | ingress-nginx is retired; one default TLS store serves the wildcard cert |
| TLS | cert-manager, Let's Encrypt, DNS-01 via DO | One wildcard cert, so no per-app issuance or rate limits |
| Metrics | metrics-server | CPU and memory in the admin health view |
| Database | DO Managed MySQL 8.4 and Managed PostgreSQL 17 | MySQL holds the platform DB. Each app's database (MySQL or PostgreSQL, chosen by `resources.db_engine`) gets its own database and user/role, over the VPC with TLS |
| Blob storage | DO Spaces (S3 API, boto3) | A bucket and access key per app |
| Secrets at rest | Fernet (`cryptography`), MultiFernet for rotation | One platform key encrypts env secrets, DB and bucket credentials |
| Infra as code | Terraform with Terraform Cloud state | Cluster, VPC, MySQL, PostgreSQL, firewall, Spaces bucket, DNS |
| CI/CD | GitHub Actions, GHCR | Public repo and images, so both are free |
| Secret scanning | gitleaks pre-commit hook, plus a CI guard against allowlists | Keeps keys out of git |
| Tests | pytest (SQLite in memory, real login flow), Playwright e2e | Infra clients are faked behind interfaces; e2e and pentest suites run against the live site |

## How it works

**Desired state lives in the platform DB.** The API validates a request, reads the image manifest (size cap, non-root
check, digest), stores the app, and returns. A reconciler loop inside the API process runs every 15 seconds and
converges Kubernetes, MySQL, PostgreSQL and Spaces toward what the DB says. It renders the manifests, hashes them, and applies
only on change. A drift sweep re-checks running apps against that hash, so a deleted namespace or a rebuilt cluster
gets repaired. Two replicas run, but only the one holding a MySQL named lock does the work.

**Isolation.** Every app gets its own namespace with a ResourceQuota and LimitRange. Pods run non-root with Pod
Security Admission set to `restricted`, no service account token, and no privilege escalation. NetworkPolicies deny
everything by default: ingress only from Traefik, egress to the internet and to the database servers, nothing to other apps, and SMTP
ports are blocked. Each app gets its own DB user and bucket key, never the platform's.

**Quota enforcement.** MySQL, PostgreSQL and Spaces have no per-tenant quotas, so the platform polls sizes itself. Over 50 MB, an
app's DB user loses INSERT, UPDATE and CREATE, its open sessions are killed, and the pod is rolled if that can't be
confirmed. Over 100 MB, the bucket key swaps to read-only. Users can free space and get write access back.

**Image watcher.** Apps deploy a pinned digest. For floating tags, a job re-resolves the tag every 10 minutes, checks
the new image against the caps, and redeploys when the digest moved. A bad new image (too big, root user) is rejected
and the old one keeps running.

**Secrets.** Env var values, DB passwords and bucket keys are Fernet-encrypted in the platform DB. They are decrypted
only when the reconciler writes the app's Kubernetes Secret. `FERNET_KEY` lives in a GitHub secret and a k8s Secret, and
rotation is supported.

## Cost

About $75/mo baseline (node, managed MySQL, managed PostgreSQL, load balancer, Spaces) and about $103 while the autoscaler runs a second
node. Per-app cost is about $0 until the node fills up. Details, sources and what we couldn't verify:
[docs/ARCHITECTURE.md §1](docs/ARCHITECTURE.md#1-hosting-cost).

## Repo layout

```
app/         FastAPI backend (routers/, models.py, reconciler.py, quota.py, image_check.py, providers/)
frontend/    Vue 3 + Vite + Pinia + Tailwind (built into the app image)
cli/         Command-line client
tests/       pytest; tests/e2e and tests/pentest run against the live site
alembic/     DB migrations
infra/       Terraform (DigitalOcean), state in Terraform Cloud
deploy/      Kubernetes manifests and Helm values (traefik, cert-manager, metrics-server, platform, mysql)
testapps/    Sample apps used by e2e
docs/        Ledger, architecture, operations, requests
```

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
git config core.hooksPath .githooks      # gitleaks on staged changes

uvicorn app.main:app --reload            # SQLite at ./data/picopaas.db
python -m pytest tests/ -v               # SQLite in memory, real login flow

alembic upgrade head                     # migrations
cd frontend && npm install && npm run dev
```

Outside `ENVIRONMENT=dev`, set `FERNET_KEY` (the JWT key is derived from it). See [CLAUDE.md](CLAUDE.md) for the full
list of env vars and [docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md) for running a platform and [docs/deployment/](docs/deployment/README.md) for the live instance.
