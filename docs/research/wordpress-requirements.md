# WordPress on PicoPaaS: site inventory, runtime design, platform requirements

Date: 2026-10-23. Author: wp-expert. Supersedes the "no volume, S3 offload" recommendation in `wordpress.md`:
Chas wants the WordPress install and all uploads on a persistent block volume. Nothing was deployed or provisioned.
The backup at `~/Desktop/sitedump/` was read, never copied; secrets in it (DB password, salts, hashes, commenter emails,
certs) are noted only as "present".

## A. Site inventory (no secrets)

| Item | Finding |
|---|---|
| Site | Single site, **not multisite**, table prefix `wp_`, home and siteurl both `https://chasberndt.com` (no www redirect in WP; www is an alias in Apache) |
| WordPress | **6.8.2** (db_version 60421). Current stable is **7.1.3**, so the move includes a core upgrade (7.x needs PHP 7.4+, recommends 8.3+) |
| Old server | CentOS 8 VM, 1.8 GB RAM, 1 vCPU. **PHP 7.2.24** (EOL since 2020, php-fpm via Apache proxy_fcgi), Apache 2.4.37, MySQL client 8.0.21. DB was already a **DigitalOcean managed MySQL 8.0.45**. Docroot `/mnt/data/web` (a separate data volume) |
| PHP extensions used | gd, json, mbstring, mysqlnd, pdo, pecl-zip, xml (ImageMagick CLI installed, no imagick extension). New image adds mysqli, exif, intl, bcmath |
| Theme | Active: **casper 1.1.5** (a Ghost-style theme, 1.3 MB). Also installed: twentytwenty, 21, 22, 23, 24, 25 (about 28 MB of unused default themes) |
| Active plugins (6) | akismet 5.7.2, code-prettify 1.5.2, **jetpack 15.8.1 (69 MB, heavy)**, limit-login-attempts-reloaded 3.3.10, **wp-security-audit-log 5.2.2 (WP Activity Log)**, wp-security-scan 4.0.5. No caching plugin, no mu-plugins, no object cache |
| Sizes | Whole site 199 MB. `wp-content` 134 MB: plugins 89 MB, themes 29 MB, **uploads 16 MB (101 files, years 2015-2026, largest 2.1 MB)**. `wp-includes` 54 MB, `wp-admin` 10 MB |
| Extra top-level dirs | `error-pages/` (403/404/500 static pages, used by Apache ErrorDocument) and `kab/` (64 KB, 5 versions of a Windows `KillAllBindings.exe` download). Both are plain static files outside WordPress and must be served by nginx from the root |
| DB dump | 54.6 MB `mysqldump` 8.0.21 of 19 tables, InnoDB, every table has a primary key (OK for `sql_require_primary_key`). Contains a `SET @@GLOBAL.GTID_PURGED` line that must be stripped for managed MySQL. Two tables are utf8mb4, rest utf8mb3 |
| Table bloat | **97% of the dump is one plugin's audit log**: `wp_wsal_metadata` 42.1 MB (775,478 rows) and `wp_wsal_occurrences` 10.8 MB (128,622 rows). Everything else together is about 1.7 MB: `wp_options` 0.95 MB (5,777 rows, **5,460 are transients**, only about 80 KB autoloaded, biggest rows are `limit_login_logged` 87 KB and Jetpack data), `wp_posts` 355 rows (82 posts, 2 pages, 20 attachments, 170 revisions, 73 private, 5 drafts), 9 comments (8 spam, 1 approved), 1 user. Plugin tables: `wp__wsd_plugin_*` (WP Security Scan; live traffic log), `wp_jetpack_sync_queue` |
| Permalinks | `permalink_structure` is **empty: plain `?p=123` links**. The `.htaccess` holds only an empty "BEGIN/END WordPress" block, so **no rewrite rules need porting** (nginx still gets the standard `try_files` fallback so pretty links work if he enables them). Apache only did http-to-https and www aliasing, which Traefik/the platform handle |
| Cron | 34 scheduled hooks, from Jetpack (sync, WAF rules update, stats cleanup), WP Activity Log (reports, pruning), WP Security Scan, limit-login digests, core update checks. Server crond only ran the nightly `backup-wordpress` script. No server-side wp-cron call, so WP ran page-load cron |
| Mail | No MTA or SMTP plugin found; `fail2ban-sendmail` package only. Comment/notification mail probably went through local sendmail. Needs an HTTPS-API mailer on PicoPaaS |
| Security layers on old host | mod_security (WAF) on Apache and fail2ban. Neither ports; nginx rules plus limit-login-attempts cover the basics |
| Writes outside wp-content | `jetpack-waf` (inside wp-content). Jetpack WAF wants `auto_prepend_file`, which the old site did not configure under PHP-FPM (nothing found); skip it. `.htaccess` is not used. `FS_METHOD=direct`, `WP_AUTO_UPDATE_CORE=minor`, MySQL SSL flag already set in `wp-config.php` |
| exportscript | `backup-wordpress`: a root cron.daily shell script that tars the site, Apache config, Let's Encrypt certs, server specs and a `mysqldump` into `/mnt/data/backups/sitedump.tar.gz` (no remote copy visible). It is the whole backup story today, so the new platform needs its own (REQ-9) |
| Jetpack | Connected to WordPress.com (plan data present in options). Jetpack needs inbound `/xmlrpc.php` and outbound HTTPS to `*.wordpress.com`/`jetpack.com`. It is also the largest CPU/RAM/disk consumer. Recommend dropping it unless he uses a specific module (stats, related posts) |

