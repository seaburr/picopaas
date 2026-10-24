#!/usr/bin/env bash
# Smoke test of a built backup image against REAL MySQL 8.4 and PostgreSQL 17 (TLS on, as on the managed servers):
# db backup, restore of that snapshot into the same database, then forget of that snapshot (twice). Then a volume backup of a
# read-only mounted directory, its prune (retention) in a second Job, a forget, and a whole-backup PACKAGE (db + blob storage +
# volume parts -> one .tar.gz with MANIFEST.json) whose contents are compared with the originals. The pytest script tests stub
# mariadb-dump, pg_dump and restic, so a flag the packaged client rejects (2026-10-24: mariadb-dump --connect-timeout) only shows up here.
# Usage: images/backup/tests/smoke.sh <image>   (run by .github/workflows/backup-image.yml before the push; needs docker)
set -euo pipefail
IMAGE="${1:?usage: smoke.sh <image>}"
NET="bk-smoke-$$"
PW=not-the-password
WORK="$(mktemp -d)"
# The repos under $WORK are owned by the image's UID 10001, so they are removed from inside a container.
cleanup() {
  docker rm -f "$NET-mysql" "$NET-pg" >/dev/null 2>&1 || true
  docker volume rm -f "$NET-vol" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker run --rm --user 0 --entrypoint sh -v "$WORK:/w" "$IMAGE" -c 'rm -rf /w/* /w/.[!.]*' >/dev/null 2>&1 || true
  rm -rf "$WORK" 2>/dev/null || true
}
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
  # owner delete of one backup: restic forget <snapshot> --prune, then again (already gone, e.g. pruned by retention = success)
  out="$(run "$engine" "$host" "$port" -e MODE=forget -e KIND=db -e SNAPSHOT="$snap")" || true; echo "$out"
  [[ "$(tail -n1 <<<"$out")" == 'PICOPAAS_RESULT {"forgotten":true'* ]] || { echo "SMOKE FAIL: $engine forget"; exit 1; }
  [[ "$out" == *"deleting the snapshot"* ]] || { echo "SMOKE FAIL: $engine forget did not delete"; exit 1; }
  out="$(run "$engine" "$host" "$port" -e MODE=forget -e KIND=db -e SNAPSHOT="$snap")" || true; echo "$out"
  [[ "$(tail -n1 <<<"$out")" == 'PICOPAAS_RESULT {"forgotten":true'* && "$out" == *"already gone"* ]] \
    || { echo "SMOKE FAIL: $engine forget of a missing snapshot"; exit 1; }
done

# --- volume backup, prune, package ------------------------------------------------------------
# A named volume stands in for the PVC: files owned by the app's UID, mounted read-only into the Job like the real thing.
docker volume create "$NET-vol" >/dev/null
docker run --rm --user 0 --entrypoint sh -v "$NET-vol:/volume" "$IMAGE" -c '
  mkdir -p /volume/wp-content/uploads /volume/empty && echo "hello volume" > /volume/index.php
  echo "image bytes" > "/volume/wp-content/uploads/a picture.png" && ln -s index.php /volume/link
  head -c 300000 /dev/urandom > /volume/wp-content/big.bin && chown -R 10001:10001 /volume'
