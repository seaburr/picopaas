# Grafana on PicoPaaS

[Grafana](https://grafana.com/) dashboards running as an ordinary PicoPaaS app, backed by the platform's
**MySQL or PostgreSQL** database (`grafana.yaml` / `grafana-postgres.yaml`) so dashboards, users and settings survive restarts and redeploys.

## No custom image

There is **no Dockerfile here** — and that's the point. The official `grafana/grafana:13.2.4` image already
runs as a non-root numeric UID (472), and Grafana is configured entirely through `GF_*` environment variables,
so PicoPaaS can run the public image directly. Pin a version, don't use `:latest`.

(Because there's nothing to build, Grafana is **not** in the `examples.yml` build matrix — that workflow only
builds the images that have Dockerfiles.)

## Make it yours

Everything is in `grafana.yaml`. The only value that isn't a platform ref is the admin password, which is a
**secret**: it's listed by name under `secrets:` and its value is set through the dashboard's secret editor or
the API, never in the file.

Key choices (see `../../docs/RECIPES.md` for the full write-up):

- `GF_DATABASE_URL: ${{ db.url }}` — PicoPaaS accepts a `${{ ... }}` value only when it's exactly one
  reference, so use the single `db.url` ref (a full `mysql://...` or `postgres://...` DSN), not host/port vars glued together.
- PostgreSQL variant (`grafana-postgres.yaml`): `resources.db_engine: pg`, `GF_DATABASE_TYPE=postgres`, and
  `GF_DATABASE_SSL_MODE=require` (Grafana reads the TLS mode from this variable, not the URL). `db.url` is then a
  `postgres://...?sslmode=require` DSN. The MySQL CA-file workaround below does not apply.
- MySQL: `GF_DATABASE_SSL_MODE=skip-verify` + `GF_DATABASE_CA_CERT_PATH=/etc/ssl/certs/ca-certificates.crt` — managed
  MySQL requires TLS, and Grafana's driver insists on reading a CA file even for skip-verify, so point it at
  the CA bundle already in the image. Encrypted but unverified; CA-verified TLS would need a PEM mount the
  platform doesn't offer.
- `GF_DATABASE_MAX_OPEN_CONN=8` / `GF_DATABASE_MAX_IDLE_CONN=2` — keep the pool under the 10-connection cap per
  DB user.
- `healthCheck: { enabled: true, path: /api/health }` — Grafana's real health endpoint; the app goes ready
  only once it can reach its database.

## Deploy on PicoPaaS

1. Create an app with a **database** of your chosen engine (no bucket), port **3000**, image `grafana/grafana:13.2.4`, and the env
   from `grafana.yaml`.
2. Set the `GF_SECURITY_ADMIN_PASSWORD` secret's value in the dashboard or via the API.
3. Open the app's URL and log in as your admin user.

Apply with the CLI, `curl`, or the dashboard exactly as in the Ghost recipe:

```bash
picopaas apply -f grafana.yaml
```

## Limitation: single replica only

Run Grafana at **one replica**. PicoPaaS gives each app a single ClusterIP Service on the app port and no
per-pod addressing, so multi-replica HA that needs pods to gossip with named peers — Grafana's unified-alerting
memberlist, for instance — can't discover its peers and won't work. That's intentional and out of scope. A
single-replica Grafana is the supported shape.
