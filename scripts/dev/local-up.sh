#!/usr/bin/env bash
# Bring PicoPaaS up on a LOCAL Kubernetes cluster as the "generic" target (CLOUD_PROVIDER=generic):
# metrics-server, Traefik, PostgreSQL 16 + MySQL 8.4 (TLS, self-signed CA), Garage, then the platform.
# See deploy/generic/README.md. Idempotent: re-running reuses secrets, CA and data.
#
#   scripts/dev/local-up.sh                 everything
#   scripts/dev/local-up.sh --deps-only     everything except the platform Deployment
#   scripts/dev/local-up.sh verify          only the dependency checks
# Env: KUBECONFIG (REQUIRED; never falls back to ~/.kube/config), TRAEFIK_HTTP_PORT (default 80), SKIP_BUILD=1
# (reuse the existing picopaas:local image), PICOPAAS_IMAGE (default picopaas:local).
#
# Safety: refuses unless the current context is docker-desktop or kind-* AND the API server is on localhost.
# No secret is written to disk: they are generated here and created straight in the cluster.
# shellcheck disable=SC2016  # single quotes are deliberate: variables expand inside the pod, not here
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
GEN="$ROOT/deploy/generic"
IMAGE="${PICOPAAS_IMAGE:-picopaas:local}"
HTTP_PORT="${TRAEFIK_HTTP_PORT:-80}"
TRAEFIK_CHART_VERSION="${TRAEFIK_CHART_VERSION:-42.0.0}"            # same pins as .github/workflows/bootstrap.yml
METRICS_SERVER_CHART_VERSION="${METRICS_SERVER_CHART_VERSION:-3.14.0}"
MODE="${1:-all}"

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
say() { printf '\n==> %s\n' "$*"; }

# --- secret helpers (values stay in the cluster; nothing is written to the repo) -------------------------------
rand() { openssl rand -hex "$(( ($1 + 1) / 2 ))" | cut -c1-"$1"; }
secret_exists() { "${K[@]}" -n "$1" get secret "$2" >/dev/null 2>&1; }
secret_get() { "${K[@]}" -n "$1" get secret "$2" -o "go-template={{index .data \"$3\"}}" | base64 -d; }
# ensure_secret NS NAME KEY=VALUE...   (create only when absent, so re-runs never rotate anything)
ensure_secret() {
  local ns="$1" name="$2"; shift 2
  secret_exists "$ns" "$name" && return 0
  local args=() kv
  for kv in "$@"; do args+=("--from-literal=$kv"); done
  "${K[@]}" -n "$ns" create secret generic "$name" "${args[@]}" >/dev/null
  echo "created secret $ns/$name"
}
kustomize_apply() { # kustomize_apply DIR [sed-expr]
  "${K[@]}" kustomize --load-restrictor=LoadRestrictionsNone "$1" | sed -e "${2:-}" | "${K[@]}" apply -f -
}

# --- verification ----------------------------------------------------------------------------------------------
verify() {
  say "verify: PostgreSQL (verify-full against the generated CA, non-TLS must be rejected)"
  "${K[@]}" -n picopaas-data exec postgres-0 -- sh -ec '
    export PGPASSWORD="$PICOPAAS_ADMIN_PASSWORD"
    psql "host=postgres.picopaas-data.svc user=picopaas_admin dbname=postgres sslmode=verify-full sslrootcert=/etc/pg-tls/ca.crt" \
      -Atc "select version(), (select ssl from pg_stat_ssl where pid = pg_backend_pid())"
    if psql "host=postgres.picopaas-data.svc user=picopaas_admin dbname=postgres sslmode=disable" -Atc "select 1" >/dev/null 2>&1; then
      echo "FAIL: PostgreSQL accepted a non-TLS connection"; exit 1; fi
    echo "non-TLS rejected: ok"'
  say "verify: MySQL (VERIFY_IDENTITY against the generated CA)"
  "${K[@]}" -n picopaas-data exec mysql-0 -- sh -ec '
    mysql -h mysql.picopaas-data.svc -upicopaas_admin -p"$MYSQL_ADMIN_PASSWORD" --ssl-mode=VERIFY_IDENTITY \
      --ssl-ca=/etc/mysql-tls/ca.crt -Nse "select version(); show status like \"Ssl_cipher\"" 2>/dev/null'
  say "verify: Garage status"
  "${K[@]}" -n picopaas-data exec garage-0 -- /garage -c /etc/garage/garage.toml status
  if "${K[@]}" -n picopaas-data exec garage-0 -- /garage -c /etc/garage/garage.toml status | grep -q "NO ROLE ASSIGNED"; then
    echo "FAIL: Garage layout not applied" >&2; exit 1
  fi
  say "verify: metrics-server + Traefik"
  "${K[@]}" top node 2>&1 | head -3 || true
  "${K[@]}" -n traefik get deploy traefik
}
if [ "$MODE" = "verify" ]; then verify; exit 0; fi

