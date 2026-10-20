# Ghost on PicoPaaS

A real, mainstream CMS (Ghost 5) running on PicoPaaS within the platform's constraints — proof that the
platform can host a genuine app, not just toy test images. Live demo: deployed as `ghost-demo`.

## What works
- **Ghost serves**: homepage, `/ghost/` admin, and `/rss/` all return 200.
- **Persistent**: all content lives in the PicoPaaS **MySQL** database, so it survives restarts/redeploys
  (verified: restarted the app — pod replaced, ephemeral disk wiped — and the site + data were intact).
- **Fits the caps**: image **168 MiB** (< 256 MB image cap), boots and runs within **384 MB RAM** (0 restarts),
  schema uses ~3 MB of the 50 MB DB.

## Why a custom image (not the stock `ghost` image)
The official `ghost:5-alpine` image starts as **root** and `su-exec`'s down to `node`; PicoPaaS rejects root
images and forces a non-root numeric UID (10001). So this image:
1. Runs as **UID 10001** directly, skipping the stock root entrypoint.
2. Adds an **/etc/passwd entry** for 10001 — Node's `os.userInfo()` throws for a UID with no passwd entry
   (the Node analog of the Python `getpass.getuser()` gotcha; see the User Guide troubleshooting).
3. **Seeds `content/`** (default themes incl. the active "source" theme) from `content.orig` at build, because
   the stock entrypoint normally does this and we bypass it — and PicoPaaS `content/` is ephemeral anyway.
4. Ships an **entrypoint that translates** PicoPaaS's `UPPER_SNAKE` env refs into Ghost's lowercase,
   `__`-nested config keys (PicoPaaS env keys must match `^[A-Z_][A-Z0-9_]*$`, so Ghost's `database__*` can't
   be set directly).
5. `chown`s **only `content/`** (chowning the whole tree doubled the image via a duplicate layer).

## Deploy it
1. Build: push to `examples/ghost/**` → the `examples` workflow publishes
   `ghcr.io/seaburr/picopaas-example-ghost:<sha>` (make the GHCR package **public** — PicoPaaS pulls anonymously).
2. Create an app with a **database** (`want_db: true`), port **2368**, image the tag above, and these env refs:
   `DB_HOST=${{ db.host }}`, `DB_PORT=${{ db.port }}`, `DB_NAME=${{ db.name }}`, `DB_USER=${{ db.user }}`,
   `DB_PASSWORD=${{ db.password }}`, `DB_CA_CERT=${{ db.ca_cert }}`.
3. **Two-step URL**: Ghost needs its public URL, but the subdomain is generated at create and there is no
   `${{ app.url }}` ref. So the first boot crash-loops (the entrypoint exits if `SITE_URL` is unset); read the
   generated hostname, then PATCH env to add `SITE_URL=https://<hostname>` and it boots.
4. Finish setup at `/ghost/`.

## Known limitation: uploaded images
The default (lean) image stores uploaded images on the **ephemeral disk**, so they're lost on restart
(content/posts in MySQL are fine). Build with `--build-arg WITH_S3=1` and set a storage bucket plus
`S3_*` env to persist uploads in the PicoPaaS bucket via `ghost-storage-adapter-s3`. Caveat: the S3 adapter
pulls in aws-sdk v2 and pushes the image to ~**297 MiB**, **over the 256 MB cap** — so the S3 variant needs a
slimmer adapter (aws-sdk v3) or heavier pruning before it can be deployed. Tracked as a follow-up.

## Platform findings (what hosting a real CMS surfaced)
- Image cap is on **compressed** layers; lean Ghost (168 MiB) fits, but add an S3 adapter and it doesn't.
- Non-root enforcement + forced UID 10001 means stock images with root entrypoints need a wrapper.
- `os.userInfo()`/`getpass.getuser()` crash for passwdless UIDs on modern bases (Python 3.13, Node) — set a
  passwd entry and `USER`/`HOME`.
- No `${{ app.url }}` ref → a two-step deploy for apps that need to know their own URL.
- Uppercase-only env keys → apps wanting lowercase/nested config need an in-container translation shim.
- No persistent filesystem → state must go to MySQL and/or the bucket; the DB-backed CMS model fits well.
