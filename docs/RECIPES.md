# Recipes

PicoPaaS hosts real apps, not just toy images. This page walks through Ghost (a popular CMS) end to end, then
lists the lessons that apply to any image you bring.

> **Ghost 6.** New blogs use `ghcr.io/seaburr/picopaas-example-ghost6:latest`. Ghost 6 migration 6.61 creates a table without a
> primary key, so it needs the managed MySQL `sql_require_primary_key` setting off (request 0009). Ghost 5 is retired. The image carries performance patches (image-size probe timeout 400 ms, outbound webmentions and update check off, 2-minute frontend cache); set a Publication icon in Ghost Admin > Settings > Design so pages don't wait on a self-fetch. Upgrade runbook and rehearsal notes: `examples/ghost/README.md`.

## Deploying Ghost

The files are in `examples/ghost/` in the repo (`Dockerfile`, `docker-entrypoint.sh`).

### Why a custom image

The official `ghost:6-alpine` image starts as root and `su-exec`s down to `node`. PicoPaaS rejects root and forces
a non-root numeric UID (10001). So we build a thin image on top that runs as 10001 directly.

### The image

`FROM ghost:6-alpine`, plus:

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

This is abridged; the S3 adapter install is in the real file. The final image is about 185 MiB.

### The entrypoint shim

PicoPaaS env keys must match `^[A-Z_][A-Z0-9_]*$`, but Ghost wants lowercase, `__`-nested keys such as
`database__connection__host`. The entrypoint translates, then `exec`s Ghost:

- `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` become `database__connection__*`.
- `SITE_URL` becomes `url`.
- `DB_CA_CERT` becomes `database__connection__ssl__ca`, so MySQL TLS is verified.
- `S3_*` becomes the S3 storage config. `assetHost` is set to the public Spaces URL
  (`https://<bucket>.<region>.digitaloceanspaces.com`) so image links resolve. The adapter's default is an AWS-style URL, which is wrong for DigitalOcean Spaces.
- `security__staffDeviceVerification=false`. PicoPaaS blocks SMTP, so Ghost's sign-in verification email can't
  send and would lock you out of the admin.

### Deploy on PicoPaaS

Deploy from the dashboard (**New app**, filling in the fields below), or from the CLI / `curl` with a YAML spec.
The spec is the whole app: `name`, `image`, `port`, `resources` (database + bucket), and `env` — where you map
the platform refs onto the names Ghost's entrypoint expects.

**`ghost-blog.yaml`**

```yaml
apiVersion: picopaas/v1
name: ghost-blog
image: ghcr.io/seaburr/picopaas-example-ghost6:latest
port: 2368
resources:
  db: true
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

Quote the `${{ ... }}` values — the braces aren't valid unquoted YAML. No `secrets:` block is needed here:
every value is a platform ref, not a literal secret. (`${{ app.url }}` resolves to this app's own public URL — a custom domain or reserved subdomain if one is set, otherwise the generated host,
so there's no second step to set it; `${{ db.* }}` / `${{ storage.* }}` are injected from the database and bucket
you requested under `resources`.)

Get an API key from **Account → API keys** and export it:

```bash
export PICOPAAS_TOKEN=pp_sk_...
```

**CLI:**

```bash
picopaas apply -f ghost-blog.yaml
```

**curl** (the `/apps/spec` endpoint takes `{"spec": "<yaml>"}`; `jq -Rs` wraps the file for you):

```bash
curl -fsS -X POST https://platform.picopaas.xyz/apps/spec \
  -H "Authorization: Bearer $PICOPAAS_TOKEN" \
  -H "Content-Type: application/json" \
  --data "$(jq -Rs '{spec: .}' ghost-blog.yaml)"
```

Both return the created app with its generated `https://<name>.picopaas.xyz` hostname. It deploys in
one step (no URL to set afterward); open `/ghost/` there to finish setup. Re-running `apply` with the same `name`
updates the app in place.

Posts and settings live in MySQL and uploaded images in the bucket, so both survive restarts.

## Status / uptime page (Gatus)

The files are in `examples/status/` in the repo (`Dockerfile`, `config.yaml`, `README.md`).

### Why a custom image

Apps have no config-file mount, so Gatus's `config.yaml` has to be baked into the image. The upstream
`twinproduction/gatus` image is `FROM scratch` and runs as root; PicoPaaS rejects root, so the wrapper sets a numeric
`USER 10001:10001` (works on scratch, no `/etc/passwd` needed).

### The image

```dockerfile
FROM twinproduction/gatus:v5.37.0
USER 10001:10001
COPY config.yaml /config/config.yaml
ENV GATUS_CONFIG_PATH=/config/config.yaml
EXPOSE 8080
```

The template config monitors `https://example.com` and a DNS lookup, with in-memory storage (no persistent volume,
so history resets on restart). Replace the endpoints with your own public services. Keep secrets out of the file:
reference them as `${ENV_VAR}` and set them as app env vars. SMTP egress is blocked, so alert via a webhook
(Discord, Slack, ntfy, Telegram), not email.

### Deploy on PicoPaaS

Build and push the image (the `examples` workflow publishes `ghcr.io/seaburr/picopaas-example-status`; make the
package public). Then create an app with that image, port `8080`, and no database or bucket:

```yaml
apiVersion: picopaas/v1
name: my-status
image: ghcr.io/seaburr/picopaas-example-status:latest
port: 8080
```

