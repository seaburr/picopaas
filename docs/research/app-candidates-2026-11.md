# App candidates for new templates (2026-11-02)

Date: 2026-11-02. Author role: tech-writer. Nothing was deployed or measured; this is desk research.

**Method.** Read the caps in `app/config.py` (384 MiB RAM, 200m CPU, 250 MiB scratch, 512 MB image, 50 MB DB, 100 MB bucket,
volume default 1 GiB, non-root UID 10001) and the RECIPES.md lessons, then listed the current catalog (Ghost, WordPress, Directus,
Grafana (MySQL and PG), n8n, Shlink, Miniflux, Forgejo, Memos, Vaultwarden, Wiki.js, Gatus, static site; Linkding and Uptime Kuma
queued). For each candidate: GitHub API for archived flag, last push and license (checked 2026-11-02/03); Docker Hub / GHCR
registry APIs for the **compressed linux/amd64 layer size** and the image `User`/exposed port; upstream docs for DB, storage and
extra services. **RAM numbers are my estimates (not measured, no upstream figures unless stated); each must be measured on a
throwaway app before a recipe is promoted.** Items I could not confirm are marked "unverified".

## Ranked table

| # | App | License | Image (amd64, compressed) | Image user | DB | Needs | RAM est. | Verdict | Effort | Demo value |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Vikunja (tasks/kanban) | AGPL-3.0 | `vikunja/vikunja` 55 MB | uid 1000 | SQLite, MySQL, PG | none; optional S3 for attachments | 40-100 MB | Great | S | High |
| 2 | Planka (kanban) | NOASSERTION (fair-code; check LICENSE) | `ghcr.io/plankanban/planka` 136 MB | `node` | PG only | `/app/data` (volume or S3) | 120-220 MB | Great | S-M | High |
| 3 | Umami (web analytics) | MIT | `ghcr.io/umami-software/umami:postgresql-latest` 316 MB | `nextjs` 1001 | PG only | none (Redis optional) | 150-280 MB | Great | M (RAM unknown) | Very high |
| 4 | ntfy (push notifications) | Apache-2.0 | `binwiederhier/ntfy` 37 MB | root (none set) | SQLite files or PG (`database-url`) | none; SMTP optional | 20-60 MB | Great | S | High |
| 5 | IT-Tools (dev utilities) | GPL-3.0 | `corentinth/it-tools` 23 MB | nginx root, port 80 | none (static) | wrapper to non-root nginx | 5-15 MB | Great | S | High |
| 6 | Excalidraw (whiteboard) | MIT | `excalidraw/excalidraw` 43 MB | nginx root, port 80 | none (static) | wrapper as above | 5-15 MB | Great | S | High |
| 7 | Gotify (push server) | MIT-style (API says NOASSERTION) | `gotify/server` 54 MB | root (none set) | SQLite, MySQL, PG | none | 20-50 MB | Great | S | Medium |
| 8 | Actual Budget | MIT | `actualbudget/actual-server:latest-alpine` 70 MB | root (none set) | SQLite files only | volume `/data` | 80-160 MB | Possible with tweaks | S-M | Medium |
| 9 | Node-RED | Apache-2.0 | `nodered/node-red:5-minimal` 120 MB | `node-red` | none (flow files) | volume `/data` | 80-150 MB | Possible with tweaks | M | High |
| 10 | Kutt (link shortener) | MIT | `kutt/kutt` 107 MB | root (none set) | PG, MySQL, SQLite | none (Redis optional) | 60-120 MB | Possible (duplicates Shlink) | S-M | Low |
| 11 | Wallabag (read-later) | MIT | `wallabag/wallabag` 111 MB | root, Apache port 80 | SQLite, MySQL, PG | wrapper for high port | 120-250 MB | Possible with tweaks | M | Medium |
| 12 | LinkStack (link page) | AGPL-3.0 | `linkstackorg/linkstack` 82 MB | `apache`, port 80 | SQLite default, MySQL (unverified for the image) | volume; port change | 80-150 MB | Possible with tweaks | M | High |
| 13 | Homepage (dashboard) | GPL-3.0 | `ghcr.io/gethomepage/homepage` 84 MB | root | none (YAML config) | config dir; env PUID/PGID | 60-120 MB | Possible with tweaks | M | Medium |
| 14 | DokuWiki | GPL-2.0 | `dokuwiki/dokuwiki` 224 MB | root (none set); exposes 80 and 8080 | none (flat files) | volume | 40-100 MB | Possible with tweaks | M | Low |
| 15 | BookStack (docs) | MIT | `lscr.io/linuxserver/bookstack` 96 MB | s6 `/init`, root | MySQL/MariaDB only | wrapper (no official image) | 150-250 MB | Possible with tweaks | L | Medium |

