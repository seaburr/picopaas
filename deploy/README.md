# deploy/

Plain YAML manifests + two third-party Helm charts (Traefik, cert-manager — values files
only, no templating of our own stuff in Helm). Nothing here has been `kubectl apply`d or `helm
install`ed against real infra; per `infra-ops` rules that requires an APPROVED `docs/requests/`
entry and this chunk didn't create one (no new paid resource — LB cost is already covered by
`use_load_balancer` in `infra/variables.tf` / request 0001).

## Automated bring-up (managed MySQL + LB variant)

`.github/workflows/bootstrap.yml` (workflow_dispatch, run by hand after the `terraform` workflow) automates steps 2-8
below for the managed-MySQL + LB variant: Traefik and cert-manager (pinned chart versions in the workflow `env`),
DNS A records `*` and `picopaas` -> LB IP (it owns them; leave Terraform `wildcard_lb_ip` empty), the
`digitalocean-dns` / `picopaas-env` / `picopaas-db-ca` secrets, the platform `picopaas` database + user on the managed
cluster, issuers/certificate, platform workload at the current short-sha image, and a `/healthz` check. It is safe to
re-run: existing SECRET_KEY, FERNET_KEY and the platform DB password are read back, never rotated. The manual steps
below remain the reference (and the path for the in-cluster / no-LB variants).

## Bring-up runbook (in order)

Steps marked **[0001]** wait on Chas's request-0001 checklist (approval/variant, DO token, Spaces key,
Terraform Cloud workspace, GHCR package public). Everything else is already in the repo.

1. **Terraform apply [0001: approval + variant + tokens]** - from `infra/`, with `TF_TOKEN_app_terraform_io`
   set (or via Terraform Cloud UI). `terraform apply` only after request 0001 says APPROVED. Variant flags:
   `use_load_balancer`, `use_managed_mysql`.