# --- 1. Helm: metrics-server, Traefik ------------------------------------------------------------------------
say "Helm repos"
helm repo add traefik https://traefik.github.io/charts >/dev/null 2>&1 || true
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ >/dev/null 2>&1 || true
helm repo update traefik metrics-server >/dev/null
HELM=(helm --kube-context "$CTX")

say "metrics-server $METRICS_SERVER_CHART_VERSION (--kubelet-insecure-tls)"
"${HELM[@]}" upgrade --install metrics-server metrics-server/metrics-server -n kube-system \
  --version "$METRICS_SERVER_CHART_VERSION" -f "$GEN/metrics-server-values.yaml" --wait --timeout 3m >/dev/null

say "Traefik $TRAEFIK_CHART_VERSION (LoadBalancer on localhost:$HTTP_PORT)"
"${HELM[@]}" upgrade --install traefik traefik/traefik -n traefik --create-namespace \
  --version "$TRAEFIK_CHART_VERSION" -f "$GEN/traefik-values.yaml" \
  --set ports.web.exposedPort="$HTTP_PORT" --wait --timeout 3m >/dev/null

# --- 2. Data tier: namespaces, secrets, CA ---------------------------------------------------------------------
say "Namespaces and secrets"
"${K[@]}" apply -f "$GEN/data/namespace.yaml" -f "$ROOT/deploy/platform/namespace.yaml" >/dev/null
ensure_secret picopaas-data mysql-root \
  MYSQL_ROOT_PASSWORD="$(rand 32)" MYSQL_ADMIN_PASSWORD="$(rand 48)" MYSQL_BACKUP_PASSWORD="$(rand 32)"
ensure_secret picopaas-data pg-admin POSTGRES_PASSWORD="$(rand 32)" PICOPAAS_ADMIN_PASSWORD="$(rand 48)"
ensure_secret picopaas-data garage-secrets \
  GARAGE_RPC_SECRET="$(rand 64)" GARAGE_ADMIN_TOKEN="$(rand 40)" \
  PLATFORM_KEY_ID="GK$(rand 24)" PLATFORM_KEY_SECRET="$(rand 64)"
# Backup key/bucket were added after the first release of this script: add them to an existing secret too.
if ! secret_get picopaas-data garage-secrets BACKUP_KEY_ID >/dev/null 2>&1; then
  "${K[@]}" -n picopaas-data patch secret garage-secrets --type merge -p \
    "{\"stringData\":{\"BACKUP_KEY_ID\":\"GK$(rand 24)\",\"BACKUP_KEY_SECRET\":\"$(rand 64)\",\"BACKUP_BUCKET\":\"picopaas-backups\"}}" >/dev/null
fi
ensure_secret picopaas-system picopaas-keys \
  FERNET_KEY="$(openssl rand -base64 32 | tr '+/' '-_')" PLATFORM_DB_PASSWORD="$(rand 40)"

if ! secret_exists picopaas-data db-tls; then
  say "Generating self-signed CA + server certificate (SANs: mysql, postgres services)"
  T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=picopaas-local-ca" \
    -keyout "$T/ca.key" -out "$T/ca.crt" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=picopaas-local-db" -keyout "$T/tls.key" -out "$T/tls.csr" 2>/dev/null
  cat >"$T/ext" <<EXT
subjectAltName=DNS:mysql,DNS:mysql.picopaas-data,DNS:mysql.picopaas-data.svc,DNS:mysql.picopaas-data.svc.cluster.local,DNS:postgres,DNS:postgres.picopaas-data,DNS:postgres.picopaas-data.svc,DNS:postgres.picopaas-data.svc.cluster.local,DNS:localhost
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid
EXT
  openssl x509 -req -in "$T/tls.csr" -CA "$T/ca.crt" -CAkey "$T/ca.key" -CAcreateserial -days 825 \
    -extfile "$T/ext" -out "$T/tls.crt" 2>/dev/null
  "${K[@]}" -n picopaas-data create secret generic db-tls \
    --from-file=ca.crt="$T/ca.crt" --from-file=tls.crt="$T/tls.crt" --from-file=tls.key="$T/tls.key" >/dev/null
  echo "created secret picopaas-data/db-tls (CA key discarded)"
  rm -rf "$T"; trap - EXIT
