# Umami (web analytics) on PicoPaaS

[Umami](https://umami.is/) is a privacy-friendly, cookie-less web analytics app: a dashboard plus a tiny tracker
script (`/script.js`) you add to your sites. It runs on the platform's **PostgreSQL** database; no volume or bucket.

## No custom image

The official `ghcr.io/umami-software/umami:postgresql-latest` image (316 MB compressed, amd64) runs as `USER nextjs`
on port 3000 and is configured by env vars, so PicoPaaS runs it as is. It was Umami 3.4.1 when tested. The project
publishes version tags only for 3.0.x on the `postgresql-` line, so `postgresql-latest` (or its digest) is the
practical choice; pin the digest if you want it frozen.

## Deploy

1. Create an app from `umami.yaml` (`picopaas apply -f umami.yaml --secret APP_SECRET=<random>`): database on, engine
   **PostgreSQL**, port **3000**, health check `/api/heartbeat`. The first start applies 26 Prisma migrations by itself.
2. **Change the default login immediately.** Umami ships with `admin` / `umami`. Sign in and use Settings > Profile >
   Password, or via the API:
   ```bash
   TOKEN=$(curl -s -X POST https://<app>/api/auth/login -H 'content-type: application/json' \
     -d '{"username":"admin","password":"umami"}' | jq -r .token)
   curl -X POST https://<app>/api/users/<admin-user-id> -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{"password":"<new-password>"}'
   ```
   Verified: afterwards `admin`/`umami` returns 401. The app is public from the first boot, so do this right after it comes up.
3. Add a website (Settings > Websites) and put the snippet on your site:
   `<script defer src="https://<your-app>.picopaas.xyz/script.js" data-website-id="<id>"></script>`

## Notes

- **TLS to the database:** the DSN uses `sslmode=verify-full&sslrootcert=${{ db.ca_file }}`; Prisma connected and ran
  all migrations with it. (Not separately negative-tested; Prisma's connection fails closed on a bad `sslrootcert`.)
- **Memory:** idle RAM was about 165 MiB after a restart and up to about 255 MiB on a long-running instance, with a spike
  to about 330 MiB while the first-boot migrations ran. That fits in 384 MiB but not by a wide margin; avoid heavy reports on the
  default limit.
- **Database size:** about 9.5 MB after the migrations. Umami stores one row per event, so a busy site fills 50 MB
  (the database turns read-only at the quota). Use it for low-traffic sites or delete old data (Settings > Websites > Reset/Delete data).
- **Recording:** `POST /api/send` with `{"type":"event","payload":{"website":"<id>","hostname":"...","url":"/"}}` and a
  browser-like `User-Agent` records a pageview (bots are ignored). Verified: the pageview showed in `/api/websites/<id>/stats`
  and was still there after a restart.
- **Telemetry** is off with `DISABLE_TELEMETRY=1`. No Redis is needed.
- **Boot:** deploy to serving takes about a minute (migrations on first start).
