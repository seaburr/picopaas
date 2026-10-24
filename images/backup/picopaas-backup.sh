#!/bin/sh
# Entry point of the backup image. One run = one app repo (KIND) = one Job.
#   MODE=backup   KIND=db      dump the app's database into a restic repo, then `forget --prune` with the retention policy
#   MODE=backup   KIND=bucket  copy the app's bucket objects to $WORK/bucket (s5cmd, the app's own key), restic that dir
#   MODE=restore  KIND=db      restic restore SNAPSHOT, drop the target DB's tables, load the dump (as the target's role)
#   MODE=restore  KIND=bucket  restic restore SNAPSHOT, empty the target bucket, upload the files
#   MODE=export   KIND=db|bucket  restic dump SNAPSHOT as ONE file (db: .sql.gz or .pgdump, bucket: .tar.gz) and upload it to
#                              s3://$EXPORT_BUCKET/$EXPORT_KEY with the backup key (owner download; the API presigns it)
#   MODE=purge                 forget every snapshot of the repo and prune (deleted-app retention expired)
# Inputs (env, from the per-run Secret): ENGINE (mysql|pg), DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD DB_CA,
# SRC_ENDPOINT SRC_BUCKET SRC_REGION SRC_ACCESS_KEY SRC_SECRET_KEY (bucket kind: the app whose bucket is read; for a
# restore, the TARGET app's bucket), RESTIC_REPOSITORY RESTIC_PASSWORD AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
# AWS_DEFAULT_REGION (the backup repo's key only), SNAPSHOT (restore), KEEP_DAILY KEEP_WEEKLY KEEP_MONTHLY, TAG,
# EXPORT_ENDPOINT EXPORT_BUCKET EXPORT_KEY (export), LIMIT_UPLOAD_KIB (optional), WORK_DIR (scratch, default /work; tests override).
# Output: the LAST stdout line is `PICOPAAS_RESULT {json}`; failures print `PICOPAAS_ERROR <short fixed text>` and exit
# non-zero. Nothing here ever prints credentials, object keys, the repository URL or restic's own output on failure.
# The app's bucket key reaches s5cmd only through that one command's environment (s3 below), never through arguments.
# Progress: `PICOPAAS_STEP <name>` lines (fixed text, never credentials, keys or restic output) so a stuck Job's logs show
# where it is. Every network step runs under `timeout` (restic retries connection errors for ~15 minutes per command and
# prints nothing, so an unreachable endpoint looks like a hang): QUICK_TIMEOUT for open/init/stats, LONG_TIMEOUT for data.
set -u
umask 077

step() { echo "PICOPAAS_STEP $1"; }
fail() { echo "PICOPAAS_ERROR $1"; exit 1; }

# busybox has `timeout`; fall back to running without one where it is missing (local test boxes).
if ! command -v timeout >/dev/null 2>&1; then
  timeout() { [ "$1" = -k ] && shift 2; shift; "$@"; }
fi

[ -n "${RESTIC_REPOSITORY:-}" ] || fail "the repository is not configured"
[ -n "${RESTIC_PASSWORD:-}" ] || fail "the repository password is missing"
MODE="${MODE:-backup}"
KIND="${KIND:-db}"
LIMIT="${LIMIT_UPLOAD_KIB:-0}"
QUICK_TIMEOUT="${QUICK_TIMEOUT:-120}"
LONG_TIMEOUT="${LONG_TIMEOUT:-1500}"
RESTIC="timeout -k 10 $LONG_TIMEOUT restic --limit-upload $LIMIT --cleanup-cache"
RQ="timeout -k 10 $QUICK_TIMEOUT restic"
WORK="${WORK_DIR:-/work}"
TMP="${TMP_DIR:-/tmp}"
export PGCONNECT_TIMEOUT=20

step "start mode=$MODE kind=$KIND"
# Create the repo on first use ("cat config" succeeds only when it exists). A restore never creates one.
step "opening the backup repository"
if ! $RQ cat config >/dev/null 2>&1; then
  [ "$MODE" = "restore" ] || [ "$MODE" = "export" ] && fail "the backup repository could not be opened"
  step "creating the backup repository"
  $RQ init --quiet >/dev/null 2>&1 || fail "could not open or create the backup repository"
