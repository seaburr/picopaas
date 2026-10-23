# The picopaas.xyz deployment

This directory documents the one live instance of PicoPaaS, run at `picopaas.xyz` on DigitalOcean. It is the
instance-specific counterpart of the generic [Admin guide](../ADMIN_GUIDE.md), which is served in-app to platform
admins and describes how to run any PicoPaaS. Nothing here is bundled into the product.

| File | Contents |
|---|---|
| [infrastructure.md](infrastructure.md) | Cloud layout: regions, cluster and node pool, managed databases, Spaces, DNS and domain, cost. |
| [ci-cd.md](ci-cd.md) | GitHub Actions workflows, secret names, deploy order, E2E and pentest tokens. |
| [runbooks.md](runbooks.md) | Instance runbooks with real names: key rotation, cluster replacement (2026-10-19), accepted risks, open issues. |

## Other deployment material in the repo

These already lived outside `docs/` and belong to this instance too:

| Path | Contents |
|---|---|
| `infra/` | Terraform (DOKS, managed MySQL and PostgreSQL, VPC, firewall, DNS, platform bucket). See `infra/README.md`. |
| `deploy/` | Kubernetes manifests and Helm values (Traefik, cert-manager, metrics-server, platform, in-cluster MySQL, Ghost). See `deploy/README.md`. |
| `.github/workflows/` | CI, image build, deploy, bootstrap, terraform, diag. See [ci-cd.md](ci-cd.md). |
| `docs/requests/` | Infra requests to Chas (what, why, monthly cost). |
| `docs/QUESTIONS.md`, `docs/LEDGER.md` | Project-management files for this instance. |

## If the repos are split

Planned split: the platform repo keeps `app/`, `frontend/`, `alembic/`, `tests/`, `cli/`, `examples/`, the Dockerfile,
`docs/` (User Guide, Admin guide, API, CLI, Recipes, Architecture, Pentest notes) and the generic parts of
`deploy/` (platform manifests, RBAC, default Traefik/cert-manager values). A deployment repo for `picopaas.xyz` would
take `docs/deployment/`, `infra/`, the workflows that touch the live cluster (`terraform`, `bootstrap`, `deploy`,
`diag`, `quota-e2e`), `deploy/ghost/`, `deploy/dns-sync/`, `docs/requests/` and the instance values (domain, regions,
sizes, secret names).