Apply it as in the Ghost recipe. Ask an admin for a reserved subdomain if you want a short URL.

### What it can and can't watch

Use it to monitor services hosted **outside PicoPaaS** — third-party APIs, sites on other providers, public DNS. Your app can reach the public internet, so these checks work normally.

It can't reliably monitor services hosted **on PicoPaaS itself**, including your own PicoPaaS apps (whether on a `*.picopaas.xyz` URL or a custom domain) — requests from inside the platform back to its own address don't loop around. To track uptime of your PicoPaaS apps, use an external monitoring service.

## Grafana (dashboards)

The files are in `examples/grafana/` in the repo (`README.md`, `grafana.yaml` for MySQL, `grafana-postgres.yaml`
for PostgreSQL). Grafana runs on PicoPaaS backed by the platform's MySQL or PostgreSQL database (your choice), with
its data persisting across restarts (MySQL verified: health `200`, dashboards and users survive a redeploy). The
Templates page has one card per engine.

### No custom image needed

Unlike Ghost and the status page, Grafana needs **no Dockerfile and no wrapper**. The official
`grafana/grafana:13.2.4` image already runs as a non-root numeric UID (472), and Grafana is configured
entirely through `GF_*` environment variables — no config-file mount required. So you point PicoPaaS straight
at the public image and set env vars. Pin a version (`:13.2.4`), not `:latest`, so a silent upstream bump
can't break your app.

### Choose the database engine

Request a database (`db: true`), no bucket, and pick the engine with `resources.db_engine` (`mysql` or `pg`). The
engine is fixed once the app is created. Either way Grafana gets its connection from one variable:

- `GF_DATABASE_URL=${{ db.url }}` — one ref, the whole value. PicoPaaS only accepts a `${{ ... }}` value that
  is **exactly one** reference, so you can't build `GF_DATABASE_HOST` (`host:port`) from `${{ db.host }}`,
  `${{ db.port }}` and text. `db.url` hands Grafana host, port, name, user and password in a single string.

| Engine | `resources.db_engine` | `GF_DATABASE_TYPE` | `db.url` resolves to | TLS variables |
|---|---|---|---|---|
| MySQL | `mysql` | `mysql` | `mysql://user:password@host:port/name` | `GF_DATABASE_SSL_MODE=skip-verify`, `GF_DATABASE_CA_CERT_PATH=/etc/ssl/certs/ca-certificates.crt` |
| PostgreSQL | `pg` | `postgres` | `postgres://user:password@host:port/name?sslmode=require` | `GF_DATABASE_SSL_MODE=require` |

Everything else in the spec is identical. Grafana's PostgreSQL TLS mode comes from `GF_DATABASE_SSL_MODE`, not
from the URL query, so set it explicitly. `require` encrypts the connection without verifying the server
certificate, the same encrypted-but-unverified level as the MySQL setup (`verify-full` would need the CA as a
file, which PicoPaaS can't mount).

### MySQL: TLS and the CA file

Our managed MySQL requires TLS, and Grafana's MySQL driver insists on reading a CA file from disk even when
you tell it not to verify. So:

- `GF_DATABASE_SSL_MODE=skip-verify`
- `GF_DATABASE_CA_CERT_PATH=/etc/ssl/certs/ca-certificates.crt` — the CA bundle already baked into the Grafana
  image. Pointing at it satisfies the driver's file check.

This gives you **encrypted-but-unverified** TLS: the connection is encrypted, but Grafana doesn't validate the
server certificate against a specific CA. Full CA-*verified* TLS would need a PEM file mounted into the
container, which PicoPaaS doesn't offer (there's no config-file or volume mount). Encrypted-but-unverified to
the in-platform MySQL is the honest tradeoff here.

### Connection pool cap

Each database user may hold at most **10 connections**. Size Grafana's pool below that so a restart or a surge
doesn't trip the limit (both engines):

- `GF_DATABASE_MAX_OPEN_CONN=8`
- `GF_DATABASE_MAX_IDLE_CONN=2`

### The rest of the env

- `GF_SERVER_HTTP_PORT=3000` and app `port: 3000`.
- `GF_SERVER_ROOT_URL=${{ app.url }}` — Grafana's public URL, resolved at deploy time to this app's own host.
- `GF_SECURITY_ADMIN_USER` and `GF_SECURITY_ADMIN_PASSWORD` — the first-login admin. Set the password as a
  **secret**, not a literal in the spec.
- `GF_ANALYTICS_REPORTING_ENABLED=false`, `GF_ANALYTICS_CHECK_FOR_UPDATES=false` — Grafana's outbound phone-home
  is pointless here (tenant egress is restricted anyway) and these stop it logging failures trying.
- `GF_PLUGINS_PREINSTALL_DISABLED=true` — **required.** Grafana 13 otherwise downloads its "preinstalled" app
  plugins into `/var/lib/grafana/plugins` on every boot (~250 MB), which hits the 250 MB scratch-disk cap and gets
  the pod evicted in a restart loop. Disabling preinstall keeps that directory empty and the pod stable.

### Enable the readiness probe

Grafana has a real health endpoint, so turn the probe on and point it there:

```yaml
healthCheck: { enabled: true, path: /api/health }
```

`/api/health` returns `200` once Grafana has connected to its database, so the app only goes ready when it can
actually serve — no half-up pods behind the router.

