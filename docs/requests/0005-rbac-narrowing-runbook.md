# 0005: Platform ServiceAccount RBAC narrowing (pentest MEDIUM) - rollout and verification runbook

Status: APPROVED by Chas (2026-10-21). Implemented in the working tree, **not committed, not applied**. No new paid resource
(RBAC objects only); this file exists so the apply has a written before/after.

1) **What is being deployed.** A change to `deploy/platform/rbac.yaml` (applied by `deploy.yml` on merge and by `bootstrap.yml`):
   `deployments`, `ingresses`, `networkpolicies` move out of the cluster-wide ClusterRole `picopaas-reconciler` into the
   namespaced-by-binding ClusterRole `picopaas-app-ns`; the cluster-wide role keeps only namespaces, rolebindings (create/patch/get/list),
   `bind` on `picopaas-app-ns`, and read-only pods/pods-log/nodes/metrics. A `ValidatingAdmissionPolicy` + binding
   (`picopaas-rolebinding-scope`) denies RoleBinding create/update by this SA outside `app-*` / `picopaas-*` namespaces.
2) **Why.** A compromised platform pod could otherwise create Deployments/Ingresses/NetworkPolicies in kube-system and bind itself
   (`bind` on `picopaas-app-ns`, which grants Secrets CRUD) into any namespace: cluster takeover.
3) **Cost.** $0 static, $0 per app.

## Why nothing the reconciler does is lost

Order of writes for a new app (`flatten_manifests`): Namespace, RoleBinding (`app-<id>/picopaas-platform` -> `picopaas-app-ns`), ResourceQuota,
LimitRange, 5-7 NetworkPolicies, Secret, Deployment, Service, Ingress (+ custom Ingress). Namespace and RoleBinding are the only cluster-wide
writes; everything after the RoleBinding is in `picopaas-app-ns`. Existing app namespaces (app-97, app-98) and `picopaas-system` already have
that RoleBinding, so they gain the new rules the moment the ClusterRole is updated. `tests/test_platform_rbac.py` encodes the audit (every
rendered kind, in apply order, must be allowed; no app-resource write allowed in kube-system/default/cert-manager/traefik/picopaas-data).
`kubectl apply -f rbac.yaml` applies `picopaas-app-ns` BEFORE `picopaas-reconciler`, so there is no window where the SA lacks the grant.

## BEFORE (captured read-only 2026-10-21 18:20Z, `--as=system:serviceaccount:picopaas-system:picopaas`)

| check | kube-system | default | cert-manager | traefik | picopaas-data | picopaas-system | app-97 / app-98 |
|---|---|---|---|---|---|---|---|
| create deployments | **yes** | **yes** | yes | yes | yes | yes | yes |
| create ingresses | **yes** | **yes** | yes | yes | yes | yes | yes |
| create networkpolicies | **yes** | **yes** | yes | yes | yes | yes | yes |
| create rolebindings | **yes** | **yes** | yes | yes | yes | yes | yes |
| create services/secrets/resourcequotas/limitranges | no | no | no | no | no | yes | yes |

Cluster-wide: create/delete/patch/list namespaces yes; list pods -A, get pods/log, list nodes, list metrics yes; `bind picopaas-app-ns` yes,
`bind cluster-wide-admin` no, `escalate` no. Server dry-run (`kubectl create rolebinding ... --as=SA --dry-run=server`) succeeded in kube-system,
default and app-97; `kubectl create deployment ... --dry-run=server` succeeded in kube-system. Full output: regenerate with the script below.

`kubectl diff -f deploy/platform/rbac.yaml` (server-side) shows exactly: `picopaas-app-ns` +2 rules, `picopaas-reconciler` -2 rules, +1 VAP, +1 VAP binding; nothing else changes.

## AFTER: checks to run once applied (read-only except step 4/5)