mkdir -p "$WORK/vol" && chmod 0777 "$WORK/vol"
vrun() { # extra docker args are passed through; env: MODE KIND ...
  docker run --rm --read-only --user 10001 --cap-drop ALL --tmpfs /tmp:uid=10001 -v "$NET-vol:/volume:ro" -v "$WORK/vol:/work" \
    -e RESTIC_REPOSITORY=/work/repo -e RESTIC_PASSWORD=$PW -e VOLUME_DIR=/volume "$@" "$IMAGE"
}
snaps() { docker run --rm --user 10001 --entrypoint restic -v "$WORK/vol:/work" -e RESTIC_REPOSITORY=/work/repo -e RESTIC_PASSWORD=$PW "$IMAGE" snapshots --json | docker run --rm -i --entrypoint jq "$IMAGE" length; }
out="$(vrun -e MODE=backup -e KIND=volume -e TAG=manual)" || true; echo "$out"
last="$(tail -n1 <<<"$out")"
[[ "$last" == 'PICOPAAS_RESULT {"snapshot_id"'* && "$last" != *repo_bytes* ]] || { echo "SMOKE FAIL: volume backup"; exit 1; }
vsnap="$(sed -E 's/.*"snapshot_id":"([0-9a-f]+)".*/\1/' <<<"$last")"
docker run --rm --user 0 --entrypoint sh -v "$NET-vol:/volume" "$IMAGE" -c 'echo "second" > /volume/second.txt && chown 10001:10001 /volume/second.txt'
out="$(vrun -e MODE=backup -e KIND=volume -e TAG=manual)" || true; echo "$out"
vsnap2="$(sed -E 's/.*"snapshot_id":"([0-9a-f]+)".*/\1/' <<<"$(tail -n1 <<<"$out")")"
[ "$(snaps)" = "2" ] || { echo "SMOKE FAIL: expected 2 volume snapshots"; exit 1; }
# retention in its own (platform namespace) Job: one daily kept, grouped by host + path
out="$(vrun -e MODE=prune -e KIND=volume -e KEEP_DAILY=1 -e KEEP_WEEKLY=1 -e KEEP_MONTHLY=1)" || true; echo "$out"
[[ "$(tail -n1 <<<"$out")" == 'PICOPAAS_RESULT {"pruned":true,"repo_bytes":'* ]] || { echo "SMOKE FAIL: volume prune"; exit 1; }
[ "$(snaps)" = "1" ] || { echo "SMOKE FAIL: prune should leave 1 volume snapshot"; exit 1; }
# a volume that cannot be read (mode 000 file) must not become a trusted snapshot
docker run --rm --user 0 --entrypoint sh -v "$NET-vol:/volume" "$IMAGE" -c 'echo secret > /volume/locked && chown 10001:10001 /volume/locked && chmod 000 /volume/locked'
out="$(vrun -e MODE=backup -e KIND=volume -e TAG=manual)" && { echo "SMOKE FAIL: unreadable file should fail"; exit 1; }; echo "$out"
[[ "$(tail -n1 <<<"$out")" == 'PICOPAAS_ERROR some files on the volume could not be read' ]] || { echo "SMOKE FAIL: unreadable file message"; exit 1; }
[ "$(snaps)" = "1" ] || { echo "SMOKE FAIL: an incomplete volume snapshot was kept"; exit 1; }
docker run --rm --user 0 --entrypoint sh -v "$NET-vol:/volume" "$IMAGE" -c 'rm -f /volume/locked'

# Package: three repos under one base (db from MySQL, a blob-storage tree, the volume), then ONE tar.gz.
P="$WORK/pkg"; mkdir -p "$P/out" && chmod -R 0777 "$P"
prun() { docker run --rm --read-only --user 10001 --cap-drop ALL --tmpfs /tmp:uid=10001 -v "$P:/work" -v "$NET-vol:/volume:ro" \
    --network "$NET" -e RESTIC_PASSWORD=$PW -e VOLUME_DIR=/volume "$@" "$IMAGE"; }
last="$(prun -e RESTIC_REPOSITORY=/work/repos/db -e MODE=backup -e KIND=db -e ENGINE=mysql -e DB_HOST="$NET-mysql" -e DB_PORT=3306 \
  -e DB_NAME=app_1 -e DB_USER=app_1 -e DB_PASSWORD=$PW | tail -n1)"
dsnap="$(sed -E 's/.*"snapshot_id":"([0-9a-f]+)".*/\1/' <<<"$last")"
last="$(prun -e RESTIC_REPOSITORY=/work/repos/volume -e MODE=backup -e KIND=volume | tail -n1)"
vsnap="$(sed -E 's/.*"snapshot_id":"([0-9a-f]+)".*/\1/' <<<"$last")"
# the blob-storage repo: what the bucket Job leaves behind (restic of /work/bucket); the copy from S3 is not exercised here.
# The tree lives on a tmpfs: restic reads with O_NOATIME, which Docker Desktop's bind mounts answer with EIO.
mkdir -p "$P/repos/bucket" && chmod 0777 "$P/repos/bucket"
bsnap="$(docker run --rm --user 10001 --entrypoint sh --tmpfs /work:uid=10001 --tmpfs /tmp:uid=10001 -v "$P/repos/bucket:/repo" \
  -e RESTIC_REPOSITORY=/repo -e RESTIC_PASSWORD=$PW "$IMAGE" -c '
  restic init -q && mkdir -p /work/bucket/img && echo "object one" > /work/bucket/a.txt && echo "object two" > /work/bucket/img/b.txt
  restic backup /work/bucket --host app -q --json | jq -r "select(.message_type == \"summary\") | .snapshot_id" | cut -c1-8')"
