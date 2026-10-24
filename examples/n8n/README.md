# n8n (workflow automation) on PicoPaaS — our first PostgreSQL recipe

[n8n](https://n8n.io/) — an open-source workflow-automation tool — running as an ordinary PicoPaaS app,
backed by the platform's new managed **PostgreSQL** engine. It's the proof that managed Postgres works
end to end: a per-app Postgres database and role, provisioned automatically, over CA-verified TLS, with
a workflow and the owner account surviving a pod restart (verified live at `https://n8n.picopaas.xyz`).

## No custom image

There is **no Dockerfile here** — and that's the point. The official `n8nio/n8n:2.41.7` image already
runs as a non-root user, listens on port `5678`, and is configured entirely through environment variables,
so PicoPaaS runs the public image directly. Pin a version, don't use `:latest`.

(Because there's nothing to build, n8n is **not** in the `examples.yml` build matrix — that workflow only
builds the example images that have Dockerfiles.)

## The new bit: choosing the Postgres engine

```yaml
resources:
  db: true
  db_engine: pg   # mysql (default) | pg
```

`db_engine: pg` tells PicoPaaS to back this app with **PostgreSQL** instead of MySQL. The platform
auto-provisions a per-app Postgres database and role (just like it does for MySQL), and the same
`${{ db.* }}` refs resolve to the Postgres connection details. The engine is immutable once the app has a
database. n8n is Postgres-first, which is why it's the recipe that exercises the managed-Postgres path.

## What's verified

- A per-app Postgres **database + role** was auto-provisioned on the managed `primary-pg` server.
- n8n connected over **CA-verified TLS** (`DB_POSTGRESDB_SSL_ENABLED=true` + the platform CA + reject-unauthorized).
- A created workflow **and** the owner account **survived a pod restart** — the data is in Postgres and the
  encryption key is pinned, so nothing was lost on the ephemeral disk.

## With a persistent volume (recommended)

`n8n.yaml` mounts a 1 GiB volume at `/home/node` with `N8N_USER_FOLDER=/home/node`. Mount the whole home, not just
`.n8n`: n8n also writes `~/.cache`. Verified live: the auto-generated encryption key, a stored credential, an active
webhook workflow and filesystem binary data all survived a restart, with Postgres and with SQLite on the volume (drop
`db`/`DB_*` for that), and a manual backup of the volume succeeded. Details and caveats (about a minute of downtime per
deploy, single replica, 512 MiB memory override still needed, slow first boot) are in `../../docs/RECIPES.md`.
Without volumes, delete the `volume` line and set `N8N_USER_FOLDER` to `/tmp`; then the encryption key must be pinned.

## Make it yours

Everything is in `n8n.yaml`. The one value that needs your attention is the single real secret (set by name
under `secrets:`, value provided through the dashboard's secret editor or the API, never in the file):

- **`N8N_ENCRYPTION_KEY`** — **pinned** (required without a volume, recommended with one). n8n encrypts stored credentials with this key. The
  scratch disk is wiped on restart, so if you don't pin the key, a restart regenerates it and every saved
  credential becomes undecryptable. Set it once and keep it stable.

Everything else is wired from platform refs (see `../../docs/RECIPES.md` for the full write-up):

- Database vars map one-to-one: `DB_TYPE=postgresdb`, `DB_POSTGRESDB_HOST=${{ db.host }}`,
  `DB_POSTGRESDB_PORT=${{ db.port }}`, `DB_POSTGRESDB_DATABASE=${{ db.name }}`,
  `DB_POSTGRESDB_USER=${{ db.user }}`, `DB_POSTGRESDB_PASSWORD=${{ db.password }}` (a ref; injected at deploy).
- `DB_POSTGRESDB_SSL_ENABLED=true` + `DB_POSTGRESDB_SSL_CA=${{ db.ca_cert }}` +
  `DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED=true` — **CA-verified** TLS to managed Postgres, all from env (no
  file to mount).
- `DB_POSTGRESDB_POOL_SIZE=2` — stay under the per-app role's **CONNECTION LIMIT of 5** (the 1 GB Postgres
  node has only ~22 usable connections total).
- **`N8N_USER_FOLDER` is REQUIRED** — `/home/node` with the volume, `/tmp` without. The image's default HOME isn't writable as the non-root UID.
- `N8N_HOST` / `N8N_PROTOCOL` / `N8N_PORT` / `WEBHOOK_URL` / `N8N_EDITOR_BASE_URL` — set to the app's URL.
- `NODE_OPTIONS=--max-old-space-size=384` plus optional footprint-trimming flags
  (`N8N_RUNNERS_ENABLED`, `N8N_DIAGNOSTICS_ENABLED`, `N8N_VERSION_NOTIFICATIONS_ENABLED`,
  `N8N_TEMPLATES_ENABLED`, `NODES_INCLUDE`).

## Memory: n8n needs ~512 MiB (an admin override)

n8n **OOMs at the 384 MiB default cap**. It needs an **admin resource override to ~512 MiB** (we verified 512
holds; n8n used ~338 at idle). This isn't a spec field — ask an admin to raise the memory limit for the app.

The `NODE_OPTIONS` heap cap must stay **below** the container memory limit, or you trade one OOM for another:
too low and Node heap-OOMs, too high and the container OOMs. We run **384 heap inside a 512 MiB container**.

## Deploy on PicoPaaS

1. Create an app with a **database**, **`db_engine: pg`**, port **5678**, health `/healthz`, image
   `n8nio/n8n:2.41.7`, and the env from `n8n.yaml`.
2. Set the `N8N_ENCRYPTION_KEY` secret value in the dashboard or via the API.
3. Ask an admin to raise the memory limit to **~512 MiB** (n8n OOMs at the 384 MiB default).

Apply with the CLI, `curl`, or the dashboard exactly as in the Ghost recipe:

```bash
picopaas apply -f n8n.yaml
```

Open the app's URL, create the owner account, and build a workflow. It lives in Postgres, so it survives
restarts.
