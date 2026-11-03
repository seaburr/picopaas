# Vikunja (to-do and project manager) on PicoPaaS

[Vikunja](https://vikunja.io/) is a to-do app with lists, kanban boards, gantt and table views, reminders,
CalDAV and a REST API. It runs on the platform's **PostgreSQL** database and keeps attachments in the app's
**bucket**, so it needs no volume.

## No custom image

The official `vikunja/vikunja` image (55 MB, amd64) runs as `USER 1000` on port 3456 and reads all its settings
from `VIKUNJA_*` env vars, so PicoPaaS runs it as is. Pin a version (`2.7.1` here), not `latest`.

## Deploy

1. Create an app from `vikunja.yaml` (`picopaas apply -f vikunja.yaml --secret VIKUNJA_SERVICE_SECRET=<random>`):
   database on, engine **PostgreSQL**, bucket on, port **3456**, health check `/api/v1/info`.
2. Open the app URL and register the first user (usernames need at least 3 characters).
3. Lock registration: uncomment `VIKUNJA_SERVICE_ENABLEREGISTRATION: "false"` and apply again. There is no email, so
   nobody else can be invited by mail; create further users before locking, or leave registration open for a team.

## Notes

- **TLS to the database:** `VIKUNJA_DATABASE_SSLMODE=verify-full` with `VIKUNJA_DATABASE_SSLROOTCERT=${{ db.ca_file }}`
  checks the server certificate against the platform CA. A negative test with the system CA bundle failed with
  `x509: certificate signed by unknown authority`, so the mounted CA is really used.
- **Attachments:** `VIKUNJA_FILES_TYPE=s3` with the `storage.*` refs works. A 300 KB upload landed under `files/` in
  the bucket and downloaded byte for byte, also after redeploys.
- **Secret name:** use `VIKUNJA_SERVICE_SECRET`. The older `VIKUNJA_SERVICE_JWTSECRET` still works but logs a deprecation warning.
- **Connections:** `MAXOPENCONNECTIONS=3` keeps it under the per-app Postgres limit of 5.
- **Pro features** are off without a license key (the log says so); the core app is AGPL-3.0.
- **Footprint (measured):** about 27 MiB RAM idle (35 MiB with a few requests), about 10 MB of database after the schema
  migrations, boot (migrations included) about 3 seconds.
- **Data survives** restarts: users, projects, tasks and attachments were all intact after a restart and several redeploys.

## API

```bash
TOKEN=$(curl -s -X POST https://<your-app>.picopaas.xyz/api/v1/login \
  -H 'content-type: application/json' -d '{"username":"me","password":"..."}' | jq -r .token)
curl -X PUT https://<your-app>.picopaas.xyz/api/v1/projects/1/tasks \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"title":"hello"}'
```

Create an API token in Settings for scripts instead of using the password.