Verdict key: Great = runs with env vars or a one-line wrapper; Possible = needs volume, port or user work; No = see Ruled out.
Effort: S under half a day, M a day, L more.

## Top 5 to try next

Order is demo value times effort. All sketches assume `resources.db: pg` and the refs from RECIPES.md.

### 1. Vikunja on PostgreSQL
- Image `vikunja/vikunja:2` (or pin `2.7`), user 1000 already, port 3456. No wrapper expected; set `HOME=/tmp` out of habit.
- Engine PG, no volume. Env: `VIKUNJA_DATABASE_TYPE=postgres`, `VIKUNJA_DATABASE_HOST=${{ db.host }}:${{ db.port }}`
  (host takes `host:port`; verify), `VIKUNJA_DATABASE_DATABASE=${{ db.name }}`, `VIKUNJA_DATABASE_USER`, `VIKUNJA_DATABASE_PASSWORD`,
  `VIKUNJA_DATABASE_SSLMODE=verify-full`, `VIKUNJA_DATABASE_SSLROOTCERT=${{ db.ca_file }}`, `VIKUNJA_SERVICE_PUBLICURL=${{ app.url }}`,
  `VIKUNJA_SERVICE_SECRET` (secret), `VIKUNJA_SERVICE_INTERFACE=:8080` (assumption; port must be above 1024, 3456 already is).
- Attachments: either `VIKUNJA_FILES_TYPE=s3` with the `storage.*` refs (`VIKUNJA_FILES_S3_ENDPOINT/_BUCKET/_REGION/_ACCESSKEY/_SECRETKEY`,
  `USEPATHSTYLE` as the bucket requires) or leave local and accept loss on restart. Mailer stays off (docs: password reset is then unavailable,
  new users are enabled immediately), so lock registration (`VIKUNJA_SERVICE_ENABLEREGISTRATION=false` after the first user).
