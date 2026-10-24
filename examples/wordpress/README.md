# WordPress on PicoPaaS (nginx + PHP-FPM, non-root)

One container, UID 10001, port 8080. Design and platform requirements: `docs/research/wordpress-requirements.md`.

- PHP **8.5.12** (`php:8.5.12-fpm-alpine`). It carries the 21 Oct 2026 fixes for CVE-2026-109618 and CVE-2026-109619
  (unauthenticated RCE). For 8.4 build with `--build-arg PHP_IMAGE=php:8.4.27-fpm-alpine`.
- WordPress **7.1.3** and WP-CLI **2.12.0**, both downloaded at build time and checksum-verified (SHA-1 / SHA-512).
- Extensions: gd (jpeg/webp/freetype), mysqli, exif, zip, intl, bcmath, plus mbstring, curl, xml, openssl, opcache.
- Writes only to the WordPress root and `/tmp`.

## Base image updates (read this)

The Dockerfile pins the PHP **patch version by tag, not by digest**. The `examples` workflow rebuilds weekly
(Monday 06:17 UTC, `no-cache`, `pull`), so Alpine/OpenSSL/libc fixes that Docker Hub republishes under the same tag are
picked up automatically. A digest pin would block that and would need a manual bump for every patch. What still needs a
**manual edit** is a new PHP release (`PHP_IMAGE`), a new WordPress (`WP_VERSION`, `WP_SHA1`) or a new WP-CLI. Watch
php.net for security releases. Digest of 8.5.12 on 2026-10-22 for the record:
`sha256:f09d9172dd28b8696aef729ca2c2081a132c1f8d581218c99497d552c82a92df`. The shared workflow passes
`WITH_S3=1`; the Dockerfile declares and ignores it.

## Where WordPress lives

- **No volume (vanilla milestone, works today).** WordPress is served straight from the image at `/usr/src/wordpress`;
  nothing large is copied to the scratch disk (cap 250 MB, `EPHEMERAL_LIMIT_MI`). Only what you write (uploads, plugins
  installed from the dashboard, the page cache in `/tmp`, about 25 MB after an install) counts against scratch and is lost
  on restart. Content lives in MySQL.
- **Volume mounted at `/var/www/html`** (needs the volume feature). Detected from the mount table. The first boot seeds
  the volume from the image; later boots keep it, and a newer WordPress in the image refreshes `wp-admin` and `wp-includes`
  only. The volume must be writable by UID 10001 (fsGroup), or the container exits with a clear message.

## First boot: automatic, locked-down install

An uninstalled WordPress would let anyone finish the web installer and become admin. So:

- `/wp-admin/install.php` and `/wp-admin/setup-config.php` always return 403 in nginx.
- `/wp-login.php` is rate limited per client IP (10 requests/min, burst 5, then HTTP 429). The client IP comes from
  `X-Forwarded-For` (trusted only from private ranges, `real_ip_recursive on`), so it is the visitor, not the proxy.
- On start the entrypoint waits for the database, and if WordPress is not installed it runs `wp core install` (WP-CLI)
  with `WP_URL`, `WP_TITLE`, `WP_ADMIN_USER`, `WP_ADMIN_EMAIL`. The admin password is generated inside the container,
  never read from env, and printed **once** to the app logs on a line like:
  `WORDPRESS ADMIN PASSWORD (shown once, change it after login): user=admin password=...`
  Read it from the app's logs, log in, and change it. It is not shown again on later restarts.
- If WordPress is not installed and `WP_URL` or `WP_ADMIN_EMAIL` is missing, the site answers **503** "not configured".
  While installing or waiting for the DB it also answers 503, except `/healthz`.
- PicoPaaS injects only `PORT` into the container. The public URL is available as a reference, so map
  `WP_URL: ${{ app.url }}` (it is not an automatic env var).

## Secrets: the eight auth keys and salts (required)

Generate each locally and set them as secret env vars (never in `env:`, never in git):

```bash
for n in AUTH_KEY SECURE_AUTH_KEY LOGGED_IN_KEY NONCE_KEY AUTH_SALT SECURE_AUTH_SALT LOGGED_IN_SALT NONCE_SALT; do
  echo "WORDPRESS_$n=$(openssl rand -base64 48 | tr -d '\n')"
done
```

