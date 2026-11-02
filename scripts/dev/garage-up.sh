#!/usr/bin/env bash
# Single-node Garage (https://garagehq.deuxfleurs.fr/) for the ObjectStore contract tests and the generic target.
# Idempotent: re-running reuses the container, layout, platform key and env file.
#   scripts/dev/garage-up.sh [up]   start/reuse, print `export` lines (also written to $GARAGE_DEV_DIR/garage.env)
#   scripts/dev/garage-up.sh down   remove the container, volume and state dir
# Then:  source .garage-dev/garage.env && python -m pytest tests/test_object_store_contract.py
set -euo pipefail

GARAGE_IMAGE="${GARAGE_IMAGE:-dxflrs/garage:v2.5.0}"
NAME="${GARAGE_CONTAINER:-picopaas-garage}"
DIR="${GARAGE_DEV_DIR:-$(cd "$(dirname "$0")/../.." && pwd)/.garage-dev}"
S3_PORT="${GARAGE_S3_PORT:-3900}"
ADMIN_PORT="${GARAGE_ADMIN_PORT:-3903}"
G=(docker exec "$NAME" /garage)

if [ "${1:-up}" = "down" ]; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm -f "${NAME}-data" "${NAME}-meta" >/dev/null 2>&1 || true
  rm -rf "$DIR"
  echo "garage removed"
  exit 0
fi

mkdir -p "$DIR"
chmod 700 "$DIR"
rand() { openssl rand -hex "$(( ($1 + 1) / 2 ))" | cut -c1-"$1"; }

if [ ! -f "$DIR/garage.env" ]; then
  RPC="$(rand 64)"; ADMIN_TOKEN="$(rand 40)"
  KEY_ID="GK$(rand 24)"; KEY_SECRET="$(rand 64)"
  cat >"$DIR/garage.toml" <<TOML
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "sqlite"
replication_factor = 1
rpc_bind_addr = "0.0.0.0:3901"
rpc_public_addr = "127.0.0.1:3901"
rpc_secret = "$RPC"

[s3_api]
s3_region = "garage"
api_bind_addr = "0.0.0.0:3900"

[admin]
api_bind_addr = "0.0.0.0:3903"
admin_token = "$ADMIN_TOKEN"
TOML
  cat >"$DIR/garage.env" <<ENV
export GARAGE_TEST_ADMIN_URL=http://127.0.0.1:$ADMIN_PORT
export GARAGE_TEST_ADMIN_TOKEN=$ADMIN_TOKEN
export GARAGE_TEST_S3_ENDPOINT=http://127.0.0.1:$S3_PORT
export GARAGE_TEST_REGION=garage
export GARAGE_TEST_ACCESS_KEY_ID=$KEY_ID
export GARAGE_TEST_SECRET_ACCESS_KEY=$KEY_SECRET
ENV
  chmod 600 "$DIR/garage.toml" "$DIR/garage.env"
fi
# shellcheck disable=SC1091
. "$DIR/garage.env"

if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" \
    -p "127.0.0.1:$S3_PORT:3900" -p "127.0.0.1:$ADMIN_PORT:3903" \
    -v "$DIR/garage.toml:/etc/garage.toml:ro" \
    -v "${NAME}-meta:/var/lib/garage/meta" -v "${NAME}-data:/var/lib/garage/data" \
    "$GARAGE_IMAGE" >/dev/null
fi

for _ in $(seq 1 60); do
  "${G[@]}" status >/dev/null 2>&1 && break
  sleep 1
done
"${G[@]}" status >/dev/null

# Layout (one node, one zone) only when none is applied yet.
if "${G[@]}" status 2>/dev/null | grep -q "NO ROLE ASSIGNED"; then
  NODE="$("${G[@]}" node id -q 2>/dev/null | cut -d@ -f1)"
  "${G[@]}" layout assign -z dc1 -c 1G "$NODE" >/dev/null
  "${G[@]}" layout apply --version 1 >/dev/null
fi

# Platform key (the OBJECT_STORE_ACCESS_KEY_ID/SECRET pair); imported with the generated id so it's stable.
if ! "${G[@]}" key info "$GARAGE_TEST_ACCESS_KEY_ID" >/dev/null 2>&1; then
  "${G[@]}" key import --yes -n pp-platform "$GARAGE_TEST_ACCESS_KEY_ID" "$GARAGE_TEST_SECRET_ACCESS_KEY" >/dev/null
fi

for _ in $(seq 1 30); do  # wait until the admin API answers with the token
  curl -fsS -H "Authorization: Bearer $GARAGE_TEST_ADMIN_TOKEN" "$GARAGE_TEST_ADMIN_URL/v2/GetClusterHealth" >/dev/null 2>&1 && break
  sleep 1
done
echo "garage ready ($GARAGE_IMAGE); source $DIR/garage.env"
