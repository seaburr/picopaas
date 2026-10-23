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
  if wp_cli core is-installed 2>/dev/null; then
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