If any is missing the container refuses to start and logs `FATAL: missing required secret env vars: ...` (there is no
random fallback, which would log everybody out on every restart). Keep them stable.

## Database and TLS

PicoPaaS gives `${{ db.host }}`, `${{ db.port }}`, `${{ db.name }}`, `${{ db.user }}`, `${{ db.password }}`,
`${{ db.tls }}` (`required` when the server has a CA cert) and `${{ db.ca_cert }}`. Host and port are separate variables
because a reference cannot be embedded in a longer string. With `WORDPRESS_DB_TLS=required`, `wp-config.php` sets
`MYSQL_CLIENT_FLAGS = MYSQLI_CLIENT_SSL`, so the connection is encrypted (tested against a TLS MySQL 8.4). Core WordPress
has no hook to hand over a CA file, so the server certificate is **not verified**; `db.ca_cert` is not used. Managed
MySQL's `sql_require_primary_key` is fine with core tables; check plugins that create their own.

## PicoPaaS spec (vanilla deploy)

```yaml
apiVersion: picopaas/v1
name: wordpress
image: ghcr.io/seaburr/picopaas-example-wordpress:latest
port: 8080
healthCheck: { enabled: true, path: /healthz, timeoutSeconds: 10 }
resources: { db: true, db_engine: mysql }   # API field: want_db
env:
  WORDPRESS_DB_HOST: ${{ db.host }}
  WORDPRESS_DB_PORT: ${{ db.port }}
  WORDPRESS_DB_NAME: ${{ db.name }}
  WORDPRESS_DB_USER: ${{ db.user }}
  WORDPRESS_DB_PASSWORD: ${{ db.password }}
  WORDPRESS_DB_TLS: ${{ db.tls }}
  WP_URL: ${{ app.url }}
  WP_TITLE: My site
  WP_ADMIN_USER: admin
  WP_ADMIN_EMAIL: you@example.com
  WP_PAGE_CACHE: "true"
secrets:
  - WORDPRESS_AUTH_KEY
  - WORDPRESS_SECURE_AUTH_KEY
  - WORDPRESS_LOGGED_IN_KEY
  - WORDPRESS_NONCE_KEY
  - WORDPRESS_AUTH_SALT
  - WORDPRESS_SECURE_AUTH_SALT
  - WORDPRESS_LOGGED_IN_SALT
  - WORDPRESS_NONCE_SALT
```

**Platform-admin overrides:** CPU limit **500m** (200m default is slow for the admin and installer; the install takes
about 20 s at full speed). RAM 384 MB should hold (4 FPM workers, about 200 MiB steady). DB 50 MB and the 10 connection
cap are enough for a vanilla site. The health check passes only after nginx and php-fpm are up; the 503 "installing"
state does not affect `/healthz`. If the install is slow on a throttled pod, raise `timeoutSeconds`.

## Moving an existing site here