- Risk: PG DB is 50 MB; fine for tasks. Sources: [config](https://vikunja.io/docs/config-options/), [repo](https://github.com/go-vikunja/vikunja).

### 2. Planka on PostgreSQL (+ bucket)
- **License decision (Chas, 2026-11-03):** users may run it themselves on PicoPaaS; PicoPaaS never offers or markets it as a managed service.
- Image `ghcr.io/plankanban/planka:latest` (pin a tag), user `node`, port 1337, entry `./start.sh`.
- Env (from upstream compose): `BASE_URL=${{ app.url }}`, `DATABASE_URL=postgresql://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}`
  plus TLS (compose comments mention `PGSSLMODE` and `KNEX_REJECT_UNAUTHORIZED_SSL_CERTIFICATE`; prove `verify-full` with `NODE_EXTRA_CA_CERTS`
  or `PGSSLROOTCERT=${{ db.ca_file }}`), `SECRET_KEY` (secret), `DEFAULT_ADMIN_EMAIL/_PASSWORD/_NAME/_USERNAME`.
- Storage: `/app/data` is a volume upstream. Use `S3_ENDPOINT/_REGION/_ACCESS_KEY_ID/_SECRET_ACCESS_KEY/_BUCKET`, `S3_FORCE_PATH_STYLE=true` with the
  `storage.*` refs so no volume is needed; avatars and attachments then count against the 100 MB bucket. Leave SMTP unset.
- Risk: Node memory; set `NODE_OPTIONS=--max-old-space-size=256` if it nears 384 MB. License is "fair-code" in 2.x; read it before showcasing.
  Sources: [compose](https://raw.githubusercontent.com/plankanban/planka/master/docker-compose.yml), [docs](https://docs.planka.cloud/docs/welcome/).

### 3. Umami on PostgreSQL
- Image `ghcr.io/umami-software/umami:postgresql-latest` (316 MB, under the cap), user `nextjs` 1001, port 3000, `HOSTNAME=0.0.0.0`.
- Env: `DATABASE_URL` (PG DSN with `sslmode`; Prisma wants `sslrootcert` as a path, use `${{ db.ca_file }}`), `APP_SECRET` (secret),
  `DISABLE_TELEMETRY=1`. No volume, no bucket, Redis optional (omit). Default login is `admin` / `umami`: change it on first visit
  (or script it); the first start creates tables via the Prisma start script, which needs a few seconds and CPU.
- Why interesting: analytics for the blog and landing page, hosted on the platform itself. Risks: Next.js idle RAM unknown (measure; raise memory
  override if over 300 MB), 50 MB DB fills with event rows (it stores raw events; plan for a retention job or a bigger DB override), `BASE_PATH`
  is build time so serve at the root of a (sub)domain. UID 10001 vs image 1001: writable dirs are `/app` only; confirm no runtime writes outside `/tmp`.
  Sources: [install](https://docs.umami.is/docs/install), [env vars](https://docs.umami.is/docs/environment-variables), [Dockerfile](https://raw.githubusercontent.com/umami-software/umami/master/Dockerfile).

### 4. ntfy on PostgreSQL
- Image `binwiederhier/ntfy:v2` (37 MB), entrypoint `ntfy`, command `serve`. Image sets no `User`, so it likely runs root upstream; the Go binary
  does not need root when `listen-http` is above 1024, so probably no wrapper (verify; fall back to a one-line wrapper like Memos).
- Env: `NTFY_BASE_URL=${{ app.url }}`, `NTFY_LISTEN_HTTP=:8080`, `NTFY_DATABASE_URL=postgres://...?sslmode=verify-full&sslrootcert=${{ db.ca_file }}`
  (docs: when `database-url` is set, `cache-file`, `auth-file` and `web-push-file` must NOT be set), `NTFY_AUTH_DEFAULT_ACCESS=deny-all`,
  `NTFY_BEHIND_PROXY=true`, `NTFY_CACHE_DURATION=12h`. No volume, no bucket; SMTP not needed (only for email in/out).
- Demo: `curl -d "deployed" https://<app>/mytopic` from a CI job; instant, visual. Risk: open pub/sub is an abuse magnet, keep it auth-gated.
  Sources: [config docs](https://docs.ntfy.sh/config/), [repo](https://github.com/binwiederhier/ntfy).

### 5. IT-Tools and Excalidraw (static, near-zero RAM)
- Both ship nginx on port 80 as root. One wrapper pattern covers both: `FROM nginxinc/nginx-unprivileged` (port 8080, non-root), then
  `COPY --from=<upstream image> /usr/share/nginx/html /usr/share/nginx/html`; set `HOME=/tmp`. Images 23 MB and 43 MB, no DB, no volume, no bucket.
- Env: none. Effort is two Dockerfiles and two templates. Excalidraw's published image was last pushed 2026-05-06 and IT-Tools `latest` on Docker
  Hub dates from 2024-10 (a `nightly` is 2026-02; the repo was pushed 2026-10-27), so pin a digest, or build from source in the wrapper.
- Risk: Excalidraw's collab/library features call external services (egress 443 is allowed); not needed for drawing. IT-Tools sends nothing server side.
  Sources: [IT-Tools](https://github.com/CorentinTh/it-tools), [Excalidraw](https://github.com/excalidraw/excalidraw).

## Per-candidate notes (rows 7-15)

- **Gotify** ([config](https://gotify.net/docs/config)): `GOTIFY_DATABASE_DIALECT=postgres`, `GOTIFY_DATABASE_CONNECTION` (key=value DSN, add `sslmode`),
  `GOTIFY_SERVER_PORT=8080`, `GOTIFY_DEFAULTUSER_PASS` (secret), `GOTIFY_UPLOADEDIMAGESDIR=/tmp/images`. Overlaps ntfy; build only one first (ntfy is more active, PG-native).
- **Actual Budget** ([docker](https://actualbudget.org/docs/install/docker/), [config](https://actualbudget.org/docs/config/)): stores `account.sqlite` plus
  budget blobs under `/data`, so it needs the volume (1 GiB is ample) and shows off volume backups. Docs do not say root vs non-root: test with UID 10001,
  set `ACTUAL_DATA_DIR`, `ACTUAL_PORT=8080`. Financial data on a throwaway PaaS is a poor look; position as a demo, not a recommendation.
- **Node-RED** ([repo](https://github.com/node-red/node-red)): `5-minimal` is 120 MB, user `node-red`, port 1880, flows in `/data` (volume). Must set `adminAuth`
  via a settings file or the editor is open remote code execution; that is the real blocker and the abuse risk. Overlaps n8n; demo value is high.
- **Kutt** ([env](https://raw.githubusercontent.com/thedevs-network/kutt/main/.example.env)): `DB_CLIENT=pg`, `DB_SSL=true`, `JWT_SECRET`, `DEFAULT_DOMAIN`,
  registration disabled by default, Redis off by default, mail off. Straightforward but duplicates Shlink; only worth it for a UI-first contrast. Root image: wrapper.
- **Wallabag** ([repo](https://github.com/wallabag/wallabag)): Symfony app on Apache port 80, root entrypoint; needs a port/user wrapper (the FreshRSS problem).
  Supports MySQL and PG, so DB fits. Heavier than Linkding for the same job; Linkding stays ahead in the queue.
- **LinkStack** ([repo](https://github.com/LinkStackOrg/LinkStack)): Alpine + Apache + PHP 8.2, runs as `apache` but exposes 80/443; needs the Apache port
  moved or a different front. Docker docs live in `linkstack-docker` (not read). Upstream pushed 2026-10-26. Best link-in-bio demo; volume for SQLite.
- **Homepage** ([docker](https://gethomepage.dev/installation/docker/)): image `User root`; config uses PUID/PGID, not a `user:` option, and the
  Docker-socket integration is irrelevant on k8s. Static YAML dashboard, so demo value is mostly cosmetic.
- **DokuWiki** ([repo](https://github.com/dokuwiki/dokuwiki)): flat files, so volume mandatory; 224 MB image; exposing 8080 hints at an alternate port but the
  user is not set. Wiki.js already fills the wiki slot; skip unless a no-DB wiki is wanted.
- **BookStack** ([repo](https://github.com/BookStackApp/BookStack)): MIT, MySQL only (fits our MySQL 8). No official image; LinuxServer's uses s6 `/init`
  and expects root, so a rootless PHP-FPM + nginx build (like the WordPress recipe) is needed. Large effort for a wiki we already have.

## Ruled out

- **Mealie** (371 MB image fits): upstream docs set a 1000M memory limit and warn Python pre-allocates memory; no 384 MiB headroom ([docs](https://mealie.io/documentation/getting-started/installation/postgres/)). Revisit with a memory override.
- **Stirling-PDF**: ultra-lite is 396 MB (fat 1217 MB, over cap) and it is JVM based; will not idle in 384 MiB (estimate). Also a CPU hog for PDF jobs.
- **Karakeep**: image 510 MB, right at the cap, and the supported setup adds Meilisearch and a headless Chrome container ([docs](https://docs.karakeep.com/Installation/docker/)). No single-container mode confirmed.
- **Linkwarden**: image 567 MB (over 512 MB); PG plus Meilisearch plus Playwright browser; `DISABLE_BROWSER=true` removes archiving, which is the point ([env](https://docs.linkwarden.app/self-hosting/environment-variables)).
- **Docmost**: Redis is required (unverified in this pass beyond known compose; image 221 MB). **Outline**: Redis plus S3 plus an OIDC provider, no password login. Both blocked by "no Redis".
- **Hoppscotch**: PostgreSQL plus three services (AIO image bundles them), migrations run by hand, no memory figure published ([docs](https://docs.hoppscotch.io/documentation/self-host/community-edition/install-and-build)). Too heavy and fiddly.
- **Pingvin Share**: **archived 2026-05-18**; owner points to the fork Pingvin Share X ([repo](https://github.com/stonith404/pingvin-share)); the fork was not evaluated. **PsiTransfer**: maintained (pushed 2026-10-24), but local-disk uploads only, UID 1000 volume, no S3 ([repo](https://github.com/psi-4ward/psitransfer)); fine on a 1 GiB volume if a file-drop demo is wanted (S, low priority).
- **PocketBase**: GitHub repo **archived 2026-10-26**, moved to `ganigeorgiev.com/pocketbase/pocketbase` ([repo](https://github.com/pocketbase/pocketbase)); too unsettled to template this month.
- **Homarr (legacy `ajnart/homarr`)**: archived 2026-06; the rewrite is `homarr-labs/homarr` (not evaluated). **File Browser**: archived 2026-07.
- **Plausible CE**: needs ClickHouse. **Rallly**: the repo I found was a fork (upstream `lukevella/rallly`); its login is email based, which collides with no-SMTP (unverified).
- **Uptime Kuma**: v2 default tag is 617 MB (over the cap); only `2-slim` (185 MB) fits, root user, SQLite on a volume. Already queued; Gatus covers the use case.
- **Tandoor, Trilium, Grist, Formbricks, NocoDB, Kavita, Readeck, HedgeDoc**: seen in the survey, not researched (no capacity this pass). Readeck's GitHub copy was last pushed 2023 (the project has moved off GitHub, unverified).

## Surprises

- Three popular picks are archived or moving: Pingvin Share (archived), PocketBase (archived 2026-10-26, relocated), File Browser (archived).
- Uptime Kuma 2.x full images are 617 MB (compressed), over our cap; the earlier "185 MB" note only holds for `2-slim`.
- ntfy now has a first-class PostgreSQL backend, which removes the volume it used to need.
- Linkding's "plus" images are 401 and 602 MB; only the plain `latest-alpine` (51 MB) fits.
- No candidate here needs MySQL; Umami and Planka are PG only, so the PG engine is the more useful one for new templates.
