# Wiki.js 2 on PicoPaaS

[Wiki.js](https://js.wiki/) 2.5 is a wiki with a Markdown editor, page history, search and fine-grained
permissions. Pages, history and users live in the platform's **PostgreSQL** database.

## No custom image (one admin step)

The official `requarks/wiki:2.5` image (173 MB) runs as is. Its USER is the name `node`, so the platform runs
it as UID 10001. Stay on 2.x: the 3.x image is 816 MB, over the image cap.

**Needs an administrator:** Wiki.js uses a database pool of up to 10 connections, plus another pool when it renders
pages in a child process. The default Postgres limit per app is 5, and above it saving a page fails with
`too many connections for role`. The pool size is only settable in Wiki.js's config file, not in env vars, so ask an
admin to set this app's **DB connections to 10** (Admin > Apps tab > sliders icon, or `PATCH /admin/apps/{id}/resources`). 10 was enough in testing.

## Deploy

1. Create an app with a **database** (engine **PostgreSQL**), port **3000**, a **1 GiB volume at `/wiki/data`**,
   and the env from `wikijs.yaml` (or `picopaas apply -f wikijs.yaml`). Leave the health check off for the first boot.
2. Ask an admin for the connection override above.
3. Open the app URL straight away and finish the setup page: **whoever completes it becomes the admin.**

## Notes

- `DB_SSL_CA=${{ db.ca_cert_body }}`: CA-verified TLS. Wiki.js takes the certificate body without the PEM lines.
  `DATABASE_URL` does not work: Wiki.js 2's database library drops TLS from the URL and the server refuses it.
- **Why the volume:** Wiki.js writes cache and temp files to `/wiki/data`, which UID 10001 can't write in the image.
- **Size:** the schema is about 9.5 MB of the 50 MB database. Uploaded images are stored in the database too, so keep
  them small.
- **Footprint (measured):** about 45 MiB idle, 143 MiB peak during setup and rendering, so the default 384 MiB is enough.
- First boot runs migrations and is slow at the default CPU; the setup page shows when it is ready.
- No email (SMTP is blocked): add users from the admin area.
