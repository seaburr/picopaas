# Research: running WordPress on PicoPaaS

Date: 2026-10-23. Author: infra-ops. Research only: nothing built, nothing provisioned or applied.
Sources: `app/config.py`, `app/k8s_manifests.py`, `docs/ROADMAP.md` (persistent volumes), `docs/RECIPES.md`,
`docs/ARCHITECTURE.md`, DigitalOcean volume pricing and limits pages. Items marked **(verify)** are from general
WordPress/Apache knowledge and must be proven on a throwaway app before we promise them.

## Verdict

**Viable today with no platform code changes, as a custom non-root image, one replica, MySQL for content and a
Spaces bucket for media.** Persistent volumes are not required for a blog if every moving part is moved out of
the container filesystem:

- Media goes to the bucket (offload plugin).
- Plugins, themes and core are baked into the image; updates happen by building a new image.
- Content and settings live in MySQL.

The real limits are **CPU** (uncached PHP on 200m), **the 50 MB database and 100 MB bucket** (both raisable per app
by an admin), **no outbound SMTP**, and **an image we must build ourselves** (the official image runs as root).
Volumes (phase 2) would add a normal WordPress admin experience (install plugins from the dashboard, local
uploads), but they bring real platform work and are not needed to prove the platform.

Numbers in the code differ from `bootstrap.md` and `CLAUDE.md`: image cap 512 MB (`MAX_IMAGE_SIZE_BYTES`; the
`CLAUDE.md` line saying 256 MB is stale), default CPU limit 200m (bootstrap says 100m), RAM 384 MB, scratch 250 MB,
DB 50 MB, bucket 100 MB, MySQL connections 10 per app, outbound bandwidth 20 Mbit/s.

## What the platform gives WordPress, and where it collides

| Area | Platform fact | WordPress reality | Result |
|---|---|---|---|
| User | Pod forced `runAsNonRoot`, UID 10001 for named users, all capabilities dropped, no privilege escalation, seccomp RuntimeDefault | Official `wordpress:*-apache` starts Apache as root and drops to `www-data` via setuid | Official image will not start. Wrapper image needed (like the Ghost recipe) |
| Port | Any 1024-65535, `PORT` env set | Apache listens on 80 | Rewrite `ports.conf` and the vhost to 8080 in the wrapper |
| Rootfs | Writable (no `readOnlyRootFilesystem`), but counts against the 250 MB scratch disk, wiped on restart | Core is ~60 MB; the official entrypoint copies it into `/var/www/html` on every boot | Bake core into the image at the final path so nothing is copied at boot |
| Image cap | 512 MB, compressed layers | `wordpress:php8.x-apache` is roughly 250-300 MB compressed **(verify with `check-image`)**, plus plugins/theme | Fits. Alpine/fpm variants are smaller but need a second process (nginx), which means supervisord in a single container; avoid |
| TLS | Traefik terminates TLS, forwards HTTP with `X-Forwarded-Proto` | WP needs to know it is HTTPS or it emits `http://` URLs and redirect-loops | Official `wp-config-docker.php` maps `X-Forwarded-Proto: https` to `$_SERVER['HTTPS']` **(verify)**; also set `WP_HOME` and `WP_SITEURL` explicitly |
| Egress | All TCP except SMTP 25/465/587 and a few abuse ports; DNS, 443 open; reserved/private CIDRs blocked | Updates, plugin/theme downloads, oEmbed, Gravatar, RSS use 443 | Works. Mail does not (see below) |
| Replicas | One Service, stateless-only replicas | Local uploads and plugin files are per-pod | Single replica unless all state is off-pod |
| DB TLS | DO managed MySQL requires TLS | WP connects without TLS by default | Define `MYSQL_CLIENT_FLAGS` as `MYSQLI_CLIENT_SSL` in `WORDPRESS_CONFIG_EXTRA` (encrypts; core WP has no CA-verify option) **(verify)** |
| PK rule | Managed MySQL enforces `sql_require_primary_key=ON` | WP core tables all have primary keys | Core is fine. Some plugins create keyless tables and will fail to activate (this is what killed Wiki.js) |

## What's needed

