# Planka (kanban boards) on PicoPaaS

[Planka](https://planka.cloud/) is a Trello-style kanban board with projects, boards, lists, cards, labels,
attachments and real-time updates. It runs on the platform's **PostgreSQL** database and keeps attachments and
avatars in the app's **bucket**.

## License: read this first

Planka 2.x is **not** open source in the OSI sense. The repository's `LICENSE.md` is the *PLANKA Community License*
(Fair Use License v1.1). Permitted: personal, hobby and educational use, use inside your own organisation, private
hosting for a typical number of friends, family or personal projects, and free access for non-profits. **Prohibited:
operating Planka as a hosted service for third parties for any commercial gain**, or sharing accounts across
organisations; those cases need a commercial licence. The PLANKA name may be used only to say your service
incorporates it. Files marked "Pro/Enterprise" (`.pe.` in the name) need a separate licence. So: fine as your own
board. **PicoPaaS decision (2026-11-03):** users may run Planka themselves on PicoPaaS; PicoPaaS never offers or
markets it as a managed service, a template showcase or a feature, and you must not offer it to third parties from here either.

## Image

`ghcr.io/plankanban/planka:2.2.1` (136 MB compressed, amd64) runs as `USER node`, port 1337, entrypoint `./start.sh`.
Public image, not rebuilt. Pin the tag.

## What had to change (UID 10001 is not the image's UID)

| Symptom | Cause | Fix in `planka.yaml` |
|---|---|---|
| Exits at start: `EACCES: permission denied, mkdir '/app/logs'` | The winston log file defaults to `/app/logs/planka.log` | `LOG_FILE=/tmp/planka.log` |
| `no pg_hba.conf entry ... no encryption` | The DB requires TLS and Planka ignores `sslmode` in `DATABASE_URL` | `PGSSLMODE` (runtime) and `KNEX_REJECT_UNAUTHORIZED_SSL_CERTIFICATE=false` (migrations) |
| Attachment upload fails: `EACCES: permission denied, mkdir '/app/.tmp'` | Sails stages uploads in the hardcoded `/app/.tmp` | A 1 GiB volume mounted at `/app/.tmp` |

The volume holds only upload staging, nothing you need to keep. It is needed only for uploads; the app boots without it.

## Deploy

1. `picopaas apply -f planka.yaml --secret SECRET_KEY=<random-hex> --secret DEFAULT_ADMIN_PASSWORD=<password>`
   (change `DEFAULT_ADMIN_EMAIL` first; it must be a valid-looking address).
2. Open the app URL (about 45 s after the pod starts) and sign in with the admin email or username.
   The first login asks you to **accept the terms**; the API does the same through `POST /api/access-tokens/accept-terms`.
3. Create a project, a board, a list and a card. Attach a file to see the bucket in use.

## Notes

- **TLS to the database is encrypted but not CA-verified.** Planka's knexfile only supports `rejectUnauthorized:false`,
  and the Sails adapter ignores `sslmode`/`sslrootcert` in the URL. With `PGSSLMODE=verify-full` plus
  `NODE_EXTRA_CA_CERTS=${{ db.ca_file }}` the connection works, but it also worked with `verify-full` and **no** CA, so
  do not count on verification here (we could not prove it). A truly verified connection needs a wrapper image that patches
  `db/knexfile.js` and `config/datastores.js`; not built.
- **Attachments:** the 300 KB test upload landed under `private/` in the bucket and downloaded byte for byte
  (download needs the session cookie `accessToken`, not just the bearer header).
- **Data survives** restarts and redeploys (the card and its attachment were intact after five rollouts).
- **Footprint (measured):** about 70 MiB RAM at idle (peaks around 120 MiB during boot), about 10 MB of database after
  migrations. CPU sits at the 200m limit during boot. Boot to ready is about 45 seconds.
- `NODE_OPTIONS=--max-old-space-size=256` is a safety net; it was never near the cap.
- No SMTP: leave the `SMTP_*` variables unset, notifications by email and password reset are unavailable.
