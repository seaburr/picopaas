# Forgejo (git forge) on PicoPaaS

[Forgejo](https://forgejo.org/) is a self-hosted git forge: repositories, issues, pull requests, wikis and a
package registry. This recipe runs it with its repositories and SQLite database on a persistent volume.

## No custom image

The official `codeberg.org/forgejo/forgejo:<version>-rootless` image runs as `USER 1000:1000` on port 3000 and
takes its settings from env vars, so PicoPaaS runs it as is. Pin a version (`17.0.1-rootless` here). Use the
`-rootless` tag: the plain tag starts as root and is rejected.

## Deploy

1. Create an app from the **Forgejo** template (or `picopaas apply -f forgejo.yaml`): port **3000**, health
   check `/api/healthz`, a **1 GiB volume at `/var/lib/gitea`**, no database, no bucket.
2. Set the `SECRET_KEY` secret (`openssl rand -hex 32`) before the first start.
3. As soon as it is running, open the app URL and **register your account. The first account becomes the
   admin.** Until you do, anyone who finds the URL could register first.
4. Set `FORGEJO__SERVICE__DISABLE_REGISTRATION` to `true` and save. The app redeploys; your data stays on the
   volume. Add other users from Site administration.

## Notes

- **Git over HTTPS only.** Clone and push with `https://<your-app>.picopaas.xyz/<user>/<repo>.git`; use an
  access token (Settings > Applications) as the password. SSH is not available.
- **SQLite or Postgres:** SQLite on the volume keeps all data in one place, which suits a single-replica forge. To use
  the platform Postgres instead (verified live), turn the database on (PostgreSQL) and set `DB_TYPE=postgres`,
  `DB_HOST=${{ db.host }}:${{ db.port }}`, `DB_NAME=${{ db.name }}`, `DB_USER=${{ db.user }}`,
  `DB_PASSWD=${{ db.password }}`, `FORGEJO__DATABASE__SSL_MODE=verify-full`, `PGSSLROOTCERT=${{ db.ca_file }}` and
  `FORGEJO__DATABASE__MAX_OPEN_CONNS=3`. Repositories stay on the volume either way.
- **Size:** repositories, LFS and attachments share the 1 GiB volume. An admin can grow it (up to 10 GiB).
- **CPU:** large clones and pushes are slow at the default CPU limit; ask an admin for more if you use it a lot.
- **Footprint (measured):** about 98 MiB RAM right after start.
- No email (SMTP is blocked) and no Actions runner.
- `ROOT_URL`, `HTTP_PORT`, `DISABLE_SSH` and `DB_TYPE` are only read on the first boot, when `app.ini` is
  written to the volume. Change settings later with `FORGEJO__SECTION__KEY` variables, which apply on every boot.
