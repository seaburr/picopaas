# Moving a WordPress site onto PicoPaaS

Generic procedure for the image in this folder: app = this image + a volume at `/var/www/html` + managed MySQL + the app's
own bucket (used only as a staging area for the restore). Instance-specific details are kept out of this file.

## 0. What you need
- Shell access to the old server (or its backup): the site directory, a database dump, and the old `wp-config.php` (for the
  table prefix; do **not** carry its DB credentials or salts over).
- A PicoPaaS account allowed a volume, a database and a bucket. Ask the platform admin to raise limits (section 3).
- Treat the bundle as private data: it holds users, comment authors, and often secrets in `wp_options`.

## 1. Export from the old server
1. Note: WordPress version, PHP version, table prefix (`$table_prefix`), whether it is multisite (this procedure is single-site only),
   active theme and plugins (`wp plugin list --status=active` or the `active_plugins` option), `siteurl`/`home`.
2. Dump the database consistently: `mysqldump --single-transaction --no-tablespaces --set-gtid-purged=OFF --column-statistics=0 DBNAME | gzip > db.sql.gz`.
   Run it with the password in `MYSQL_PWD` or a `--defaults-extra-file`, never on the command line.
3. Tar only what WordPress needs: `tar -czf wp-content.tar.gz wp-content` (exclude cache dirs, backup plugin output, `upgrade*`,
   `*.log`). Keep core, `wp-config.php` and `.htaccess` out: the image brings its own core and a secret-free `wp-config.php`.
4. Anything served outside WordPress (download folders, static pages) goes into an optional `extra.tar.gz` that is extracted into the
   WordPress root (keep it small).

## 2. Prepare the dump
Strip what managed MySQL cannot or should not run, and large data you do not need:
- `SET @@GLOBAL.GTID_PURGED=...` (can span several lines) and `SET @@SESSION.SQL_LOG_BIN` / `@MYSQLDUMP_TEMP_LOG_BIN`: need SUPER or
  fail on managed servers. `--set-gtid-purged=OFF` avoids them at dump time; otherwise delete the statement through its closing `';`.
- `DEFINER=...` clauses (views, triggers, routines) that name a user that does not exist on the new server.
- Log-style plugin data: audit-log tables (WP Activity Log: `*_wsal_*`), security-scanner traffic logs, Jetpack sync queues, action
  scheduler logs. They are often 90%+ of the dump and are not the site. Keep the `CREATE TABLE` statements and drop their `INSERT` lines
  so the plugin does not break. Transients are removed by the restore itself.
- Check every table has a primary key (managed MySQL enforces `sql_require_primary_key`).
Verify: `gzip -dc db.sql.gz | grep -n -E 'GTID_PURGED|SQL_LOG_BIN|DEFINER'` shows nothing but comments.

## 3. Size it, and ask for overrides
| Resource | Rule of thumb | Default / cap | Ask admin for |
|---|---|---|---|
| Volume | WordPress core (about 70 MB) + `wp-content` + growth. Delete unused default themes first | 1 GiB for users, $0.10/GiB/month | more only if wp-content is large |
| Bucket | compressed bundle, temporarily (delete it afterwards) | 100 MB | raise `storage_quota_mb` above the bundle size |
| Database | imported data after stripping logs | 50 MB | raise `db_quota_mb` only if still larger |
| CPU | the restore and `search-replace` are CPU heavy; admin pages too | 200m | 500m |
| RAM | 4 PHP workers, about 200 MiB steady | 384 MB | usually none |
| Replicas | volumes are single-attach | 1 | none |