fi

# --- 3. Data tier workloads --------------------------------------------------------------------------------------
say "Applying MySQL, PostgreSQL, Garage"
kustomize_apply "$GEN/data" ""
for s in postgres mysql garage; do "${K[@]}" -n picopaas-data rollout status "statefulset/$s" --timeout 6m; done

say "Garage layout + platform key (Job garage-setup)"
"${K[@]}" -n picopaas-data delete job garage-setup --ignore-not-found >/dev/null
"${K[@]}" apply -f "$GEN/data/garage-setup-job.yaml" >/dev/null
"${K[@]}" -n picopaas-data wait --for=condition=complete job/garage-setup --timeout 3m
"${K[@]}" -n picopaas-data logs job/garage-setup | tail -4

say "PostgreSQL: picopaas_admin must be able to SET ROLE to roles it creates (PG16 createrole_self_grant; initdb only runs on first init)"
"${K[@]}" -n picopaas-data exec postgres-0 -- psql -U postgres -d postgres -qAtc \
  "ALTER ROLE picopaas_admin SET createrole_self_grant = 'set, inherit'"

say "Platform database in MySQL (picopaas / user picopaas, TLS required)"
PDB_PW="$(secret_get picopaas-system picopaas-keys PLATFORM_DB_PASSWORD)"
"${K[@]}" -n picopaas-data exec -i mysql-0 -- sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" 2>/dev/null' <<SQL
CREATE DATABASE IF NOT EXISTS picopaas CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'picopaas'@'%' IDENTIFIED BY '$PDB_PW' REQUIRE SSL;
ALTER USER 'picopaas'@'%' IDENTIFIED BY '$PDB_PW' REQUIRE SSL;
GRANT ALL ON picopaas.* TO 'picopaas'@'%';
SQL

verify
if [ "$MODE" = "--deps-only" ]; then say "deps up (platform skipped)"; exit 0; fi

# --- 4. Platform ------------------------------------------------------------------------------------------------------
if [ "${SKIP_BUILD:-0}" != "1" ]; then
  say "Building $IMAGE for $(docker info --format '{{.Architecture}}') (Docker Desktop shares its daemon with the cluster)"
  docker build -q -t "$IMAGE-base" "$ROOT" >/dev/null
  if [ "${ARM64_CRYPTO_FIX:-0}" = "1" ]; then
    # Opt-in: the cryptography 50.x aarch64 wheel gave "Illegal instruction" on the OLD Docker Desktop VM (engine 27.5.1,
    # docker-desktop k8s); not reproduced on engine 29.9.2 / kind. See deploy/generic/Dockerfile.arm64-fix.
    docker build -q -f "$GEN/Dockerfile.arm64-fix" --build-arg BASE="$IMAGE-base" -t "$IMAGE" "$GEN" >/dev/null
  else
    docker tag "$IMAGE-base" "$IMAGE"
  fi
fi
# The published backup image is amd64-only (restic/s5cmd amd64 binaries) and the node's containerd will not pull it on arm64
# ("no match for platform in manifest"). Build an arm64 copy from a temp copy of images/backup with arm64 binaries
# (checksums fetched from the pinned releases); the repo Dockerfile is not touched.
BACKUP_IMAGE_REF=""
if [ "$(docker info --format '{{.Architecture}}')" = "aarch64" ]; then
  BACKUP_IMAGE_REF="picopaas-backup:local-arm64"
  if [ "${SKIP_BUILD:-0}" != "1" ] || ! docker image inspect "$BACKUP_IMAGE_REF" >/dev/null 2>&1; then
    say "Building the backup image for arm64 ($BACKUP_IMAGE_REF)"
    BT="$(mktemp -d)"; cp -R "$ROOT"/images/backup/. "$BT"/
    rv="$(sed -n 's/^ARG RESTIC_VERSION=//p' "$BT/Dockerfile")"; sv="$(sed -n 's/^ARG S5CMD_VERSION=//p' "$BT/Dockerfile")"
    ro="$(sed -n 's/^ARG RESTIC_SHA256=//p' "$BT/Dockerfile")"; so="$(sed -n 's/^ARG S5CMD_SHA256=//p' "$BT/Dockerfile")"
    rn="$(curl -fsSL "https://github.com/restic/restic/releases/download/v$rv/SHA256SUMS" | grep "restic_${rv}_linux_arm64.bz2" | cut -d' ' -f1)"
    sn="$(curl -fsSL "https://github.com/peak/s5cmd/releases/download/v$sv/s5cmd_checksums.txt" | grep "Linux-arm64.tar.gz" | cut -d' ' -f1)"
    sed -i.bak -e "s/linux_amd64/linux_arm64/; s/Linux-64bit/Linux-arm64/; s/$ro/$rn/; s/$so/$sn/" "$BT/Dockerfile"
    docker build -q -t "$BACKUP_IMAGE_REF" "$BT" >/dev/null; rm -rf "$BT"
  fi