| # | Change | Why | Effort | Cost |
|---|---|---|---|---|
| 1 | Wrapper Dockerfile: `FROM wordpress:php8.3-apache`, `USER 10001`, Apache `Listen 8080`, `APACHE_RUN_USER=#10001`, writable pid/log/lock dirs for UID 10001, `/etc/passwd` entry, core pre-installed in `/var/www/html` | Root image cannot run under our securityContext | S | none |
| 2 | Apache tuning: `mpm_prefork`, `MaxRequestWorkers 5`, `MaxConnectionsPerChild 500`, `KeepAlive` short | Each mod_php worker with WP is ~40-60 MB; 384 MB RAM holds about 5 plus opcache. Also keeps DB connections <= 10 (no pooling in PHP: one connection per busy worker) | S | none |
| 3 | PHP tuning: `memory_limit=128M` (256M only for admin tasks), `opcache.memory_consumption=48`, `opcache.validate_timestamps=0` (code is immutable in the image), `realpath_cache_size`, `upload_max_filesize` modest | Opcache is the biggest CPU win and its shared memory counts against the 384 MB limit | S | none |
| 4 | Env config via the official `WORDPRESS_*` vars: `WORDPRESS_DB_HOST=${{ db.host }}:${{ db.port }}`, `_DB_NAME/_USER/_PASSWORD` refs, `WORDPRESS_CONFIG_EXTRA` for `WP_HOME`, `WP_SITEURL`, `MYSQL_CLIENT_FLAGS`, `DISABLE_WP_CRON`, `DISALLOW_FILE_MODS`, `WP_AUTO_UPDATE_CORE` | Platform refs fit the official entrypoint's wp-config generator | S | none |
| 5 | Pin the 8 auth keys/salts (`WORDPRESS_AUTH_KEY` etc.) as secrets | If unset, the entrypoint generates new random ones on every container start and everyone is logged out on each restart/deploy | S | none |
| 6 | Real cron: set `DISABLE_WP_CRON=true` and run `php wp-cron.php` from a background loop in the wrapper entrypoint every 5 min | WP-Cron loopback goes to the public URL, which in-cluster hits the load balancer hairpin problem (see docs/deployment/runbooks.md). CLI call avoids HTTP | S | none |
| 7 | Media offload to Spaces: WP Offload Media Lite (supports DigitalOcean Spaces **(verify)**) configured via constants from `${{ storage.* }}`; serve media from the public Spaces URL | Local `wp-content/uploads` is wiped on restart (phase 1: no volume) | M | bucket quota override (see below); no new spend, Spaces base fee already covers 250 GB |
| 8 | Immutable plugins/themes: `COPY` them into the image (or Composer/Bedrock layout) and set `DISALLOW_FILE_MODS=true` | Dashboard installs land on the scratch disk and vanish on restart while the DB still says "active", which breaks the site | M (workflow, not code) | none |
| 9 | Updates by image: pin the tag, rebuild on WP releases; set `WP_AUTO_UPDATE_CORE=false`. The platform's "redeploy when tag changes" option can track a floating tag | Auto-updates write to the filesystem | S | none |
| 10 | Mail via HTTPS API plugin (Brevo/Postmark/SendGrid/Mailgun API mailer), not SMTP | Ports 25/465/587 are blocked on purpose | S | provider free tier |
| 11 | Page cache: static-file cache plugin (WP Super Cache "simple"/mod_rewrite mode, or a plain-file cache) writing to `/tmp`-backed dir, plus optional Cloudflare in front of the custom domain | CPU: uncached WP burns 150-400 ms CPU per page **(measure)**; at 200m that is 1-2 pages/s before throttling. Same lesson as the Ghost post (about 200 ms CPU/page, 57 of 71 periods throttled at 250m) | M | none (Cloudflare free) |
| 12 | Health check on a static path (`/wp-includes/images/blank.gif` or `/wp-login.php`), not `/` | `/` redirects to the canonical URL and WP cold start is 10-30 s | S | none |
| 13 | Hardening: block `xmlrpc.php` in `.htaccess`/Apache conf, disable user enumeration (`/?author=`, REST users), 2FA plugin, strong admin password, a login rate limiter that reads the real client IP | Brute force. Platform rate limits cover only the platform login, not tenant apps | S | none |
| 14 | Verify client IP handling (mod_remoteip reads `X-Forwarded-For` from Traefik) **(verify)** | Behind the DO LB in TCP mode the peer may be the LB, so IP-based lockouts could lock everyone out | S | none |
| 15 | Admin per-app overrides (below) | Blog likely exceeds defaults | S (ops) | $0 (shared resources) |
| 16 | Migration (below) | Move the real blog | M | none |
| 17 | **Phase 2:** persistent volume support (`resources.volume`) | Dashboard plugin/theme installs, local uploads, simpler admin | L (platform) | about $0.10/mo per app for 1 GiB, plus snapshots |