fi
step "backup repository ready"

if [ "$MODE" = "purge" ]; then
  $RESTIC forget --keep-tag picopaas-keep-nothing --prune >/dev/null 2>&1 || fail "purge failed"
  echo 'PICOPAAS_RESULT {"purged":true}'
  exit 0
fi

# --- helpers ----------------------------------------------------------------------------------

# The app's bucket, through s5cmd. Credentials only in this one command's environment.
s3() {
  AWS_ACCESS_KEY_ID="$SRC_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$SRC_SECRET_KEY" \
  AWS_REGION="${SRC_REGION:-us-east-1}" AWS_DEFAULT_REGION="${SRC_REGION:-us-east-1}" \
    timeout -k 10 "$LONG_TIMEOUT" s5cmd --endpoint-url "$SRC_ENDPOINT" --numworkers 8 "$@"
}
# Number of real objects (not "folder" markers) in the whole bucket; empty output = the listing failed.
s3_count() {
  OUT="$(s3 --json ls "s3://$SRC_BUCKET/*" 2>"$TMP/s3.err")"
  RC=$?
  if [ "$RC" != 0 ]; then
    grep -q "no object found" "$TMP/s3.err" 2>/dev/null && { echo 0; return 0; }
    return 1
  fi
  # An object name with a ".." segment would make the copy write outside the scratch directory (s5cmd keeps the key's
  # directory structure and does not clean it), so refuse such a bucket before copying anything.
  if [ "$(printf '%s\n' "$OUT" | jq -rs '[.[] | select(.key? and (.key | split("/") | any(. == "..")))] | length' 2>/dev/null)" != "0" ]; then
    echo UNSAFE
    return 0
  fi
  printf '%s\n' "$OUT" | jq -rs '[.[] | select(.key? and (.key | endswith("/") | not))] | length' 2>/dev/null
}
files_in() { find "$1" -type f | wc -l | tr -d ' '; }

need_bucket() { : "${SRC_ENDPOINT:?}" "${SRC_BUCKET:?}" "${SRC_ACCESS_KEY:?}" "${SRC_SECRET_KEY:?}"; }
need_db() {
  : "${ENGINE:?}" "${DB_HOST:?}" "${DB_NAME:?}" "${DB_USER:?}" "${DB_PASSWORD:?}"
  if [ -n "${DB_CA:-}" ]; then
    printf '%s\n' "$DB_CA" > "$TMP/db-ca.pem"
    CA="$TMP/db-ca.pem"
  fi
}
CA=""
# Why a dump failed, as fixed text: the client's own stderr can carry host names and user names, so it is never printed.
dump_reason() {
  E="$TMP/dump.err"
  [ -s "$E" ] || { echo ": the dump stopped without an error message"; return; }
  if grep -qiE "max_user_connections|too many connections" "$E"; then echo ": the app's database user is at its connection limit"
  elif grep -qiE "access denied|authentication failed|password authentication" "$E"; then echo ": access denied"
  elif grep -qiE "ssl|tls|certificate" "$E"; then echo ": TLS or certificate verification failed"
  elif grep -qiE "can't connect|could not connect|connection refused|timed out|timeout|unknown .*host|could not translate host" "$E"; then echo ": could not connect to the database"
  elif grep -qiE "unknown database|does not exist" "$E"; then echo ": the database does not exist"
  elif grep -qiE "privilege|permission denied|command denied" "$E"; then echo ": the app's database user lacks a privilege the dump needs"
  elif grep -qiE "lost connection|gone away|server closed" "$E"; then echo ": the database connection dropped mid-dump"
  else echo ": unrecognised client error"
  fi
}

# --- export (owner download) ------------------------------------------------------------------