fi
IMAGE_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
# A rebuilt image under the same tag is NOT picked up by the node (imagePullPolicy IfNotPresent keeps the old one), so the
# Deployment gets a tag unique to this build.
RUN_IMAGE="${IMAGE%%:*}:build-$(printf '%s' "$IMAGE_ID" | sed 's/^sha256://' | cut -c1-12)"
docker tag "$IMAGE" "$RUN_IMAGE"

say "picopaas-db-ca and picopaas-env"
secret_get picopaas-data db-tls ca.crt >"${TMPDIR:-/tmp}/pp-ca.$$"
"${K[@]}" -n picopaas-system create secret generic picopaas-db-ca --from-file=ca.crt="${TMPDIR:-/tmp}/pp-ca.$$" \
  --from-file=pg-ca.crt="${TMPDIR:-/tmp}/pp-ca.$$" --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
rm -f "${TMPDIR:-/tmp}/pp-ca.$$"
PG_IP="$("${K[@]}" -n picopaas-data get svc postgres -o jsonpath='{.spec.clusterIP}')"
MY_IP="$("${K[@]}" -n picopaas-data get svc mysql -o jsonpath='{.spec.clusterIP}')"
ENVF="$(mktemp)"; trap 'rm -f "$ENVF"' EXIT; chmod 600 "$ENVF"
{
  echo "ENVIRONMENT=production"
  echo "FERNET_KEY=$(secret_get picopaas-system picopaas-keys FERNET_KEY)"
  echo "DATABASE_URL=mysql+pymysql://picopaas:$PDB_PW@mysql.picopaas-data.svc:3306/picopaas?ssl_ca=/etc/picopaas/db-ca/ca.crt"
  echo "CLOUD_PROVIDER=generic"
  echo "RECONCILER_ENABLED=1"
  echo "APP_DOMAIN=localhost"
  echo "PLATFORM_HOST=platform.localhost"
  echo "APEX_HOST=localhost"
  echo "CUSTOM_DOMAINS_ENABLED=0"
  echo "KAISEN_ENABLED=0"
  echo "BACKUPS_ENABLED=1"
  echo "BACKUP_KEY_SCOPED=1"
  echo "PLATFORM_BACKUP_ENABLED=0"
  echo "BACKUP_BUCKET=$(secret_get picopaas-data garage-secrets BACKUP_BUCKET)"
  echo "BACKUP_REGION=garage"
  [ -z "$BACKUP_IMAGE_REF" ] || echo "BACKUP_IMAGE=$BACKUP_IMAGE_REF"
  echo "BACKUP_ACCESS_KEY_ID=$(secret_get picopaas-data garage-secrets BACKUP_KEY_ID)"
  echo "BACKUP_SECRET_ACCESS_KEY=$(secret_get picopaas-data garage-secrets BACKUP_KEY_SECRET)"
  echo "VOLUMES_ENABLED=1"
  echo "VOLUME_STORAGE_CLASS=hostpath"
  echo "MAX_NODES=1"
  echo "MYSQL_EGRESS_MODE=in-cluster"
  echo "MYSQL_EGRESS_NAMESPACE=picopaas-data"
  echo "MYSQL_EGRESS_POD_LABELS=app=mysql"
  echo "PG_EGRESS_MODE=in-cluster"
  echo "PG_EGRESS_NAMESPACE=picopaas-data"
  echo "PG_EGRESS_POD_LABELS=app=postgres"
  echo "OBJECT_STORE_EGRESS_MODE=in-cluster"
  echo "OBJECT_STORE_EGRESS_NAMESPACE=picopaas-data"
  echo "OBJECT_STORE_EGRESS_POD_LABELS=app=garage"
  echo "BOOTSTRAP_DB_FLAVOR=self-managed"
  echo "BOOTSTRAP_PG_FLAVOR=self-managed"
  echo "MYSQL_ADMIN_USE_PROCEDURES=1"
  echo "MYSQL_SSL_CA=/etc/picopaas/db-ca/ca.crt"
  echo "BOOTSTRAP_DB_HOST=mysql.picopaas-data.svc"
  echo "BOOTSTRAP_DB_PORT=3306"
  echo "BOOTSTRAP_DB_USER=picopaas_admin"
  echo "BOOTSTRAP_DB_PASSWORD=$(secret_get picopaas-data mysql-root MYSQL_ADMIN_PASSWORD)"
  echo "BOOTSTRAP_PG_HOST=postgres.picopaas-data.svc"
  echo "BOOTSTRAP_PG_PORT=5432"
  echo "BOOTSTRAP_PG_USER=picopaas_admin"
  echo "BOOTSTRAP_PG_PASSWORD=$(secret_get picopaas-data pg-admin PICOPAAS_ADMIN_PASSWORD)"
  echo "BOOTSTRAP_PG_CA=/etc/picopaas/db-ca/pg-ca.crt"
  # Object store (Garage). Names follow docs/PORTABILITY_PLAN.md (OBJECT_STORE_*); GARAGE_* are for the admin API.
  echo "OBJECT_STORE_ENDPOINT=http://garage.picopaas-data.svc:3900"
  echo "OBJECT_STORE_REGION=garage"
  echo "BACKUP_ENDPOINT=http://garage.picopaas-data.svc:3900"
  echo "OBJECT_STORE_ACCESS_KEY_ID=$(secret_get picopaas-data garage-secrets PLATFORM_KEY_ID)"
  echo "OBJECT_STORE_SECRET_ACCESS_KEY=$(secret_get picopaas-data garage-secrets PLATFORM_KEY_SECRET)"
  echo "GARAGE_ADMIN_URL=http://garage.picopaas-data.svc:3903"
  echo "GARAGE_ADMIN_TOKEN=$(secret_get picopaas-data garage-secrets GARAGE_ADMIN_TOKEN)"
} >"$ENVF"
"${K[@]}" -n picopaas-system create secret generic picopaas-env --from-env-file="$ENVF" --dry-run=client -o yaml \
  | "${K[@]}" apply -f - >/dev/null
