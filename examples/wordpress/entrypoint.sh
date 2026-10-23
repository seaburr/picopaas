#!/bin/sh
# PID 1 child of tini. Runs as UID 10001. Writes only to the WordPress root (volume, or the image dir) and /tmp.
set -eu
SEED=/usr/src/wordpress
VOLUME_DIR="${WP_VOLUME_DIR:-/var/www/html}"
umask 022
mkdir -p /tmp/nginx /tmp/nginx-cache /tmp/opcache

log() { echo "[entrypoint] $*" >&2; }

# 0. All eight auth keys/salts must be set (no random fallback: that would log everyone out on every restart).
missing=""
for n in AUTH_KEY SECURE_AUTH_KEY LOGGED_IN_KEY NONCE_KEY AUTH_SALT SECURE_AUTH_SALT LOGGED_IN_SALT NONCE_SALT; do
  eval "v=\${WORDPRESS_$n:-}"
  [ -n "$v" ] || missing="$missing WORDPRESS_$n"
done
if [ -n "$missing" ]; then
  log "FATAL: missing required secret env vars:$missing (generate each with: openssl rand -base64 48)"
  exit 1
fi

# 1. Where does WordPress live?
#    - a volume is mounted at $VOLUME_DIR: seed it from the image if empty (core refresh when the image is newer)
#    - no volume: serve core straight from the image path; nothing is copied to scratch
if grep -q " $VOLUME_DIR " /proc/self/mountinfo 2>/dev/null; then
  WP_ROOT="$VOLUME_DIR"
  if ! touch "$WP_ROOT/.write-test" 2>/dev/null; then
    log "FATAL: $WP_ROOT is not writable by uid $(id -u). The volume must be writable for this uid (fsGroup)."
    exit 1
  fi
  rm -f "$WP_ROOT/.write-test"
  IMG_VER=$(sed -n "s/^\$wp_version = '\(.*\)';/\1/p" "$SEED/wp-includes/version.php")
  if [ ! -f "$WP_ROOT/wp-includes/version.php" ]; then
    log "empty $WP_ROOT: seeding WordPress $IMG_VER"
    cp -a "$SEED/." "$WP_ROOT/"
  else
    CUR_VER=$(sed -n "s/^\$wp_version = '\(.*\)';/\1/p" "$WP_ROOT/wp-includes/version.php")
    NEWEST=$(printf '%s\n%s\n' "$CUR_VER" "$IMG_VER" | sort -V | tail -n1)
    if [ "$CUR_VER" != "$IMG_VER" ] && [ "$NEWEST" = "$IMG_VER" ]; then
      log "volume has WordPress $CUR_VER, image has $IMG_VER: refreshing core (wp-content untouched)"
      rm -rf "$WP_ROOT/wp-admin" "$WP_ROOT/wp-includes"
      cp -a "$SEED/wp-admin" "$SEED/wp-includes" "$WP_ROOT/"
      for f in "$SEED"/*.php "$SEED"/*.html "$SEED"/*.txt; do
        [ -e "$f" ] || continue
        [ "$(basename "$f")" = wp-config.php ] && [ "${WP_CONFIG_MANAGED:-true}" != "true" ] && continue
        cp -a "$f" "$WP_ROOT/"
      done
    fi
  fi
  if [ "${WP_CONFIG_MANAGED:-true}" = "true" ]; then cp "$SEED/wp-config.php" "$WP_ROOT/wp-config.php"; fi
  log "volume mode: WordPress root $WP_ROOT"
else
  WP_ROOT="$SEED"
  log "no volume at $VOLUME_DIR: serving WordPress from the image at $WP_ROOT (uploads/plugins are ephemeral)"
fi
# A restart means no update is running: clear a stale maintenance flag left by an interrupted update.
rm -f "$WP_ROOT/.maintenance" 2>/dev/null || true
mkdir -p "$WP_ROOT/wp-content/uploads" "$WP_ROOT/wp-content/plugins" "$WP_ROOT/wp-content/themes" 2>/dev/null || true
export WP_ROOT
echo "root $WP_ROOT;" > /tmp/nginx-root.conf

# 2. nginx include files driven by env
if [ "${WP_ALLOW_XMLRPC:-false}" = "true" ]; then : > /tmp/nginx-xmlrpc.conf
else echo 'location = /xmlrpc.php { return 403; }' > /tmp/nginx-xmlrpc.conf; fi
if [ "${WP_PAGE_CACHE:-false}" = "true" ]; then echo 'set $wp_cache_off 0;' > /tmp/nginx-cache.conf
else echo 'set $wp_cache_off 1;' > /tmp/nginx-cache.conf; fi
# Not ready until we know WordPress is installed.
set_state() { # $1 = message, empty = ready
  if [ -n "$1" ]; then printf 'set $wp_not_ready 1;\nset $wp_not_ready_msg "%s";\n' "$1" > /tmp/nginx-state.conf
  else printf 'set $wp_not_ready 0;\nset $wp_not_ready_msg "";\n' > /tmp/nginx-state.conf; fi
}
set_state "WordPress is starting"

# 3. Processes
php-fpm -y /usr/local/etc/php-fpm.conf &
FPM=$!
nginx -e /dev/stderr -g 'daemon off;' &
NGX=$!


# 3b. Optional one-time restore from the app's own bucket (WP_RESTORE=1). Volume mode only; never re-runs once the marker exists.
s3_get() { # $1 = object key; streams to stdout
  curl -fsS --retry 3 --aws-sigv4 "aws:amz:${WP_RESTORE_S3_REGION:-us-east-1}:s3" --user "${WP_RESTORE_S3_ACCESS_KEY}:${WP_RESTORE_S3_SECRET_KEY}" \
    -H "x-amz-content-sha256: UNSIGNED-PAYLOAD" "${WP_RESTORE_S3_ENDPOINT%/}/${WP_RESTORE_S3_BUCKET}/$1"
}
# TLS flags for the mariadb client. With WORDPRESS_DB_CA_CERT (${{ db.ca_cert }}) the server certificate is verified;
# without it the connection is encrypted but unverified (the client's default would otherwise reject the server).
DB_SSL_FLAGS=""
if [ "${WORDPRESS_DB_TLS:-off}" = required ]; then
  if [ -n "${WORDPRESS_DB_CA_CERT:-}" ]; then printf '%s\n' "$WORDPRESS_DB_CA_CERT" > /tmp/db-ca.pem; DB_SSL_FLAGS="--ssl --ssl-ca=/tmp/db-ca.pem --ssl-verify-server-cert"
  else DB_SSL_FLAGS="--ssl --skip-ssl-verify-server-cert"; fi
fi
db_sql() { # runs mysql with the app DB creds (password via env, never on the command line)
  MYSQL_PWD="$WORDPRESS_DB_PASSWORD" mariadb --host="${WORDPRESS_DB_HOST:-localhost}" --port="${WORDPRESS_DB_PORT:-3306}" \
    --user="$WORDPRESS_DB_USER" $DB_SSL_FLAGS \
    --max-allowed-packet=64M --default-character-set=utf8mb4 "$@" "$WORDPRESS_DB_NAME"
}
restore_flow() {
  MARKER="$WP_ROOT/.picopaas-restored"
  [ -e "$MARKER" ] && { log "restore: marker $MARKER exists, skipping"; return 0; }
  if [ "$WP_ROOT" != "$VOLUME_DIR" ]; then log "restore: REFUSED, no volume mounted at $VOLUME_DIR"; return 1; fi
  for v in WP_URL WP_RESTORE_S3_ENDPOINT WP_RESTORE_S3_BUCKET WP_RESTORE_S3_ACCESS_KEY WP_RESTORE_S3_SECRET_KEY; do
    eval "x=\${$v:-}"; [ -n "$x" ] || { log "restore: REFUSED, $v is not set"; return 1; }
  done
  DBKEY="${WP_RESTORE_DB_KEY:-restore/db.sql.gz}"; CKEY="${WP_RESTORE_CONTENT_KEY:-restore/wp-content.tar.gz}"; XKEY="${WP_RESTORE_EXTRA_KEY:-restore/extra.tar.gz}"
  PFX="${WORDPRESS_TABLE_PREFIX:-wp_}"
  log "restore: step 1/6 dropping existing tables"
  tables=$(db_sql -N -e 'SHOW TABLES') || { log "restore: cannot connect to the database with the mariadb client"; return 1; }
  tables=$(printf '%s\n' "$tables" | sed '/^$/d; s/.*/`&`/' | paste -sd, -)
  if [ -n "$tables" ]; then db_sql -e "SET FOREIGN_KEY_CHECKS=0; DROP TABLE IF EXISTS $tables" || return 1; fi
  log "restore: step 2/6 importing database dump from bucket key $DBKEY"
  s3_get "$DBKEY" | gzip -dc | db_sql || { log "restore: DB import FAILED"; return 1; }
  OLD_URL=$(db_sql -N -e "SELECT option_value FROM ${PFX}options WHERE option_name='siteurl'") || return 1
  [ -n "$OLD_URL" ] || { log "restore: FAILED, no siteurl found after import (empty or truncated dump?)"; return 1; }
  log "restore: database imported; old site URL is $OLD_URL"
  log "restore: step 3/6 extracting wp-content from $CKEY onto the volume"
  rm -rf "$WP_ROOT/wp-content"
  s3_get "$CKEY" | tar -xzf - -C "$WP_ROOT" || { log "restore: wp-content extract FAILED"; return 1; }
  if curl -fsS -o /dev/null --head --aws-sigv4 "aws:amz:${WP_RESTORE_S3_REGION:-us-east-1}:s3" --user "${WP_RESTORE_S3_ACCESS_KEY}:${WP_RESTORE_S3_SECRET_KEY}" \
      -H "x-amz-content-sha256: UNSIGNED-PAYLOAD" "${WP_RESTORE_S3_ENDPOINT%/}/${WP_RESTORE_S3_BUCKET}/$XKEY" 2>/dev/null; then
    log "restore: extracting extra static files from $XKEY"
    s3_get "$XKEY" | tar -xzf - -C "$WP_ROOT" || log "restore: extra extract failed (continuing)"
  fi
  [ -d "$WP_ROOT/wp-content/themes" ] || { log "restore: FAILED, wp-content archive had no themes dir"; return 1; }
  mkdir -p "$WP_ROOT/wp-content/uploads"
  wp_cli() { php -d memory_limit=256M /usr/local/bin/wp --path="$WP_ROOT" --allow-root "$@"; }
  # Restore-time wp-cli calls skip plugins and themes: an old plugin that fatals on this PHP must not break the restore.
  wpr() { wp_cli --skip-plugins --skip-themes "$@"; }
  if [ "$OLD_URL" != "$WP_URL" ]; then
    log "restore: step 4/6 search-replace $OLD_URL -> $WP_URL (guid column skipped)"
    wpr search-replace "$OLD_URL" "$WP_URL" --all-tables --skip-columns=guid --report-changed-only >/tmp/sr.log 2>&1 \
      || { log "restore: search-replace FAILED: $(tail -3 /tmp/sr.log | tr '\n' ' ')"; return 1; }
    tail -1 /tmp/sr.log >&2
    case "$OLD_URL" in https://*)
      wpr search-replace "http://${OLD_URL#https://}" "$WP_URL" --all-tables --skip-columns=guid --report-changed-only >/tmp/sr.log 2>&1 && tail -1 /tmp/sr.log >&2 ;;
    esac
  else
    log "restore: step 4/6 old and new URL are identical, no search-replace"
  fi
  # Plugins known to fatal on current PHP (comma list of plugin slugs), e.g. abandoned plugins using removed functions.
  if [ -n "${WP_RESTORE_DEACTIVATE:-}" ]; then
    for p in $(echo "$WP_RESTORE_DEACTIVATE" | tr ',' ' '); do
      if wpr plugin deactivate "$p" >/dev/null 2>&1; then log "restore: deactivated plugin $p"; else log "restore: plugin $p not active or not found"; fi
    done
  fi
  log "restore: step 5/6 flushing caches and transients"
  wpr transient delete --all >/dev/null 2>&1 || true
  wpr cache flush >/dev/null 2>&1 || true
  rm -rf /tmp/nginx-cache/* 2>/dev/null || true
  date -u +%FT%TZ > "$MARKER"
  log "restore: step 6/6 done, marker written to $MARKER"
}

# 3c. Boot actions (idempotent, WP-CLI with plugins and themes skipped). Run each boot once WordPress is installed.
wpq() { php -d memory_limit=256M /usr/local/bin/wp --path="$WP_ROOT" --allow-root --skip-plugins --skip-themes "$@"; }
post_boot_actions() {
  # WP_REMOVE_PLUGINS=slug,slug : deactivate and delete plugins from wp-content/plugins (never must-use plugins).
  if [ -n "${WP_REMOVE_PLUGINS:-}" ]; then
    for p in $(echo "$WP_REMOVE_PLUGINS" | tr ',' ' '); do
      case "$p" in ''|*[!a-z0-9-]*) log "remove-plugins: ignoring invalid name (allowed: a-z 0-9 -)"; continue ;; esac
      if [ -d "$WP_ROOT/wp-content/plugins/$p" ]; then
        wpq plugin deactivate "$p" >/dev/null 2>&1 || true
        if wpq plugin delete "$p" >/dev/null 2>&1; then log "remove-plugins: deactivated and deleted $p"; else log "remove-plugins: could not delete $p"; fi
      else
        log "remove-plugins: $p not present, nothing to do"
      fi
    done
  fi
  # WP_CLEAR_LOGIN_LOCKOUTS=1 : clear Limit Login Attempts Reloaded lockouts and retry counters.
  if [ "${WP_CLEAR_LOGIN_LOCKOUTS:-0}" = "1" ]; then
    for o in limit_login_retries limit_login_retries_valid; do wpq option delete "$o" >/dev/null 2>&1 || true; done
    wpq option update limit_login_lockouts '[]' --format=json >/dev/null 2>&1 || true
    log "clear-lockouts: cleared Limit Login Attempts lockouts and retry counters"
  fi
  # WP_RESET_PASSWORD_USER=<login|email|id> : set a new random password once per (user, WP_RESET_PASSWORD_NONCE).
  if [ -n "${WP_RESET_PASSWORD_USER:-}" ]; then
    key=$(printf '%s|%s' "$WP_RESET_PASSWORD_USER" "${WP_RESET_PASSWORD_NONCE:-}" | sha256sum | cut -c1-32)
    if [ "$(wpq option get picopaas_pwreset_done 2>/dev/null || true)" = "$key" ]; then
      log "reset-password: already done for this user and nonce; change WP_RESET_PASSWORD_NONCE to reset again"
    else
      uid=$(wpq user get "$WP_RESET_PASSWORD_USER" --field=ID 2>/dev/null || true)
      if [ -z "$uid" ]; then
        log "reset-password: no such user (looked up by login, email and id); nothing changed"
      else
        login=$(wpq user get "$uid" --field=user_login 2>/dev/null || echo "$uid")
        RPW=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
        if wpq user update "$uid" --user_pass="$RPW" --skip-email >/dev/null 2>&1; then
          wpq option update picopaas_pwreset_done "$key" >/dev/null 2>&1 || true
          echo "WORDPRESS PASSWORD RESET for $login (shown once, change it after login): $RPW" >&2
        else
          log "reset-password: update FAILED; nothing changed"
        fi
        RPW=""
      fi
    fi
  fi
}

# 4. First-boot install with WP-CLI (the web installer is blocked in nginx, so nobody can take over an empty site).
install_flow() {
  wp_cli() { php -d memory_limit=256M /usr/local/bin/wp --path="$WP_ROOT" --allow-root "$@"; }
  tries=0
  until php -r '
    mysqli_report(MYSQLI_REPORT_OFF);
    $m = mysqli_init();
    $tls = getenv("WORDPRESS_DB_TLS") === "required";
    $ok = @$m->real_connect(getenv("WORDPRESS_DB_HOST") ?: "localhost", getenv("WORDPRESS_DB_USER"), getenv("WORDPRESS_DB_PASSWORD"), getenv("WORDPRESS_DB_NAME"), (int)(getenv("WORDPRESS_DB_PORT") ?: 3306), null, $tls ? MYSQLI_CLIENT_SSL : 0);
    exit($ok ? 0 : 1);' 2>/dev/null; do
    tries=$((tries + 1))
    [ "$tries" -le 60 ] || { log "database not reachable after 5 minutes; still retrying"; tries=0; }
    set_state "WordPress is waiting for the database"; sleep 5
  done
  if [ "${WP_RESTORE:-0}" = "1" ]; then
    set_state "WordPress is restoring from backup, retry in a few minutes"
    restore_flow || { set_state "WordPress restore failed or refused, see the app logs"; return 0; }
  fi
  if wp_cli core is-installed 2>/dev/null; then
    post_boot_actions
    set_state ""; log "WordPress is installed; serving"; return 0
  fi
  if [ -z "${WP_URL:-}" ] || [ -z "${WP_ADMIN_EMAIL:-}" ]; then
    set_state "WordPress is not configured: set WP_URL and WP_ADMIN_EMAIL"
    log "WordPress is NOT installed and WP_URL / WP_ADMIN_EMAIL are not both set. Serving 503. Set them and redeploy."
    return 0
  fi
  set_state "WordPress is installing, retry in a minute"
  PW=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  if wp_cli core install --url="$WP_URL" --title="${WP_TITLE:-WordPress}" --admin_user="${WP_ADMIN_USER:-admin}" \
       --admin_password="$PW" --admin_email="$WP_ADMIN_EMAIL" --skip-email >/tmp/wp-install.log 2>&1; then
    echo "WORDPRESS ADMIN PASSWORD (shown once, change it after login): user=${WP_ADMIN_USER:-admin} password=$PW" >&2
    post_boot_actions
    set_state ""
    log "WordPress installed at $WP_URL"
  else
    log "wp core install FAILED: $(tr '\n' ' ' </tmp/wp-install.log)"
    set_state "WordPress install failed, see the app logs"
  fi
  PW=""
  rm -f /tmp/wp-install.log
}
(
  install_flow
  nginx -e /dev/stderr -s reload 2>/dev/null || kill -HUP "$NGX" 2>/dev/null || true
) &
INST=$!
# Reload nginx whenever the state file changes (every set_state is followed by a reload poll below).
(
  sleep 3   # let nginx install its signal handlers before the first HUP
  last=""
  while :; do
    cur=$(cat /tmp/nginx-state.conf 2>/dev/null || true)
    if [ "$cur" != "$last" ]; then last="$cur"; kill -HUP "$NGX" 2>/dev/null || true; fi
    sleep 2
  done
) &
WATCH=$!

CRON=0
if [ "${WP_CRON_ENABLED:-true}" = "true" ]; then
  (
    sleep 90
    while :; do
      if grep -q 'wp_not_ready 0' /tmp/nginx-state.conf 2>/dev/null; then
        php -d memory_limit=192M -d max_execution_time=300 "$WP_ROOT/wp-cron.php" >/dev/null 2>&1 || log "wp-cron run failed"
      fi
      sleep "${WP_CRON_INTERVAL:-300}"
    done
  ) &
  CRON=$!
fi

trap 'kill $FPM $NGX $CRON $WATCH 2>/dev/null || true; exit 0' TERM INT
while kill -0 $FPM 2>/dev/null && kill -0 $NGX 2>/dev/null; do sleep 2; done
log "a child process exited; shutting down"
kill $FPM $NGX $CRON $WATCH 2>/dev/null || true
exit 1