## 4. Deploy the empty app, then upload the bundle
1. Deploy the vanilla app from README.md with `resources: { db: true, storage: true }`, a volume `{size_gi, mount_path: /var/www/html}`, and
   the eight salt secrets. Let the automatic first install finish (note: it generates an admin you will not use; the restore replaces all
   users with the old site's).
2. Upload the bundle with the CLI (no bucket credentials needed), to the keys `restore/db.sql.gz`, `restore/wp-content.tar.gz`,
   optionally `restore/extra.tar.gz` (or set `WP_RESTORE_*_KEY`):
   `picopaas storage upload myapp ./db.sql.gz --key restore/db.sql.gz` (repeat per file). Each file must fit in the bucket's remaining
   quota (ask an admin to raise `storage_quota_mb` for large bundles).
3. Set `WP_RESTORE: "1"`, `WP_RESTORE_S3_ENDPOINT: ${{ storage.endpoint }}`, `_REGION`, `_BUCKET`, `_ACCESS_KEY`, `_SECRET_KEY` from the
   `${{ storage.* }}` references, `WORDPRESS_DB_CA_CERT: ${{ db.ca_cert }}`, optionally `WP_RESTORE_DEACTIVATE` (section 7), and `WP_URL`
   set to the **new** public URL. Redeploy.

## 5. What `WP_RESTORE=1` does (once)
Refuses to run without a volume at `/var/www/html`, without the S3 variables, or when the marker file `/var/www/html/.picopaas-restored`
exists. Otherwise, with progress lines in the app logs (no secrets): 1 drop all existing tables; 2 stream the dump from the bucket into
MySQL over TLS; 3 replace `wp-content` from the tarball (streamed, nothing staged on scratch); 4 read the old `siteurl` from the dump and
run `wp search-replace OLD NEW --all-tables --skip-columns=guid` (https, then the http variant; serialized data is handled by WP-CLI);
5 deactivate listed plugins, delete transients, flush caches; 6 write the marker. During the restore the site answers 503 (except `/healthz`).
On any failure the marker is not written, the site stays 503 with the reason in the logs, and the next start retries from step 1.
Restarts afterwards skip the restore entirely.

## 6. Verify (no logins needed)
- `/` shows the title, theme and posts; a few `/?p=ID` URLs and pretty permalinks return 200; theme CSS/JS and an uploaded image return 200.
- No `PHP Fatal` lines in the app logs; `/wp-login.php` is 200; `/wp-admin/install.php` is 403; `/xmlrpc.php` is 403 unless you enabled it.
- No leftover references to the old host in the home page source.
- Restart the app: logs show `restore: marker ... exists, skipping`, uploads still served (volume persistence).
- Then log in as yourself, check Settings, Permalinks, plugins, comments; set the admin password if you reset it.

## 7. Plugin compatibility on current PHP
Old plugins and themes written for PHP 7 can fatal on 8.x (removed functions such as `create_function`, `each`, dynamic property use is only a
deprecation). Find them in the logs after the restore. Deactivate the fatal ones with `WP_RESTORE_DEACTIVATE=slug1,slug2` (also works later:
WP-CLI with `--skip-plugins`). Do not edit plugin or theme files in this repo; replace or drop the plugin.

## 8. Rollback
Nothing touches the old server. To redo: stop the app, delete the marker (`rm /var/www/html/.picopaas-restored`, needs a shell on the volume)
or simply delete the app (the volume, DB and bucket go with it) and start again. To go back to the old site, leave DNS pointing at it until you
have verified everything; lower the TTL a day before cutover.

## 9. Cleanup
- Delete the bundle objects from the bucket as soon as the restore succeeds (`restore/*`): they contain user data.
- Remove local copies of the bundle; keep the original backup offline.
- Remove `WP_RESTORE` and the `WP_RESTORE_*` variables from the spec (the marker already prevents a re-run), and lower any temporary overrides.
- Set up backups (`docs/research/wordpress-backups.md`) before relying on the new site.

## Rehearsal log (throwaway test, generic findings)
- Local rehearsal with MySQL 8.4 (TLS), an S3-compatible stand-in that validates SigV4 (rclone), read-only rootfs, uid 10001, caps dropped.
- `curl --aws-sigv4` works for streaming GETs from S3-compatible stores; send `x-amz-content-sha256: UNSIGNED-PAYLOAD`.
- Alpine's mariadb client 11.x verifies server certificates by default and needs `mariadb-connector-c` for MySQL 8's `caching_sha2_password`; both handled.
- A multi-line `GTID_PURGED` statement survives a naive line filter and breaks the import at the first continuation line: strip to the closing `';`.
- Pipelines hide failures in POSIX sh: the entrypoint checks that `siteurl` exists after import and that the tarball produced a themes directory.
- A PHP 7-era security plugin fataled on PHP 8.5 (`create_function()` removed); everything else (theme, Jetpack, other plugins) rendered with no PHP errors.
- Restore of a 0.3 MB (compressed) dump and a 69 MB tarball took well under two minutes locally; the volume held about 235 MB.

## Rehearsal log, live run on a throwaway PicoPaaS app
- Spec: volume 1 GiB at `/var/www/html`, managed MySQL, bucket quota raised from 100 to 150 MB, CPU 500m, image pinned by tag. Bundle uploaded with the platform's presigned
  PUT (`picopaas storage upload`): 65 MB in about 10 s.
- Restore (`WP_RESTORE=1`): all six steps completed on the first run in about two minutes after the redeploy; the old URL was found from the dump and 48 replacements were made.
  Measured resource peaks were small (a few tens of millicores and about 60 MiB sampled once a minute).
- Public checks passed: home page title and posts, `?p=` URLs, theme and plugin assets, uploaded images and an extra static download all 200; `wp-login.php` 200;
  `install.php`, `setup-config.php`, `xmlrpc.php` 403; no PHP fatals after the one known-bad plugin was deactivated.
- Restart: the log showed `marker ... exists, skipping`; uploads were still served from the volume.
- Gotcha: a restart while WordPress was running a background plugin auto-update left `.maintenance` and visitors saw "Briefly unavailable for scheduled maintenance" for
  up to ten minutes (WordPress ignores the flag after that). The image now disables background updaters by default (`WORDPRESS_AUTOMATIC_UPDATES=true` re-enables them) and
  removes a stale `.maintenance` at boot.
- Presigned upload size cap: a body of 20 bytes sent against a URL signed for 10 bytes was rejected by Spaces with HTTP 403 (SignatureDoesNotMatch).
- Cleanup performed: bundle objects deleted from the bucket (a presigned GET then returned 404), restore variables removed from the spec, local copies deleted.
