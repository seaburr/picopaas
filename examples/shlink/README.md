# Shlink (URL shortener) on PicoPaaS

[Shlink](https://shlink.io/) — a self-hosted URL shortener with a REST API — running as an ordinary PicoPaaS
app, backed by the platform's **MySQL** database so short URLs and visit stats survive restarts and redeploys.

## No custom image

There is **no Dockerfile here** — and that's the point. The official `shlinkio/shlink:5.1.8` image already runs
as a non-root numeric UID (`USER 1001`) and is configured entirely through environment variables, so PicoPaaS
can run the public image directly. Pin a version, don't use `:latest`.

(Because there's nothing to build, Shlink is **not** in the `examples.yml` build matrix — that workflow only
builds the example images that have Dockerfiles.)

## Make it yours

Everything is in `shlink.yaml`. Two values need your attention:

- **`DEFAULT_DOMAIN`** — the bare host your short URLs are built on. It **must** be a bare hostname (no scheme,
  no path), so you **cannot** use `${{ app.url }}` (that resolves to a full `https://...` URL). Ask an admin for
  a reserved subdomain and set this to it, e.g. `shlink.picopaas.xyz`.
- **`INITIAL_API_KEY`** — a long random string you choose, set as a **secret** (listed by name under
  `secrets:`, value provided through the dashboard's secret editor or the API, never in the file). Shlink seeds
  a first API key with this value on startup.

Other key choices (see `../../docs/RECIPES.md` for the full write-up):

- Database vars map platform refs one-to-one: `DB_HOST=${{ db.host }}`, `DB_PORT=${{ db.port }}`,
  `DB_NAME=${{ db.name }}`, `DB_USER=${{ db.user }}`, `DB_PASSWORD=${{ db.password }}`.
- `DB_USE_ENCRYPTION=true` — Shlink's one clean switch for TLS to managed MySQL (no CA file, no skip-verify).
- `WEB_WORKER_NUM=4` / `TASK_WORKER_NUM=4` — keep both worker pools under the 10-connection cap per DB user.
- `healthCheck: { enabled: true, path: /rest/health }` — Shlink's real health endpoint; the app goes ready only
  once it can reach its database.

A harmless `GeoLite2` warning appears on boot (IP geolocation of visits is off because no MaxMind license key is
set). The shortener, redirects and visit counts all work regardless.

## Deploy on PicoPaaS

1. Ask an admin for a reserved subdomain and set `DEFAULT_DOMAIN` in `shlink.yaml` to that bare host.
2. Create an app with a **database** (no bucket), port **8080**, image `shlinkio/shlink:5.1.8`, and the env from
   `shlink.yaml`.
3. Set the `INITIAL_API_KEY` secret's value in the dashboard or via the API.

Apply with the CLI, `curl`, or the dashboard exactly as in the Ghost recipe:

```bash
picopaas apply -f shlink.yaml
```

## Create your first short URL

Shlink is driven entirely through its REST API, authenticated with the `X-Api-Key` header:

```bash
curl -fsS https://shlink.picopaas.xyz/rest/v3/short-urls \
  -H "X-Api-Key: $INITIAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"longUrl": "https://example.com/some/very/long/link"}'
```

Short URLs and visit stats live in MySQL, so they survive restarts.