if [ "$MODE" = "export" ]; then
  : "${SNAPSHOT:?}" "${EXPORT_ENDPOINT:?}" "${EXPORT_BUCKET:?}" "${EXPORT_KEY:?}"
  rm -rf "$WORK/restore" "$WORK/export.out"
  mkdir -p "$WORK/restore"
  step "reading the snapshot from the repository"
  if [ "$KIND" = "bucket" ]; then
    $RESTIC restore "$SNAPSHOT:$WORK/bucket" --target "$WORK/restore" >/dev/null 2>&1 \
      || fail "restoring the snapshot from the repository failed"
    step "packing the files"
    tar -czf "$WORK/export.out" -C "$WORK/restore" . >/dev/null 2>&1 || fail "packing the files failed"
  else
    case "$EXPORT_KEY" in
      *.pgdump) $RESTIC dump "$SNAPSHOT" db.pgdump > "$WORK/export.out" 2>/dev/null || fail "reading the snapshot from the repository failed" ;;
      *) $RESTIC dump "$SNAPSHOT" db.sql 2>/dev/null | gzip -c > "$WORK/export.out"
         # a pipeline's status is gzip's: a snapshot with no db.sql (wrong engine) dumps nothing, which gzip turns into a header
         [ "$(gzip -dc "$WORK/export.out" | head -c 1 | wc -c | tr -d ' ')" = "1" ] || fail "the snapshot is not a MySQL dump" ;;
    esac
    [ -s "$WORK/export.out" ] || fail "reading the snapshot from the repository failed"
  fi
  rm -rf "$WORK/restore"
  SIZE="$(wc -c < "$WORK/export.out" | tr -d ' ')"
  step "uploading the file"
  AWS_REGION="${AWS_DEFAULT_REGION:-us-east-1}" timeout -k 10 "$LONG_TIMEOUT" \
    s5cmd --endpoint-url "$EXPORT_ENDPOINT" cp "$WORK/export.out" "s3://$EXPORT_BUCKET/$EXPORT_KEY" >/dev/null 2>"$TMP/s3.err" \
    || fail "uploading the file failed"
  rm -f "$WORK/export.out"
  echo "PICOPAAS_RESULT {\"exported\":true,\"bytes\":$SIZE}"
  exit 0
fi

# --- restore ----------------------------------------------------------------------------------