2. **Kubeconfig [0001: DO token]** - `doctl kubernetes cluster kubeconfig save picopaas-atl1`.
3. **Traefik** (helm, Ingress API only; install the chart's CRDs, needed for the default TLSStore)
   ```
   helm repo add traefik https://traefik.github.io/charts && helm repo update
   helm install traefik traefik/traefik -n traefik --create-namespace \
     --version 42.0.0 -f deploy/traefik/values.yaml
   ```
   LB variant: take the Service's external IP (`kubectl -n traefik get svc traefik`) into Terraform
   `wildcard_lb_ip` and re-apply. The no-LB (hostPort) variant is not wired in the Traefik values yet.
4. **cert-manager** (helm) + DNS token secret + issuer/certificate
   ```
   helm repo add jetstack https://charts.jetstack.io && helm repo update
   helm install cert-manager jetstack/cert-manager -n cert-manager --create-namespace \
     --version <PIN_AT_INSTALL_TIME> -f deploy/cert-manager/values.yaml
   ```
   Create `digitalocean-dns` (see Secrets), then apply `deploy/cert-manager/clusterissuer.yaml` and
   `certificate.yaml`. Try `letsencrypt-staging` first.
5. **Platform namespace + RBAC + secrets**
   ```
   kubectl apply -f deploy/platform/namespace.yaml -f deploy/platform/rbac.yaml
   ```
   Create `picopaas-env` (see Secrets). `DATABASE_URL` is the platform's own metadata DB.
6. **MySQL for app databases**
   - **Managed (`use_managed_mysql = true`)**: nothing to deploy; register the managed server as a
     `DbServer` using Terraform outputs. Skip to step 7.
   - **In-cluster (`false`)**:
     ```
     kubectl apply -f deploy/mysql/namespace.yaml
     # create secrets mysql-root and mysql-backup-spaces (see Secrets)
     kubectl apply -f deploy/mysql/networkpolicy.yaml -f deploy/mysql/statefulset.yaml
     kubectl -n picopaas-data rollout status statefulset/mysql
     kubectl apply -f deploy/mysql/backup-cronjob.yaml
     ```
     root is localhost-only. Register the platform's `picopaas-env` with `BOOTSTRAP_DB_HOST=mysql.picopaas-data.svc`,
     `BOOTSTRAP_DB_USER=picopaas_admin`, `BOOTSTRAP_DB_PASSWORD=<MYSQL_ADMIN_PASSWORD>` (never root). Backups use
     `picopaas_backup` (read-only). Users are created by the `mysql-initdb` ConfigMap on first init only.
     Hardening an existing MySQL (initdb won't rerun): `kubectl -n picopaas-data exec -it mysql-0 -- sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD"'`,
     run the SQL from `deploy/mysql/statefulset.yaml` (`10-users.sh`) with real passwords, then
     `RENAME USER 'root'@'%' TO 'root'@'localhost'` (or `DROP USER 'root'@'%'`) and remove `MYSQL_ROOT_HOST`.
     Verify a manual backup: `kubectl -n picopaas-data create job --from=cronjob/mysql-backup test-backup`.
7. **Platform workload**
   ```
   kubectl apply -f deploy/platform/deployment.yaml -f deploy/platform/service.yaml -f deploy/platform/ingress.yaml
   ```
   After this, `deploy.yml` (GitHub Actions) rolls new images out automatically once `DIGITALOCEAN_TOKEN` exists.
   The GHCR package must be public **[0001]** or the pull fails.
8. **DNS**
   - LB variant: Terraform owns the wildcard A record (step 3). `picopaas` is covered by the wildcard.
   - No-LB variant: install dns-sync, which keeps `*` and `picopaas` A records on the node IP(s):
     ```
     kubectl apply -f deploy/dns-sync/dns-sync.yaml   # creates ns + RBAC + CronJob
     # create do-dns-token in picopaas-dns-sync (see Secrets); the CronJob fails until it exists
     ```
     It only upserts/deletes type-A records named `*` and `picopaas`. TXT records are never touched.

## MySQL restore (in-cluster variant)

Backups: `s3://<platform bucket>/mysql-backups/mysql-YYYYMMDD-HHMMSS.sql.gz`, nightly 03:17 UTC, 7 days kept.
```
# 1. fetch (any machine with the Spaces key)
aws --endpoint-url https://atl1.digitaloceanspaces.com s3 cp \
  s3://<bucket>/mysql-backups/<file>.sql.gz ./restore.sql.gz
# 2. load into the running server (dump includes all databases, users and grants)
gunzip -c restore.sql.gz | kubectl -n picopaas-data exec -i mysql-0 -- \
  sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD"'
```
Total loss (PVC gone): redeploy step 6, wait for `mysql-0` Ready, then run step 2. Restoring over a live
server overwrites same-named databases; stop the platform first (`kubectl -n picopaas-system scale deploy/picopaas --replicas=0`)
if you need a consistent point in time. Managed MySQL uses DO's own backups instead.

## Secrets to create by hand (placeholders only; never in this repo)

```
kubectl create secret generic digitalocean-dns -n cert-manager \
  --from-literal=access-token='<DO_API_TOKEN_PLACEHOLDER>'

kubectl create secret generic picopaas-env -n picopaas-system \
  --from-literal=SECRET_KEY='<placeholder>' --from-literal=FERNET_KEY='<placeholder>' \
  --from-literal=DATABASE_URL='<placeholder>' --from-literal=DO_API_TOKEN='<placeholder>' \
  --from-literal=SPACES_ACCESS_KEY_ID='<placeholder>' --from-literal=SPACES_SECRET_ACCESS_KEY='<placeholder>'

# in-cluster MySQL only
kubectl create secret generic mysql-root -n picopaas-data \
  --from-literal=MYSQL_ROOT_PASSWORD='<placeholder>' \
  --from-literal=MYSQL_ADMIN_PASSWORD='<placeholder, >=32 chars e.g. openssl rand -hex 24>' \
  --from-literal=MYSQL_BACKUP_PASSWORD='<placeholder>'
kubectl create secret generic mysql-backup-spaces -n picopaas-data \
  --from-literal=SPACES_ACCESS_KEY_ID='<placeholder>' --from-literal=SPACES_SECRET_ACCESS_KEY='<placeholder>' \
  --from-literal=SPACES_BUCKET='<platform bucket name>' --from-literal=SPACES_ENDPOINT='https://atl1.digitaloceanspaces.com'

# no-LB variant only
kubectl create secret generic do-dns-token -n picopaas-dns-sync \
  --from-literal=DO_API_TOKEN='<placeholder>'
```
Prefer a DO token scoped to domains only for `do-dns-token` / `digitalocean-dns` if DO offers it.

## Known RBAC exposure (flag for scm-security)

`deploy/platform/rbac.yaml`'s `picopaas-reconciler` ClusterRole grants `secrets`
get/list/watch/create/update/patch/delete at **cluster scope**, because the reconciler creates
and manages an arbitrary, dynamically-named namespace per app (`app-<id>`) and there's no way to
pre-scope a namespaced Role to namespaces that don't exist yet. The same is true of
`deployments`, `services`, `ingresses`, `resourcequotas`, `limitranges`, and `networkpolicies`.

**Practical effect: a compromised platform pod (e.g. via an RCE in the FastAPI app) can read
every Secret in every app namespace cluster-wide**, not just the ones it created — including
per-app DB credentials and Spaces keys for every tenant. No wildcard verbs/resources were used,
there's no `roles`/`rolebindings`/`escalate`/`bind`/`impersonate`, no `nodes`, and no
`pods/exec`/`attach`/`portforward` — but the Secrets-cluster-wide exposure is real and is the
single biggest blast-radius risk in this manifest set for a single-tenant-compromise-to
cross-tenant-breach scenario. Needs a `docs/PENTEST.md` entry and a mitigation discussion
(namespace-scoped Roles created per app namespace instead of one ClusterRole is the likely fix,
at the cost of more moving parts in the reconciler).

## Not done / notes

- Nothing under `deploy/` has been applied; `kubeconform` (CI job) validates the manifests offline.
- Chart versions are pinned in `bootstrap.yml` env (Traefik 42.0.0, cert-manager v1.21.2); the manual steps above still say `<PIN_AT_INSTALL_TIME>`.
- Managed MySQL needs TLS: the platform DATABASE_URL uses `?ssl_ca=/etc/picopaas/db-ca/ca.crt` (secret `picopaas-db-ca`, mounted by the Deployment). `MySQLAdminClient` does not yet pass `ssl=` to pymysql (app gap, see ledger).
- The mysql StatefulSet relies on the official image running as uid 999 with `fsGroup: 999`; verify on first boot.
