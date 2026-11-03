# Forgejo (git forge) on PicoPaaS

[Forgejo](https://forgejo.org/) is a self-hosted git forge: repositories, issues, pull requests, wikis and a
package registry. This recipe runs it with its metadata (users, issues) in the platform MySQL and its git repositories on a
persistent volume.

## No custom image

The official `codeberg.org/forgejo/forgejo:<version>-rootless` image runs as `USER 1000:1000` on port 3000 and
takes its settings from env vars, so PicoPaaS runs it as is. Pin a version (`17.0.1-rootless` here). Use the
`-rootless` tag: the plain tag starts as root and is rejected.

## Deploy

1. Create an app from the **Forgejo** template (or `picopaas apply -f forgejo.yaml`): port **3000**, health
   check `/api/healthz`, a **1 GiB volume at `/var/lib/gitea`**, a **MySQL database**, no bucket.
2. Set the `SECRET_KEY` secret (`openssl rand -hex 32`) before the first start.
3. As soon as it is running, open the app URL and **register your account. The first account becomes the
   admin.** Until you do, anyone who finds the URL could register first.
4. Set `FORGEJO__SERVICE__DISABLE_REGISTRATION` to `true` and save. The app redeploys; your data stays on the
   volume. Add other users from Site administration.

## Notes

- **Git over HTTPS only.** Clone and push with `https://<your-app>.picopaas.xyz/<user>/<repo>.git`; use an
  access token (Settings > Applications) as the password. SSH is not available.
- **MySQL (main path, verified live 2026-11-03):** `resources.db: true`, `db_engine: mysql`, and `DB_TYPE=mysql`,
  `DB_HOST=${{ db.host }}:${{ db.port }}`, `DB_NAME=${{ db.name }}`, `DB_USER=${{ db.user }}`, `DB_PASSWD=${{ db.password }}`,
  `FORGEJO__DATABASE__SSL_MODE=true`, `SSL_CERT_FILE=${{ db.ca_file }}`, `FORGEJO__DATABASE__MAX_OPEN_CONNS=3`,
  `FORGEJO__DATABASE__MAX_IDLE_CONNS=2` (the per-app limit is 10 connections).
  - **TLS is CA-verified.** Forgejo's MySQL `SSL_MODE` is `true` (verify against Go's root store), `skip-verify` or `disable`.
    Pointing `SSL_CERT_FILE` at the platform CA adds it to the roots Go trusts. Negative test: with `SSL_CERT_FILE` set to
    the public bundle the app fails to start with `x509: certificate signed by unknown authority`. Outbound HTTPS still
    works (a GitHub-API migration with issues imported fine) because Go also reads `/etc/ssl/certs`.
  - **Charset:** on first boot Forgejo alters the database to collation `utf8mb4_0900_as_cs` (a warning in the log, harmless);
    an issue titled with CJK and an emoji round-trips.
  - **Size:** about 2.5 MB after the migrations, 3.4 MB with a few repos and issues, against the 50 MB cap. Metadata
    only, so the cap is ample; git data is on the volume.
  - Boot takes about 15 s (schema migration on the first start). RAM was about 90-100 MiB idle and peaked at 221 MiB during
    the first start plus a push and a migration (384 MB limit).
  - Verified: first user becomes admin, repo create, push and clone over HTTPS, issue create, restart and three redeploys kept
    everything, and `DISABLE_REGISTRATION=true` returns 403 on sign-up.
- **SQLite fallback:** if you would rather have one place for all data, drop the database and the `DB_*`, `SSL_*` and
  `MAX_*` variables and set `DB_TYPE=sqlite3`; the database then lives on the volume next to the repositories (verified
  2026-11-01, about 98 MiB RAM). The platform PostgreSQL also works (`DB_TYPE=postgres`, `FORGEJO__DATABASE__SSL_MODE=verify-full`,
  `PGSSLROOTCERT=${{ db.ca_file }}`, `MAX_OPEN_CONNS=3`, limit 5).
  `DB_TYPE` is read only on the first boot: pick the engine before the first start; you cannot switch it afterwards without a migration.
- **Size:** repositories, LFS and attachments share the 1 GiB volume. An admin can grow it (up to 10 GiB).
- **CPU:** large clones and pushes are slow at the default CPU limit; ask an admin for more if you use it a lot.
- No email (SMTP is blocked) and no Actions runner.
- `ROOT_URL`, `HTTP_PORT`, `DISABLE_SSH` and `DB_TYPE` are only read on the first boot, when `app.ini` is
  written to the volume. Change settings later with `FORGEJO__SECTION__KEY` variables, which apply on every boot.