## Persistent volumes: what the ROADMAP plan covers and what WordPress still needs

The plan in `docs/ROADMAP.md` (opt-in `resources.volume`, one RWO DO block volume per app, Recreate strategy, fsGroup,
quota, reclaim Delete, snapshots, pentest) is the right base. Facts checked now: block storage is
**$0.10/GiB/month, 1 GiB minimum, billed while the volume exists even if detached**; snapshots **$0.06/GiB/month**;
DOKS allows **15 volumes per node** (raised from 7 in August 2024). So one WordPress volume is about $0.10/month.

What is missing or sharper for WordPress:

1. **Mount shadows the image.** Mounting a volume at `wp-content` hides the plugins and themes baked into the image.
   Needs either a first-boot seed (entrypoint copies image defaults into an empty volume) or a mount at
   `wp-content/uploads` only. The platform should allow a configurable `mountPath` (and ideally multiple), not a fixed
   `/data`.
2. **Recreate deploy gap.** Every deploy and every node drain is a full outage for a volume app (about a minute).
   Acceptable for a blog, but the page cache and Cloudflare should hide it. Auto-update on tag change would cause
   regular short outages.
3. **Disk accounting.** A volume is outside the 250 MB scratch limit. Needs its own cap (the size) and the admin override
   path (`volume_size_gi`), since growing a DO volume is allowed but shrinking is not.
4. **Backups.** The plan says "a snapshot policy" but nothing exists. WordPress needs DB and files backed up together.
   DO managed MySQL keeps daily backups for the whole cluster only (restoring one tenant DB is manual), so a per-app
   export is needed (see Backups).
5. **Permissions.** `fsGroup` with UID 10001 works; the image still needs correct modes for `wp-content`.
6. **Scheduling.** RWO volume pins the pod's zone/node attach; one node's 15-attach limit is fine for the ~8-12 apps per
   node, but a drain or autoscaler scale-down moves the volume (attach/detach takes about a minute and can stick).
   Single replica only, enforced by the platform (reject replicas > 1 for volume apps).
7. **Platform export/clone.** The CLI `export` command prints a spec only. Nothing moves volume data in or out, so
   migration into the volume needs a one-off pod or an in-app upload (see Migration).

## Limits the blog will likely exceed, and suggested admin overrides

| Limit | Default | Likely need | Suggested override |
|---|---|---|---|
| CPU limit | 200m | Page cache hits are cheap; admin pages, cache rebuilds and image resizing are CPU heavy | 500m (the same bump Chas applied to Ghost: TTFB 0.45-1.4 s to 0.2-0.45 s). Request stays 64m |
| Memory | 384 MB | 5 Apache workers + opcache + WP admin; media upload resizing with GD/Imagick can spike | Try 384 MB with the tuning in row 2-3; if OOM-killed, 512 MB (the n8n precedent) |
| DB quota | 50 MB | Small blog: 5-20 MB. Old blog with revisions, spam comments, plugin tables, transients: 50-200 MB | 256 MB. Run revision/transient cleanup first |
| DB connections | 10 | One per busy Apache worker; admin cron overlaps | Keep `MaxRequestWorkers` <= 8, or raise to 15 |
| Bucket quota | 100 MB | Years of images are commonly 300 MB - 2 GB | 2048 MB (ceiling is 51200 MB) |
| Scratch disk | 250 MB | Page cache + temp uploads + any dashboard-installed code | 512 MB if the page cache lives on disk |
| Image size | 512 MB | Fits | none |
| Bandwidth | 20 Mbit/s | Media from Spaces, not the pod, so fine | none |
| Replicas | 1 | Stay at 1 | none |

## Operational notes

- **Custom domain:** supported (CNAME to the platform LB plus TXT ownership check, HTTP-01 certificate). Set `WP_HOME` and
  `WP_SITEURL` to the final `https://` host and keep the platform hostname as a secondary. If using the platform URL
  during testing, WP will redirect to whichever is configured, so test on one host at a time.
- **Backups (phase 1):** nothing built in. Options: (a) a WP plugin that dumps the DB to the same bucket (counts against
  quota; UpdraftPlus-style, S3 API to Spaces **(verify with Spaces)**); (b) `wp db export` run from a one-off container
  with the app's creds (DB is only reachable from inside the VPC); (c) DO managed MySQL cluster backups for disaster
  recovery only. Media in Spaces has no versioning; keep a copy by running `rclone sync` from Chas's own machine.
  This is the weakest point for a "real" blog and deserves an honest note in the docs.