if [ "$MODE" = "restore" ]; then
  : "${SNAPSHOT:?}"
  rm -rf "$TMP/restore" "$WORK/restore"
  if [ "$KIND" = "bucket" ]; then
    need_bucket
    mkdir -p "$WORK/restore"
    step "restoring the snapshot from the repository"
    $RESTIC restore "$SNAPSHOT:$WORK/bucket" --target "$WORK/restore" >/dev/null 2>&1 \
      || fail "restoring the snapshot from the repository failed"
    FILES="$(files_in "$WORK/restore")"
    step "replacing the target bucket's objects"
    # Replace, not merge: empty the target bucket (a wildcard with nothing to match is fine), then upload.
    s3 rm "s3://$SRC_BUCKET/*" >/dev/null 2>"$TMP/s3.err" || grep -q "no object found" "$TMP/s3.err" \
      || fail "emptying the target bucket failed"
    if [ "$FILES" -gt 0 ]; then
      s3 cp "$WORK/restore/*" "s3://$SRC_BUCKET/" >/dev/null 2>"$TMP/s3.err" || fail "uploading the restored objects failed"
    fi
    GOT="$(s3_count)"
    [ "${GOT:-x}" = "$FILES" ] || fail "the restored bucket does not match the snapshot"
    rm -rf "$WORK/restore"
    echo "PICOPAAS_RESULT {\"restored\":true,\"objects\":$FILES}"
    exit 0
  fi
  need_db
  mkdir -p "$TMP/restore"
  step "restoring the snapshot from the repository"
  $RESTIC restore "$SNAPSHOT" --target "$TMP/restore" >/dev/null 2>&1 || fail "restoring the snapshot from the repository failed"
  step "clearing and loading the target database"
  if [ "$ENGINE" = "pg" ]; then
    [ -s "$TMP/restore/db.pgdump" ] || fail "the snapshot is not a PostgreSQL dump (wrong engine for the target)"
    export PGSSLMODE=require PGPASSWORD="$DB_PASSWORD"
    [ -n "$CA" ] && { export PGSSLMODE=verify-ca; export PGSSLROOTCERT="$CA"; }
    PSQL="psql -h $DB_HOST -p ${DB_PORT:-25060} -U $DB_USER -d $DB_NAME -v ON_ERROR_STOP=1 -q"
    # Drop everything the app's role can see in its own database, then load the dump (replace, not merge).
    $PSQL -At -c "SELECT format('DROP VIEW IF EXISTS %I.%I CASCADE;', schemaname, viewname) FROM pg_views WHERE schemaname NOT IN ('pg_catalog','information_schema') UNION ALL SELECT format('DROP TABLE IF EXISTS %I.%I CASCADE;', schemaname, tablename) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')" \
      > "$TMP/drop.sql" 2>/dev/null || fail "reading the target database failed"
    $PSQL -f "$TMP/drop.sql" >/dev/null 2>&1 || fail "clearing the target database failed"
    pg_restore -h "$DB_HOST" -p "${DB_PORT:-25060}" -U "$DB_USER" -d "$DB_NAME" --no-owner --no-acl --exit-on-error \
      "$TMP/restore/db.pgdump" >/dev/null 2>&1 || fail "loading the dump into the target database failed"
  else
    [ -s "$TMP/restore/db.sql" ] || fail "the snapshot is not a MySQL dump (wrong engine for the target)"
    export MYSQL_PWD="$DB_PASSWORD"
    SSL=""
    [ -n "$CA" ] && SSL="--ssl-ca=$CA --ssl-verify-server-cert"
    MY="mariadb --connect-timeout=20 -h $DB_HOST -P ${DB_PORT:-3306} -u $DB_USER $SSL"
    # CHAR(96) is a backtick; keeps the SQL free of shell-quoting tricks.
    gen_drop() {
      echo "SET FOREIGN_KEY_CHECKS=0;"
      $MY -N -B -e "SELECT CONCAT('DROP VIEW IF EXISTS ', CHAR(96), table_name, CHAR(96), ';') FROM information_schema.tables WHERE table_schema = DATABASE() AND table_type = 'VIEW'" "$DB_NAME" || return 1
      $MY -N -B -e "SELECT CONCAT('DROP TABLE IF EXISTS ', CHAR(96), table_name, CHAR(96), ';') FROM information_schema.tables WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'" "$DB_NAME"
    }
    gen_drop > "$TMP/drop.sql" 2>/dev/null || fail "reading the target database failed"
    $MY "$DB_NAME" < "$TMP/drop.sql" >/dev/null 2>&1 || fail "clearing the target database failed"
    # The source's DEFINER (another app's role, or the managed server's admin) can't be set by this role: drop it so views,
    # routines and triggers are created as the target role.
    LC_ALL=C sed -E 's/DEFINER=`[^`]*`@`[^`]*`//g' "$TMP/restore/db.sql" > "$TMP/load.sql" || fail "preparing the dump failed"
    $MY "$DB_NAME" < "$TMP/load.sql" >/dev/null 2>&1 || fail "loading the dump into the target database failed"
  fi
  rm -rf "$TMP/restore"
  echo 'PICOPAAS_RESULT {"restored":true}'
  exit 0
fi

# --- backup -----------------------------------------------------------------------------------

TAGARG=""
[ -n "${TAG:-}" ] && TAGARG="--tag $TAG"

if [ "$KIND" = "bucket" ]; then
  need_bucket
  rm -rf "$WORK/bucket"; mkdir -p "$WORK/bucket"
  # restic cannot read S3 as a source: copy to scratch first (bounded by the Job's emptyDir sizeLimit).
  step "listing the bucket"
  EXPECT="$(s3_count)"
  [ -n "$EXPECT" ] || fail "listing the bucket failed"
  [ "$EXPECT" != UNSAFE ] || fail "the bucket has object names that can't be backed up safely"
  step "copying $EXPECT objects"
  if [ "$EXPECT" -gt 0 ]; then
    s3 cp "s3://$SRC_BUCKET/*" "$WORK/bucket/" >/dev/null 2>"$TMP/s3.err" || fail "copying the bucket's objects failed"
  fi
  [ "$(files_in "$WORK/bucket")" = "$EXPECT" ] || fail "the copy of the bucket is incomplete"
  step "writing the snapshot"
  $RESTIC backup "$WORK/bucket" --json $TAGARG --host "app" > "$TMP/backup.json" 2>/dev/null
  RESTIC_RC=$?
  rm -rf "$WORK/bucket"
  [ "$RESTIC_RC" = "0" ] || fail "writing the backup failed"
