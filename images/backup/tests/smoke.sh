#!/usr/bin/env bash
# Smoke test of a built backup image against REAL MySQL 8.4 and PostgreSQL 17 (TLS on, as on the managed servers):
# db backup, then restore of that snapshot into the same database. The pytest script tests stub mariadb-dump, pg_dump and
# restic, so a flag the packaged client rejects (2026-10-24: mariadb-dump --connect-timeout) only shows up here.
# Usage: images/backup/tests/smoke.sh <image>   (run by .github/workflows/backup-image.yml before the push; needs docker)
set -euo pipefail
IMAGE="${1:?usage: smoke.sh <image>}"
NET="bk-smoke-$$"
PW=not-the-password
WORK="$(mktemp -d)"
cleanup() { docker rm -f "$NET-mysql" "$NET-pg" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

docker network create "$NET" >/dev/null
docker run -d --name "$NET-mysql" --network "$NET" -e MYSQL_ROOT_PASSWORD=$PW -e MYSQL_DATABASE=app_1 \
  -e MYSQL_USER=app_1 -e MYSQL_PASSWORD=$PW mysql:8.4 >/dev/null
docker run -d --name "$NET-pg" --network "$NET" -e POSTGRES_PASSWORD=$PW -e POSTGRES_USER=app_1 -e POSTGRES_DB=app_1 \
  postgres:17 -c ssl=on -c ssl_cert_file=/etc/ssl/certs/ssl-cert-snakeoil.pem \
  -c ssl_key_file=/etc/ssl/private/ssl-cert-snakeoil.key >/dev/null

for _ in $(seq 1 60); do
  docker exec "$NET-mysql" mysql -uapp_1 -p$PW -h 127.0.0.1 -e "SELECT 1" app_1 >/dev/null 2>&1 \
    && docker exec "$NET-pg" pg_isready -h 127.0.0.1 -U app_1 >/dev/null 2>&1 && break
  sleep 2
done
docker exec "$NET-mysql" mysql -uapp_1 -p$PW -h 127.0.0.1 app_1 \
  -e "CREATE TABLE posts (id int primary key, t text); INSERT INTO posts VALUES (1,'hello');" 2>/dev/null
docker exec -e PGPASSWORD=$PW "$NET-pg" psql -h 127.0.0.1 -U app_1 -d app_1 -q \
  -c "CREATE TABLE posts (id int primary key, t text); INSERT INTO posts VALUES (1,'hello');"

run() { # engine host port MODE extra-env...
  local engine=$1 host=$2 port=$3; shift 3
  docker run --rm --network "$NET" --read-only --user 10001 --cap-drop ALL --tmpfs /tmp:uid=10001 \
    -v "$WORK/$engine:/work" -e ENGINE="$engine" -e DB_HOST="$host" -e DB_PORT="$port" -e DB_NAME=app_1 \
    -e DB_USER=app_1 -e DB_PASSWORD=$PW -e RESTIC_REPOSITORY=/work/repo -e RESTIC_PASSWORD=$PW "$@" "$IMAGE"
}

for spec in "mysql $NET-mysql 3306" "pg $NET-pg 5432"; do
  read -r engine host port <<<"$spec"
  mkdir -p "$WORK/$engine" && chmod 0777 "$WORK/$engine"
  out="$(run "$engine" "$host" "$port" -e MODE=backup -e KIND=db)" || true; echo "$out"
  last="$(tail -n1 <<<"$out")"
  [[ "$last" == 'PICOPAAS_RESULT {"snapshot_id"'* ]] || { echo "SMOKE FAIL: $engine backup"; exit 1; }
  snap="$(sed -E 's/.*"snapshot_id":"([0-9a-f]+)".*/\1/' <<<"$last")"
  out="$(run "$engine" "$host" "$port" -e MODE=restore -e KIND=db -e SNAPSHOT="$snap")" || true; echo "$out"
  [[ "$(tail -n1 <<<"$out")" == *'"restored":true'* ]] || { echo "SMOKE FAIL: $engine restore"; exit 1; }
done
echo "SMOKE OK"
