# Work Ledger

Append-only. Every agent adds a line when it starts and when it finishes a chunk, so work survives
token exhaustion. Format: `- YYYY-MM-DD [agent] STATUS: what (files / commit)`.
STATUS ∈ START, DONE, BLOCKED, HANDOFF. **Next session: read "Now / Next" first.**

## Now / Next
- BLOCKED on Chas: request 0001 (infra approval + secrets). Infra work stops here, but app work does not.
- NEXT (backend-dev): scaffold `app/` (FastAPI, models User/App/AppEnv, auth with first-user-uberadmin,
  pytest with SQLite and the real login flow), following tribal's layout. See ARCHITECTURE.md §2.
- NEXT (frontend-dev, after the backend auth endpoints exist): scaffold `frontend/` (Vue 3, Vite, Pinia, Tailwind), with login/register pages and an app list.
- NEXT (infra-ops): `infra/` Terraform for request 0001 is written (not applied). Still TODO:
  `deploy/` manifests (ingress-nginx, cert-manager, mysql, platform), and the `dns-sync` CronJob
  for the no-load-balancer case.
- NEXT (backend-dev): spike the DO API for per-bucket Spaces keys (ARCHITECTURE.md §2 ⚠).

## Log
- 2026-10-18 [manager] DONE: bootstrap. Researched DO pricing, picked the lean architecture (~$30/mo),
  wrote CLAUDE.md, ARCHITECTURE.md, request 0001, agent definitions, .gitignore, CI secret scan.
- 2026-10-18 [infra-ops] START: write (not apply) Terraform for request 0001 in `infra/`, plus a
  `terraform` CI job.
- 2026-10-18 [infra-ops] DONE: wrote `infra/` (versions.tf, variables.tf, network.tf, cluster.tf,
  storage.tf, database.tf, firewall.tf, dns.tf, outputs.tf, terraform.tfvars.example, README.md) —
  VPC, DOKS (version via data source, auto-upgrade, maintenance window), private platform Spaces
  bucket, optional managed MySQL + DB firewall trusting the k8s cluster, optional node firewall for
  the no-LB case, `seaburr.io` domain as a data source only (no TXT records touched), conditional
  wildcard A record with a TODO for wiring the LB IP, S3 backend pointed at a manually-created
  `picopaas-tfstate` Spaces bucket. `terraform fmt -recursive` and `terraform init -backend=false &&
  terraform validate` both pass locally. Added a `terraform` job to `.github/workflows/ci.yml`
  (hashicorp/setup-terraform; fmt -check + init -backend=false + validate in `infra/`), secret-scan
  job untouched. Nothing applied; no secrets written. Not committed.