### Deploy on PicoPaaS

Apply the spec exactly as in the Ghost recipe (dashboard **New app**, or `picopaas apply -f grafana.yaml`, or
the `/apps/spec` curl). The admin password is the one value that isn't a platform ref, so set it as a secret:
leave `GF_SECURITY_ADMIN_PASSWORD` in the spec's `secrets:` list (name only) and provide the value through the
dashboard's secret editor or the API — never in the YAML file. Open the app's URL and log in as your admin
user.

### Single replica only

HA and clustering are not supported on PicoPaaS. Run Grafana at **one replica**. PicoPaaS gives each app a single ClusterIP Service on the app port, and nothing
that lets one pod discover and address the others individually. Multi-replica setups that need pods to gossip
with named peers — Grafana's 2+ replica HA with unified-alerting memberlist, for example — have no way to find
each other, so they don't work. This is intentional and out of scope: the platform does not offer
headless/per-pod Services or peer discovery. A single-replica Grafana on MySQL or PostgreSQL is the supported shape, and it's
plenty for a throwaway dashboards instance.

## Shlink (URL shortener) on MySQL

The files are in `examples/shlink/` in the repo (`README.md`, `shlink.yaml`). [Shlink](https://shlink.io/) is a
self-hosted URL shortener with a REST API. It runs on PicoPaaS backed by the platform's MySQL database, with
short URLs and visit stats persisting across restarts (verified: health `200`, links survive a redeploy).

### No custom image needed

Like Grafana, Shlink needs **no Dockerfile and no wrapper**. The official `shlinkio/shlink:5.1.8` image already
runs as a non-root numeric UID (`USER 1001`) and is configured entirely through environment variables, so you
point PicoPaaS straight at the public image. Pin a version (`:5.1.8`), not `:latest`. The app listens on port
`8080` and has a real health endpoint at `/rest/health`. Request a database (`db: true`), no bucket
(`storage: false`).

### Point it at the platform MySQL

Shlink takes its database config as individual env vars, so map the platform refs one-to-one:

- `DB_DRIVER=mysql`
- `DB_HOST=${{ db.host }}`, `DB_PORT=${{ db.port }}`, `DB_NAME=${{ db.name }}`, `DB_USER=${{ db.user }}`
- `DB_PASSWORD=${{ db.password }}` — set this as a **secret**, not a literal in the spec.

### TLS to MySQL — one clean switch

Our managed MySQL requires TLS. Shlink has a first-class switch for it:

- `DB_USE_ENCRYPTION=true`

That's the whole story — no CA file to mount and no skip-verify to reason about, unlike the Grafana recipe above
where the driver forces a CA-path workaround. Shlink's PDO MySQL layer negotiates an encrypted connection
directly from this one flag.

### The public URL

It must be a bare host, not a full URL. Shlink's `DEFAULT_DOMAIN` is the domain your short URLs are built on, and it must be a **bare host** (no scheme,
no path) — e.g. `shlink.<domain>`. This is the one place you **cannot** use `${{ app.url }}`: that ref resolves
to a full `https://...` URL, which Shlink would fold into malformed short links. Use your reserved subdomain's
bare hostname instead (ask an admin for a reserved subdomain; see the Ghost recipe). Then tell Shlink it's
served over HTTPS:

- `DEFAULT_DOMAIN=shlink.<domain>` (bare host, literal — not a ref)
- `IS_HTTPS_ENABLED=true`

### Getting your first API key

Shlink's API and the short-URL management all go through an API key. Seed one at first boot:

- `INITIAL_API_KEY=<a long random string>` — set this as a **secret**. Shlink creates a key with this value on
  startup if none exists.

Then call the REST API with that key in the `X-Api-Key` header, for example:

```bash
curl -fsS https://shlink.<domain>/rest/v3/short-urls \
  -H "X-Api-Key: $INITIAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"longUrl": "https://example.com/some/very/long/link"}'
```

### Connection pool cap

Each database user may hold at most **10 connections**. Shlink runs a web worker pool and a background task
worker pool, each of which opens DB connections, so keep both small:

- `WEB_WORKER_NUM=4`
- `TASK_WORKER_NUM=4`

Those defaults stay comfortably under the 10-per-user limit even during a restart overlap. Raising them on a
throwaway instance mostly buys you a tripped connection cap.

### The GeoLite2 warning is harmless

Shlink logs a warning on boot that it couldn't download the GeoLite2 database (it needs a MaxMind license key
you haven't set). That only disables **IP geolocation** of visits — the shortener, redirects and visit counts
all work normally. Leave it off unless you want geolocation, in which case add a `GEOLITE_LICENSE_KEY` secret.

### Enable the readiness probe

Shlink has a real health endpoint, so turn the probe on and point it there:

```yaml
healthCheck: { enabled: true, path: /rest/health }
```

`/rest/health` returns `200` once Shlink has connected to its database, so the app only goes ready when it can
actually serve.

### Deploy on PicoPaaS

Apply the spec exactly as in the Ghost recipe (dashboard **New app**, or `picopaas apply -f shlink.yaml`, or the
`/apps/spec` curl). The database credentials come from platform refs (so `DB_PASSWORD: ${{ db.password }}` stays
in `env` — the platform injects the real value), but `INITIAL_API_KEY` is a literal you choose, so it's a
**secret**: list it by name under `secrets:` and provide the value through the dashboard's secret editor or the
API, never in the YAML file. Open the app's URL to confirm
it's up, then drive it with the REST API using your key.

