# deploy/

Plain YAML manifests + two third-party Helm charts (ingress-nginx, cert-manager — values files
only, no templating of our own stuff in Helm). Nothing here has been `kubectl apply`d or `helm
install`ed against real infra; per `infra-ops` rules that requires an APPROVED `docs/requests/`
entry and this chunk didn't create one (no new paid resource — LB cost is already covered by
`use_load_balancer` in `infra/variables.tf` / request 0001).

## Install order

1. **ingress-nginx**
   ```
   helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
   helm repo update
   # Pin at install time: run `helm search repo ingress-nginx/ingress-nginx --versions | head`
   # and pick the latest stable chart version — not hardcoded here so this doesn't go stale.
   helm install ingress-nginx ingress-nginx/ingress-nginx \
     -n ingress-nginx --create-namespace \
     --version <PIN_AT_INSTALL_TIME> \
     -f deploy/ingress-nginx/values.yaml
   ```
   If `use_load_balancer = true` (default, matches `infra/variables.tf`): after this installs,
   `kubectl get svc -n ingress-nginx` for the LB's external IP, then feed it into Terraform's
   `wildcard_lb_ip` and re-apply (see that variable's comment for why it's a chicken-and-egg step).
   If `use_load_balancer = false`: edit `deploy/ingress-nginx/values.yaml` to swap in the
   commented hostPort/DaemonSet block first, and the (not yet written) `dns-sync` CronJob is a
   hard prerequisite for DNS to work at all.

2. **cert-manager**
   ```
   helm repo add jetstack https://charts.jetstack.io
   helm repo update
   # Pin at install time: `helm search repo jetstack/cert-manager --versions | head`
   helm install cert-manager jetstack/cert-manager \
     -n cert-manager --create-namespace \
     --version <PIN_AT_INSTALL_TIME> \
     -f deploy/cert-manager/values.yaml
   ```
   Then create the DO DNS token secret **by hand** (see "Secrets to create by hand" below),
   then:
   ```
   kubectl apply -f deploy/cert-manager/clusterissuer.yaml
   kubectl apply -f deploy/cert-manager/certificate.yaml
   ```
   Test with `letsencrypt-staging` first (edit `certificate.yaml`'s `issuerRef.name`) before
   switching to `letsencrypt-prod`, to avoid burning Let's Encrypt's production rate limits on a
   typo. `kubectl describe certificate wildcard-seaburr-io -n ingress-nginx` to watch issuance.

3. **platform**
   ```
   kubectl apply -f deploy/platform/namespace.yaml
   kubectl apply -f deploy/platform/rbac.yaml
   ```
   Then create the `picopaas-env` secret **by hand** (see below), then:
   ```
   kubectl apply -f deploy/platform/deployment.yaml
   kubectl apply -f deploy/platform/service.yaml
   kubectl apply -f deploy/platform/ingress.yaml
   ```

## Secrets to create by hand (placeholders only — never in this repo)

```
# cert-manager namespace, DNS-01 solver credential
kubectl create secret generic digitalocean-dns \
  -n cert-manager \
  --from-literal=access-token='<DO_API_TOKEN_PLACEHOLDER>'

# picopaas-system namespace, platform app config
kubectl create secret generic picopaas-env -n picopaas-system \
  --from-literal=SECRET_KEY='<placeholder>' \
  --from-literal=FERNET_KEY='<placeholder>' \
  --from-literal=DATABASE_URL='<placeholder>' \
  --from-literal=DO_API_TOKEN='<placeholder>' \
  --from-literal=DO_SPACES_KEY='<placeholder>' \
  --from-literal=DO_SPACES_SECRET='<placeholder>'
```

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

## Not done in this chunk

- `dns-sync` CronJob (no-LB variant) — still a TODO per `infra/variables.tf` and ARCHITECTURE.md.
- In-cluster MySQL StatefulSet manifests (only relevant if `use_managed_mysql = false`).
- Chart version pins above are deliberately left as "pin at install time" placeholders — this
  chunk had a hard ~10-minute budget and did not spend it on a version lookup; whoever runs the
  install should grab current stable versions at that time.
