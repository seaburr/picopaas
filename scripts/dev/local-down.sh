#!/usr/bin/env bash
# Tear down what scripts/dev/local-up.sh created on the LOCAL cluster (generic target).
#   scripts/dev/local-down.sh              remove everything (tenant namespaces, platform, data tier, Traefik, metrics-server)
#   scripts/dev/local-down.sh --keep-data  keep namespace picopaas-data (DB/Garage volumes and secrets) for the next up
# Same guard as local-up.sh: KUBECONFIG must be set, context docker-desktop or kind-*, API server on localhost.
set -euo pipefail

if [ -z "${KUBECONFIG:-}" ]; then
  echo "refusing: KUBECONFIG is not set (this script never uses ~/.kube/config)" >&2
  exit 1
fi
CTX="$(kubectl config current-context)"
case "$CTX" in
  docker-desktop | kind-*) ;;
  *) echo "refusing: current context is '$CTX', expected docker-desktop or kind-*" >&2; exit 1 ;;
esac
SERVER="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
case "$SERVER" in
  https://127.0.0.1:* | https://localhost:* | https://kubernetes.docker.internal:* | https://\[::1\]:*) ;;
  *) echo "refusing: API server '$SERVER' is not local" >&2; exit 1 ;;
esac
K=(kubectl --context "$CTX")

echo "==> tenant namespaces (app-*) and the platform"
"${K[@]}" get ns -o name | grep '^namespace/app-' | xargs -r "${K[@]}" delete --wait=false 2>/dev/null || true
"${K[@]}" delete ns picopaas-system --ignore-not-found --wait=true
"${K[@]}" delete validatingadmissionpolicybinding,validatingadmissionpolicy picopaas-rolebinding-scope --ignore-not-found
"${K[@]}" delete clusterrolebinding picopaas-reconciler --ignore-not-found
"${K[@]}" delete clusterrole picopaas-reconciler picopaas-app-ns --ignore-not-found
"${K[@]}" delete priorityclass picopaas-platform --ignore-not-found

if [ "${1:-}" != "--keep-data" ]; then
  echo "==> data tier (MySQL, PostgreSQL, Garage; PVCs use reclaimPolicy Delete)"
  "${K[@]}" delete ns picopaas-data --ignore-not-found --wait=true
fi

echo "==> Traefik and metrics-server"
helm --kube-context "$CTX" uninstall traefik -n traefik 2>/dev/null || true
"${K[@]}" delete ns traefik --ignore-not-found --wait=false
helm --kube-context "$CTX" uninstall metrics-server -n kube-system 2>/dev/null || true
echo "done (images picopaas:local etc. stay in Docker; remove with: docker rmi picopaas:local)"
