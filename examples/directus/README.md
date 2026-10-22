# Directus (headless CMS) on PicoPaaS

[Directus](https://directus.io/) — an open-source headless CMS and data platform — running as an ordinary
PicoPaaS app. It's the recipe that exercises the **whole platform stack at once**: the managed **MySQL**
database for its data *and* the **Spaces bucket** for uploaded files, both surviving restarts and redeploys
(verified: data and an uploaded file both survived a pod restart).

## No custom image

There is **no Dockerfile here** — and that's the point. The official `directus/directus:12.4.1` image already
runs as a non-root user (`node`), is ~242 MB (under the 256 MB cap), and is configured entirely through
environment variables, so PicoPaaS can run the public image directly. Pin a version, don't use `:latest`.

(Because there's nothing to build, Directus is **not** in the `examples.yml` build matrix — that workflow only
builds the example images that have Dockerfiles.)

## What's verified

Directus fits PicoPaaS cleanly: non-root, env-only, no custom build, and **PK-clean** (its schema doesn't trip
DigitalOcean's `sql_require_primary_key` default, unlike Wiki.js). It uses **both** managed stores — MySQL for
structured data and the Spaces bucket for files — over **CA-verified** TLS to MySQL.

## Make it yours

Everything is in `directus.yaml`. The values that need your attention are the three real secrets (set by name
under `secrets:`, values provided through the dashboard's secret editor or the API, never in the file):

- **`KEY`** and **`SECRET`** — two required random strings. Generate them once and keep them **stable** across
  deploys; rotating them invalidates sessions and breaks data encrypted at rest.
- **`ADMIN_PASSWORD`** — the first admin account's password (paired with `ADMIN_EMAIL` in `env`).

Everything else is wired from platform refs (see `../../docs/RECIPES.md` for the full write-up):

- Database vars map one-to-one: `DB_HOST=${{ db.host }}`, `DB_PORT=${{ db.port }}`,
  `DB_DATABASE=${{ db.name }}`, `DB_USER=${{ db.user }}`, `DB_PASSWORD=${{ db.password }}`.
- `DB_SSL__CA=${{ db.ca_cert }}` — **CA-verified** TLS to managed MySQL. Directus accepts the CA content
  directly, so there's no file to mount and no skip-verify to reason about.
- `DB_POOL__MIN=0` / `DB_POOL__MAX=5` — keep the pool under the 10-connection-per-DB-user cap.
- S3 file storage: `STORAGE_LOCATIONS=s3`, `STORAGE_S3_DRIVER=s3`, with key/secret/bucket/region/endpoint from
  the `${{ storage.* }}` refs. No path-style flag is needed for Spaces.
- `PUBLIC_URL=${{ app.url }}`, `TELEMETRY=false`.
- **`HOME=/tmp` and `PM2_HOME=/tmp/.pm2` are REQUIRED** — without them the image's pm2 wrapper tries to `mkdir`
  a home dir as the non-root UID and fails with `EACCES`, so the container never serves.

## Leave the health check OFF

Directus **cold-starts slowly** — ~90–110s on the 0.2-core limit (it loads its modules twice). That's far longer
than the platform's liveness probe tolerates (30s delay + 3×20s), so an **enabled probe restart-loops the pod**.
The spec therefore sets `healthCheck: { enabled: false }`. The trade-off is honest: no liveness protection for
the app, and ~90s of downtime on each deploy or restart. (If probe timing is ever relaxed, `/server/ping` is the
public health path — `/server/health` returns `403` to unauthenticated callers.)

A harmless boot WARN — `Couldn't bundle App extensions (EACCES ...)` — appears because the filesystem is
read-only and extensions aren't used. The UI works fine. Run a **single replica** (scaled-out wasn't tested).

## Deploy on PicoPaaS

1. Create an app with a **database and a bucket**, port **8055**, `auto_update: false`, image
   `directus/directus:12.4.1`, and the env from `directus.yaml`.
2. Set the `KEY`, `SECRET`, and `ADMIN_PASSWORD` secret values in the dashboard or via the API.

Apply with the CLI, `curl`, or the dashboard exactly as in the Ghost recipe:

```bash
picopaas apply -f directus.yaml
```

Give it ~90s on first boot, then open the app's URL and log in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`. Collections
and content live in MySQL and uploaded files in the bucket, so both survive restarts.
