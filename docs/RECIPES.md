# Recipes

PicoPaaS hosts real apps, not just toy images. Each recipe below follows the same layout: a facts table, the full app
spec as YAML, an env var table, gotchas, and the steps after the first boot. The general lessons for bringing your own
image come after the recipes.

Recipes with a one-click template: Ghost, WordPress, Directus, Grafana, n8n, Shlink, Miniflux, Forgejo, Memos, Vaultwarden,
Wiki.js, Vikunja, Umami, ntfy, IT-Tools, Excalidraw, Gatus and a static site. Planka is a recipe only (a spec in
`examples/planka/`, no template card; licence decision). All specs are in [`examples/`](../examples/).

## Contents

- [Deploying a spec](#deploying-a-spec)
- [Ghost (blog/CMS)](#ghost-blogcms)
- [WordPress (blog/CMS)](#wordpress-blogcms)
- [Directus (headless CMS)](#directus-headless-cms)
- [Grafana (dashboards)](#grafana-dashboards)
- [n8n (workflow automation)](#n8n-workflow-automation)
- [Shlink (URL shortener)](#shlink-url-shortener)
- [Miniflux (RSS reader)](#miniflux-rss-reader)
- [Forgejo (git forge)](#forgejo-git-forge)
- [Memos (notes)](#memos-notes)
- [Vaultwarden (passwords)](#vaultwarden-passwords)
- [Wiki.js 2 (wiki)](#wikijs-2-wiki)
- [Vikunja (tasks)](#vikunja-tasks)
- [Umami (web analytics)](#umami-web-analytics)
- [Planka (kanban boards)](#planka-kanban-boards)
- [ntfy (push messages)](#ntfy-push-messages)
- [IT-Tools (dev utilities)](#it-tools-dev-utilities)
- [Excalidraw (whiteboard)](#excalidraw-whiteboard)
- [Gatus (status page)](#gatus-status-page)
- [Static site (nginx)](#static-site-nginx)
- [Lessons for bringing your own image](#lessons-for-bringing-your-own-image)
- [Apps we evaluated](#apps-we-evaluated)
- [Candidate recipes (2026-10-28)](#candidate-recipes-2026-10-28)

## Deploying a spec

Every recipe below has a **Deploy on PicoPaaS** block. It is the whole app as a YAML spec: `image`, `port`, `healthCheck`,
`resources` (database, bucket, volume) and `env`. Deploy it in one of three ways.

- **Dashboard.** Pick the app on the **Templates** page (the template id is in each facts table), or use **New app** and
  fill in the same fields.
- **CLI.** Export an API key from **Account > API keys**, then run `picopaas apply -f app.yaml`.
- **curl.** The `/apps/spec` endpoint takes `{"spec": "<yaml>"}`; `jq -Rs` wraps the file for you.

```bash
export PICOPAAS_TOKEN=pp_sk_...
picopaas apply -f app.yaml

curl -fsS -X POST https://platform.picopaas.xyz/apps/spec \
  -H "Authorization: Bearer $PICOPAAS_TOKEN" \
  -H "Content-Type: application/json" \
  --data "$(jq -Rs '{spec: .}' app.yaml)"
```

Both return the created app with its generated `https://<name>.picopaas.xyz` hostname. Re-running `apply` with the same
`name` updates the app in place.

Rules that apply to every spec:

- Quote the `${{ ... }}` values. The braces are not valid unquoted YAML.
- `${{ db.* }}` and `${{ storage.* }}` are filled in from the database and bucket you request under `resources`.
  `${{ app.url }}` is this app's own public URL (a custom domain or reserved subdomain if set, otherwise the generated
  host), so there is no second step to set it.
- A value that is not a platform ref (a password you choose, a signing key) is a **secret**. List its name under
  `secrets:` and set the value in the dashboard's secret editor or through the API. Never put it in the YAML file.
- The volume and `db_engine` blocks need the matching feature on your instance. The engine is fixed once the app has a database.
- Pin image versions. Do not use `:latest` for a public upstream image.

## Ghost (blog/CMS)

[Ghost](https://ghost.org/) is a publishing platform. It runs on MySQL for posts and the bucket for images, built on a thin non-root image.

| | |
|---|---|
| Image | `ghcr.io/seaburr/picopaas-example-ghost6:latest` (about 185 MiB) |
| Port | 2368 |
| Database | MySQL |
| Bucket | yes (uploaded images) |
| Volume | none |
| Template id | `ghost` |
| RAM measured | about 140-170 MiB (limit 384 MiB) |
| Verified | live, 2026-10-25 |

> **Ghost 6.** Migration 6.61 creates a table without a primary key, so it needs the managed MySQL `sql_require_primary_key`
> setting off (request 0009). Ghost 5 is retired. The image carries performance patches (image-size probe timeout 400 ms,
> outbound webmentions and update check off, 2-minute frontend cache). Upgrade runbook and rehearsal notes:
> `examples/ghost/README.md`.

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: ghost
image: ghcr.io/seaburr/picopaas-example-ghost6:latest
port: 2368
resources:
  db: true
  db_engine: mysql
  storage: true
env:
  DB_HOST: "${{ db.host }}"
  DB_PORT: "${{ db.port }}"
  DB_NAME: "${{ db.name }}"
  DB_USER: "${{ db.user }}"
  DB_PASSWORD: "${{ db.password }}"
  DB_CA_CERT: "${{ db.ca_cert }}"
  S3_ENDPOINT: "${{ storage.endpoint }}"
  S3_REGION: "${{ storage.region }}"
  S3_BUCKET: "${{ storage.bucket }}"
  S3_ACCESS_KEY: "${{ storage.access_key }}"
  S3_SECRET_KEY: "${{ storage.secret_key }}"
  SITE_URL: "${{ app.url }}"
```

### Env vars

| Name | Value | Why |
|---|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `${{ db.* }}` refs | The entrypoint turns them into `database__connection__*`. |
| `DB_CA_CERT` | `${{ db.ca_cert }}` | Becomes `database__connection__ssl__ca`, so MySQL TLS is verified. |
| `S3_ENDPOINT`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | `${{ storage.* }}` refs | The S3 storage adapter config. |
| `SITE_URL` | `${{ app.url }}` | Becomes Ghost's `url`. |

No secrets are needed: every value is a platform ref.

### Gotchas

- **Why a custom image.** The official `ghost:6-alpine` starts as root and drops to `node`. PicoPaaS rejects root and forces
  UID 10001, so the wrapper runs as 10001 directly.
- **Env keys.** PicoPaaS keys must match `^[A-Z_][A-Z0-9_]*$`, but Ghost wants lowercase `__`-nested keys. The entrypoint
  shim translates them, then `exec`s Ghost.
- **Asset host.** The shim sets `assetHost` to the public Spaces URL (`https://<bucket>.<region>.digitaloceanspaces.com`).
  The adapter's default is an AWS-style URL, which is wrong for Spaces.
- **Sign-in email is off.** The shim sets `security__staffDeviceVerification=false`. PicoPaaS blocks SMTP, so the
  verification email could not send and would lock you out.
- **CPU-bound.** The first start is slow. Ask an admin for more CPU (up to 1000m) for sustained traffic.
- **Set a Publication icon** in Ghost Admin > Settings > Design so pages don't wait on a self-fetch.

The image is `FROM ghost:6-alpine` plus:

- An `/etc/passwd` entry for UID 10001. Node's `os.userInfo()` throws for a UID with no entry.
- `content/` seeded from `content.orig` at build. The stock root entrypoint normally does this and we bypass it.
- `chown` of `content/` only. Chowning the whole tree duplicates it in a new layer and doubles the image.
- The S3 blob storage adapter, behind build-arg `WITH_S3=1` (the published image is built with it).
- `HOME=/tmp`, which any UID can write.

```dockerfile
FROM ghost:6-alpine
ARG WITH_S3=0
USER root
RUN echo "pico:x:10001:10001::/var/lib/ghost:/sbin/nologin" >> /etc/passwd && \
    cd /var/lib/ghost && cp -a content.orig/. content/ && \
    chown -R 10001:10001 /var/lib/ghost/content
COPY docker-entrypoint.sh /pico-entrypoint.sh
RUN chmod +x /pico-entrypoint.sh
ENV HOME=/tmp
USER 10001:10001
ENTRYPOINT ["/pico-entrypoint.sh"]
```

This is abridged; the S3 adapter install is in the real file. The files are in `examples/ghost/`.

### After the first boot

1. Open `/ghost/` on the app's URL and finish the setup page. Whoever completes it becomes the owner.
2. Posts and settings live in MySQL and uploaded images in the bucket, so both survive restarts.

## WordPress (blog/CMS)

[WordPress](https://wordpress.org/) on a non-root nginx + PHP-FPM image (PHP 8.5, WordPress 7.1, WP-CLI). The first install
runs automatically and is locked down. A volume for the whole site is optional.

| | |
|---|---|
| Image | `ghcr.io/seaburr/picopaas-example-wordpress:latest` |
| Port | 8080 |
| Database | MySQL |
| Bucket | none |
| Volume | optional, 1 GiB at `/var/www/html` |
| Template id | `wordpress` |
| RAM measured | not measured live; about 200 MiB steady expected (4 FPM workers) |
| Verified | see `examples/wordpress/README.md` |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: wordpress
image: ghcr.io/seaburr/picopaas-example-wordpress:latest
port: 8080
healthCheck: { enabled: true, path: /healthz, timeoutSeconds: 10 }
resources:
  db: true
  db_engine: mysql
  volume: { sizeGi: 1, mountPath: /var/www/html }
env:
  WORDPRESS_DB_HOST: "${{ db.host }}"
  WORDPRESS_DB_PORT: "${{ db.port }}"
  WORDPRESS_DB_NAME: "${{ db.name }}"
  WORDPRESS_DB_USER: "${{ db.user }}"
  WORDPRESS_DB_PASSWORD: "${{ db.password }}"
  WORDPRESS_DB_TLS: "${{ db.tls }}"
  WP_URL: "${{ app.url }}"
  WP_TITLE: "My site"
  WP_ADMIN_USER: "admin"
  WP_ADMIN_EMAIL: "you@example.com"
  WP_PAGE_CACHE: "true"
secrets:   # names only; set the values in the dashboard or API
  - WORDPRESS_AUTH_KEY
  - WORDPRESS_SECURE_AUTH_KEY
  - WORDPRESS_LOGGED_IN_KEY
  - WORDPRESS_NONCE_KEY
  - WORDPRESS_AUTH_SALT
  - WORDPRESS_SECURE_AUTH_SALT
  - WORDPRESS_LOGGED_IN_SALT
  - WORDPRESS_NONCE_SALT
```

### Env vars

| Name | Value | Why |
|---|---|---|
| `WORDPRESS_DB_HOST`, `_PORT`, `_NAME`, `_USER`, `_PASSWORD` | `${{ db.* }}` refs | Database connection. |
| `WORDPRESS_DB_TLS` | `${{ db.tls }}` | `required` makes the connection use TLS. |
| `WP_URL` | `${{ app.url }}` | Used by the first install. Not an automatic env var. |
| `WP_TITLE`, `WP_ADMIN_USER`, `WP_ADMIN_EMAIL` | literals | Used by the first install. Change the email to yours. |
| `WP_PAGE_CACHE` | `"true"` | Page cache in `/tmp`. |
| `WORDPRESS_AUTH_KEY`, `_SECURE_AUTH_KEY`, `_LOGGED_IN_KEY`, `_NONCE_KEY`, `_AUTH_SALT`, `_SECURE_AUTH_SALT`, `_LOGGED_IN_SALT`, `_NONCE_SALT` | secrets | Required. The container refuses to start if any is missing. Keep them stable. |

### Gotchas

- **Eight secrets, no fallback.** Generate each one locally (`openssl rand -base64 48`) and keep it. A random fallback would log everybody out on every restart.
- **TLS is encrypted, not verified.** Core WordPress has no hook for a CA file, so `db.ca_cert` is not used.
- **CPU.** Ask an admin for 500m. The 200m default makes the installer and dashboard slow.
- **No volume.** It still runs, but uploads and plugins live on the scratch disk and are lost on restart. Content stays in MySQL.
- **Locked-down paths.** `/wp-admin/install.php` and `setup-config.php` always return 403. `/wp-login.php` is rate limited per client IP.
- **Updates.** The image rebuilds weekly. A new PHP, WordPress or WP-CLI version needs a manual edit in the Dockerfile.

### After the first boot

1. Read the admin password from the app's logs. It is printed once, on a line starting `WORDPRESS ADMIN PASSWORD`.
2. Log in as `WP_ADMIN_USER` and change the password.
3. To move an existing site, follow `examples/wordpress/MIGRATION.md`. Details: `examples/wordpress/README.md`.

## Directus (headless CMS)

[Directus](https://directus.io/) is an open-source headless CMS. It uses MySQL and the bucket together, so it exercises the full platform stack.

| | |
|---|---|
| Image | `directus/directus:12.4.1` (about 242 MB, runs as `node`) |
| Port | 8055 |
| Database | MySQL |
| Bucket | yes (uploaded files) |
| Volume | none |
| Template id | `directus` |
| RAM measured | not recorded; slow cold start (90-110 s at the default CPU) |
| Verified | live at `https://directus.picopaas.xyz`: data and an uploaded file survived a pod restart |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: directus
image: directus/directus:12.4.1
port: 8055
healthCheck: { enabled: false }
resources:
  db: true
  db_engine: mysql
  storage: true
env:
  DB_CLIENT: "mysql"
  DB_HOST: "${{ db.host }}"
  DB_PORT: "${{ db.port }}"
  DB_DATABASE: "${{ db.name }}"
  DB_USER: "${{ db.user }}"
  DB_PASSWORD: "${{ db.password }}"
  DB_SSL__CA: "${{ db.ca_cert }}"
  DB_POOL__MIN: "0"
  DB_POOL__MAX: "5"
  ADMIN_EMAIL: "admin@example.com"
  STORAGE_LOCATIONS: "s3"
  STORAGE_S3_DRIVER: "s3"
  STORAGE_S3_KEY: "${{ storage.access_key }}"
  STORAGE_S3_SECRET: "${{ storage.secret_key }}"
  STORAGE_S3_BUCKET: "${{ storage.bucket }}"
  STORAGE_S3_REGION: "${{ storage.region }}"
  STORAGE_S3_ENDPOINT: "${{ storage.endpoint }}"
  PUBLIC_URL: "${{ app.url }}"
  TELEMETRY: "false"
  HOME: "/tmp"
  PM2_HOME: "/tmp/.pm2"
secrets:   # names only; set the values in the dashboard or API
  - KEY
  - SECRET
  - ADMIN_PASSWORD
```

The health check is off on purpose. See Gotchas.

### Env vars

| Name | Value | Why |
|---|---|---|
| `DB_CLIENT` | `mysql` | Database driver. |
| `DB_HOST`, `DB_PORT`, `DB_DATABASE`, `DB_USER`, `DB_PASSWORD` | `${{ db.* }}` refs | Database connection. |
| `DB_SSL__CA` | `${{ db.ca_cert }}` | CA-verified TLS. Directus (via Knex) takes the CA content directly, with no file to mount. |
| `DB_POOL__MIN`, `DB_POOL__MAX` | `0`, `5` | Stay under the 10-connection-per-user cap. |
| `STORAGE_LOCATIONS`, `STORAGE_S3_DRIVER` | `s3` | Store uploaded files in the bucket. |
| `STORAGE_S3_KEY`, `_SECRET`, `_BUCKET`, `_REGION`, `_ENDPOINT` | `${{ storage.* }}` refs | Bucket access. No path-style flag is needed for Spaces. |
| `KEY`, `SECRET` | secrets | Signing and encryption. Generate once and keep them stable. |
| `ADMIN_EMAIL` | literal | First admin account. Change it. |
| `ADMIN_PASSWORD` | secret | First admin password. |
| `PUBLIC_URL` | `${{ app.url }}` | Directus's public URL. |
| `TELEMETRY` | `false` | Turn off the outbound phone-home. |
| `HOME`, `PM2_HOME` | `/tmp`, `/tmp/.pm2` | **Required.** See Gotchas. |

### Gotchas

- **`HOME` and `PM2_HOME` are required.** The image starts Directus under pm2, which tries to create a home directory for the
  non-root UID. Without a writable path it fails with `EACCES` before it binds the port.
- **Leave the health check off.** Directus loads its module graph twice at boot, about 90-110 s on the 0.2-core limit. The
  platform's liveness timing (30 s initial delay, then 3 x 20 s) restart-loops it first.
- **The trade-off.** With no probe there is no liveness protection (a wedged process is not restarted), and every deploy or
  restart has about 90 s of downtime. If probe timing is ever relaxed, `/server/ping` is the public path to use.
  `/server/health` returns `403` to anonymous callers.
- **Boot warning.** `Couldn't bundle App extensions (EACCES ...)` is cosmetic. The filesystem is read-only and you are not using custom extensions.
- **One replica.** Scaled-out Directus was not tested. See "Single replica only" under Grafana.
- **Do not rotate `KEY` and `SECRET`.** That invalidates sessions and breaks data encrypted at rest.

### After the first boot

1. Wait about 90 s, then open the app's URL.
2. Log in as `ADMIN_EMAIL` with `ADMIN_PASSWORD`.

## Grafana (dashboards)

[Grafana](https://grafana.com/) with its state in the platform database. MySQL and PostgreSQL are equal choices, and the
Templates page has one card per engine.

| | |
|---|---|
| Image | `grafana/grafana:13.2.4` (official, runs as UID 472, no wrapper) |
| Port | 3000 |
| Database | MySQL or PostgreSQL |
| Bucket | none |
| Volume | none |
| Template id | `grafana` (MySQL), `grafana-pg` (PostgreSQL) |
| RAM measured | not recorded |
| Verified | MySQL live: health `200`, dashboards and users survive a redeploy |

### Deploy on PicoPaaS

MySQL spec (`examples/grafana/grafana.yaml`). The PostgreSQL spec is `examples/grafana/grafana-postgres.yaml`; the second table below lists what differs.

```yaml
apiVersion: picopaas/v1
name: grafana
image: grafana/grafana:13.2.4
port: 3000
healthCheck: { enabled: true, path: /api/health }
resources:
  db: true
  db_engine: mysql
env:
  GF_SERVER_HTTP_PORT: "3000"
  GF_SERVER_ROOT_URL: "${{ app.url }}"
  GF_DATABASE_TYPE: "mysql"
  GF_DATABASE_URL: "${{ db.url }}"
  GF_DATABASE_SSL_MODE: "skip-verify"
  GF_DATABASE_CA_CERT_PATH: "/etc/ssl/certs/ca-certificates.crt"
  GF_DATABASE_MAX_OPEN_CONN: "8"
  GF_DATABASE_MAX_IDLE_CONN: "2"
  GF_SECURITY_ADMIN_USER: "admin"
  GF_ANALYTICS_REPORTING_ENABLED: "false"
  GF_PLUGINS_PREINSTALL_DISABLED: "true"
  GF_ANALYTICS_CHECK_FOR_UPDATES: "false"
secrets:   # names only; set the values in the dashboard or API
  - GF_SECURITY_ADMIN_PASSWORD
```

### Env vars

| Name | Value | Why |
|---|---|---|
| `GF_SERVER_HTTP_PORT` | `3000` | Matches the app `port`. |
| `GF_SERVER_ROOT_URL` | `${{ app.url }}` | Grafana's public URL. |
| `GF_DATABASE_TYPE` | `mysql` or `postgres` | Engine. |
| `GF_DATABASE_URL` | `${{ db.url }}` | One ref carries host, port, name, user and password. |
| `GF_DATABASE_SSL_MODE` | see the engine table | TLS mode. |
| `GF_DATABASE_CA_CERT_PATH` | `/etc/ssl/certs/ca-certificates.crt` | MySQL only. The driver insists on a CA file on disk. |
| `GF_DATABASE_MAX_OPEN_CONN`, `_MAX_IDLE_CONN` | `8`, `2` | Stay under the 10-connection-per-user cap. |
| `GF_SECURITY_ADMIN_USER` | `admin` | First-login admin. |
| `GF_SECURITY_ADMIN_PASSWORD` | secret | First-login password. |
| `GF_ANALYTICS_REPORTING_ENABLED`, `GF_ANALYTICS_CHECK_FOR_UPDATES` | `false` | Stop the outbound phone-home. |
| `GF_PLUGINS_PREINSTALL_DISABLED` | `true` | **Required.** See Gotchas. |

| Engine | `resources.db_engine` | `GF_DATABASE_TYPE` | `db.url` resolves to | TLS variables |
|---|---|---|---|---|
| MySQL | `mysql` | `mysql` | `mysql://user:password@host:port/name` | `GF_DATABASE_SSL_MODE=skip-verify`, `GF_DATABASE_CA_CERT_PATH=/etc/ssl/certs/ca-certificates.crt` |
| PostgreSQL | `pg` | `postgres` | `postgres://user:password@host:port/name?sslmode=require` | `GF_DATABASE_SSL_MODE=require` |

### Gotchas

- **TLS is encrypted but unverified** on both engines. Grafana's PostgreSQL mode comes from `GF_DATABASE_SSL_MODE`, not the
  URL query, so set it explicitly. The platform now mounts the CA as a file (`${{ db.ca_file }}`), so
  `GF_DATABASE_CA_CERT_PATH=${{ db.ca_file }}` with `GF_DATABASE_SSL_MODE=true` (MySQL) or `verify-full` (PostgreSQL) should
  give verified TLS. Not verified with Grafana yet; the specs above are the tested ones.
- **`GF_PLUGINS_PREINSTALL_DISABLED=true` is required.** Grafana 13 otherwise downloads its preinstalled app plugins into
  `/var/lib/grafana/plugins` on every boot (about 250 MB). That hits the 250 MB scratch-disk cap and the pod is evicted in a restart loop.
- **Pool cap.** Each database user may hold at most 10 connections, so keep the pool at 8 open and 2 idle.
- **Pin the version** (`:13.2.4`), not `:latest`.
- **Health check.** `/api/health` returns `200` once Grafana has connected to its database, so the app goes ready only when it can serve.
- **Single replica only.** HA and clustering are not supported. PicoPaaS gives each app one ClusterIP Service and no way for
  pods to find each other, so setups that need named peers (Grafana's unified-alerting memberlist) cannot work. One replica is plenty for a throwaway instance.

### After the first boot

1. Open the app's URL and log in as `admin` with the password you set as a secret.
2. Add data sources. Outbound traffic is restricted, so only public data sources work.

## n8n (workflow automation)

[n8n](https://n8n.io/) is an open-source workflow tool. It was the first PostgreSQL-backed recipe: a per-app database and
role, CA-verified TLS, and a workflow and owner account that survive a restart.

| | |
|---|---|
| Image | `n8nio/n8n:2.41.7` (official, non-root) |
| Port | 5678 |
| Database | PostgreSQL (`db_engine: pg`) |
| Bucket | none |
| Volume | 1 GiB at `/home/node` (required in the template) |
| Template id | `n8n` |
| RAM measured | about 338 MiB at idle; **needs an admin override to 512 MiB** |
| Verified | live at `https://n8n.picopaas.xyz`; volume variants on a throwaway app |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: n8n
image: n8nio/n8n:2.41.7
port: 5678
healthCheck: { enabled: true, path: /healthz }
resources:
  db: true
  db_engine: pg
  volume: { sizeGi: 1, mountPath: /home/node }
env:
  DB_TYPE: "postgresdb"
  DB_POSTGRESDB_HOST: "${{ db.host }}"
  DB_POSTGRESDB_PORT: "${{ db.port }}"
  DB_POSTGRESDB_DATABASE: "${{ db.name }}"
  DB_POSTGRESDB_USER: "${{ db.user }}"
  DB_POSTGRESDB_PASSWORD: "${{ db.password }}"
  DB_POSTGRESDB_SSL_ENABLED: "true"
  DB_POSTGRESDB_SSL_CA: "${{ db.ca_cert }}"
  DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED: "true"
  DB_POSTGRESDB_POOL_SIZE: "2"
  N8N_PROTOCOL: "https"
  N8N_PORT: "5678"
  WEBHOOK_URL: "${{ app.url }}"
  N8N_EDITOR_BASE_URL: "${{ app.url }}"
  GENERIC_TIMEZONE: "UTC"
  N8N_USER_FOLDER: "/home/node"
  N8N_DEFAULT_BINARY_DATA_MODE: "filesystem"
  NODE_OPTIONS: "--max-old-space-size=384"
  EXECUTIONS_DATA_SAVE_ON_SUCCESS: "none"
  EXECUTIONS_DATA_MAX_AGE: "72"
  EXECUTIONS_DATA_PRUNE_MAX_COUNT: "1000"
  N8N_PROXY_HOPS: "1"
  N8N_COMMUNITY_PACKAGES_ENABLED: "false"
  N8N_RUNNERS_ENABLED: "false"
  N8N_DIAGNOSTICS_ENABLED: "false"
  N8N_VERSION_NOTIFICATIONS_ENABLED: "false"
  N8N_TEMPLATES_ENABLED: "false"
  NODES_INCLUDE: '["n8n-nodes-base.webhook", "n8n-nodes-base.manualTrigger", "n8n-nodes-base.set", "n8n-nodes-base.httpRequest", "n8n-nodes-base.scheduleTrigger", "n8n-nodes-base.noOp", "n8n-nodes-base.code", "n8n-nodes-base.if", "n8n-nodes-base.respondToWebhook"]'
secrets:   # names only; set the values in the dashboard or API
  - N8N_ENCRYPTION_KEY
```

### Env vars

| Name | Value | Why |
|---|---|---|
| `DB_TYPE` | `postgresdb` | Driver. |
| `DB_POSTGRESDB_HOST`, `_PORT`, `_DATABASE`, `_USER`, `_PASSWORD` | `${{ db.* }}` refs | Database connection. |
| `DB_POSTGRESDB_SSL_ENABLED`, `_SSL_REJECT_UNAUTHORIZED` | `true` | TLS on, certificate checked. |
| `DB_POSTGRESDB_SSL_CA` | `${{ db.ca_cert }}` | The platform CA, passed as a value. |
| `DB_POSTGRESDB_POOL_SIZE` | `2` | Each Postgres role is limited to 5 connections. |
| `N8N_PROTOCOL`, `N8N_PORT` | `https`, `5678` | Public address parts. |
| `WEBHOOK_URL`, `N8N_EDITOR_BASE_URL` | `${{ app.url }}` | n8n builds webhook and OAuth callback URLs from them. |
| `GENERIC_TIMEZONE` | `UTC` | Schedule timezone. |
| `N8N_USER_FOLDER` | `/home/node` | **Required.** The image has no usable HOME for UID 10001. |
| `N8N_DEFAULT_BINARY_DATA_MODE` | `filesystem` | Binary data goes on the volume, not the database quota. |
| `NODE_OPTIONS` | `--max-old-space-size=384` | Heap ceiling. Must stay below the container memory limit. |
| `EXECUTIONS_DATA_SAVE_ON_SUCCESS`, `_MAX_AGE`, `_PRUNE_MAX_COUNT` | `none`, `72`, `1000` | Keep history inside the 50 MB database. |
| `N8N_PROXY_HOPS` | `1` | The app sits behind Traefik. Without it the login rate limiter sees every visitor as the proxy. |
| `N8N_COMMUNITY_PACKAGES_ENABLED` | `false` | Avoid the trap where installed nodes vanish but workflows that use them stay. |
| `N8N_RUNNERS_ENABLED`, `N8N_DIAGNOSTICS_ENABLED`, `N8N_VERSION_NOTIFICATIONS_ENABLED`, `N8N_TEMPLATES_ENABLED` | `false` | Trim memory and outbound phone-home. |
| `NODES_INCLUDE` | JSON list of nine common nodes | Loads only those nodes, which cuts memory. Drop it to load all (and raise the memory override). |
| `N8N_ENCRYPTION_KEY` | secret | Decrypts saved credentials. Generate once, never change. |

`N8N_HOST` is not in the template. Add it (a bare host) if you want it set. If you change the app's address, update
`N8N_HOST`, `WEBHOOK_URL` and `N8N_EDITOR_BASE_URL`, or the URLs n8n shows you are dead links.

### Gotchas

- **Mount at `/home/node`, not `/home/node/.n8n`.** n8n creates `<folder>/.n8n` and `<folder>/.cache` (the compiled UI, about
  29 MB). Mounting only `.n8n` makes it die on boot with `EACCES: mkdir '/home/node/.cache'`.
- **Memory.** n8n is OOM-killed at the 384 MiB default, even on SQLite. Ask an admin for 512 MiB. With the full node
  catalog it OOMs even at 512 MiB, so keep `NODES_INCLUDE`. Heap 384 MiB inside a 512 MiB container is the tested setting.
- **First boot is slow, and `/healthz` answers early.** A fresh database runs hundreds of migrations (1 to 2 minutes at 200m
  CPU). The app can show Running while `/rest/*` still returns 404. Wait for `/rest/settings` to return 200.
- **One replica, short outage on every deploy.** The volume attaches to one pod, so the old pod stops first (about 56 s measured).
- **The Code node runs JavaScript only.** There is no Python, so n8n logs that the Python task runner failed. The
  "task runner mode is deprecated" warning needs a second container for the fix, which PicoPaaS does not support. Ignore it.
- **Schema size.** n8n's schema alone uses about 16 MB of the 50 MB database. The default history (14 days or 10,000 runs) fills
  the rest within days on a frequent schedule, and the database goes read-only. Watch the database bar and lower the two numbers if it climbs.
- **Pin `N8N_ENCRYPTION_KEY` anyway** if you restore the database into a new app or may lose the volume. It also decrypts the credentials stored in Postgres.

### Volume checks

Verified live on a throwaway app (pod restarted through `POST /apps/{id}/restart`, about one minute of downtime):

| Check | Result |
|---|---|
| Boot with the volume, Postgres and SQLite-on-volume variants | Healthy in both. The image passed the 512 MB image cap. |
| **No `N8N_ENCRYPTION_KEY` set** | n8n generated a key into `.n8n/config` on the volume. After a restart a stored credential still decrypted, the workflow was active, and the webhook answered. |
| Binary data, `N8N_DEFAULT_BINARY_DATA_MODE=filesystem` | A 200 KB file from a Code node was written under `.n8n/storage/` and survived the restart. It counts against the volume, not the scratch disk. |
| SQLite on the volume (no `db: true`) | Owner, credential, workflow and executions survived. The cheapest setup, for one person and light use. |
| Postgres plus volume | Same results. The volume holds only key, binary data and nodes. Prefer it for separate quotas or database backups. |
| Backups | A manual backup of the Postgres variant succeeded for `db` (0.5 MB) and `volume` (29 MB, mostly the UI cache). The volume part is a file-level copy; stop writes for a consistent SQLite copy. Restoring a volume is not self-service yet. |

What the volume changes in `/home/node/.n8n`:

| Item | With a volume |
|---|---|
| `config` (encryption key) | Survives. |
| `storage/` (binary data) | Survives. Prune old executions so it doesn't fill the volume. |
| `nodes/` (community nodes) | Survive restarts. Not tested; keep community packages off unless needed. |
| `n8nEventLog.log` | Survives, so interrupted executions recover better. |
| `database.sqlite` | Survives, so a database is optional. |

### Without a volume

If volumes are off on your platform, delete the `volume` block and set `N8N_USER_FOLDER=/tmp` (scratch disk). Then these
live in `/tmp/.n8n` and are lost on restart. Workflows, credentials, users and history stay in Postgres.

| In `/tmp/.n8n` | Effect of losing it on restart |
|---|---|
| `config` | None, if `N8N_ENCRYPTION_KEY` is pinned. |
| `storage/` | Old executions' file attachments disappear. Large files also count toward the 250 MB scratch disk and can get the pod evicted. |
| `nodes/` | Community nodes are gone. Stick to built-in nodes. |
| `n8nEventLog.log` | Weaker crash recovery. |

Database binary mode (`N8N_DEFAULT_BINARY_DATA_MODE=database`) survives restarts but uses the 50 MB quota. The S3 mode needs
an n8n Enterprise license. So without a volume, n8n suits light automation (webhooks, API calls, notifications), not file pipelines.

### After the first boot

1. Ask an admin to raise the memory limit to about 512 MiB.
2. Wait for `/rest/settings` to return 200, then open the app's URL.
3. Create the owner account and build a workflow.

## Shlink (URL shortener)

[Shlink](https://shlink.io/) is a self-hosted URL shortener with a REST API. Short URLs and visit stats live in MySQL.

| | |
|---|---|
| Image | `shlinkio/shlink:5.1.8` (official, runs as UID 1001) |
| Port | 8080 |
| Database | MySQL |
| Bucket | none |
| Volume | none |
| Template id | `shlink` |
| RAM measured | not recorded |
| Verified | live: health `200`, links survive a redeploy |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: shlink
image: shlinkio/shlink:5.1.8
port: 8080
healthCheck: { enabled: true, path: /rest/health }
resources:
  db: true
  db_engine: mysql
env:
  DB_DRIVER: "mysql"
  DB_HOST: "${{ db.host }}"
  DB_PORT: "${{ db.port }}"
  DB_NAME: "${{ db.name }}"
  DB_USER: "${{ db.user }}"
  DB_PASSWORD: "${{ db.password }}"
  DB_USE_ENCRYPTION: "true"
  DEFAULT_DOMAIN: "short.example.com"
  IS_HTTPS_ENABLED: "true"
  WEB_WORKER_NUM: "4"
  TASK_WORKER_NUM: "4"
secrets:   # names only; set the values in the dashboard or API
  - INITIAL_API_KEY
```

Replace `short.example.com` with your app's bare hostname.

### Env vars

| Name | Value | Why |
|---|---|---|
| `DB_DRIVER` | `mysql` | Driver. |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `${{ db.* }}` refs | Database connection. |
| `DB_USE_ENCRYPTION` | `true` | The one switch for TLS. No CA file to mount. |
| `DEFAULT_DOMAIN` | bare host, a literal | The domain short URLs are built on. |
| `IS_HTTPS_ENABLED` | `true` | Short URLs use `https`. |
| `WEB_WORKER_NUM`, `TASK_WORKER_NUM` | `4`, `4` | Each pool opens DB connections. Keep under the 10-per-user cap. |
| `INITIAL_API_KEY` | secret | Shlink creates a key with this value on first start. |

### Gotchas

- **`DEFAULT_DOMAIN` must be a bare host** (no scheme, no path). This is the one place you cannot use `${{ app.url }}`: it
  resolves to a full `https://...` URL, which would produce malformed short links. Create the app first, then edit the value
  to match its hostname (or a reserved subdomain; ask an admin).
- **GeoLite2 warning is harmless.** Shlink logs that it could not download the GeoLite2 database. Only IP geolocation of
  visits is off. For geolocation, add a `GEOLITE_LICENSE_KEY` secret.
- **Raising the worker counts** on a throwaway instance mostly buys you a tripped connection cap.
- **Health check.** `/rest/health` returns `200` once Shlink has connected to its database.

### After the first boot

1. Open the app's URL to confirm it is up.
2. Call the REST API with your key in the `X-Api-Key` header:

```bash
curl -fsS https://<your-host>/rest/v3/short-urls \
  -H "X-Api-Key: $INITIAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"longUrl": "https://example.com/some/very/long/link"}'
```

## Miniflux (RSS reader)

[Miniflux](https://miniflux.app/) is a minimalist feed reader with a web UI and REST API, on PostgreSQL.

| | |
|---|---|
| Image | `miniflux/miniflux:2.3.4` (official, USER 65534, 14 MB) |
| Port | 8080 |
| Database | PostgreSQL |
| Bucket | none |
| Volume | none |
| Template id | `miniflux` |
| RAM measured | about 21 MiB; database 9.7 MB of 50 MB |
| Verified | live, 2026-11-01: migrations, admin, two feeds fetched through egress, API |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: miniflux
image: miniflux/miniflux:2.3.4
port: 8080
healthCheck: { enabled: true, path: /healthcheck }
resources:
  db: true
  db_engine: pg
env:
  DATABASE_URL: "postgres://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}?sslmode=verify-full&sslrootcert=${{ db.ca_file }}"
  DATABASE_MAX_CONNS: "3"
  DATABASE_MIN_CONNS: "1"
  RUN_MIGRATIONS: "1"
  CREATE_ADMIN: "1"
  ADMIN_USERNAME: "admin"
  BASE_URL: "${{ app.url }}"
  POLLING_FREQUENCY: "60"
  BATCH_SIZE: "25"
  WORKER_POOL_SIZE: "2"
  CLEANUP_ARCHIVE_READ_DAYS: "30"
  CLEANUP_ARCHIVE_UNREAD_DAYS: "60"
secrets:   # names only; set the values in the dashboard or API
  - ADMIN_PASSWORD
```

### Env vars

| Name | Value | Why |
|---|---|---|
| `DATABASE_URL` | built from `${{ db.* }}` refs, `sslmode=verify-full&sslrootcert=${{ db.ca_file }}` | CA-verified TLS. A wrong CA fails with "certificate signed by unknown authority". |
| `DATABASE_MAX_CONNS`, `DATABASE_MIN_CONNS` | `3`, `1` | Stay under the Postgres role limit of 5. |
| `RUN_MIGRATIONS` | `1` | Migrate the schema on start. |
| `CREATE_ADMIN`, `ADMIN_USERNAME` | `1`, `admin` | Create the `admin` user on first start. |
| `ADMIN_PASSWORD` | secret | Password for `admin`. |
| `BASE_URL` | `${{ app.url }}` | Public URL. |
| `POLLING_FREQUENCY`, `BATCH_SIZE`, `WORKER_POOL_SIZE` | `60`, `25`, `2` | Gentle polling for a small instance. |
| `CLEANUP_ARCHIVE_READ_DAYS`, `CLEANUP_ARCHIVE_UNREAD_DAYS` | `30`, `60` | Archive old entries so article bodies don't fill the quota. |

### Gotchas

- **Keep the cleanup settings.** The schema is about 9 MB. At the 50 MB quota the database turns read-only and refreshes fail.
- **Change the password in Miniflux's Settings**, not in the secret, after the first boot.

### After the first boot

1. Sign in as `admin` with `ADMIN_PASSWORD`.
2. Change the password in Settings.
3. Add feeds. Details: `examples/miniflux/README.md`.

## Forgejo (git forge)

[Forgejo](https://forgejo.org/) is a git forge with issues and pull requests. Git works over HTTPS only (no SSH), with no email and no Actions.

| | |
|---|---|
| Image | `codeberg.org/forgejo/forgejo:17.0.1-rootless` (USER 1000:1000) |
| Port | 3000 |
| Database | Platform MySQL, CA-verified (SQLite or PostgreSQL also work, see below) |
| Bucket | none |
| Volume | 1 GiB at `/var/lib/gitea` (required: git repositories and `app.ini`) |
| Template id | `forgejo` |
| RAM measured | about 90-100 MiB idle, 221 MiB peak (first boot + push + migration) |
| Verified | live on MySQL, 2026-11-03: sign-up as admin, repo push/clone over HTTPS, issue with emoji/CJK, GitHub migration, restart + 3 redeploys kept everything, closed sign-up holds; 3.4 MB of the 50 MB DB |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: forgejo
image: codeberg.org/forgejo/forgejo:17.0.1-rootless
port: 3000
healthCheck: { enabled: true, path: /api/healthz }
resources:
  db: true
  db_engine: mysql
  volume: { sizeGi: 1, mountPath: /var/lib/gitea }
env:
  ROOT_URL: "${{ app.url }}"
  HTTP_PORT: "3000"
  DISABLE_SSH: "true"
  DB_TYPE: "mysql"
  DB_HOST: "${{ db.host }}:${{ db.port }}"
  DB_NAME: "${{ db.name }}"
  DB_USER: "${{ db.user }}"
  DB_PASSWD: "${{ db.password }}"
  FORGEJO__DATABASE__SSL_MODE: "true"
  SSL_CERT_FILE: "${{ db.ca_file }}"
  FORGEJO__DATABASE__MAX_OPEN_CONNS: "3"
  FORGEJO__DATABASE__MAX_IDLE_CONNS: "2"
  FORGEJO__SERVER__START_SSH_SERVER: "false"
  FORGEJO__SERVER__OFFLINE_MODE: "true"
  FORGEJO__MAILER__ENABLED: "false"
  FORGEJO__CRON_UPDATE_CHECKER__ENABLED: "false"
  FORGEJO__ACTIONS__ENABLED: "false"
  FORGEJO__SERVICE__DISABLE_REGISTRATION: "false"
secrets:   # names only; set the values in the dashboard or API
  - SECRET_KEY
```

### Env vars

| Name | Value | Why |
|---|---|---|
| `ROOT_URL` | `${{ app.url }}` | Public URL, written to `app.ini` on the first boot only. |
| `HTTP_PORT` | `3000` | Matches the app `port`. |
| `DISABLE_SSH`, `FORGEJO__SERVER__START_SSH_SERVER` | `true`, `false` | Only HTTPS reaches the app. |
| `DB_TYPE` | `mysql` | Platform MySQL. Chosen on the first boot only. |
| `DB_HOST` | `${{ db.host }}:${{ db.port }}` | Host and port in one value. |
| `DB_NAME`, `DB_USER`, `DB_PASSWD` | `${{ db.* }}` refs | Credentials. |
| `FORGEJO__DATABASE__SSL_MODE` | `true` | TLS with certificate checks (not `skip-verify`). |
| `SSL_CERT_FILE` | `${{ db.ca_file }}` | Lets Go trust the platform CA. Outbound HTTPS (repo migrations) still works. |
| `FORGEJO__DATABASE__MAX_OPEN_CONNS`, `MAX_IDLE_CONNS` | `3`, `2` | Stays well under the 10-connection limit per app. |
| `FORGEJO__SERVER__OFFLINE_MODE` | `true` | No outbound lookups. |
| `FORGEJO__MAILER__ENABLED` | `false` | SMTP is blocked. |
| `FORGEJO__CRON_UPDATE_CHECKER__ENABLED` | `false` | No update checks. |
| `FORGEJO__ACTIONS__ENABLED` | `false` | No Actions. |
| `FORGEJO__SERVICE__DISABLE_REGISTRATION` | `false` at first | Set to `true` after you register. |
| `SECRET_KEY` | secret | Any long random string. Setting it skips the install wizard. |

### Database

MySQL is the default. Checked live 2026-11-03: a wrong CA (`SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`) fails with
`x509: certificate signed by unknown authority`, so the connection really is verified. Forgejo converts the database to
`utf8mb4` on the first boot (a warning in the log is normal).

**SQLite instead:** leave the database off, set `DB_TYPE=sqlite3` and drop the `DB_*`, `SSL_CERT_FILE` and
`FORGEJO__DATABASE__*` vars. The database file then lives on the volume.

**PostgreSQL instead** (verified live 2026-11-01): turn the database on with PostgreSQL and set:

| Name | Value |
|---|---|
| `DB_TYPE` | `postgres` |
| `DB_HOST` | `${{ db.host }}:${{ db.port }}` |
| `DB_NAME`, `DB_USER`, `DB_PASSWD` | `${{ db.* }}` refs |
| `FORGEJO__DATABASE__SSL_MODE` | `verify-full` |
| `PGSSLROOTCERT` | `${{ db.ca_file }}` |
| `FORGEJO__DATABASE__MAX_OPEN_CONNS` | `3` |

### Gotchas

- **Use the `-rootless` tag.** The plain tag runs as root and is rejected.
- **Env names.** `FORGEJO__SECTION__KEY` works with the UPPER_SNAKE rule because Forgejo lowercases the section.
- **First boot only.** `ROOT_URL`, `HTTP_PORT` and `DB_TYPE` are written to `app.ini` on the volume on the first boot. The `FORGEJO__*` variables apply on every boot. Pick the database before the first start; switching later needs a data migration.
- **Back up the volume and the database.** Repositories are on the volume, users and issues in MySQL.
- **Migrations with issues are slow.** Importing a GitHub repo with its issues can take minutes.

### After the first boot

1. Register your account straight away. The first account to register becomes the admin.
2. Set `FORGEJO__SERVICE__DISABLE_REGISTRATION=true` and redeploy.
3. Clone and push over HTTPS. Details: `examples/forgejo/README.md`.

## Memos (notes)

[Memos](https://usememos.com/) is a lightweight notes and microblog app on PostgreSQL. The official image runs as root, so
a two-line `Dockerfile` (`FROM <upstream>` + `USER 10001:10001`) is built by `examples.yml`. Memos drops privileges itself and already owns its data dir as 10001.

| | |
|---|---|
| Image | `ghcr.io/seaburr/picopaas-example-memos:latest` |
| Port | 5230 |
| Database | PostgreSQL |
| Bucket | none |
| Volume | none |
| Template id | `memos` |
| RAM measured | about 24 MiB; schema 8.8 MB of 50 MB |
| Verified | live, 2026-11-01: first account is admin, memos kept across a restart or redeploy, closed sign-ups hold |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: memos
image: ghcr.io/seaburr/picopaas-example-memos:latest
port: 5230
healthCheck: { enabled: true, path: /healthz }
resources:
  db: true
  db_engine: pg
env:
  MEMOS_PORT: "5230"
  MEMOS_DRIVER: "postgres"
  MEMOS_DSN: "postgres://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}?sslmode=verify-full&sslrootcert=${{ db.ca_file }}"
  MEMOS_INSTANCE_URL: "${{ app.url }}"
```

### Env vars

| Name | Value | Why |
|---|---|---|
| `MEMOS_PORT` | `5230` | Matches the app `port`. |
| `MEMOS_DRIVER` | `postgres` | Driver. |
| `MEMOS_DSN` | built from `${{ db.* }}` refs, `sslmode=verify-full&sslrootcert=${{ db.ca_file }}` | CA-verified TLS (same form as Miniflux). |
| `MEMOS_INSTANCE_URL` | `${{ app.url }}` | Public URL. |

### Gotchas

- **Attachments** are stored in the database by default and count toward the 50 MB quota.
- **API paths.** The REST API lives under `/api/...`. The old `/api/v1` paths are gone in 26.x.

### After the first boot

1. Create your account right away. The first account becomes the admin.
2. Turn off sign-ups in Memos' settings. Details: `examples/memos/README.md`.

## Vaultwarden (passwords)

[Vaultwarden](https://github.com/dani-garcia/vaultwarden) is a Bitwarden-compatible server. For test use, not passwords you need.
The official image runs as root, so a two-line `Dockerfile` (`FROM <upstream>` + `USER 10001:10001`) is built by `examples.yml`.

| | |
|---|---|
| Image | `ghcr.io/seaburr/picopaas-example-vaultwarden:latest` |
| Port | 8080 (`ROCKET_PORT`) |
| Database | none (SQLite on the volume) |
| Bucket | none |
| Volume | 1 GiB at `/data` (required) |
| Template id | `vaultwarden` |
| RAM measured | about 29 MiB |
| Verified | live, 2026-11-01: register and login over the Bitwarden API, account and key kept across a redeploy, closed sign-ups refuse new accounts |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: vaultwarden
image: ghcr.io/seaburr/picopaas-example-vaultwarden:latest
port: 8080
healthCheck: { enabled: true, path: /alive }
resources:
  volume: { sizeGi: 1, mountPath: /data }
env:
  DOMAIN: "${{ app.url }}"
  ROCKET_PORT: "8080"
  SIGNUPS_ALLOWED: "true"
  INVITATIONS_ALLOWED: "false"
  SHOW_PASSWORD_HINT: "false"
```

### Env vars

| Name | Value | Why |
|---|---|---|
| `DOMAIN` | `${{ app.url }}` | Public URL. |
| `ROCKET_PORT` | `8080` | The image default is not 8080. |
| `SIGNUPS_ALLOWED` | `true` at first | Set to `false` after you register. |
| `INVITATIONS_ALLOWED` | `false` | Invitations need email. |
| `SHOW_PASSWORD_HINT` | `false` | Hints need email. |

No secrets are needed. The SQLite database, attachments and the RSA signing key all live in `/data`.

### Gotchas

- **No email.** SMTP is blocked, so use an authenticator app for two-factor login.
- **`/admin` is off.** To enable it, add an `ADMIN_TOKEN` secret holding an Argon2 PHC hash (made with `vaultwarden hash`), never a plain password.

### After the first boot

1. Create your account right away.
2. Set `SIGNUPS_ALLOWED=false` and redeploy.

## Wiki.js 2 (wiki)

[Wiki.js](https://js.wiki/) is a wiki with a Markdown editor and page history, on PostgreSQL. Stay on 2.x: the 3.x image is 816 MB, over the image cap.

| | |
|---|---|
| Image | `requarks/wiki:2.5` (official; its USER is the name `node`, so it runs as 10001) |
| Port | 3000 |
| Database | PostgreSQL |
| Bucket | none |
| Volume | 1 GiB at `/wiki/data` (required) |
| Template id | `wikijs` |
| RAM measured | about 45 MiB idle, 143 MiB peak; schema 9.5 MB of 50 MB |
| Verified | live, 2026-11-01: setup, GraphQL login, page create and render, pages kept across a restart |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: wikijs
image: requarks/wiki:2.5
port: 3000
resources:
  db: true
  db_engine: pg
  volume: { sizeGi: 1, mountPath: /wiki/data }
env:
  DB_TYPE: "postgres"
  DB_HOST: "${{ db.host }}"
  DB_PORT: "${{ db.port }}"
  DB_NAME: "${{ db.name }}"
  DB_USER: "${{ db.user }}"
  DB_PASS: "${{ db.password }}"
  DB_SSL: "true"
  DB_SSL_CA: "${{ db.ca_cert_body }}"
  NODE_OPTIONS: "--max-old-space-size=300"
  HOME: "/tmp"
```

There is no health check in the template.

### Env vars

| Name | Value | Why |
|---|---|---|
| `DB_TYPE` | `postgres` | Driver. |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASS` | `${{ db.* }}` refs | Database connection. Note `DB_PASS`. |
| `DB_SSL` | `true` | TLS on. |
| `DB_SSL_CA` | `${{ db.ca_cert_body }}` | CA-verified TLS. Takes the certificate's base64 body only. |
| `NODE_OPTIONS` | `--max-old-space-size=300` | Heap ceiling inside the 384 MiB limit. |
| `HOME` | `/tmp` | A writable home for UID 10001. |

### Gotchas

- **Use `db.ca_cert_body`.** A full PEM fails with `SELF_SIGNED_CERT_IN_CHAIN`, and `DATABASE_URL` loses TLS.
- **The volume is required.** `/wiki/data` is not writable as 10001 in the image. Pages and uploads still live in the database.
- **An admin must raise DB connections to 10.** Wiki.js's knex pool (10) plus the render child process exceed the Postgres limit of 5, and saving pages fails with `too many connections for role`. The pool size is config-file only.
- **Uploaded images are stored in the database**, so watch the 50 MB quota.

### After the first boot

1. Ask an admin to set this app's `db_max_connections` to 10.
2. Open the app's URL and finish the setup page right away. Whoever completes it becomes the admin.
3. Details: `examples/wikijs/README.md`.

## Vikunja (tasks)

[Vikunja](https://vikunja.io/) is a to-do and project manager on PostgreSQL, with task attachments in the bucket.

| | |
|---|---|
| Image | `vikunja/vikunja:2.7.1` (official, USER 1000, 55 MB) |
| Port | 3456 |
| Database | PostgreSQL |
| Bucket | yes (attachments) |
| Volume | none |
| Template id | `vikunja` |
| RAM measured | about 27 MiB idle; database 10.4 MB of 50 MB; boot about 3 s |
| Verified | live, 2026-11-03: restart-safe |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: vikunja
image: vikunja/vikunja:2.7.1
port: 3456
healthCheck: { enabled: true, path: /api/v1/info }
resources:
  db: true
  db_engine: pg
  storage: true
env:
  HOME: "/tmp"
  VIKUNJA_SERVICE_PUBLICURL: "${{ app.url }}/"
  VIKUNJA_DATABASE_TYPE: "postgres"
  VIKUNJA_DATABASE_HOST: "${{ db.host }}:${{ db.port }}"
  VIKUNJA_DATABASE_DATABASE: "${{ db.name }}"
  VIKUNJA_DATABASE_USER: "${{ db.user }}"
  VIKUNJA_DATABASE_PASSWORD: "${{ db.password }}"
  VIKUNJA_DATABASE_SSLMODE: "verify-full"
  VIKUNJA_DATABASE_SSLROOTCERT: "${{ db.ca_file }}"
  VIKUNJA_DATABASE_MAXOPENCONNECTIONS: "3"
  VIKUNJA_DATABASE_MAXIDLECONNECTIONS: "2"
  VIKUNJA_FILES_TYPE: "s3"
  VIKUNJA_FILES_S3_ENDPOINT: "${{ storage.endpoint }}"
  VIKUNJA_FILES_S3_BUCKET: "${{ storage.bucket }}"
  VIKUNJA_FILES_S3_REGION: "${{ storage.region }}"
  VIKUNJA_FILES_S3_ACCESSKEY: "${{ storage.access_key }}"
  VIKUNJA_FILES_S3_SECRETKEY: "${{ storage.secret_key }}"
  VIKUNJA_FILES_S3_USEPATHSTYLE: "false"
  VIKUNJA_MAILER_ENABLED: "false"
secrets:   # names only; set the values in the dashboard or API
  - VIKUNJA_SERVICE_SECRET
```

### Env vars

| Name | Value | Why |
|---|---|---|
| `HOME` | `/tmp` | A writable home for UID 10001. |
| `VIKUNJA_SERVICE_PUBLICURL` | `${{ app.url }}/` | Public URL. |
| `VIKUNJA_DATABASE_TYPE` | `postgres` | Driver. |
| `VIKUNJA_DATABASE_HOST` | `${{ db.host }}:${{ db.port }}` | Host and port in one value. |
| `VIKUNJA_DATABASE_DATABASE`, `_USER`, `_PASSWORD` | `${{ db.* }}` refs | Database connection. |
| `VIKUNJA_DATABASE_SSLMODE`, `_SSLROOTCERT` | `verify-full`, `${{ db.ca_file }}` | CA-verified TLS. A negative test with the wrong CA failed with "unknown authority". |
| `VIKUNJA_DATABASE_MAXOPENCONNECTIONS`, `_MAXIDLECONNECTIONS` | `3`, `2` | Stay under the Postgres role limit of 5. |
| `VIKUNJA_FILES_TYPE` | `s3` | Attachments go to the bucket. |
| `VIKUNJA_FILES_S3_ENDPOINT`, `_BUCKET`, `_REGION`, `_ACCESSKEY`, `_SECRETKEY` | `${{ storage.* }}` refs | Bucket access. |
| `VIKUNJA_FILES_S3_USEPATHSTYLE` | `false` | Spaces uses virtual-hosted addressing. |
| `VIKUNJA_MAILER_ENABLED` | `false` | SMTP is blocked. |
| `VIKUNJA_SERVICE_SECRET` | secret | JWT signing key. Use this, not the deprecated `JWTSECRET`. |

### Gotchas

- **No email, so no password reset.** Keep your password safe.
- **Health check** is `/api/v1/info`.

### After the first boot

1. Register the first user.
2. Add `VIKUNJA_SERVICE_ENABLEREGISTRATION=false` and redeploy. Existing users keep working.
3. Details: `examples/vikunja/README.md`.

## Umami (web analytics)

[Umami](https://umami.is/) is privacy-friendly website analytics on PostgreSQL.

| | |
|---|---|
| Image | `ghcr.io/umami-software/umami:postgresql-latest` (3.4.1 when tested, USER nextjs, 316 MB) |
| Port | 3000 |
| Database | PostgreSQL |
| Bucket | none |
| Volume | none |
| Template id | `umami` |
| RAM measured | about 166-254 MiB idle, peak near 328 MiB during first-boot migrations (limit 384 MiB); database 9.5 MB |
| Verified | live, 2026-11-03 |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: umami
image: ghcr.io/umami-software/umami:postgresql-latest
port: 3000
healthCheck: { enabled: true, path: /api/heartbeat }
resources:
  db: true
  db_engine: pg
env:
  HOME: "/tmp"
  HOSTNAME: "0.0.0.0"
  DATABASE_URL: "postgresql://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}?sslmode=verify-full&sslrootcert=${{ db.ca_file }}"
  DISABLE_TELEMETRY: "1"
secrets:   # names only; set the values in the dashboard or API
  - APP_SECRET
```

### Env vars

| Name | Value | Why |
|---|---|---|
| `HOME` | `/tmp` | A writable home for UID 10001. |
| `HOSTNAME` | `0.0.0.0` | Listen on all interfaces. |
| `DATABASE_URL` | built from `${{ db.* }}` refs, `sslmode=verify-full&sslrootcert=${{ db.ca_file }}` | Connects and migrates. No negative CA test was run. |
| `DISABLE_TELEMETRY` | `1` | No outbound phone-home. |
| `APP_SECRET` | secret | Salt for session hashing. |

### Gotchas

- **Change the default login at once.** It is `admin` / `umami` and the app is public from the first request.
- **Memory is the tight part.** Avoid heavy reports on a small instance.
- **Use it on low-traffic sites.** Umami stores one row per event, so a busy site fills the 50 MB database.
- **Pin by digest.** There is no `postgresql-3.4.1` tag, so `postgresql-latest` moves. Pin the digest to freeze it.
- **Health check** is `/api/heartbeat`.

### After the first boot

1. Sign in as `admin` / `umami` and change the password.
2. Add your website and put the tracking snippet on it. Details: `examples/umami/README.md`.

## Planka (kanban boards)

[Planka](https://planka.app/) is a kanban board app on PostgreSQL, with attachments in the bucket. It is a recipe only: there is no template card (licence decision).

| | |
|---|---|
| Image | `ghcr.io/plankanban/planka:2.2.1` (official, USER node) |
| Port | 1337 |
| Database | PostgreSQL |
| Bucket | yes (attachments and avatars) |
| Volume | 1 GiB at `/app/.tmp` (upload staging) |
| Template id | none (spec in `examples/planka/planka.yaml`) |
| RAM measured | about 70 MiB idle; boot about 45 s; database about 10 MB |
| Verified | live, 2026-11-03 |

> **License.** Planka 2.x uses the PLANKA Community License (Fair Use v1.1), which bars running it as a commercial hosted
> service for third parties. Users may run it themselves on PicoPaaS. PicoPaaS does not offer or market it as a managed
> service. Read the license first.

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: planka
image: ghcr.io/plankanban/planka:2.2.1
port: 1337
healthCheck: { enabled: true, path: / }
resources:
  db: true
  db_engine: pg
  storage: true
  volume: { sizeGi: 1, mountPath: /app/.tmp }
env:
  HOME: "/tmp"
  LOG_FILE: "/tmp/planka.log"
  BASE_URL: "${{ app.url }}"
  DATABASE_URL: "postgresql://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}"
  PGSSLMODE: "verify-full"
  NODE_EXTRA_CA_CERTS: "${{ db.ca_file }}"
  KNEX_REJECT_UNAUTHORIZED_SSL_CERTIFICATE: "false"
  S3_ENDPOINT: "${{ storage.endpoint }}"
  S3_REGION: "${{ storage.region }}"
  S3_ACCESS_KEY_ID: "${{ storage.access_key }}"
  S3_SECRET_ACCESS_KEY: "${{ storage.secret_key }}"
  S3_BUCKET: "${{ storage.bucket }}"
  S3_FORCE_PATH_STYLE: "false"
  DEFAULT_ADMIN_EMAIL: "admin@example.com"
  DEFAULT_ADMIN_NAME: "Admin"
  DEFAULT_ADMIN_USERNAME: "admin"
  NODE_OPTIONS: "--max-old-space-size=256"
secrets:   # names only; set the values in the dashboard or API
  - SECRET_KEY
  - DEFAULT_ADMIN_PASSWORD
```

### Env vars

| Name | Value | Why |
|---|---|---|
| `HOME` | `/tmp` | A writable home for UID 10001. |
| `LOG_FILE` | `/tmp/planka.log` | The default `/app/logs` is not writable, and the app exits at start. |
| `BASE_URL` | `${{ app.url }}` | Public URL. |
| `DATABASE_URL` | built from `${{ db.* }}` refs, no TLS options | Planka's own settings cannot take a CA file. |
| `PGSSLMODE`, `NODE_EXTRA_CA_CERTS` | `verify-full`, `${{ db.ca_file }}` | TLS for the runtime connection. |
| `KNEX_REJECT_UNAUTHORIZED_SSL_CERTIFICATE` | `false` | The only way to get TLS for the migration step. |
| `S3_ENDPOINT`, `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET` | `${{ storage.* }}` refs | Bucket access. |
| `S3_FORCE_PATH_STYLE` | `false` | Spaces uses virtual-hosted addressing. |
| `DEFAULT_ADMIN_EMAIL`, `_NAME`, `_USERNAME` | literals | The first admin, created on first boot. Later edits don't change it. |
| `NODE_OPTIONS` | `--max-old-space-size=256` | Heap ceiling. |
| `SECRET_KEY` | secret | Signs sessions. |
| `DEFAULT_ADMIN_PASSWORD` | secret | The first admin's password. |

### Gotchas

- **The database connection is encrypted but not CA-verified.** A verified one needs a wrapper that patches the knexfile and datastores (not built).
- **Fixes for UID 10001.** `LOG_FILE=/tmp/planka.log`, and a writable `/app/.tmp` (the volume). Without it every attachment upload fails with `EACCES`.
- **Slow boot.** About 45 s.

### After the first boot

1. Log in with the default admin email and password.
2. Accept the terms on the first login.
3. Details: `examples/planka/README.md`.

## ntfy (push messages)

[ntfy](https://ntfy.sh/) sends push notifications over HTTP. The upstream image runs as root, so the wrapper adds `USER 10001:10001` and `CMD ["serve"]`. All state (cache, users, tokens) is in PostgreSQL.

| | |
|---|---|
| Image | `ghcr.io/seaburr/picopaas-example-ntfy:latest` (upstream v2.37.1) |
| Port | 8080 |
| Database | PostgreSQL |
| Bucket | none |
| Volume | none |
| Template id | `ntfy` |
| RAM measured | about 15 MiB; database about 8.3 MB of 50 MB |
| Verified | live, 2026-11-03 |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: ntfy
image: ghcr.io/seaburr/picopaas-example-ntfy:latest
port: 8080
healthCheck: { enabled: true, path: /v1/health }
resources:
  db: true
  db_engine: pg
env:
  NTFY_LISTEN_HTTP: ":8080"
  NTFY_BASE_URL: "${{ app.url }}"
  NTFY_BEHIND_PROXY: "true"
  NTFY_DATABASE_URL: "postgres://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}?sslmode=verify-full&sslrootcert=${{ db.ca_file }}&pool_max_conns=3"
  NTFY_AUTH_DEFAULT_ACCESS: "deny-all"
  NTFY_CACHE_DURATION: "12h"
secrets:   # names only; set the values in the dashboard or API
  - NTFY_AUTH_USERS
```

### Env vars

| Name | Value | Why |
|---|---|---|
| `NTFY_LISTEN_HTTP` | `:8080` | Matches the app `port`. |
| `NTFY_BASE_URL` | `${{ app.url }}` | Public URL. |
| `NTFY_BEHIND_PROXY` | `true` | Trust the proxy's forwarded address. |
| `NTFY_DATABASE_URL` | built from `${{ db.* }}` refs, `sslmode=verify-full&sslrootcert=${{ db.ca_file }}&pool_max_conns=3` | CA-verified TLS. A negative test with the system CA bundle fails `x509: certificate signed by unknown authority`, so verification is real. |
| `NTFY_AUTH_DEFAULT_ACCESS` | `deny-all` | Anonymous publish gets 403. An open ntfy is an abuse magnet. |
| `NTFY_CACHE_DURATION` | `12h` | Message history kept. |
| `NTFY_AUTH_USERS` | secret | Comma-separated `name:bcrypt-hash:role`. |

### Gotchas

- **Bake in `CMD ["serve"]`.** Upstream has `ENTRYPOINT ["ntfy"]` and no `CMD`, and PicoPaaS has no command override. The first published image lacked it and exited printing help.
- **Do not set cache-file, auth-file or web-push-file.** With a database URL, ntfy writes no files.
- **Nothing works until `NTFY_AUTH_USERS` has a user.** Access is deny-all.
- **Hashes contain `$`.** Single-quote the value in a shell or the CLI.
- **Users removed from `NTFY_AUTH_USERS`** are deleted at the next start.
- **No email.** SMTP is blocked.
- **Status.** The app can show `deploying` for a while after ntfy is already serving. Judge by `/v1/health`.

### After the first boot

1. Make a bcrypt hash with `docker run --rm -i binwiederhier/ntfy user hash`, and build `NTFY_AUTH_USERS` as `name:hash:admin`.
2. Deploy with the secret: `picopaas apply -f ntfy.yaml --secret 'NTFY_AUTH_USERS=name:$2a$10$...:admin'`.
3. Publish with `curl --user "$NTFY_USER" -d "deployed" https://<app>/mytopic`, where `NTFY_USER` is `name:password`.
4. Read back with `curl --user "$NTFY_USER" "https://<app>/mytopic/json?poll=1&since=all"`.

## IT-Tools (dev utilities)

[IT-Tools](https://it-tools.tech/) is a collection of developer tools (encoders, hashes, converters) that run in the browser. The wrapper copies the upstream static files into `nginx-unprivileged` with an SPA fallback so deep links work.

| | |
|---|---|
| Image | `ghcr.io/seaburr/picopaas-example-it-tools:latest` (about 108 MB) |
| Port | 8080 |
| Database | none |
| Bucket | none |
| Volume | none |
| Template id | `it-tools` |
| RAM measured | about 20 MiB (measured locally) |
| Verified | live, 2026-11-03: `/` and a deep link return 200 |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: it-tools
image: ghcr.io/seaburr/picopaas-example-it-tools:latest
port: 8080
healthCheck: { enabled: true, path: / }
```

### Env vars

None. Nothing to configure.

### Gotchas

- The upstream image may be stale. The wrapper inherits that: pin or build from a newer source tag to refresh.

### After the first boot

Open the app's URL. Everything runs in the browser. Details: `examples/it-tools/README.md`.

## Excalidraw (whiteboard)

[Excalidraw](https://excalidraw.com/) is a virtual whiteboard for sketching diagrams. The wrapper copies the upstream static files into `nginx-unprivileged` with an SPA fallback.

| | |
|---|---|
| Image | `ghcr.io/seaburr/picopaas-example-excalidraw:latest` (about 159 MB) |
| Port | 8080 |
| Database | none |
| Bucket | none |
| Volume | none |
| Template id | `excalidraw` |
| RAM measured | about 20 MiB (measured locally) |
| Verified | live, 2026-11-03: `/` and a deep link return 200 |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: excalidraw
image: ghcr.io/seaburr/picopaas-example-excalidraw:latest
port: 8080
healthCheck: { enabled: true, path: / }
```

### Env vars

None. Nothing to configure.

### Gotchas

- **Live collaboration and the library browser** call external services, which may not work from here. Drawing works fully in the browser.
- The upstream `excalidraw/excalidraw:latest` was pushed 2026-05-06, and the wrapper inherits that.

### After the first boot

Open the app's URL and draw. Details: `examples/excalidraw/README.md`.

## Gatus (status page)

[Gatus](https://github.com/TwiN/gatus) probes public endpoints on a schedule and shows a live dashboard. The config file is baked into the image, because apps have no config-file mount.

| | |
|---|---|
| Image | `ghcr.io/seaburr/picopaas-example-status:latest` (Gatus `v5.37.0`; make the package public) |
| Port | 8080 |
| Database | none |
| Bucket | none |
| Volume | none |
| Template id | `status` |
| RAM measured | not recorded |
| Verified | renders green out of the box |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: status
image: ghcr.io/seaburr/picopaas-example-status:latest
port: 8080
```

Fork `examples/status/`, edit `config.yaml`, build and push your own image, then point `image` at it.

### Env vars

None needed. Keep secrets out of `config.yaml`: reference them as `${ENV_VAR}` and set them as app env vars or secrets.

### Gotchas

- **Why a custom image.** The upstream `twinproduction/gatus` image is `FROM scratch` and runs as root. The wrapper sets a numeric `USER 10001:10001` (works on scratch, no `/etc/passwd` needed).
- **History is in memory** and resets on restart.
- **Alerts.** SMTP egress is blocked, so alert via a webhook (Discord, Slack, ntfy, Telegram), not email.
- **Watch services outside PicoPaaS.** Your app reaches the public internet, so checks on third-party APIs, other hosts and public DNS work. It cannot reliably watch services hosted on PicoPaaS itself, on a `*.picopaas.xyz` URL or a custom domain, because requests from inside the platform back to its own address don't loop around. Use an external monitor for those.

```dockerfile
FROM twinproduction/gatus:v5.37.0
USER 10001:10001
COPY config.yaml /config/config.yaml
ENV GATUS_CONFIG_PATH=/config/config.yaml
EXPOSE 8080
```

### After the first boot

1. Open the app's URL. The shipped config monitors `https://example.com` and a DNS lookup.
2. Replace the endpoints with your own and redeploy. Ask an admin for a reserved subdomain if you want a short URL.

## Static site (nginx)

A starter: `nginxinc/nginx-unprivileged:alpine` serving a default page. Swap in your own image once it works.

| | |
|---|---|
| Image | `nginxinc/nginx-unprivileged:alpine` (runs as UID 101) |
| Port | 8080 |
| Database | none |
| Bucket | none |
| Volume | none |
| Template id | `static-site` |
| RAM measured | not recorded |
| Verified | starter template |

### Deploy on PicoPaaS

```yaml
apiVersion: picopaas/v1
name: static-site
image: nginxinc/nginx-unprivileged:alpine
port: 8080
```

### Env vars

None.

### Gotchas

- It is a placeholder page. Replace the image with your own static site build.

### After the first boot

Open the app's URL to see the default page.

## Lessons for bringing your own image

- **Bake the start command into the image.** PicoPaaS has no command override: if the upstream image has only an
  `ENTRYPOINT` (ntfy: `ENTRYPOINT ["ntfy"]`, no `CMD`), the wrapper must add the subcommand (`CMD ["serve"]`). Test the
  wrapper locally with a bare `docker run <image>`, never with extra arguments the platform won't pass.
- **Run as a non-root numeric UID.** PicoPaaS forces UID 10001. If the stock image uses a root entrypoint, build
  a wrapper that does the root-time setup at build and runs as the UID.
- **Add an `/etc/passwd` entry for the UID.** Python 3.13 `getpass.getuser()` and Node `os.userInfo()` throw for a
  UID with no entry. Also set `HOME=/tmp`.
- **Env keys are UPPER_SNAKE.** If your app wants lowercase or nested config, translate in an entrypoint.
- **Use the refs:** `${{ app.url }}`, `${{ db.host }}`, `${{ db.port }}`, `${{ db.name }}`, `${{ db.user }}`,
  `${{ db.password }}`, `${{ db.url }}`, `${{ db.ca_cert }}`, `${{ db.ca_cert_body }}`, `${{ db.ca_file }}`,
  `${{ db.tls }}`, `${{ storage.endpoint }}`, `${{ storage.region }}`,
  `${{ storage.bucket }}`, `${{ storage.access_key }}`, `${{ storage.secret_key }}`. See the
  [User guide](USER_GUIDE.md) and [API](API.md) for the full set.
- **Build connection strings from parts.** A value can mix text and several refs, so a DSN such as
  `postgres://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}?sslmode=verify-full&sslrootcert=${{ db.ca_file }}`
  works (values are inserted as is, no encoding; generated passwords are letters and digits). For a client that wants
  the CA as a file, the platform mounts it at `/etc/picopaas/db-ca.pem` (`${{ db.ca_file }}`): no entrypoint needed.
- **No persistent filesystem.** The 250 MB scratch disk is wiped on restart. Keep state in the MySQL database and
  uploads in the blob storage bucket, and set your S3 client's public URL or asset host to the bucket's public Spaces URL.
- **No outbound SMTP.** Disable features that send email, or they will error.
- **Fit the caps:** 512 MB image, 200m CPU (the default), 384 MB RAM, 50 MB database, 100 MB bucket, 250 MB scratch disk.

## Apps we evaluated

Not every popular app runs on PicoPaaS, and we'd rather tell you up front than have you hit the wall yourself.
Two things define the shape of what fits: **images must run as a non-root numeric UID**, and the managed
database is shared, so its settings are platform-wide. DigitalOcean's MySQL used to enforce
`sql_require_primary_key=ON`; Ghost 6 (migration 6.61 creates a table without a primary key) and Wiki.js both
collide with that, so the platform setting is turned off for our cluster (request 0009). We conform to the apps rather than
patch them. Here's where that landed for two apps we tried.

### Also ruled out for now

Desk research and a live pass ([research notes](research/app-candidates-2026-11.md)) ruled these out for the 384 MB, 512 MB image and
no-sidecar limits: Mealie (needs about 1 GB), Stirling-PDF (JVM), Karakeep and Linkwarden (image at or over the cap, plus
Meilisearch and a browser), Docmost and Outline (Redis), Hoppscotch (several services), Plausible (ClickHouse), and Uptime Kuma's
default image (617 MB; only `2-slim` fits). Pingvin Share, PocketBase and File Browser are archived or moving upstream.

### FreshRSS — not supported

[FreshRSS](https://freshrss.org/) is a great self-hosted feed reader, but the official `freshrss/freshrss`
image **runs as root**. Its entrypoint does root-time work on every boot — rewriting its config, enabling
Apache modules, and `chown`-ing its data directory — so it can't simply be told to start as UID 1001. PicoPaaS
rejects root images (non-root only), and there's no env var that moves that startup work out of root.

The HTTP side was actually solvable: FreshRSS lets you move Apache off the privileged port 80 with the `LISTEN`
env var, which is all PicoPaaS needs (non-root containers can't bind ports below 1024). But the root entrypoint
was the blocker. FreshRSS would fit fine behind a **custom non-root image** (the same pattern as the Ghost
recipe: do the root-time setup at build, run as the UID), which we haven't built. Out of the box, it doesn't
run here.

### Wiki.js (now a recipe)

[Wiki.js](https://js.wiki/) came much closer than most. It runs **non-root** out of the box and even does TLS to
MySQL cleanly with a CA-verified connection (`DB_SSL_CA`). The only wall was in the database: its **first schema
migration creates a table without a primary key**, and DigitalOcean's managed MySQL used to reject that
(`sql_require_primary_key=ON`). That setting is being turned off cluster-wide (request 0009), so the blocker should be
gone. Wiki.js was re-tested on PostgreSQL on 2026-11-01 and works; see the recipe above.
Note the flip side: once the setting is off, tenants can create tables without primary keys, so apps no longer
get that guarantee from the server.

## Candidate recipes (2026-10-28)

> Historical research notes. The Miniflux, Vaultwarden, Memos, Forgejo and Wiki.js picks are now recipes (sections above); where a
> note below says the CA "must be a file" or "untested", it is out of date: use `${{ db.ca_file }}` and `${{ db.ca_cert_body }}`.

**Status 2026-11-01:** Miniflux, Forgejo and Wiki.js 2.5 run with the public images unchanged; Memos and Vaultwarden with
one-line wrappers (root upstream). All five are live-verified recipes and templates (sections above). Linkding and Uptime
Kuma also run as root (wrapper needed, not started).

Original research notes follow (2026-10-28). Compressed image sizes were read from the Docker Hub API on 2026-10-28
(the platform cap is 512 MB); RAM figures are third-party or upstream estimates and **must be measured** on a throwaway
app before a recipe is promoted. Fit rules used: non-root UID 10001 (or a wrapper like the Ghost recipe), single
container, port above 1024, no outbound SMTP, 384 MB RAM, 200m CPU default (override available), 50 MB DB,
1 GiB volume, daily backups. Existing recipes are Ghost, WordPress, n8n, Directus, Grafana, Shlink and Gatus.

| Pick | Engine / volume | Image (compressed) | Expected RAM | Effort |
|---|---|---|---|---|
| Miniflux (RSS reader) | PG, no volume | 14 MB | 30-60 MB | S |
| Vaultwarden (password manager) | SQLite on volume (or PG) | 63 MB (alpine) | 20-50 MB | S-M |
| Memos (notes / microblog) | PG, no volume | 26 MB | 30-80 MB | S |
| Linkding (bookmarks) | SQLite on volume | 51 MB (`latest-alpine`) | 80-150 MB | M |
| Forgejo or Gitea (git forge) | SQLite on volume or PG | about 70 MB (Gitea rootless 68 MB verified) | 100-200 MB | M-L |

### 1. Miniflux (best first pick)
- **Why viable:** Go binary, Apache-2.0, PostgreSQL only, which is exactly the engine we offer. Official Dockerfile is
  `USER 65534`, `EXPOSE 8080`, `LISTEN_ADDR=0.0.0.0:8080`, so the **public image runs unmodified** (no wrapper). Image is 14 MB.
  Useful for throwaway projects: a personal feed reader with an API.
- **Needs:** `resources.db: true`, `db_engine: pg`. Env: `DATABASE_URL` (a DSN, so build it from the `${{ db.* }}` refs with
  `sslmode=verify-full&sslrootcert=...`; the CA must be a file, so likely a small entrypoint writing `db.ca_cert` to `/tmp`,
  or `sslmode=require` as a fallback), `RUN_MIGRATIONS=1`, `CREATE_ADMIN=1`, `ADMIN_USERNAME`, `ADMIN_PASSWORD` (secret),
  `BASE_URL=${{ app.url }}`, `POLLING_FREQUENCY`. No volume, no bucket.
- **Risks:** 50 MB DB fills slowly but real: article bodies are stored, so set `CLEANUP_ARCHIVE_UNREAD_DAYS` and a low
  `BATCH_SIZE`/entry retention. At 100m CPU, feed refresh bursts are slow but harmless. Egress: check feeds are reachable
  through our egress policy. Verify its PG usage works with the platform's two-stage size quota (read-only default).
- **Effort:** small. Spec plus README, maybe a 10-line entrypoint for the CA file.

### 2. Vaultwarden
- **Why viable:** Rust, AGPL-3.0, tiny and fast, huge demand. Needs only a data folder, so it is the showcase for
  **volumes + backups**: SQLite DB, attachments and RSA keys live in `/data`. 63 MB alpine image.
- **Needs:** `resources.volume` mounted at `/data`, single replica (already enforced). Env: `DOMAIN=${{ app.url }}`,
  `ROCKET_PORT=8080` (the image defaults to 80, so this override is required), `ADMIN_TOKEN` (secret, or leave unset to disable
  the admin page), `SIGNUPS_ALLOWED=false` after the first account, `ROCKET_ADDRESS=0.0.0.0`. Optional PG instead of
  SQLite (`DATABASE_URL`) which avoids relying on the volume, but the 50 MB DB is plenty for a vault.
- **Risks:** the stock image's user is root; upstream documents running with `--user`, and `/data` then needs `fsGroup`
  (already set). Needs HTTPS (we provide). Email (invites, 2FA by mail) is impossible with SMTP blocked, so use TOTP only.
  WebSocket notifications need the ingress to pass upgrades. A password manager is a security-sensitive offering: a
  disclaimer that it is for throwaway or test use, and the restore drill must be proven first.
- **Effort:** small to medium (verify non-root + volume write + backup/restore).

### 3. Memos
- **Why viable:** Go, MIT, 26 MB image, supports PostgreSQL (`MEMOS_DRIVER=postgres`, `MEMOS_DSN`) so no volume is needed;
  SQLite on `/var/opt/memos` also works. Good for notes and a lightweight blog-like timeline.
- **Needs:** `db_engine: pg` and `MEMOS_DSN` (DSN built from refs), `MEMOS_PORT=8080`, `MEMOS_MODE=prod`. Optional bucket
  for attachments (S3 storage is a built-in setting), else DB or local attachments on a volume.
- **Risks:** the upstream Dockerfile starts as root and drops with `su-exec`; the entrypoint must be bypassed (run the binary
  directly as 10001) and the `nonroot` passwd entry is irrelevant but `HOME=/tmp` is wise. Attachments stored in the DB
  would burn the 50 MB quota, so default to S3 or a volume. Fast-moving versions (0.31 stable, 26.x rc), so pin.
- **Effort:** small. Wrapper image only if the entrypoint insists on root.

### 4. Linkding
- **Why viable:** Python/Django, MIT, bookmark manager with browser extension and API. Only the plain `latest-alpine` tag (51 MB) fits;
  the `-plus` variants with headless-browser archiving are 401 and 602 MB (over the cap, per 2026-11-02 research). SQLite on a volume at `/etc/linkding/data`, or PG.
- **Needs:** volume, `LD_SUPERUSER_NAME`, `LD_SUPERUSER_PASSWORD` (secret), `LD_SERVER_PORT=8080` if supported by the
  nginx/uwsgi config (verify), `LD_CSRF_TRUSTED_ORIGINS=${{ app.url }}`. Optional PG via `LD_DB_ENGINE=postgres`, `LD_DB_*`.
- **Risks:** runs nginx + uWSGI in one container; check non-root and that nginx can bind a high port and write its temp
  dirs (they need `/tmp`). Memory with uWSGI workers can pass 150 MB; cut `LD_REQUEST_*` workers. Favicon and snapshot
  fetches need egress.
- **Effort:** medium (the multi-process container is the unknown).

### 5. Forgejo (or Gitea)
- **Why viable:** Go, GPL-3.0 (Forgejo) or MIT (Gitea), the most "real" thing a throwaway project can host: git over HTTPS,
  issues, a registry. The **rootless** image exists (Gitea rootless is 68 MB; confirm Forgejo's rootless tag), runs as UID 1000
  by default, and idles at roughly 100-200 MB. Config is env-driven (`GITEA__section__KEY`, `FORGEJO__...`), so a small
  entrypoint is needed because our env keys are UPPER_SNAKE (the Ghost recipe's translation pattern; `GITEA__DATABASE__HOST`
  already fits `^[A-Z_][A-Z0-9_]*$`, so it may work with no shim).
- **Needs:** volume (repos, LFS) at `/var/lib/gitea`, `db_engine: pg` for metadata (SSL via `GITEA__database__SSL_MODE`,
  CA file again), `GITEA__server__ROOT_URL`, `DOMAIN`, `HTTP_PORT=3000`, `DISABLE_SSH=true` (we expose HTTP only; **SSH git
  is not available**), `INSTALL_LOCK=true`, admin via `gitea admin user create` at start.
- **Risks:** biggest of the five. 1 GiB volume caps repository size. Git operations are CPU-heavy at 100m (needs a CPU
  override). No SSH, no email. Abuse surface: a public forge can host arbitrary content, so keep signups off and treat as
  admin-approved. 50 MB DB is ample for metadata. Actions runners are out of scope.
- **Effort:** medium to large.

### Not picked, and why
- **Uptime Kuma:** Uptime Kuma 2.x full images are 617 MB (over the 512 MB cap); only the `2-slim` tag (185 MB) fits, per 2026-11-02 research (SQLite on a volume, about 150-250 MB RAM), but Gatus already covers
  status pages, so it is a duplicate. Kept as a reserve pick.
- **Listmonk:** Go and PG, but it exists to send email and SMTP is blocked. Only viable with an HTTP email API sender, so not a
  fit for the offer today.
- **Plausible / Umami:** Plausible needs ClickHouse (a second service). Umami is PG-only Node and plausible later, but its image
  was not verified here.
- **Nextcloud, Mattermost, Discourse, Metabase:** RAM or multi-service needs exceed 384 MB.

### Wiki.js 2.x (superseded)

Superseded by "Wiki.js 2 on PostgreSQL" above (live-tested 2026-11-01). Kept for the research notes.

**Why it was rejected:** its first schema migration creates a table without a primary key, and managed MySQL had
`sql_require_primary_key=ON` (see "Apps we evaluated" above and request 0009). Nothing else blocked it: it runs non-root and does
TLS with `DB_SSL_CA`.

**What changed:** PostgreSQL is the officially recommended Wiki.js database and we now offer it (so the MySQL primary-key
rule is moot), and the setting is off anyway. Volumes exist but Wiki.js does not need one: pages, history, users and
search index live in the database, and uploaded assets can be stored in the DB (counts against 50 MB) or sent to a storage target.

**Facts checked 2026-10-28:**
- `requarks/wiki:2` (2.5.317) is **173 MB compressed**, comfortably under 512 MB. Uncompressed layers are larger (not measured);
  confirm how the platform's cap is evaluated before deploy.
- `requarks/wiki:3.0.0-beta.*` is **816 MB**, over the cap and still beta (new betas published daily). **Use 2.x only.**
- Wiki.js publishes no official RAM minimum. Node app with webpack-built client; expect roughly 200-350 MB RSS (**unmeasured**;
  same ballpark as n8n's 338 MB). It likely **will not be stable at the 384 MB default**, so plan for the same admin memory
  override to ~512 MiB and `NODE_OPTIONS=--max-old-space-size=320` below the container limit.

**Verdict:** supportable, with caveats. Footprint (RAM) is the real limit, not the DB. Expect first-start migrations to
be CPU-heavy at 100m (give 300m+ for first boot, then drop). Sizing risk: the 50 MB DB holds many pages of text but
not many images; send assets to the bucket (S3 storage target needs the Spaces credentials configured in-app, not via env).

**Recipe sketch (`examples/wikijs/wikijs.yaml`, no custom image if the image really runs as UID 1000 without root work; else a wrapper):**
```yaml
apiVersion: picopaas/v1
name: wiki
image: ghcr.io/requarks/wiki:2.5   # pin 2.5.317; docker.io/requarks/wiki:2 is the same image
port: 3000
resources:
  db: true
  db_engine: pg
env:
  DB_TYPE: postgres
  DB_HOST: "${{ db.host }}"
  DB_PORT: "${{ db.port }}"
  DB_NAME: "${{ db.name }}"
  DB_USER: "${{ db.user }}"
  DB_PASS: "${{ db.password }}"
  DB_SSL: "true"
  DB_SSL_CA: "${{ db.ca_cert_body }}"   # verified 2026-11-01: base64 body only, not the full PEM
  NODE_OPTIONS: "--max-old-space-size=320"
```
Open checks: the image must run with a read-only-ish root FS (it writes `/wiki/data` for the git/sideload cache, so mount a
scratch path or set `DATA_PATH`); the setup wizard writes config to the DB, so the first visitor creates the admin (race on a
public URL: deploy and complete setup immediately); Git storage and search engine "DB - Basic" only (no Elasticsearch).
Rated: effort small if it passes the check, plus the admin memory override.
