#!/usr/bin/env bash
# PostgreSQL for the live PostgresAdmin tests (tests/test_postgres_admin.py), shaped like a managed server:
# TLS only (hostssl, private CA) and a non-superuser admin with CREATEROLE + CREATEDB that owns the `postgres` database.
# Idempotent: re-running reuses the container, certificates and env file.
#   scripts/dev/postgres-up.sh [up]   start/reuse (env written to $POSTGRES_DEV_DIR/postgres.env)
#   scripts/dev/postgres-up.sh down   remove the container and state dir
# Then:  source .postgres-dev/postgres.env && python -m pytest tests/ -m postgres
set -euo pipefail

POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:17}"
NAME="${POSTGRES_CONTAINER:-picopaas-postgres}"
DIR="${POSTGRES_DEV_DIR:-$(cd "$(dirname "$0")/../.." && pwd)/.postgres-dev}"
PORT="${POSTGRES_DEV_PORT:-15432}"

if [ "${1:-up}" = "down" ]; then
  docker rm -f -v "$NAME" >/dev/null 2>&1 || true
  rm -rf "$DIR"
  echo "postgres removed"
  exit 0
fi

mkdir -p "$DIR"
chmod 700 "$DIR"

if [ ! -f "$DIR/postgres.env" ]; then
  ROOT_PW="$(openssl rand -hex 20)"; ADMIN_PW="$(openssl rand -hex 20)"
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=picopaas-dev-ca" \
    -keyout "$DIR/ca.key" -out "$DIR/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=picopaas-dev-postgres" \
    -keyout "$DIR/tls.key" -out "$DIR/tls.csr" 2>/dev/null
  openssl x509 -req -days 30 -in "$DIR/tls.csr" -CA "$DIR/ca.crt" -CAkey "$DIR/ca.key" -CAcreateserial \
    -out "$DIR/tls.crt" 2>/dev/null
  cat >"$DIR/pg_hba.conf" <<'HBA'
local all all trust
hostssl all all all scram-sha-256
HBA
  cat >"$DIR/10-admin.sh" <<'INIT'
#!/bin/bash
set -eu
psql -v ON_ERROR_STOP=1 -U postgres -d postgres <<SQL
CREATE ROLE picopaas_admin LOGIN CREATEROLE CREATEDB PASSWORD '${PICOPAAS_ADMIN_PASSWORD}';
ALTER DATABASE postgres OWNER TO picopaas_admin;
SQL
INIT
  cat >"$DIR/postgres.env" <<ENV
export POSTGRES_ADMIN_URL=postgresql://picopaas_admin:$ADMIN_PW@127.0.0.1:$PORT
export POSTGRES_TEST_ROOT_URL=postgresql://postgres:$ROOT_PW@127.0.0.1:$PORT
export POSTGRES_TEST_CA_FILE=$DIR/ca.crt
ENV
  chmod 600 "$DIR/postgres.env" "$DIR/ca.key" "$DIR/tls.key"
  # The container's postgres user reads these through the bind mount before the entrypoint copies them.
  chmod 644 "$DIR/tls.crt" "$DIR/ca.crt" "$DIR/pg_hba.conf"
  chmod 755 "$DIR/10-admin.sh"
fi
# shellcheck disable=SC1091
. "$DIR/postgres.env"

if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  docker rm -f -v "$NAME" >/dev/null 2>&1 || true
  ROOT_PW="${POSTGRES_TEST_ROOT_URL#postgresql://postgres:}"; ROOT_PW="${ROOT_PW%%@*}"
  ADMIN_PW="${POSTGRES_ADMIN_URL#postgresql://picopaas_admin:}"; ADMIN_PW="${ADMIN_PW%%@*}"
  # The server key must be owned by postgres with mode 0600, so it's copied inside the container at start.
  docker run -d --name "$NAME" -p "127.0.0.1:$PORT:5432" \
    -e POSTGRES_PASSWORD="$ROOT_PW" -e PICOPAAS_ADMIN_PASSWORD="$ADMIN_PW" \
    -v "$DIR/tls.crt:/mnt/tls/tls.crt:ro" -v "$DIR/tls.key:/mnt/tls/tls.key:ro" \
    -v "$DIR/pg_hba.conf:/mnt/tls/pg_hba.conf:ro" -v "$DIR/10-admin.sh:/docker-entrypoint-initdb.d/10-admin.sh:ro" \
    --entrypoint bash "$POSTGRES_IMAGE" -c '
      mkdir -p /run/pg-tls
      cp /mnt/tls/tls.crt /mnt/tls/tls.key /mnt/tls/pg_hba.conf /run/pg-tls/
      chown -R postgres:postgres /run/pg-tls
      chmod 600 /run/pg-tls/tls.key
      exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/run/pg-tls/tls.crt \
        -c ssl_key_file=/run/pg-tls/tls.key -c hba_file=/run/pg-tls/pg_hba.conf' >/dev/null
fi

# TCP readiness: during first-boot init the temporary server listens on the unix socket only.
for _ in $(seq 1 60); do
  docker exec "$NAME" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$NAME" pg_isready -h 127.0.0.1 -U postgres >/dev/null
echo "postgres ready ($POSTGRES_IMAGE); source $DIR/postgres.env"