```bash
export KUBECONFIG=/path/to/picopaas-nyc3-kubeconfig.yaml
SA=system:serviceaccount:picopaas-system:picopaas
can() { printf "%-60s %s\n" "$*" "$(kubectl auth can-i "$@" --as=$SA 2>/dev/null | head -1)"; }

# 1. RBAC objects landed and the deploy applied cleanly
kubectl get clusterrole picopaas-reconciler -o json | jq -r '.rules[] | "\(.apiGroups) \(.resources) \(.verbs)"'   # no deployments/ingresses/networkpolicies
kubectl get clusterrole picopaas-app-ns -o json | jq -r '.rules[] | "\(.apiGroups) \(.resources)"'               # now includes apps/deployments + networking.k8s.io
kubectl get validatingadmissionpolicy,validatingadmissionpolicybinding picopaas-rolebinding-scope

# 2. can-i matrix. kube-system / default / cert-manager / traefik / picopaas-data creates must now be NO:
for ns in kube-system default cert-manager traefik picopaas-data; do
  for r in deployments.apps ingresses.networking.k8s.io networkpolicies.networking.k8s.io; do can create $r -n $ns; done; done
# ...and these must stay YES (app namespaces and the platform namespace):
for ns in app-97 app-98 picopaas-system; do
  for r in deployments.apps ingresses.networking.k8s.io networkpolicies.networking.k8s.io services secrets resourcequotas limitranges; do can create $r -n $ns; done; done
for v in get list watch update patch delete; do for r in deployments.apps ingresses.networking.k8s.io networkpolicies.networking.k8s.io; do can $v $r -n app-97; done; done
# unchanged (YES): create/delete/patch/list namespaces, list pods -A, get pods/log -n app-97, list nodes, list pods.metrics.k8s.io -A,
#                  create/patch rolebindings (cluster-wide RBAC still allows it; the VAP is what stops kube-system), bind clusterroles/picopaas-app-ns
# unchanged (NO):  list secrets -n kube-system, bind clusterroles/cluster-admin, escalate clusterroles

# 3. Admission policy (server dry-run, nothing persists). kube-system/default must be DENIED, app-97 must be allowed:
for ns in kube-system default cert-manager app-97 picopaas-system; do
  echo "== $ns"; kubectl create rolebinding vap-probe --clusterrole=picopaas-app-ns --serviceaccount=picopaas-system:picopaas \
    -n $ns --as=$SA --dry-run=server 2>&1 | grep -v '^Warning: version'; done
# expect: "denied request: ValidatingAdmissionPolicy 'picopaas-rolebinding-scope' ... may only create RoleBindings in app-* or picopaas-*"
#         for kube-system, default, cert-manager; "created (server dry run)" for app-97 and picopaas-system.
# The VAP only matches this SA: your admin kubeconfig must still be able to create RoleBindings anywhere
#   (kubectl create rolebinding vap-probe --clusterrole=view --user=x -n kube-system --dry-run=server  -> created (server dry run)).
```

4. **Existing apps keep working through a platform rollout.** `deploy.yml` restarts the platform pods; then `kubectl -n picopaas-system logs deploy/picopaas | grep -iE "forbidden|403"` must show nothing new,
   and the drift sweep must leave app-97 / app-98 `running` (Health page / `GET /apps`). To force a re-apply of an existing app without a user change, use the app's Restart button (`POST /apps/{id}/restart`: changes the pod template, so the sweep re-applies Deployment+Secret+Ingress+NetworkPolicies).
5. **Throwaway app end to end (new namespace + RoleBinding + Deployment + Ingress; the real regression test).** As any active user (admin is fine):
   ```bash
   curl -sS -X POST https://platform.picopaas.xyz/apps/spec -H "Authorization: Bearer $PICOPAAS_TOKEN" -H 'Content-Type: application/json' \
     -d '{"spec":"apiVersion: picopaas/v1\nname: rbac-check\nimage: ghcr.io/seaburr/picopaas-testapp-hello:latest\nport: 8080\nenv:\n  APP_VERSION: \"1.0.0\"\n"}'
   # poll until status running (a minute or two), note the app id N
   kubectl get ns app-N; kubectl -n app-N get rolebinding picopaas-platform,deploy,ingress,netpol,svc,resourcequota,limitrange
   curl -sS -o /dev/null -w '%{http_code}\n' https://<hostname>.picopaas.xyz/        # 200
   # also exercise the other writes: edit an env var (Secret + Deployment roll), then Suspend (delete_ingress + scale 0), then unsuspend
   curl -sS -X DELETE https://platform.picopaas.xyz/apps/N -H "Authorization: Bearer $PICOPAAS_TOKEN"   # delete_namespace; confirm `kubectl get ns app-N` -> NotFound
   ```
   Note: counts toward the user's 5-app cap and the 384MB/100m caps; delete it afterwards. (The weekly `quota-e2e.yml` also deploys apps and is a second, independent check.)

## Rollback (if app deploys start failing with `forbidden` in the platform log)

* Fastest, no redeploy: `kubectl delete validatingadmissionpolicybinding picopaas-rolebinding-scope` (only if the VAP itself is the problem), and/or
  re-add the two moved rules to `picopaas-reconciler` with `kubectl edit clusterrole picopaas-reconciler` (or `git revert` the commit; `deploy.yml` re-applies the old file).
* The reconciler retries failed apps with backoff (1m, 5m, 15m, 60m), so once the grant is restored apps heal without user action.

## Not changed (flagged)

* `picopaas-system` keeps `picopaas-app-ns` through the existing RoleBinding, so the SA can still write Deployments/Ingresses/NetworkPolicies there. That is no wider than before (it was cluster-wide). Shrinking it to a secrets-only role would
  change that RoleBinding's `roleRef`, which is immutable (`kubectl apply` fails), so it needs delete + recreate: separate follow-up.
* `namespaces` create/patch/**delete** stay cluster-wide (SSA needs patch; teardown needs delete). kube-system/default/kube-public cannot be deleted
  (API server refuses: verified by server dry-run as the SA), but the SA could still delete cert-manager, traefik, picopaas-system or picopaas-data, or relabel any namespace
  (e.g. loosen Pod Security). A second ValidatingAdmissionPolicy (namespace writes by this SA must be named `app-*`, plus its own) would close it; not done here (out of scope; it touches the Namespace apply path).
* Binding to a Role in a tenant namespace is unrestricted by the VAP (it matches `app-*`); an attacker-chosen `app-*` namespace they create themselves gets nothing the SA doesn't already have.
