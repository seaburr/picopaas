# Recipes

PicoPaaS hosts real apps, not just toy images. This page walks through Ghost (a popular CMS) end to end, then
lists the lessons that apply to any image you bring.

## Deploying Ghost

The files are in `examples/ghost/` in the repo (`Dockerfile`, `docker-entrypoint.sh`).

### Why a custom image

The official `ghost:5-alpine` image starts as root and `su-exec`s down to `node`. PicoPaaS rejects root and forces
a non-root numeric UID (10001). So we build a thin image on top that runs as 10001 directly.

### The image

`FROM ghost:5-alpine`, plus:

- An `/etc/passwd` entry for UID 10001. Node's `os.userInfo()` throws for a UID with no entry.
- `content/` seeded from `content.orig` at build. The stock root entrypoint normally does this and we bypass it.
- `chown` of `content/` only. Chowning the whole tree duplicates it in a new layer and doubles the image.
- The S3 storage adapter, behind build-arg `WITH_S3=1` (the published image is built with it).
- `HOME=/tmp`, which any UID can write.

```dockerfile
FROM ghost:5-alpine
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
image: ghcr.io/seaburr/picopaas-example-ghost:latest
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

Get an API key from the **API keys** page and export it:

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

## Grafana (dashboards) on MySQL

The files are in `examples/grafana/` in the repo (`README.md`, `grafana.yaml`). Grafana runs on PicoPaaS
backed by the platform's MySQL database, with its data persisting across restarts (verified: health `200`,
dashboards and users survive a redeploy).

### No custom image needed

Unlike Ghost and the status page, Grafana needs **no Dockerfile and no wrapper**. The official
`grafana/grafana:13.2.4` image already runs as a non-root numeric UID (472), and Grafana is configured
entirely through `GF_*` environment variables — no config-file mount required. So you point PicoPaaS straight
at the public image and set env vars. Pin a version (`:13.2.4`), not `:latest`, so a silent upstream bump
can't break your app.

### Point it at the platform MySQL

Request a database (`db: true`), no bucket, and wire Grafana's database env:

- `GF_DATABASE_TYPE=mysql`
- `GF_DATABASE_URL=${{ db.url }}` — one ref, the whole value. PicoPaaS only accepts a `${{ ... }}` value that
  is **exactly one** reference, so you can't build a DSN by concatenating `${{ db.host }}`, `${{ db.port }}`
  and text. Use `db.url` (it resolves to `mysql://user:password@host:port/name`), which is the one ref that
  hands Grafana everything in a single string.

### TLS and the CA file

Our managed MySQL requires TLS, and Grafana's MySQL driver insists on reading a CA file from disk even when
you tell it not to verify. So:

- `GF_DATABASE_SSL_MODE=skip-verify`
- `GF_DATABASE_CA_CERT_PATH=/etc/ssl/certs/ca-certificates.crt` — the CA bundle already baked into the Grafana
  image. Pointing at it satisfies the driver's file check.

This gives you **encrypted-but-unverified** TLS: the connection is encrypted, but Grafana doesn't validate the
server certificate against a specific CA. Full CA-*verified* TLS would need a PEM file mounted into the
container, which PicoPaaS doesn't offer (there's no config-file or volume mount). Encrypted-but-unverified to
the in-platform MySQL is the honest tradeoff here.

### Keep the connection pool under the cap

Each database user may hold at most **10 connections**. Size Grafana's pool below that so a restart or a surge
doesn't trip the limit:

- `GF_DATABASE_MAX_OPEN_CONN=8`
- `GF_DATABASE_MAX_IDLE_CONN=2`

### The rest of the env

- `GF_SERVER_HTTP_PORT=3000` and app `port: 3000`.
- `GF_SERVER_ROOT_URL=${{ app.url }}` — Grafana's public URL, resolved at deploy time to this app's own host.
- `GF_SECURITY_ADMIN_USER` and `GF_SECURITY_ADMIN_PASSWORD` — the first-login admin. Set the password as a
  **secret**, not a literal in the spec.
- `GF_ANALYTICS_REPORTING_ENABLED=false`, `GF_ANALYTICS_CHECK_FOR_UPDATES=false` — Grafana's outbound phone-home
  is pointless here (tenant egress is restricted anyway) and these stop it logging failures trying.

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

### Single replica only — HA/clustering is not supported on PicoPaaS

Run Grafana at **one replica**. PicoPaaS gives each app a single ClusterIP Service on the app port, and nothing
that lets one pod discover and address the others individually. Multi-replica setups that need pods to gossip
with named peers — Grafana's 2+ replica HA with unified-alerting memberlist, for example — have no way to find
each other, so they don't work. This is intentional and out of scope: the platform does not offer
headless/per-pod Services or peer discovery. A single MySQL-backed Grafana is the supported shape, and it's
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

### The public URL — a bare host, not a full URL

Shlink's `DEFAULT_DOMAIN` is the domain your short URLs are built on, and it must be a **bare host** (no scheme,
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

### Keep the connection pool under the cap

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

## Directus (headless CMS) on MySQL + S3

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

### Point it at the platform MySQL — with CA-verified TLS

Directus takes its database config as individual env vars, so map the platform refs one-to-one:

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

### The two required secrets, the admin, and the rest

- `KEY` and `SECRET` — two required random strings Directus needs for signing and encryption. **Generate them
  once and keep them stable** across deploys: rotating them invalidates sessions and breaks data encrypted at
  rest. Set both as **secrets**.
- `ADMIN_EMAIL` (a literal in the spec) plus `ADMIN_PASSWORD` (a **secret**) — the first admin account.
- `PUBLIC_URL=${{ app.url }}` — Directus's public URL, resolved at deploy time to this app's own host.
- `TELEMETRY=false` — turn off the outbound phone-home (tenant egress is restricted anyway).

### `HOME=/tmp` and `PM2_HOME=/tmp/.pm2` are required

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
  uploads in the storage bucket, and set your S3 client's public URL or asset host to the bucket's public Spaces URL.
- **No outbound SMTP.** Disable features that send email, or they will error.
- **Fit the caps:** 512 MB image, 200m CPU, 384 MB RAM, 50 MB database, 100 MB bucket, 250 MB scratch disk.

## Apps we evaluated (and what didn't fit)

Not every popular app runs on PicoPaaS, and we'd rather tell you up front than have you hit the wall yourself.
Two things define the shape of what fits: **images must run as a non-root numeric UID**, and the managed
database keeps its **integrity defaults** (notably, every table needs a primary key). We don't relax either of
those platform-wide to shoehorn in a single app — the isolation and the data guarantees are the product. Here's
where that landed for two apps we tried.

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

### Wiki.js — not supported (today)

[Wiki.js](https://js.wiki/) came much closer. It runs **non-root** out of the box and even does TLS to MySQL
cleanly with a CA-verified connection (`DB_SSL_CA`), so none of the usual blockers applied. The wall was in the
database, and it's a subtle one:

DigitalOcean's managed MySQL enforces `sql_require_primary_key=ON` cluster-wide — every table you create must
have a primary key. Wiki.js's **first schema migration creates a table without one**, and the server rejects
that `CREATE TABLE` outright, so Wiki.js can never finish its initial setup. There's no app-level env var in
Wiki.js to add the missing key or to work around the requirement.

The only fixes would be platform-side: turn off `sql_require_primary_key` for the whole managed database, or
carve out a separate database with it relaxed. We deliberately **keep the integrity default** rather than loosen
it platform-wide for one app — a required primary key is a guarantee every other tenant benefits from. So
Wiki.js is a "not today": clean on every axis except one upstream migration that collides with a default we're
choosing to keep.