else
  need_db
  step "dumping the database into the repository"
  # The dump streams straight into restic (no scratch file). pipefail is not POSIX sh, so the dump's exit status goes
  # through a marker file.
  rm -f "$TMP/dump.status"
  if [ "$ENGINE" = "pg" ]; then
    NAME=db.pgdump
    export PGSSLMODE=require
    [ -n "$CA" ] && { export PGSSLMODE=verify-ca; export PGSSLROOTCERT="$CA"; }
    ( PGPASSWORD="$DB_PASSWORD" \
        pg_dump -h "$DB_HOST" -p "${DB_PORT:-25060}" -U "$DB_USER" -d "$DB_NAME" -Fc --no-owner --no-acl 2>"$TMP/dump.err"
      echo $? > "$TMP/dump.status" ) \
    | $RESTIC backup --stdin --stdin-filename "$NAME" --json $TAGARG --host "app" > "$TMP/backup.json" 2>/dev/null
  else
    NAME=db.sql
    FLAGS="--single-transaction --quick --routines --triggers --events"
    # MySQL 8's dump needs --no-tablespaces / --set-gtid-purged=OFF; MariaDB's dump neither has nor needs them.
    HELP="$(mariadb-dump --help 2>/dev/null)"
    echo "$HELP" | grep -q -- '--no-tablespaces' && FLAGS="$FLAGS --no-tablespaces"
    echo "$HELP" | grep -q -- '--set-gtid-purged' && FLAGS="$FLAGS --set-gtid-purged=OFF"
    SSL=""
    [ -n "$CA" ] && SSL="--ssl-ca=$CA --ssl-verify-server-cert"
    # mariadb-dump has no --connect-timeout (only the mariadb client does; it fails with "unknown variable"), so the
    # whole dump runs under timeout instead.
    ( MYSQL_PWD="$DB_PASSWORD" timeout -k 10 "$LONG_TIMEOUT" mariadb-dump -h "$DB_HOST" -P "${DB_PORT:-3306}" -u "$DB_USER" $SSL $FLAGS "$DB_NAME" 2>"$TMP/dump.err"
      echo $? > "$TMP/dump.status" ) \
    | $RESTIC backup --stdin --stdin-filename "$NAME" --json $TAGARG --host "app" > "$TMP/backup.json" 2>/dev/null
  fi
  RESTIC_RC=$?
  if [ "$(cat "$TMP/dump.status" 2>/dev/null || echo 1)" != "0" ]; then
    # The dump died mid-stream but restic still sealed what it received as a snapshot: a truncated "latest backup" that a
    # restore would trust. Remove it before failing (the next run's forget --prune reclaims the space).
    BAD="$(grep '"message_type":"summary"' "$TMP/backup.json" 2>/dev/null | tail -n1 | jq -r '.snapshot_id // empty' 2>/dev/null)"
    [ -n "$BAD" ] && $RQ forget "$BAD" >/dev/null 2>&1
    fail "database dump failed$(dump_reason)"
  fi
  [ "$RESTIC_RC" = "0" ] || fail "writing the backup failed"
fi

SUMMARY="$(grep '"message_type":"summary"' "$TMP/backup.json" | tail -n1)"
SNAP="$(echo "$SUMMARY" | jq -r '.snapshot_id // empty' | cut -c1-8)"
ADDED="$(echo "$SUMMARY" | jq -r '.data_added // 0')"
[ -n "$SNAP" ] || fail "backup finished without a snapshot"

step "applying retention"
$RESTIC forget --keep-daily "${KEEP_DAILY:-1}" --keep-weekly "${KEEP_WEEKLY:-1}" --keep-monthly "${KEEP_MONTHLY:-1}" \
  --group-by host,paths --prune >/dev/null 2>&1 || fail "retention cleanup failed"

REPO="$($RQ stats --mode raw-data --json 2>/dev/null | jq -r '.total_size // 0')"
echo "PICOPAAS_RESULT {\"snapshot_id\":\"$SNAP\",\"bytes_added\":${ADDED:-0},\"repo_bytes\":${REPO:-0}}"
