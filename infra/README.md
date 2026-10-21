# PicoPaaS infra (Terraform, DigitalOcean)

Status: **written, not applied.** Nothing here may run `terraform apply` or any `doctl`/`kubectl`
create command against real infra until `docs/requests/0001-core-infra.md` says APPROVED by Chas.
See `.claude/agents/infra-ops.md` for the rule.

## What this is

Terraform for request 0001 (`docs/requests/0001-core-infra.md`): a DOKS cluster, a platform Spaces
bucket, optional managed MySQL, and the DNS/firewall wiring needed depending on two flags:

- `use_managed_mysql` (default `true`) — managed DO MySQL vs. in-cluster MySQL (the in-cluster
  StatefulSet itself lives in `deploy/`, not here).
- `use_load_balancer` (default `true`) — a DO Load Balancer (created implicitly by Traefik's
  `Service.type: LoadBalancer` in `deploy/`) vs. Traefik on node hostPorts + a firewall rule
  here opening 80/443 on the nodes.

The Terraform code is identical across all four flag combinations — see cost table below.

## State: Terraform Cloud

State and runs live in Terraform Cloud: org `seaburr`, workspace `picopaas` (the `cloud {}` block in
`versions.tf`), the same setup as tribal. Locally, run `terraform login` once. CI uses
`TF_TOKEN_app_terraform_io` from the GitHub secret `TF_API_TOKEN`.

Runs execute remotely in TFC by default, so the DO credentials below have to be **sensitive
environment variables on the TFC workspace**. Shell env vars on your machine don't reach a remote run.

## Credentials (never in files)

Set as environment variables (locally, or as GitHub Actions secrets — never written to any file in
this repo):

| Env var | Used for |
|---|---|
| `DIGITALOCEAN_TOKEN` | the `digitalocean` provider (DO API) |
| `SPACES_ACCESS_KEY_ID` / `SPACES_SECRET_ACCESS_KEY` | the `digitalocean` provider's Spaces calls (the platform bucket) |
| `TF_TOKEN_app_terraform_io` | Terraform Cloud auth (CI: GitHub secret `TF_API_TOKEN`) |

Variables that aren't secret but you don't want to retype can go in `terraform.tfvars` (copy from
`terraform.tfvars.example`, gitignored) or as `TF_VAR_<name>` env vars. Example:

```bash
export DIGITALOCEAN_TOKEN=...       # from GitHub secret, not typed into a file
export AWS_ACCESS_KEY_ID=...        # Spaces access key
export AWS_SECRET_ACCESS_KEY=...    # Spaces secret key
export TF_VAR_use_managed_mysql=true
export TF_VAR_use_load_balancer=true

cd infra
terraform init
terraform plan   # review only — do not apply without an APPROVED request 0001
```

## Cost per flag combination

From `docs/requests/0001-core-infra.md` / `docs/ARCHITECTURE.md` §1, ATL1, static monthly cost
(excludes per-app marginal cost, which is ~$0 for the first 8–10 apps then ~$2/app):

| `use_managed_mysql` | `use_load_balancer` | Monthly | Notes |
|---|---|---|---|
| `false` | `false` | **~$30** | lean option: node $24 + Spaces $5 + volume $1 |
| `false` | `true`  | **~$42** | adds DO LB $12 |
| `true`  | `false` | **~$49** | adds managed MySQL (Premium Intel in ATL1, ~$20 est.) |
| `true`  | `true`  | **~$61** | managed option Chas is leaning toward; over the $40 budget by ~$21 |

**Managed MySQL price in ATL1 is unconfirmed.** Per Chas (2026-10-18), ATL1 managed MySQL is
Premium Intel only — the basic 1 GiB slug ($15.15/mo) may not be orderable there at all. The ~$20
figure is an estimate; check the DO control panel (or `doctl databases options sizes`) when
actually ordering, and update this table once confirmed.

## Files

| File | Contents |
|---|---|
| `versions.tf` | provider + Terraform Cloud (`cloud {}`) config |
| `variables.tf` | all input variables, with cost/caveat comments |
| `network.tf` | VPC |
| `cluster.tf` | DOKS cluster + node pool (version via `digitalocean_kubernetes_versions` data source, auto-upgrade, maintenance window) |
| `storage.tf` | platform Spaces bucket (private) |
| `database.tf` | optional managed MySQL cluster + DB firewall trusting the k8s cluster |
| `firewall.tf` | DO firewall opening 80/443 to nodes, only when `use_load_balancer = false` |
| `dns.tf` | `picopaas.xyz` domain as a **data source only** (never created/destroyed) + conditional wildcard A record |
| `outputs.tf` | cluster id/endpoint, mysql host (sensitive), bucket names |
| `terraform.tfvars.example` | copy to `terraform.tfvars` (gitignored) |

## DNS — read this before touching anything DNS-related

- The `picopaas.xyz` domain is looked up with `data "digitalocean_domain"` only. This Terraform must
  never create or destroy the domain.
- **Never manage or touch TXT records.** General rule, kept from the old domain (the Bluesky TXT on seaburr.io) —
  nothing in this directory should ever define a `digitalocean_record` of type `TXT`.
- The wildcard `*.picopaas.xyz` A record is only created here when `use_load_balancer = true` *and*
  `wildcard_lb_ip` is set. That IP comes from the DO Load Balancer that Traefik's
  `Service.type: LoadBalancer` creates — which doesn't exist until `deploy/` is applied, after this
  Terraform. See the `TODO` comment on `variables.wildcard_lb_ip` in `variables.tf` for the
  two-step apply sequence.
- When `use_load_balancer = false`, DNS for the wildcard is **not** managed by Terraform at all —
  a `dns-sync` CronJob (TODO, not yet written — see `ARCHITECTURE.md` trade-offs) is expected to
  keep `*.picopaas.xyz` pointed at the current node's public IP, polling every ~2 minutes via the DO
  API, since node IPs change on recycle/upgrade.

- **Chas adds the domain in DO** (Networking > Domains) after moving the registrar nameservers to
  `ns1/ns2/ns3.digitalocean.com` (request 0003). Terraform keeps it a data source, so `plan`/`apply`
  fail until the domain exists in DO. Nothing here creates it.

## Validating locally (never applying)

```bash
cd infra
terraform fmt -recursive
terraform init -backend=false
terraform validate
```

The `infra-ops` agent may run `fmt`/`init -backend=false`/`validate` locally. It must never run
`terraform apply`, `terraform init` with the real backend against live state in an uncontrolled
way, or any `doctl`/`kubectl` create command, unless `docs/requests/0001-core-infra.md` says
APPROVED.