Migration takeaways: the real content is tiny (about 2 MB of DB, 16 MB uploads). With the audit-log tables left out, the DB
fits the **default 50 MB cap**; with them it needs about 100 MB. Recommend importing without `wp_wsal_*` and
`wp__wsd_plugin_live_traffic`, and truncating the 5,460 transients.

## B. Target runtime design

### Versions (checked 2026-10-23)

- **PHP vulnerability:** CVE-2026-109618 (memory corruption on out-of-memory handling) and CVE-2026-109619 (use-after-free of
  state kept between requests), CVSS v4 9.2 each, **unauthenticated remote code execution** in PHP core; public exploits
  exist and exploitation attempts were reported. Out-of-band PHP release on **21 Oct 2026**. Affects PHP 5.4 through 8.5
  (web SAPIs incl. PHP-FPM; mod_php most exposed). **Fixed: 8.5.12, 8.4.27, 8.3.36, 8.2.35.** 8.1 and older get no routine fixes
  (the old server's PHP 7.2 is unpatched). WordPress.org says no WordPress update is needed. Interim mitigations
  (`session.upload_progress.enabled=0`, `fatal_error_backtraces=0`) are applied in the shipped config as defense in depth.
  Sources: Rapid7, Qualys, Google Cloud and watchTowr write-ups dated 20-22 Oct 2026.
- **Pinned:** `php:8.5.12-fpm-alpine` (Alpine 3.24, published to Docker Hub 2026-10-22), index digest
  `sha256:f09d9172dd28b8696aef729ca2c2081a132c1f8d581218c99497d552c82a92df`. Same-day tags exist for `8.4.27-fpm-alpine`
  (`sha256:6b83fd76...`) and `8.3.36-fpm-alpine` (`sha256:aaf1ad25...`) if a plugin breaks on 8.5.
- **WordPress:** 7.1.3 stable (SHA-1 `491a78c7...` from wordpress.org). Supports PHP 7.4 to 8.5. Plugin compatibility is the
  risk on 8.5, not core; test his six plugins (wp-security-scan 4.0.5 is old).
- **nginx:** latest stable line is nginx-unprivileged **1.30.6** (alpine3.24); mainline is 1.31.7. The image uses Alpine's
  own `nginx` package (same Alpine 3.24 base, no second image needed), run unprivileged. Switch to the nginxinc binary only
  if a pinned upstream version is required.

### Container layout: one container (works today)

Two containers in one pod (nginx and php-fpm as separate containers sharing a socket or localhost) is the cleaner
Kubernetes pattern but **needs a platform change**: PicoPaaS renders exactly one container per app
(`app/k8s_manifests.py`: a single `containers[0]` named `app`, one resource set and one image). A shared-volume sidecar would
also need emptyDir plumbing. So the example runs **both processes in one container**: `tini` (PID 1) supervises a small
POSIX shell entrypoint that starts php-fpm, nginx and the wp-cron loop, and exits (so the platform restarts the pod) if
nginx or php-fpm dies. It is the same pattern the Ghost recipe uses. Memory and CPU limits are shared by all three, which is
what we want at 384 MB.

- **UID/GID:** 10001:10001 (numeric `USER`, passwd/group entries exist so PHP and Node-style lookups work). The platform
  already forces `runAsNonRoot`, `runAsUser` 10001, caps dropped, no privilege escalation, seccomp RuntimeDefault.
- **Port:** 8080 (set `port: 8080`).
- **Read-only rootfs:** supported. The entrypoint writes only to `/var/www/html` and `/tmp`. nginx temp paths, pid and logs
  (stderr/stdout), the php-fpm socket and pid, sessions, opcache file cache and the page cache are all under `/tmp`. The
  platform does not set `readOnlyRootFilesystem` today; if it ever does, `/tmp` and the volume are the only writable mounts needed.
- **Volume mount:** `/var/www/html` is the **whole WordPress install** (core, wp-content, uploads, wp-config.php), as on the
  old VM. On first boot into an empty volume the entrypoint copies the seed from `/usr/src/wordpress` (core 7.1.3 checksum-
  verified at build). Later boots keep the volume; if the image carries a newer WordPress than the volume, `wp-admin`,
  `wp-includes` and root files are refreshed and `wp-content` is untouched. Without a volume the same code seeds the
  container filesystem each start (about 90 MB of the 250 MB scratch disk). If the directory is not writable the container
  exits with a clear message (the volume must be owned by, or group-writable for, UID/GID 10001: fsGroup).
- **`wp-config.php`:** written by the entrypoint every boot from a secret-free template that reads env vars, so secrets
  never land on the volume or in a backup. Salts: one `WORDPRESS_SECRET` derives all eight, or set the eight individually.
  `DISABLE_WP_CRON`, `DISALLOW_FILE_EDIT`, `FS_METHOD=direct`, temp dir `/tmp`, revisions capped at 10.

### PHP and FPM sizing for 384 MB RAM, 200m CPU (default)

| Setting | Value | Reason |
|---|---|---|
| `pm` | `ondemand`, `max_children=4`, idle timeout 20 s, `max_requests=400` | A WP worker is 40-60 MB; 4 workers plus nginx (about 10 MB), opcache (64 MB) and an occasional wp-cron CLI fit in 384 MB. Idle footprint is small. Also keeps DB connections far under the 10 per app cap. Measured: **about 197 MiB** after an install, one page and a cron run |
| `memory_limit` | 128M web, 192M for the cron CLI | Do not set `-1`: that mitigation sentence in the PHP advisories is a stop-gap and with a cgroup limit one runaway can OOM-kill the master. Patched PHP is the fix |
| opcache | 64 MB, 8 MB interned strings, 10,000 files, `validate_timestamps=1` with `revalidate_freq=60`, JIT off | Dashboard plugin changes on the volume must be noticed, so not the image-immutable `validate_timestamps=0`. JIT off saves RAM and CPU |
| uploads | `upload_max_filesize=24M`, `post_max_size=32M`, nginx `client_max_body_size 32m`, `max_input_vars=3000` | The advisories recommend keeping `post_max_size` small; his largest photo is 2 MB |
| security | `expose_php=Off`, `display_errors=Off`, errors to stderr, `session.upload_progress.enabled=0`, `fatal_error_backtraces=Off`, `zend.exception_ignore_args=On` | Mitigations for the October CVEs plus hygiene |

Overrides needed: **CPU limit 500m** (admin override; Ghost precedent) for admin pages, image resizing and cache rebuilds.
RAM 384 MB should hold; fall back to 512 MB if an image-heavy import OOMs. Check `DEFAULT_*` and `OVERRIDE_*` in `app/config.py`: both are already within range.

### nginx (file `examples/wordpress/nginx.conf`)

- Permalinks: `try_files $uri $uri/ /index.php?$args` (the standard WordPress fallback; his site uses plain `?p=` links so there were no `.htaccess` rules to port).
- PHP only for real `.php` files (`try_files $uri =404`), `PATH_INFO` handled, `HTTP_PROXY` cleared.
- Deny: `/xmlrpc.php` (unless `WP_ALLOW_XMLRPC=true`, needed by Jetpack), dotfiles, `wp-config.php`, `readme.html`, `license.txt`,
  any `.php`/`.phtml`/`.phar` under `wp-content/uploads`, `upgrade*` and `jetpack-waf`, `*.log/.sql/.bak/.zip` in wp-content, direct hits on `wp-includes/*.php`.
- Static assets: 30-day `Cache-Control: public`, gzip for text types, `server_tokens off`, `nosniff`.
- Optional full-page cache (`WP_PAGE_CACHE=true`): fastcgi cache in `/tmp` (40 MB), 60 s, bypassed for non-GET, any query string,
  `wordpress_logged_in`/`wp-postpass`/`comment_author` cookies, `wp-admin`, `wp-login`, `wp-json`, `xmlrpc`. Verified `MISS` then `HIT`.
- HTTPS: `X-Forwarded-Proto: https` from Traefik sets `HTTPS=on` for PHP (nginx map and again in `wp-config.php`); `WP_HOME`/`WP_SITEURL` from `${{ app.url }}`. Tested: the install page redirected to the `https://` host with no loop.
- Client IP: `X-Forwarded-For` trusted only from private ranges (`set_real_ip_from`). **Verify behind the DO load balancer** that the real visitor IP arrives, or Limit Login Attempts can lock out everyone.
- Health: `/healthz` (php-fpm `ping`, no DB, no WP load) and `/healthz-static` (nginx only). Use `/healthz` with `timeoutSeconds: 10`.

### wp-cron, mail, egress

- Cron: page-load cron disabled; the entrypoint runs `php wp-cron.php` from the CLI every 300 s (first run after 60 s). No HTTP loopback, no extra pod.
- Mail: SMTP (25/465/587) is blocked on purpose. Use an HTTPS-API mailer plugin (Brevo, Postmark, Mailgun, SendGrid API). Needs provider account and API key as a secret; no platform change.
- Egress: TCP 443 is open (api.wordpress.org updates, plugin/theme downloads, Gravatar, Akismet, Jetpack/WordPress.com). DB egress policy already exists. **No egress change needed.**

## C. Platform requirements for the platform engineering team

Priority key: **V** = needed for the vanilla milestone, **M** = needed to migrate Chas's site, **N** = nice to have.

**The vanilla milestone needs no platform change** (REQ-0 is just a check): the example image runs without a volume today,
seeded from the image, with MySQL for content. Vanilla on a volume (first-class WordPress admin experience) needs REQ-1 to REQ-5.

### REQ-0 (V): confirm the image and DB path with no platform change
Deploy `examples/wordpress` on a throwaway app with `db: true`; get to the installer, install, log in, restart, confirm DB content survives and uploads do not. Acceptance: image passes `check-image` (compressed 119 MB measured, under the 512 MB cap); `/healthz` ready within the probe window; install completes under `sql_require_primary_key`. Admin sets CPU to 500m.

### REQ-1 (V for the volume milestone): persistent volume per app (`resources.volume`)
- What: opt-in `resources.volume: { sizeGi: N, mountPath: /var/www/html }`, one `ReadWriteOnce` PVC on `do-block-storage`, mounted in the app container.
- Why: Chas wants core, plugins, themes and uploads on a persistent volume, like his old VM.
- Acceptance: files written to the mount survive pod restart, redeploy and node drain; mount path configurable (validated absolute, not `/`, `/tmp` or system paths, unique); visible in the app page, `export` output and API; spec round-trips.
- Size and cost: default 2 GiB, minimum 1 GiB (the DO minimum), **$0.10 per GiB per month, billed while the volume exists even if detached**, so 2 GiB is $0.20/mo; snapshots $0.06/GiB/mo. Grow-only (DO cannot shrink). His site needs about 0.2 GiB now (65 MB core + 134 MB wp-content, trimmed by deleting unused themes and Jetpack), so 1 GiB works and 2 GiB leaves headroom.

### REQ-2 (V for volume): ownership and permissions
- What: pod `fsGroup: 10001` (and `fsGroupChangePolicy: OnRootMismatch` to avoid slow recursive chowns) so the non-root UID can write the new volume.
- Acceptance: a fresh volume is writable by UID 10001 with no root init container; files keep working after reattach on another node.

### REQ-3 (V for volume): `Recreate` strategy and single replica for volume apps
- What: when a volume exists, Deployment `strategy: Recreate` and replicas pinned to 1 (reject larger values, including admin `replicas`). RWO volumes deadlock a rolling surge.
- Acceptance: a deploy and an image-tag update replace the pod with a gap of at most about a minute and no stuck `Multi-Attach` state; the quota logic that admits (replicas + 1) pods still works (no surge pod).
- Note: every deploy, restart and node drain is a short outage; the page cache and, optionally, Cloudflare hide it for readers.

### REQ-4 (V for volume): delete with the app, quota and admin override
- What: volume goes with the app (reclaim `Delete`, or a delete-on-app-delete job), plus a per-account/per-app size cap and platform-admin override (`volume_size_gi` in `PATCH /admin/apps/{id}/resources`, with min/max constants next to the other `OVERRIDE_*` ones, suggested 1 to 50 GiB), namespace `ResourceQuota` on `requests.storage` and PVC count, and surfaced usage (bytes used from `statfs`) in the health/quota views.
- Acceptance: deleting the app removes the PVC and the DO volume (no orphans, verified in the DO console); a request above the cap is a 422; growing is allowed, shrinking is rejected with a clear message; usage appears in the UI.

### REQ-5 (V for volume): request file and budget
- What: a `docs/requests/` entry for block storage before building (rule in CLAUDE.md); cost formula $0.10/GiB/mo per volume plus snapshots; confirm the node attach limit (15 per node, ROADMAP) against pods per node.
- Acceptance: approved request exists; ledger and `docs/ARCHITECTURE.md` cost model updated.

### REQ-6 (M): admin override of DB size for the import
- What: none new in code: the DB quota override already exists (`OVERRIDE_DB_QUOTA_MIN_MB`..`MAX_MB` up to 1024). Action: raise this app to **100 MB** only if the audit-log tables are imported; default 50 MB suffices without them. DB connections: leave at 10 (4 FPM workers).
- Acceptance: import completes, quota poll does not flip the DB read-only.

### REQ-7 (M): one-off job or exec to import a DB dump and run `wp` commands
- What: a way to run a command with the app's environment and DB credentials inside the cluster (the DB is only reachable from inside the VPC): `picopaas run <app> -- <cmd>` as a short-lived Job from the app's image and env (already in ROADMAP "Backlog"), or `exec` into the running pod. For WordPress: `mysql` client import of the 54 MB dump, then `wp search-replace`. The example image would add `mariadb-client` and `wp-cli` (about 15 MB); decide with this REQ.
- Why: there is no DB shell or import endpoint today.
- Acceptance: a Job inheriting env/secrets, egress policy and DB access runs to completion with logs retrievable, resource-capped, time-limited, and mounting the app's volume when set (same RWO volume: the Job must run while the app is scaled to zero or on the same node; define which).

### REQ-8 (M): get files into the volume (migration upload)
- What: a supported way to put the ~200 MB site (or just `wp-content`, 134 MB) onto the volume. Options in order of effort: (a) REQ-7 job that downloads a tarball from a presigned URL/Spaces bucket and extracts it onto the volume; (b) `picopaas cp` streaming upload/download via `kubectl exec tar`; (c) a temporary web upload from inside WordPress (needs admin plugin import such as All-in-One WP Migration, limited by `upload_max_filesize` 24M so no).
- Acceptance: `wp-content/uploads` (101 files, 16 MB) and the selected plugins/themes arrive with correct ownership (10001), verified by checksum. Prefer (a): reuses the 100 MB-capped (admin-raisable) bucket as a staging area; remove the staging object after.

### REQ-9 (M): backups of volume and database together
- What: scheduled volume snapshots (DO CSI `VolumeSnapshot`, $0.06/GiB/mo) with retention (e.g. 7 daily), a restore procedure, and a per-app MySQL export that lands in the app's bucket or an admin-visible location (managed MySQL backups restore the whole cluster only). The old site had a nightly tarball script; parity is the goal.
- Acceptance: documented restore tested on a throwaway app: volume from snapshot plus DB from export reproduces the site.

### REQ-10 (M): inbound XML-RPC / Jetpack and client IP
- What: if Jetpack is kept, `WP_ALLOW_XMLRPC=true` works as is (no platform change) but needs the real client IP for rate limiting: confirm PROXY protocol or `X-Forwarded-For` through the DO load balancer and Traefik. If IP is the LB's, document a limit-login configuration that does not lock out all visitors.
- Acceptance: a test request shows the visitor IP in the nginx access log and in `REMOTE_ADDR` for PHP.

### REQ-11 (M): custom domain for chasberndt.com
- What: existing custom domain feature (CNAME + TXT check + HTTP-01). Set `WORDPRESS_HOME=https://chasberndt.com` at cutover; lower DNS TTL a day before; handle `www` (apex plus `www` alias on the old Apache). The apex needs an ALIAS/flattened CNAME at his DNS host, which may be a platform limitation to confirm.
- Acceptance: apex and `www` serve the site over HTTPS; `www` redirects to the apex (nginx redirect or DNS-level).

### REQ-12 (N): memory and CPU headroom for admin work
- What: allow a temporary per-app memory override to 512 MB for imports and image regeneration (precedent n8n), and CPU 500m. Both are within existing override ranges.
- Acceptance: documented in the recipe; node sizing counts WordPress as a "heavy" app (about 200 MB steady, 384 MB cap).

### REQ-13 (N): two-container pods
- What: optional support for a second container (sidecar) per app, to run nginx and php-fpm separately with their own limits. Not needed: the single-container image works and is tested.

### REQ-14 (N): scheduled jobs per app
- What: platform CronJob (ROADMAP backlog) running `wp-cron.php` or backups, so cron does not depend on the web pod. The in-container loop is sufficient for the blog.

### REQ-15 (N): mail relay or documented HTTPS-API path
- What: a recipe entry for HTTPS-API mailers (already listed in the guide), or an allowlisted relay. No platform change required.

### Summary table

| REQ | Vanilla (no volume) | Vanilla on volume | Migration |
|---|---|---|---|
| 0 smoke test | yes | yes | yes |
| 1 volume | | yes | yes |
| 2 fsGroup | | yes | yes |
| 3 Recreate, 1 replica | | yes | yes |
| 4 delete, quota, override | | yes | yes |
| 5 request and budget | | yes | yes |
| 6 DB override | | | yes (only with audit log tables) |
| 7 one-off job or exec | | | yes |
| 8 file upload to volume | | | yes |
| 9 backups | | | yes |
| 10 client IP, XML-RPC | | | yes (if Jetpack) |
| 11 custom domain | | | yes |
| 12-15 | | | nice |

## D. Migration plan (later)

1. Prep locally (outside the repo): copy the dump with `wp_wsal_*`, `wp__wsd_plugin_live_traffic` and transients excluded, strip `GTID_PURGED`. Drop unused default themes and decide on Jetpack. 2. Deploy the vanilla app with the volume, DB and a throwaway hostname. 3. Import the DB (REQ-7), upload `wp-content/uploads`, `themes/casper` and the kept plugins (REQ-8), plus `error-pages/` and `kab/` as static roots (nginx location or copy into the web root; note `kab` is a binary download area). 4. `wp search-replace https://chasberndt.com <throwaway-url>` for testing, then back at cutover (or set `WORDPRESS_HOME` and keep the DB URLs). 5. Test plugins on PHP 8.5 (use 8.4.27 if needed), then DNS cutover (REQ-11) and keep the old host read-only for a week. WordPress 6.8.2 to 7.1.3 runs its DB upgrade routine on first admin login; take the REQ-9 backup first.

## E. Open items to verify on the platform
Compressed image size via `check-image` (119 MB measured locally with `docker save | gzip`, uncompressed 486 MB); behaviour under the real pod securityContext and ephemeral-storage limit; `/healthz` within the liveness window on a cold, CPU-throttled start; client IP behind the LB; MySQL TLS against the managed cluster; page cache behaviour with the casper theme; Jetpack on a non-public hostname.

## F. Update 2026-10-23 (hardening pass)

- First boot is automatic and locked down: `wp core install` via WP-CLI 2.12.0 (checksum-verified) from `WP_URL`, `WP_ADMIN_EMAIL`, `WP_TITLE`, `WP_ADMIN_USER`; random admin password printed once to the app logs; `/wp-admin/install.php` and `setup-config.php` always 403; 503 "not configured" if not installed and URL/email missing. `WP_URL` must be mapped from `${{ app.url }}` because PicoPaaS injects only `PORT`.
- Salts are eight required secrets; the container exits with a clear log line if any is missing (no derived or random fallback).
- No volume: core is served from the image path (`/usr/src/wordpress`), so nothing big is copied to scratch (cap 250 MB). With a volume mounted at `/var/www/html` the entrypoint seeds it. This supersedes the "seeded from image each start" wording in section B.
- DB TLS: `MYSQL_CLIENT_FLAGS = MYSQLI_CLIENT_SSL` when `${{ db.tls }}` is `required`; the platform's `${{ db.ca_cert }}` cannot be used by core WP (no hook), so the server cert is not verified. A `db.php` drop-in could add verification later.
- PHP is tag-pinned (`php:8.5.12-fpm-alpine`) so the weekly `examples` rebuild picks up base-image patches; PHP/WP/WP-CLI bumps remain manual.