- **Page cache vs persistence:** cache files live on scratch and rebuild after a restart; that is fine.
- **Security:** keep to the isolation story. No `.htaccess`-writing plugins needed (immutable image), no file editor
  (`DISALLOW_FILE_EDIT`), no `wp-config.php` secrets in the image (all via env/secrets). Avoid heavyweight security
  plugins (Wordfence's scanner will throttle 200m).
- **Updates:** WP security releases are frequent. Image pin + rebuild is a small CI workflow in the user's repo; fits
  the platform's "redeploy when tag changes".

## Migration path for Chas's blog

1. Pick the smaller path: **WXR export/import** (Tools > Export > Import) moves posts, pages, comments, menus and
   attachments (re-downloaded from the old URL), but not plugin settings, widgets or theme mods. Clean, good if the
   site is mostly content and the old DB has cruft. Imports run PHP for a long time at 200m: raise CPU to 500m and
   `max_execution_time` temporarily, and import in chunks.
2. **DB dump + uploads copy** keeps everything exactly. `mysqldump` (no `--all-databases`, add
   `--set-gtid-purged=OFF`; check every table has a PK first), load it with a one-off pod/job inside the VPC, run
   `wp search-replace old-url new-url --all-tables`, and `rclone` the uploads into the bucket and rewrite attachment
   URLs (or let the offload plugin do "Offload existing media").
3. Restore on a throwaway hostname first, switch DNS (lower TTL a day ahead), then add the custom domain.
4. Keep the old host read-only for a week.

The missing platform piece for path 2 is a supported way to run a one-off command against an app's DB. Today there is
no DB shell or import endpoint (confirm with the backend); a "run once" helper would help every migration.

## Recommended minimal path

**Phase 1 (no volume, no platform change, about an afternoon of image work + testing):**
wrapper image + tuning + env config + pinned salts + `DISABLE_WP_CRON` and a background cron loop + media offload +
immutable plugins + page cache + HTTPS-API mail + xmlrpc block. Throwaway app first on the platform hostname with a
sample import; then admin raises CPU 500m, DB 256 MB, bucket 2 GB; then migrate with WXR; then custom domain.
Document it as a recipe (RECIPES.md and `examples/wordpress/`, not in the examples.yml matrix) once verified.

**Phase 2 (only if phase 1 chafes):** build `resources.volume` from the ROADMAP with the WordPress additions above
(mountPath, seed-on-empty, snapshots, replicas guard). Payoff is dashboard plugin installs and local uploads; cost about
$0.10/month per volume. A new `docs/requests/` file is required first.

## What this implies for the platform

- A real stateful consumer is the best forcing function for volumes, but WordPress shows the platform can host
  "stateful-feeling" apps without them if state is split into DB + bucket + immutable image. That should be
  the recommended pattern in the docs; volumes become an escape hatch, not the default.
- The recurring pains (root images, SMTP, no one-off job runner, CPU for PHP/Node SSR, backups, a client IP behind the
  LB) show up in every recipe. Candidates: a documented "wrapper image" template for official root images, a supported
  mail relay or allowlisted HTTPS-API guidance, a run-once job (migrations, cron, `wp db import`), a per-app backup
  export to the app's bucket, and PROXY protocol for correct client IPs.
- A page-level cache or CDN story (Cloudflare guidance is the cheap one) moves from "deferred" to "needed": PHP and Node
  CMSs both live or die by it at 200m.
- Capacity: 5 workers x ~50 MB means a WordPress app uses its full 384 MB. Count it as a "heavy" app when sizing nodes;
  with the 5-apps-per-user cap and ~8-12 apps per node, one WordPress per node-ish is realistic.
- Honest positioning: good for a personal blog or low-traffic site, not for a plugin-heavy, media-heavy or high-traffic
  one. Say so in the recipe, like the "Apps we evaluated" section.

## Open items for verification before promising anything

1. Official image under UID 10001 with Apache on 8080 and dropped caps (start, serve, write to `wp-content`).
2. HTTPS detection and no redirect loop behind Traefik.
3. MySQL TLS via `MYSQL_CLIENT_FLAGS` against the managed cluster; table creation under `sql_require_primary_key`.
4. Compressed image size from `check-image`.
5. Offload plugin against a Spaces bucket (public read, custom endpoint, ACL); plugin tables PK-clean.
6. CPU per cached/uncached page and memory with 5 workers; confirm the throttling numbers.
7. Client IP seen by Apache.