out="$(prun -e MODE=package -e REPO_BASE=/work/repos -e PARTS="db=$dsnap,bucket=$bsnap,volume=$vsnap" -e APP_NAME='my "app"' -e SET_ID=7 \
  -e SET_CREATED_AT=2026-10-24T12:00:00Z -e PICOPAAS_VERSION=smoke -e MAX_VOLUME_BYTES=1073741824 \
  -e EXPORT_ENDPOINT=http://unused -e EXPORT_BUCKET=unused -e EXPORT_KEY=exports/1/1.tar.gz -e EXPORT_TO_FILE=/work/out/package.tar.gz)" || true; echo "$out"
[[ "$(tail -n1 <<<"$out")" == 'PICOPAAS_RESULT {"exported":true,"bytes":'* ]] || { echo "SMOKE FAIL: package"; exit 1; }
# a volume bigger than a download may hold is refused, not half written
out="$(prun -e MODE=package -e REPO_BASE=/work/repos -e PARTS="volume=$vsnap" -e SET_ID=7 -e MAX_VOLUME_BYTES=1000 \
  -e EXPORT_ENDPOINT=http://unused -e EXPORT_BUCKET=unused -e EXPORT_KEY=exports/1/2.tar.gz -e EXPORT_TO_FILE=/work/out/too-big.tar.gz)" && { echo "SMOKE FAIL: oversize volume should fail"; exit 1; }
[[ "$(tail -n1 <<<"$out")" == 'PICOPAAS_ERROR the volume backup is too big to download as one file' ]] || { echo "SMOKE FAIL: oversize message"; exit 1; }
# Verify inside a container (it has tar, jq, diff): file list, manifest, sha256s, and the volume tree equals the original.
docker run --rm --user 10001 --entrypoint sh -v "$P:/work:ro" -v "$NET-vol:/volume:ro" "$IMAGE" -c '
  set -e; mkdir -p /tmp/x /tmp/v /tmp/b && cd /tmp/x && tar -xzf /work/out/package.tar.gz
  [ "$(ls | sort | tr "\n" " ")" = "MANIFEST.json blob-storage.tar.gz database.sql.gz volume.tar.gz " ] || { echo "files: $(ls)"; exit 1; }
  [ "$(jq -r ".app, .set_id, .engine, .picopaas_version, (.parts | length)" MANIFEST.json | tr "\n" " ")" = "my \"app\" 7 mysql smoke 3 " ] || { jq . MANIFEST.json; exit 1; }
  jq -r ".parts[] | .sha256 + \"  \" + .file" MANIFEST.json | sha256sum -c - >/dev/null
  jq -e ".parts | map(.kind) == [\"db\", \"bucket\", \"volume\"]" MANIFEST.json >/dev/null
  gzip -dc database.sql.gz | grep -q "hello"
  tar -xzf blob-storage.tar.gz -C /tmp/b && [ "$(cat /tmp/b/a.txt)" = "object one" ] && [ "$(cat /tmp/b/img/b.txt)" = "object two" ]
  tar -xzf volume.tar.gz -C /tmp/v && diff -r /tmp/v/volume /volume && [ "$(readlink /tmp/v/volume/link)" = "index.php" ]
  echo "package contents match"' || { echo "SMOKE FAIL: package contents"; exit 1; }
# forget of a volume snapshot works like the other kinds
out="$(vrun -e MODE=forget -e KIND=volume -e SNAPSHOT="$vsnap2")" || true; echo "$out"
[[ "$(tail -n1 <<<"$out")" == 'PICOPAAS_RESULT {"forgotten":true'* ]] || { echo "SMOKE FAIL: volume forget"; exit 1; }
echo "SMOKE OK"
