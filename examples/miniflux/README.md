# Miniflux (RSS reader) on PicoPaaS

[Miniflux](https://miniflux.app/) is a minimalist feed reader with a web UI and a REST API (also Fever and
Google Reader compatible, so mobile apps work). It runs on the platform's **PostgreSQL** database.

## No custom image

The official `miniflux/miniflux` image (14 MB) runs as `USER 65534` on port 8080 and reads all its settings
from env vars, so PicoPaaS runs it as is. Pin a version (`2.3.4` here), not `latest`.

## Deploy

1. Create an app from the **Miniflux** template (or `picopaas apply -f miniflux.yaml`): database on, engine
   **PostgreSQL**, port **8080**, health check `/healthcheck`.
2. Set the `ADMIN_PASSWORD` secret before the first start. Miniflux creates the `admin` user with it on first
   boot (`CREATE_ADMIN=1`); changing the secret later does not change the password, use the Settings page.
3. Open the app URL and sign in as `admin`.

## Notes

- **TLS to the database:** `DATABASE_URL` is built from the `db.*` refs with `sslmode=verify-full` and
  `sslrootcert=${{ db.ca_file }}` (the platform CA, mounted at `/etc/picopaas/db-ca.pem`), so the server certificate
  is checked. `${{ db.url }}` also works but only encrypts (`sslmode=require`).
- **Connections:** `DATABASE_MAX_CONNS=3` keeps it under the per-app Postgres limit of 5.
- **Database size:** the schema is about 9 MB. Article bodies are stored, so keep the cleanup settings (or
  lower them) to stay under 50 MB. At the quota the database turns read-only and refreshes fail.
- **Feeds** are fetched over the internet (outbound HTTP/HTTPS is allowed). Email is not available.
- **Footprint (measured):** about 21 MiB RAM at idle with two feeds, so the defaults are plenty.

## API

```bash
curl -u admin:"$ADMIN_PASSWORD" https://<your-app>.picopaas.xyz/v1/me
curl -u admin:"$ADMIN_PASSWORD" -X POST https://<your-app>.picopaas.xyz/v1/feeds \
  -d '{"feed_url":"https://blog.picopaas.xyz/rss/","category_id":1}'
```

Create an API key in Settings for scripts instead of using the password.