Short URLs and visit stats live in MySQL, so they survive restarts.

## Directus on MySQL + S3

The files are in `examples/directus/` in the repo (`README.md`, `directus.yaml`).
[Directus](https://directus.io/) is an open-source headless CMS and data platform. It runs on PicoPaaS backed by
the platform's MySQL database **and** the Spaces bucket, so it's the recipe that exercises the **full platform
stack at once** (verified live at `https://directus.picopaas.xyz`: its data and an uploaded file both survived a
pod restart, with CA-verified TLS to MySQL and no `sql_require_primary_key` issue).

### No custom image needed

Like Grafana and Shlink, Directus needs **no Dockerfile and no wrapper**. The official
`directus/directus:12.4.1` image already runs as a non-root user (`node`), is about **242 MB** (under the 256 MB
cap), and is configured entirely through environment variables. Point PicoPaaS straight at the public image,
port `8055`. Pin a version (`:12.4.1`), not `:latest`. Request **both** a database (`db: true`) and a bucket
(`storage: true`), and leave `auto_update: false`.

### Point it at MySQL (CA-verified)

This connection uses CA-verified TLS. Directus takes its database config as individual env vars, so map the platform refs one-to-one:

- `DB_CLIENT=mysql`
- `DB_HOST=${{ db.host }}`, `DB_PORT=${{ db.port }}`, `DB_DATABASE=${{ db.name }}`, `DB_USER=${{ db.user }}`
- `DB_PASSWORD=${{ db.password }}` — a ref; the platform injects the real password at deploy time.

Our managed MySQL requires TLS, and here's the nice part:

- `DB_SSL__CA=${{ db.ca_cert }}` — **CA-verified** TLS. Directus (via Knex) accepts the CA *content* directly in
  this variable, so you get a fully verified connection with **no file to mount** and no skip-verify to reason
  about. This is cleaner than the Grafana recipe, where the driver forces a CA-path-on-disk workaround and you
  settle for encrypted-but-unverified.

Keep the connection pool under the **10-connection-per-DB-user** cap:

- `DB_POOL__MIN=0`, `DB_POOL__MAX=5`

### Files on the Spaces bucket (S3)

Directus stores uploaded files through an S3 driver, pointed at the platform's Spaces bucket:

- `STORAGE_LOCATIONS=s3`, `STORAGE_S3_DRIVER=s3`
- `STORAGE_S3_KEY=${{ storage.access_key }}`, `STORAGE_S3_SECRET=${{ storage.secret_key }}` (a ref; injected at
  deploy time), `STORAGE_S3_BUCKET=${{ storage.bucket }}`, `STORAGE_S3_REGION=${{ storage.region }}`,
  `STORAGE_S3_ENDPOINT=${{ storage.endpoint }}`

No path-style flag is needed for Spaces — the default virtual-hosted addressing works.

### Secrets and admin

- `KEY` and `SECRET` — two required random strings Directus needs for signing and encryption. **Generate them
  once and keep them stable** across deploys: rotating them invalidates sessions and breaks data encrypted at
  rest. Set both as **secrets**.
- `ADMIN_EMAIL` (a literal in the spec) plus `ADMIN_PASSWORD` (a **secret**) — the first admin account.
- `PUBLIC_URL=${{ app.url }}` — Directus's public URL, resolved at deploy time to this app's own host.
- `TELEMETRY=false` — turn off the outbound phone-home (tenant egress is restricted anyway).

### Required `HOME` and `PM2_HOME`

Set both, or the app never serves:

- `HOME=/tmp`
- `PM2_HOME=/tmp/.pm2`

The image launches Directus under a pm2 wrapper that, on boot, tries to `mkdir` a home directory for the
non-root UID. The container's real home isn't writable by that UID, so without these two pointing pm2 at a
writable path (`/tmp` is writable for any UID) it fails with `EACCES` and the process dies before it binds the
port. This is the single most important line in the recipe.

### Leave the health check OFF

This is the one place Directus bends the usual advice. **Don't enable the readiness/liveness probe:**

```yaml
healthCheck: { enabled: false }
```

Directus **cold-starts slowly** — about **90–110s** on the 0.2-core CPU limit, because it loads its module
graph twice during boot. The platform's liveness-probe timing (30s initial delay, then 3×20s) is far too tight
for that, so an enabled probe decides the pod is dead and **restart-loops it** before Directus ever finishes
starting. Turning the probe off lets it boot.

The trade-off is real and worth stating plainly: with no probe, the app has **no liveness protection** (a wedged
process won't be auto-restarted) and you'll see **~90s of downtime on every deploy or restart** while it cold-starts.
For a throwaway CMS that's an acceptable deal; for anything you lean on, know what you're giving up. (If the
platform's probe timing is ever relaxed, `/server/ping` is the public health path to point at — note that
`/server/health` returns `403` to unauthenticated callers, so it's not usable as a probe path.)

### Two harmless boot notes

- A WARN on boot: `Couldn't bundle App extensions (EACCES ...)`. The container filesystem is read-only and you
  aren't using custom extensions, so this is cosmetic — the admin UI and API work normally.
- Run a **single replica**. Scaled-out Directus wasn't tested here (and see the Grafana recipe on why
  multi-replica clustering isn't a platform feature).

### Deploy on PicoPaaS

Apply the spec exactly as in the Ghost recipe (dashboard **New app**, or `picopaas apply -f directus.yaml`, or
the `/apps/spec` curl). Three values aren't platform refs — `KEY`, `SECRET`, and `ADMIN_PASSWORD` — so list them
by name under `secrets:` and provide the values through the dashboard's secret editor or the API, never in the
YAML file. Give the first boot ~90s, then open the app's URL and log in as `ADMIN_EMAIL` / `ADMIN_PASSWORD`.

Directus is a clean demonstration of the full stack: it runs **non-root**, needs **no custom build**, is
**PK-clean** (no `sql_require_primary_key` collision), and uses **MySQL and the Spaces bucket together** — so
both your content and your uploads survive restarts.

## n8n on PostgreSQL

The files are in `examples/n8n/` in the repo (`README.md`, `n8n.yaml`). [n8n](https://n8n.io/) is an
open-source workflow-automation tool. It's PicoPaaS's **first PostgreSQL-backed recipe**, and the proof that
the new managed Postgres engine works end to end (verified live at `https://n8n.picopaas.xyz`): a per-app
Postgres database and role, auto-provisioned by the platform, reached over CA-verified TLS, with a created
workflow **and** the owner account surviving a pod restart.

### No custom image needed

Like Grafana, Shlink and Directus, n8n needs **no Dockerfile and no wrapper**. The official
`n8nio/n8n:2.41.7` image already runs as a non-root user, listens on port `5678`, and is configured entirely
through environment variables. Point PicoPaaS straight at the public image. Pin a version (`:2.41.7`), not
`:latest`. It has a real health endpoint at `/healthz`, so turn the readiness probe on:

```yaml
healthCheck: { enabled: true, path: /healthz }
```

### Choosing the engine

This recipe introduces the `db_engine` selector. Request a database and set the engine to `pg`:

```yaml
resources:
  db: true
  db_engine: pg   # mysql | pg
```

`db_engine: pg` tells PicoPaaS to back the app with **PostgreSQL** instead of MySQL. The platform
**auto-provisions a per-app Postgres database and role** (exactly as it does for MySQL — a dedicated
`app_<id>` database and login role, isolated from every other tenant), and the same `${{ db.* }}` refs
resolve to the Postgres connection details. The engine is **immutable** once the app has a database (to
switch, create a new app). MySQL and PostgreSQL are equal choices (a spec that omits `db_engine` still gets MySQL, so older specs are unaffected). n8n is Postgres-first,
which is why it's the recipe that exercises this path.

### Point it at Postgres (TLS)

The connection uses CA-verified TLS. n8n takes its database config as individual env vars, so map the platform refs one-to-one:

- `DB_TYPE=postgresdb`
- `DB_POSTGRESDB_HOST=${{ db.host }}`, `DB_POSTGRESDB_PORT=${{ db.port }}`,
  `DB_POSTGRESDB_DATABASE=${{ db.name }}`, `DB_POSTGRESDB_USER=${{ db.user }}`
- `DB_POSTGRESDB_PASSWORD=${{ db.password }}` — a ref; the platform injects the real password at deploy time.
  (On the app it's stored as a secret env var, but its *value* is this platform ref, so there's no literal to
  put in the file.)

Our managed Postgres requires TLS, and n8n's Postgres driver takes the CA content inline — no file to mount:

- `DB_POSTGRESDB_SSL_ENABLED=true`
- `DB_POSTGRESDB_SSL_CA=${{ db.ca_cert }}` — the platform CA PEM, passed as a value
- `DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED=true` — **CA-verified** TLS (the server cert is validated against that
  CA), cleaner than the Grafana skip-verify dance and matching Directus's MySQL `DB_SSL__CA` approach.

### Postgres pool cap

Managed Postgres is tighter than MySQL here. Each per-app role gets a **CONNECTION LIMIT of 5** (not 10): the
cheapest 1 GB Postgres node has only ~22 usable connections total. Size n8n's pool well under it:

- `DB_POSTGRESDB_POOL_SIZE=2`

### Pin the encryption key

Without a volume, pinning is required so credentials survive restarts (with a volume it is optional; see below). n8n encrypts stored credentials with `N8N_ENCRYPTION_KEY`. The scratch disk is wiped on every restart, so if
the key isn't pinned, a restart regenerates it and **every saved credential becomes undecryptable**:

- `N8N_ENCRYPTION_KEY` — set it as a **secret**, generate it once, and keep it stable across deploys. This is
  the one value that isn't a platform ref.

The workflow definitions and the owner account live in Postgres (not on disk), so with the key pinned they
survive restarts — which is exactly what we verified.

### Persistent volume (recommended)

Persistent volumes are enabled on picopaas.xyz, and n8n is the app they were made for. n8n keeps its encryption key,
binary data, community nodes and (by default) its SQLite database in the user folder. Mount a volume there and add
this to the spec:

```yaml
resources:
  db: true
  db_engine: pg
  volume: { sizeGi: 1, mountPath: /home/node }
env:
  N8N_USER_FOLDER: /home/node
```

Two details cost us a few failed boots, so copy them exactly:

- **Mount at `/home/node`, not `/home/node/.n8n`.** The image runs as UID 10001 with no usable HOME, so n8n resolves its
  user folder from `N8N_USER_FOLDER` and creates `<folder>/.n8n` (config, storage, nodes, event log, SQLite) **and**
  `<folder>/.cache` (the compiled web UI, about 29 MB). Mounting only `.n8n` makes n8n die on boot with
  `EACCES: mkdir '/home/node/.cache'`. With no `N8N_USER_FOLDER` it tries `/.n8n` and dies the same way. Mounting the
  whole home puts both on the volume.
- `N8N_USER_FOLDER=/tmp` (scratch disk) is only for the volume-less setup further down.

Verified live on a throwaway app (pod restarted through `POST /apps/{id}/restart`, about one minute of downtime):

| Check | Result |
|---|---|
| Boot with the volume, Postgres variant and SQLite-on-volume variant | Healthy in both. The image passed the 512 MB image cap. |
| **No `N8N_ENCRYPTION_KEY` set** | n8n generated a key into `.n8n/config` on the volume. After a restart a stored credential still decrypted, the workflow was still active, and the webhook still answered. Pinning the key is no longer needed to survive restarts. |
| Binary data, `N8N_DEFAULT_BINARY_DATA_MODE=filesystem` | Works. A 200 KB file from a Code node is written under `.n8n/storage/workflows/<id>/executions/<n>/binary_data/` and the execution, with its file, was still there after the restart. It counts against the volume, not the 250 MB scratch disk. |
| SQLite on the volume (`database.sqlite` plus `-wal`, no `db: true`) | Owner, credential, workflow and executions survived a restart. This is the cheapest setup, with no database quota to manage. It suits one person and light use. |
| Postgres plus volume | Same results; the volume then holds only key, binary data and nodes. Prefer it when you want the 50 MB quota separated from files or want database backups. |
| Backups | A manual backup of the Postgres variant succeeded for both parts: `db` (0.5 MB) and `volume` (29 MB, mostly the UI cache). The volume part is a file-level copy, so for the SQLite variant stop writes (or use Postgres) if you need a consistent copy. Restoring a volume is not self-service yet. |

Still pin `N8N_ENCRYPTION_KEY` as a secret if you restore the database into a new app or may lose the volume: the key
is also what decrypts the credentials stored in Postgres. With a pinned key n8n skips generating one.

What this changes compared with running without a volume:

| In `/home/node/.n8n` | With a volume |
|---|---|
| `config` (encryption key) | Survives. |
| `storage/` (binary data) | Survives; no more scratch-disk pressure. Prune old executions so it doesn't fill the volume. |
| `nodes/` (community nodes) | Survive restarts. We did not test installing one; keep `N8N_COMMUNITY_PACKAGES_ENABLED=false` unless you need it. |
| `n8nEventLog.log` | Survives, so interrupted executions recover better. |
| `database.sqlite` | Survives, so a database is optional. |

Caveats for volume apps:

- **One replica, short outage on every deploy.** The volume attaches to a single pod, so every deploy, restart and node
  move stops the old pod first (we measured about 56 seconds to Running).
- **Memory is unchanged: 512 MiB override still required.** With the default 384 MiB cap n8n was OOM-killed in a loop,
  even on SQLite and with the trimmed `NODES_INCLUDE` list. With the full node catalog it OOMs even at 512 MiB, so keep
  `NODES_INCLUDE` or ask for a larger override. A volume does not change this.
- **First boot is slow, and `/healthz` answers early.** On a fresh database n8n runs hundreds of migrations
  (1 to 2 minutes at 200m CPU). The app can show Running while `/rest/*` still returns 404 for another minute or so, and
  the first boot may be restarted once by the probes. Wait for `/rest/settings` to return 200 before scripting against it.
- **The UI cache lives on the volume**, so allow 1 GiB: about 29 MB is cache, the rest is your data.

### Without a volume: `N8N_USER_FOLDER=/tmp`

If volumes are off on your platform, point the user folder at the scratch disk:

- `N8N_USER_FOLDER=/tmp`

The image's default HOME isn't writable as the non-root UID, so n8n can't create its `.n8n` working directory
and dies on boot. Pointing it at `/tmp` (writable for any UID) fixes it. This is the n8n equivalent of
Directus's `HOME=/tmp` / `PM2_HOME=/tmp/.pm2` line. Everything below in this section ("What is lost on restart") applies
only to this setup; with a volume you can skip it.

### What is lost on restart (no volume)

Without a volume, these live in the scratch folder `/tmp/.n8n` instead of Postgres. Your workflows, credentials, users and execution history are in Postgres and
survive restarts (verified). These are what live in the folder:

| In `/tmp/.n8n` | What it's for | Effect of losing it on restart |
|---|---|---|
| `config` | Copy of the encryption key | None. `N8N_ENCRYPTION_KEY` is pinned in env. |
| `storage/` | Binary data from executions (files flowing through workflows). n8n 2.x stores these on disk by default. | Old executions' file attachments disappear, and a workflow paused on a Wait node with a file loses it. Large files also count toward the **250 MB scratch disk**, so a workflow that moves big files can get the pod evicted. |
| `nodes/` | Community nodes installed from the UI | They're gone after a restart. Stick to built-in nodes. |
| `n8nEventLog.log` | Event bus log, used to recover executions interrupted by a crash | Weaker crash recovery for executions that were in flight. |

Database binary mode (`N8N_DEFAULT_BINARY_DATA_MODE=database`) survives restarts
but uses the 50 MB database quota (see below). The S3 mode needs an n8n Enterprise license. So without a volume n8n suits
**light automation that doesn't handle files**, such as webhooks, API calls and notifications. It doesn't suit file
pipelines. Use the volume setup above for those.

### Prune executions (50 MB)

n8n's schema alone uses about **16 MB** of the 50 MB quota, measured with one workflow and almost no runs. By default
n8n keeps 14 days or 10,000 executions of history. A workflow on a frequent schedule would fill the rest within days.
The database would then go read-only and n8n would stop working. Keep the history small:

- `EXECUTIONS_DATA_SAVE_ON_SUCCESS=none` saves only failed runs. Successful runs are the bulk of the history.
- `EXECUTIONS_DATA_MAX_AGE=72` keeps history for 72 hours.
- `EXECUTIONS_DATA_PRUNE_MAX_COUNT=1000` caps the number of runs kept.

Watch the database bar on the app page. If it climbs, lower the two numbers.

### Startup-warning settings

Three more settings, taken from n8n's own startup warnings:

- `N8N_PROXY_HOPS=1`. The app sits behind Traefik, which sets `X-Forwarded-For`. Without this, n8n logs
  `ERR_ERL_UNEXPECTED_X_FORWARDED_FOR`, and its login rate-limiter sees every visitor as the proxy's IP, so one
  person's failed logins can lock everyone out.
- `N8N_COMMUNITY_PACKAGES_ENABLED=false`. Without a volume, community nodes installed from the UI live in the user folder and vanish
  on the next restart, while workflows that use them stay in the database and break. Turning installs off avoids
  that trap.
- **If you change the app's address, update `N8N_HOST`, `WEBHOOK_URL` and `N8N_EDITOR_BASE_URL` too.** n8n uses
  them to build webhook and OAuth callback URLs. If they point at an old name, the URLs n8n shows you are dead links.

The **Code node runs JavaScript only.** The image has no Python, so n8n logs that it couldn't start the Python task
runner, and Python code fails. n8n 2.x also warns that the built-in task-runner mode is deprecated. The
recommended "external" mode needs a second container, which PicoPaaS doesn't support, so ignore that warning.

### Public URL and env

- `N8N_HOST` (bare host, e.g. `n8n.picopaas.xyz`), `N8N_PROTOCOL=https`, `N8N_PORT=5678`,
  `WEBHOOK_URL=https://<host>/`, `N8N_EDITOR_BASE_URL=https://<host>` — n8n wants these set to its own public
  address (a reserved subdomain or custom domain if you have one; ask an admin for a reserved subdomain as in
  the Ghost recipe).
- `GENERIC_TIMEZONE=UTC`.

### Memory override

n8n needs an admin override to ~512 MiB. This is the honest caveat. n8n **OOMs at the 384 MiB default cap**, so it needs an **admin resource override to
~512 MiB** (we verified 512 MiB holds; n8n used ~338 MiB at idle). Memory isn't a spec field — ask an admin to
raise the limit for the app, the same control that raises CPU.

The interplay with Node's heap cap matters: `NODE_OPTIONS=--max-old-space-size=384` sets the V8 heap ceiling,
and it must stay **below** the container memory limit or you just trade one OOM for another — too low and Node
heap-OOMs, too high and the container OOMs. We run **384 MiB of heap inside a 512 MiB container**.

Optional footprint-trimming flags (all from the live env) keep n8n lean on the small instance:

- `N8N_RUNNERS_ENABLED=false`, `N8N_DIAGNOSTICS_ENABLED=false`,
  `N8N_VERSION_NOTIFICATIONS_ENABLED=false`, `N8N_TEMPLATES_ENABLED=false` — turn off task runners, the
  outbound phone-home, update checks and the templates gallery (none useful on a throwaway, egress-restricted
  instance).
- `NODES_INCLUDE=["n8n-nodes-base.webhook", ...]` — load only the nodes you use instead of the whole catalog,
  which noticeably cuts memory. Drop it to load everything (and raise the memory override accordingly).

### Deploy on PicoPaaS

Apply the spec exactly as in the Ghost recipe (dashboard **New app** with **Database** ticked and the engine
set to Postgres, or `picopaas apply -f n8n.yaml`, or the `/apps/spec` curl). The one value that isn't a
platform ref is `N8N_ENCRYPTION_KEY`, so list it by name under `secrets:` and provide the value through the
dashboard's secret editor or the API, never in the YAML file. Ask an admin to raise the memory limit to
~512 MiB. Then open the app's URL, create the owner account, and build a workflow.

n8n is the end-to-end proof of managed Postgres on PicoPaaS: a per-app database and role, CA-verified TLS, and
data that survives restarts.

## WordPress (nginx + PHP-FPM, volume, MySQL)

A non-root WordPress image lives in `examples/wordpress/` (PHP 8.5, WordPress 7.1, WP-CLI, automatic locked-down first install,
optional volume for the whole install, one-time restore from a backup bundle). Start with `examples/wordpress/README.md`
for the spec and env vars, and `examples/wordpress/MIGRATION.md` to move an existing site onto PicoPaaS.

## Lessons for bringing your own image

- **Run as a non-root numeric UID.** PicoPaaS forces UID 10001. If the stock image uses a root entrypoint, build
  a wrapper that does the root-time setup at build and runs as the UID.
- **Add an `/etc/passwd` entry for the UID.** Python 3.13 `getpass.getuser()` and Node `os.userInfo()` throw for a
  UID with no entry. Also set `HOME=/tmp`.
- **Env keys are UPPER_SNAKE.** If your app wants lowercase or nested config, translate in an entrypoint.
- **Use the refs:** `${{ app.url }}`, `${{ db.host }}`, `${{ db.port }}`, `${{ db.name }}`, `${{ db.user }}`,
  `${{ db.password }}`, `${{ db.ca_cert }}`, `${{ storage.endpoint }}`, `${{ storage.region }}`,
  `${{ storage.bucket }}`, `${{ storage.access_key }}`, `${{ storage.secret_key }}`. See the
  [User guide](USER_GUIDE.md) and [API](API.md) for the full set.
- **No persistent filesystem.** The 250 MB scratch disk is wiped on restart. Keep state in the MySQL database and
  uploads in the blob storage bucket, and set your S3 client's public URL or asset host to the bucket's public Spaces URL.
- **No outbound SMTP.** Disable features that send email, or they will error.
- **Fit the caps:** 512 MB image, 200m CPU, 384 MB RAM, 50 MB database, 100 MB bucket, 250 MB scratch disk.

## Apps we evaluated

Not every popular app runs on PicoPaaS, and we'd rather tell you up front than have you hit the wall yourself.
Two things define the shape of what fits: **images must run as a non-root numeric UID**, and the managed
database is shared, so its settings are platform-wide. DigitalOcean's MySQL used to enforce
`sql_require_primary_key=ON`; Ghost 6 (migration 6.61 creates a table without a primary key) and Wiki.js both
collide with that, so the platform setting is being turned off (request 0009). We conform to the apps rather than
patch them. Here's where that landed for two apps we tried.

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

### Wiki.js: untested since the primary-key change (see the 2026-10-28 re-assessment below)

[Wiki.js](https://js.wiki/) came much closer than most. It runs **non-root** out of the box and even does TLS to
MySQL cleanly with a CA-verified connection (`DB_SSL_CA`). The only wall was in the database: its **first schema
migration creates a table without a primary key**, and DigitalOcean's managed MySQL used to reject that
(`sql_require_primary_key=ON`). That setting is being turned off cluster-wide (request 0009), so the blocker should be
gone. We have **not re-tested Wiki.js since the change**, so treat it as "should work, unverified" until someone runs it.
Note the flip side: once the setting is off, tenants can create tables without primary keys, so apps no longer
get that guarantee from the server.

## Candidate recipes (2026-10-28)

Research only: nothing here is built or tested. Compressed image sizes were read from the Docker Hub API on 2026-10-28
(the platform cap is 512 MB); RAM figures are third-party or upstream estimates and **must be measured** on a throwaway
app before a recipe is promoted. Fit rules used: non-root UID 10001 (or a wrapper like the Ghost recipe), single
container, port above 1024, no outbound SMTP, 384 MB RAM, 100m CPU default (override available), 50 MB DB,
1 GiB volume, daily backups. Existing recipes are Ghost, WordPress, n8n, Directus, Grafana, Shlink and Gatus.

| Pick | Engine / volume | Image (compressed) | Expected RAM | Effort |
|---|---|---|---|---|
| Miniflux (RSS reader) | PG, no volume | 14 MB | 30-60 MB | S |
| Vaultwarden (password manager) | SQLite on volume (or PG) | 63 MB (alpine) | 20-50 MB | S-M |
| Memos (notes / microblog) | PG, no volume | 26 MB | 30-80 MB | S |
| Linkding (bookmarks) | SQLite on volume | 51 MB (alpine) | 80-150 MB | M |
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
- **Why viable:** Python/Django, MIT, bookmark manager with browser extension and API. The `-alpine` tag is 51 MB (the
  `-plus` variants with headless-browser archiving are 400-600 MB, avoid). SQLite on a volume at `/etc/linkding/data`, or PG.
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
- **Uptime Kuma:** the slim image is 185 MB and fits (SQLite on a volume, about 150-250 MB RAM), but Gatus already covers
  status pages, so it is a duplicate. Kept as a reserve pick.
- **Listmonk:** Go and PG, but it exists to send email and SMTP is blocked. Only viable with an HTTP email API sender, so not a
  fit for the offer today.
- **Plausible / Umami:** Plausible needs ClickHouse (a second service). Umami is PG-only Node and plausible later, but its image
  was not verified here.
- **Nextcloud, Mattermost, Discourse, Metabase:** RAM or multi-service needs exceed 384 MB.

### Wiki.js 2.x re-assessment (verdict: supportable, now with a 512 MB memory override; worth a test)

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
  DB_SSL_CA: "${{ db.ca_cert }}"     # Wiki.js takes the PEM content; confirm inline vs path
  NODE_OPTIONS: "--max-old-space-size=320"
```
Open checks: the image must run with a read-only-ish root FS (it writes `/wiki/data` for the git/sideload cache, so mount a
scratch path or set `DATA_PATH`); the setup wizard writes config to the DB, so the first visitor creates the admin (race on a
public URL: deploy and complete setup immediately); Git storage and search engine "DB - Basic" only (no Elasticsearch).
Rated: effort small if it passes the check, plus the admin memory override.