`WP_RESTORE=1` restores a database dump and a `wp-content` tarball from the app's own bucket, once, onto a mounted volume.
Full procedure, bundle preparation, sizing and rollback: [MIGRATION.md](MIGRATION.md). Backup strategy: `docs/research/wordpress-backups.md`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `WORDPRESS_DB_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD` | | From `${{ db.* }}` |
| `WORDPRESS_DB_TLS` | `off` | `required` (from `${{ db.tls }}`) encrypts the MySQL connection |
| `WORDPRESS_<AUTH_KEY...NONCE_SALT>` | required | Eight secrets, see above |
| `WORDPRESS_TABLE_PREFIX` | `wp_` | Table prefix (use `wp_` for the migrated site) |
| `WP_URL` | required for install | Public URL; also pins `WP_HOME` and `WP_SITEURL` |
| `WP_ADMIN_EMAIL` | required for install | Admin email |
| `WP_ADMIN_USER` / `WP_TITLE` | `admin` / `WordPress` | Used by the first-boot install |
| `WP_PAGE_CACHE` | `false` | nginx full-page cache in `/tmp` for anonymous GETs (60 s) |
| `WP_AUTOINDEX_PATHS` | empty | Comma-separated static folders outside WordPress that get a directory listing, e.g. `/kab/,/files/`. Files are served as-is; PHP and dotfiles there are refused; `/wp-*` paths are ignored |
| `WP_ALLOW_XMLRPC` | `false` | `true` allows `/xmlrpc.php` (Jetpack needs it) |
| `WP_CRON_ENABLED` / `WP_CRON_INTERVAL` | `true` / `300` | CLI wp-cron loop (page-load cron is off) |
| `WP_CONFIG_MANAGED` | `true` | Volume mode: rewrite `wp-config.php` on each boot |
| `WP_RESTORE` | unset | `1` = one-time restore from the bucket (volume required, see MIGRATION.md) |
| `WP_RESTORE_S3_ENDPOINT` / `_REGION` / `_BUCKET` / `_ACCESS_KEY` / `_SECRET_KEY` | | Map from `${{ storage.* }}` |
| `WP_RESTORE_DB_KEY` / `_CONTENT_KEY` / `_EXTRA_KEY` | `restore/db.sql.gz` / `restore/wp-content.tar.gz` / `restore/extra.tar.gz` | Object keys of the bundle (extra is optional) |
| `WP_RESTORE_DEACTIVATE` | unset | Comma list of plugin slugs to deactivate after the restore (plugins that fatal on current PHP) |
| `WORDPRESS_DB_CA_CERT` | unset | `${{ db.ca_cert }}`; lets the restore's mariadb client verify the DB certificate |
| `WP_REMOVE_PLUGINS` | unset | Comma list of plugin slugs (`[a-z0-9-]+`) to deactivate and delete from `wp-content/plugins` on boot. Must-use plugins are never touched. Idempotent |
| `WP_CLEAR_LOGIN_LOCKOUTS` | unset | `1` clears Limit Login Attempts Reloaded lockouts and retry counters on boot |
| `WP_RESET_PASSWORD_USER` / `WP_RESET_PASSWORD_NONCE` | unset | Login, email or ID of a user whose password is reset once: a random password is generated in the container and printed once to the app logs as `WORDPRESS PASSWORD RESET for <login> (shown once, change it after login): ...`. Restarts do not reset again; change the nonce (any string) to reset again. Use when mail (lost-password email) is not set up |
| `WORDPRESS_AUTOMATIC_UPDATES` | `false` | `true` re-enables background core/plugin/theme updates (off by default; a restart during one leaves a maintenance flag) |
| `WORDPRESS_AUTO_UPDATE_CORE` | `false` | Core updates come from new images |
| `WORDPRESS_DEBUG` | `false` | WP_DEBUG (logged, never displayed) |

Mail: SMTP ports are blocked; use a mail plugin that calls a provider's HTTPS API.
Health check: `/healthz` (nginx + php-fpm, no database) or `/healthz-static` (nginx only).

## Build and local test

```bash
docker build -t wp-picopaas:test examples/wordpress
docker network create wpn
docker run -d --name wpdb --network wpn -e MYSQL_ROOT_PASSWORD=not-the-password -e MYSQL_DATABASE=wordpress mysql:8.4
# salts -> salts.env (see above, one VAR=value per line)
docker run --rm --name wpt --network wpn -p 8080:8080 --env-file salts.env \
  --read-only --tmpfs /tmp --tmpfs /usr/src/wordpress/wp-content/uploads:uid=10001 \
  --user 10001 --cap-drop ALL --security-opt no-new-privileges \
  -e WORDPRESS_DB_HOST=wpdb -e WORDPRESS_DB_PORT=3306 -e WORDPRESS_DB_NAME=wordpress \
  -e WORDPRESS_DB_USER=root -e WORDPRESS_DB_PASSWORD=not-the-password -e WORDPRESS_DB_TLS=required \
  -e WP_URL=http://localhost:8080 -e WP_ADMIN_EMAIL=you@example.com wp-picopaas:test
```

Watch the log for the admin password line. Volume mode: add `--tmpfs /var/www/html:uid=10001,gid=10001,size=250m`.
