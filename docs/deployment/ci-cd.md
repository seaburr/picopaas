# CI/CD

GitHub Actions in `.github/workflows/`. All are manual (`workflow_dispatch`) unless noted. `bootstrap` and `deploy`
share a concurrency lock. For the generic upgrade story see the [Admin guide](../ADMIN_GUIDE.md).

| Workflow | Use it when | Notes |
|---|---|---|
| `ci` (push/PR) | Automatic | gitleaks (no allowlist files allowed), terraform validate, kubeconform, backend tests, MySQL integration, frontend. |
| `image` (push to main) | Automatic | Builds and pushes `ghcr.io/seaburr/picopaas:<short sha>`. |
| `terraform` | Create or change DO infra (cluster, MySQL, PostgreSQL, VPC, firewall, DNS, Spaces bucket) | Input `apply` (default true; untick for plan only). Skips if `TF_API_TOKEN` is unset. Pre-checks the MySQL slug and that the node size currently has capacity in the region. Sets the TFC workspace to local execution. |
| `bootstrap` | Fresh or replaced cluster, or when platform add-ons/secrets change | Installs Traefik, cert-manager, metrics-server, upserts DNS A records `*` and `picopaas`, creates secrets and the platform DB, issuers and wildcard cert, deploys the platform, waits for health (up to about 10 min). Idempotent. Input `sha`. |
| `deploy` (also after a successful `image` run on main) | Roll a new platform version | Applies RBAC, deployment (image + env checksum), service, ingress; waits for rollout. Input `sha`. Skips if there is no token or no cluster `picopaas-nyc3`. |
| `diag` | Something is broken in-cluster | Inputs `namespace` (default `picopaas-system`), `selector`, `tail`. Prints nodes, pods, events, non-ready pod descriptions and logs with secret values masked. |
| `testapps` (push to `testapps/**`) | Automatic, or run to rebuild | Builds `hello`, `probe`, `toolbox`, `webpage` to `ghcr.io/seaburr/picopaas-testapp-*`. |
| `quota-e2e` | Weekly | Runs the slow quota-fill E2E tests (`E2E_SLOW=1`). |
| `examples`, `floating-tag-test` | As needed | Example-app builds and the floating-tag watcher test. |

Order for a new environment: `terraform`, `bootstrap`, register the first user (becomes platform admin), then run
`tests/e2e` and `tests/pentest` (see their READMEs).

## Rollback

Re-run `deploy` with an earlier `sha` (every image is tagged by commit). Migrations run at start and only move forward,
so prefer a forward fix when one shipped. For a one-off: `kubectl -n picopaas-system rollout undo deploy/picopaas`.

## Secret names

Stored as GitHub repo secrets (write-only; keep your own copy in a password manager). No values appear in the repo.

| GitHub secret | Purpose |
|---|---|
| `PICOPAAS_FERNET_KEY` | The only platform key (see [runbooks.md](runbooks.md)). Bootstrap fails if it is missing. |
| `DIGITALOCEAN_TOKEN`, `SPACES_ACCESS_KEY_ID`, `SPACES_SECRET_ACCESS_KEY` | Terraform, bootstrap, and the platform's own provisioning of buckets and keys. `DO_API_TOKEN` is an alias in the platform env. Needs DNS write scope for the domain. |
| `TF_API_TOKEN` | Terraform Cloud state (`TF_TOKEN_app_terraform_io` in CI). |
| `E2E_USER_TOKEN`, `E2E_ADMIN_TOKEN` | `pp_sk_` API keys for the live E2E suite (a test customer and the platform admin). `E2E_ADMIN_TOKEN` is Chas's own account. Optional E2E env: `E2E_IMAGE_TAG`, `E2E_SLOW`, `E2E_APP_PORT`, `E2E_BIG_IMAGE`; pentest victim variables are listed in `tests/pentest/README.md`. |

Platform pod env (k8s Secret `picopaas-env`): `FERNET_KEY` (+ optional `FERNET_KEYS_PREVIOUS`), `DATABASE_URL`,
`DIGITALOCEAN_TOKEN`, `SPACES_ACCESS_KEY_ID`, `SPACES_SECRET_ACCESS_KEY`, `BOOTSTRAP_DB_*`, `BOOTSTRAP_PG_*`. Other
knobs set at bootstrap: `MYSQL_EGRESS_MODE`/`MYSQL_EGRESS_CIDR`, `PG_EGRESS_CIDR`, `MYSQL_SSL_CA`, `INGRESS_CLASS`
(traefik), `INGRESS_NAMESPACE` (traefik), `APP_DOMAIN`, `MAX_NODES` (2).