SUM="$({ cat "$ENVF"; echo "$IMAGE_ID"; } | shasum -a 256 | cut -c1-16)"
rm -f "$ENVF"; trap - EXIT

say "Backup namespace, RBAC and egress policy (deploy/platform/backup.yaml, DB CIDRs = the in-cluster Service IPs)"
sed -e "s#__DB_EGRESS_CIDR__#$MY_IP/32#" -e "s#__PG_EGRESS_CIDR__#$PG_IP/32#" "$ROOT/deploy/platform/backup.yaml" | "${K[@]}" apply -f - >/dev/null
"${K[@]}" apply -f "$GEN/platform/backup-db-egress.yaml" >/dev/null  # selector peers instead of ClusterIP /32 (see file)

say "Applying the platform ($RUN_IMAGE, rollout key $SUM)"
kustomize_apply "$GEN/platform" "s|picopaas.io/env-checksum: .*|picopaas.io/env-checksum: \"$SUM\"|;s|image: picopaas:local|image: $RUN_IMAGE|"
"${K[@]}" -n picopaas-system rollout status deploy/picopaas --timeout 3m || {
  echo "platform not ready; logs:" >&2; "${K[@]}" -n picopaas-system logs deploy/picopaas --tail=30 >&2 || true; exit 1; }

say "healthz via Traefik"
URL="http://localhost:$HTTP_PORT/healthz"
for _ in $(seq 1 30); do
  curl -fsS -H "Host: platform.localhost" "$URL" && { echo; echo "UP: http://platform.localhost:$HTTP_PORT/"; exit 0; }
  sleep 2
done
echo "healthz did not answer via Traefik" >&2; exit 1
