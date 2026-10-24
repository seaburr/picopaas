#!/bin/sh
# Entry point of the backup image. One run = one app repo (kind) = one Job.
#   MODE=backup  dump the app's database into a restic repo, then `forget --prune` with the retention policy
#   MODE=purge   forget every snapshot of the repo and prune (deleted-app retention expired)
# Inputs (env, from the per-run Secret): ENGINE (mysql|pg), DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD DB_CA,
# RESTIC_REPOSITORY RESTIC_PASSWORD AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION,
# KEEP_DAILY KEEP_WEEKLY KEEP_MONTHLY, TAG (optional), LIMIT_UPLOAD_KIB (optional).
# Output: the LAST stdout line is `PICOPAAS_RESULT {json}`; failures print `PICOPAAS_ERROR <short fixed text>` and exit
# non-zero. Nothing here ever prints credentials, the repository URL or restic's own output on failure.
set -u
umask 077

fail() { echo "PICOPAAS_ERROR $1"; exit 1; }

: "${RESTIC_REPOSITORY:?}" "${RESTIC_PASSWORD:?}"
MODE="${MODE:-backup}"
LIMIT="${LIMIT_UPLOAD_KIB:-0}"
RESTIC="restic --limit-upload $LIMIT --cleanup-cache"

# Create the repo on first use ("cat config" succeeds only when it exists).
if ! restic cat config >/dev/null 2>&1; then
  restic init --quiet >/dev/null 2>&1 || fail "could not open or create the backup repository"
fi

if [ "$MODE" = "purge" ]; then
  $RESTIC forget --keep-tag picopaas-keep-nothing --prune >/dev/null 2>&1 || fail "purge failed"
  echo 'PICOPAAS_RESULT {"purged":true}'
  exit 0
fi

: "${ENGINE:?}" "${DB_HOST:?}" "${DB_NAME:?}" "${DB_USER:?}" "${DB_PASSWORD:?}"
CA=""
if [ -n "${DB_CA:-}" ]; then
  printf '%s\n' "$DB_CA" > /tmp/db-ca.pem
  CA=/tmp/db-ca.pem
fi

TAGARG=""
[ -n "${TAG:-}" ] && TAGARG="--tag $TAG"

# The dump streams straight into restic (no scratch file). pipefail is not POSIX sh, so the dump's exit status goes
# through a fifo-free marker file.
rm -f /tmp/dump.status
if [ "$ENGINE" = "pg" ]; then
  NAME=db.pgdump
  export PGSSLMODE=require
  [ -n "$CA" ] && { export PGSSLMODE=verify-ca; export PGSSLROOTCERT="$CA"; }
  ( PGPASSWORD="$DB_PASSWORD" \
      pg_dump -h "$DB_HOST" -p "${DB_PORT:-25060}" -U "$DB_USER" -d "$DB_NAME" -Fc --no-owner --no-acl 2>/tmp/dump.err
    echo $? > /tmp/dump.status ) \
  | $RESTIC backup --stdin --stdin-filename "$NAME" --json $TAGARG --host "app" > /tmp/backup.json 2>/dev/null
else
  NAME=db.sql
  FLAGS="--single-transaction --quick --routines --triggers --events"
  # MySQL 8's dump needs --no-tablespaces / --set-gtid-purged=OFF; MariaDB's dump neither has nor needs them.
  HELP="$(mariadb-dump --help 2>/dev/null)"
  echo "$HELP" | grep -q -- '--no-tablespaces' && FLAGS="$FLAGS --no-tablespaces"
  echo "$HELP" | grep -q -- '--set-gtid-purged' && FLAGS="$FLAGS --set-gtid-purged=OFF"
  SSL=""
  [ -n "$CA" ] && SSL="--ssl-ca=$CA --ssl-verify-server-cert"
  ( MYSQL_PWD="$DB_PASSWORD" mariadb-dump -h "$DB_HOST" -P "${DB_PORT:-3306}" -u "$DB_USER" $SSL $FLAGS "$DB_NAME" 2>/tmp/dump.err
    echo $? > /tmp/dump.status ) \
  | $RESTIC backup --stdin --stdin-filename "$NAME" --json $TAGARG --host "app" > /tmp/backup.json 2>/dev/null
fi
RESTIC_RC=$?
if [ "$(cat /tmp/dump.status 2>/dev/null || echo 1)" != "0" ]; then
  # The dump died mid-stream but restic still sealed what it received as a snapshot: a truncated "latest backup" that a
  # restore would trust. Remove it before failing (the next run's forget --prune reclaims the space).
  BAD="$(grep '"message_type":"summary"' /tmp/backup.json 2>/dev/null | tail -n1 | jq -r '.snapshot_id // empty' 2>/dev/null)"
  [ -n "$BAD" ] && restic forget "$BAD" >/dev/null 2>&1
  fail "database dump failed"
fi
[ "$RESTIC_RC" = "0" ] || fail "writing the backup failed"

SUMMARY="$(grep '"message_type":"summary"' /tmp/backup.json | tail -n1)"
SNAP="$(echo "$SUMMARY" | jq -r '.snapshot_id // empty' | cut -c1-8)"
ADDED="$(echo "$SUMMARY" | jq -r '.data_added // 0')"
[ -n "$SNAP" ] || fail "backup finished without a snapshot"

$RESTIC forget --keep-daily "${KEEP_DAILY:-7}" --keep-weekly "${KEEP_WEEKLY:-4}" --keep-monthly "${KEEP_MONTHLY:-6}" \
  --group-by host,paths --prune >/dev/null 2>&1 || fail "retention cleanup failed"

REPO="$(restic stats --mode raw-data --json 2>/dev/null | jq -r '.total_size // 0')"
echo "PICOPAAS_RESULT {\"snapshot_id\":\"$SNAP\",\"bytes_added\":${ADDED:-0},\"repo_bytes\":${REPO:-0}}"
