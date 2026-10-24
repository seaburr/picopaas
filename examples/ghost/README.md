# Ghost on PicoPaaS

A real, mainstream CMS (Ghost 5) running on PicoPaaS within the platform's constraints — proof that the
platform can host a genuine app, not just toy test images. Live demo: deployed as app `ghost-blog`.

## What works
- **Ghost serves**: homepage, `/ghost/` admin, and `/rss/` all return 200.
- **Persistent**: all content lives in the PicoPaaS **MySQL** database, so it survives restarts/redeploys
  (verified: restarted the app — pod replaced, ephemeral disk wiped — and the site + data were intact).
- **Fits the caps**: image ~**185 MiB** (< 512 MB image cap), boots and runs within **384 MB RAM** (0 restarts),
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
2. Create an app with a **database** and a **blob storage bucket**, port **2368**, image
   `ghcr.io/seaburr/picopaas-example-ghost:latest`, and these env refs:
   `DB_HOST=${{ db.host }}`, `DB_PORT=${{ db.port }}`, `DB_NAME=${{ db.name }}`, `DB_USER=${{ db.user }}`,
   `DB_PASSWORD=${{ db.password }}`, `DB_CA_CERT=${{ db.ca_cert }}`, `S3_ENDPOINT=${{ storage.endpoint }}`,
   `S3_REGION=${{ storage.region }}`, `S3_BUCKET=${{ storage.bucket }}`, `S3_ACCESS_KEY=${{ storage.access_key }}`,
   `S3_SECRET_KEY=${{ storage.secret_key }}`, `SITE_URL=${{ app.url }}`.
3. **Single step**: `${{ app.url }}` gives Ghost its public URL at deploy time, so there is no second deploy.
4. Finish setup at `/ghost/`.

## Uploaded images
The published image (built by `.github/workflows/examples.yml` with `WITH_S3=1`) includes the
`ghost-storage-adapter-s3` adapter, so uploads persist in the PicoPaaS blob storage bucket. The entrypoint sets the adapter's
`assetHost` to the public Spaces URL (`https://<bucket>.<region>.digitaloceanspaces.com`) so image links resolve
(the adapter default is AWS-style). A plain local `docker build` defaults to `WITH_S3=0` (uploads on ephemeral disk).

## Staff device verification
The entrypoint sets `security__staffDeviceVerification=false`. PicoPaaS blocks outbound SMTP, so the sign-in
verification email can't be delivered and would lock the admin out. Trade-off: this also drops optional email 2FA.

## Platform findings (what hosting a real CMS surfaced)
- Image cap is on **compressed** layers; the S3 variant (~185 MiB) fits the 512 MB cap.
- Non-root enforcement + forced UID 10001 means stock images with root entrypoints need a wrapper.
- `os.userInfo()`/`getpass.getuser()` crash for passwdless UIDs on modern bases (Python 3.13, Node) — set a
  passwd entry and `USER`/`HOME`.
- `${{ app.url }}` ref added, so apps can know their own URL in one deploy.
- Uppercase-only env keys → apps wanting lowercase/nested config need an in-container translation shim.
- No persistent filesystem → state must go to MySQL and/or the bucket; the DB-backed CMS model fits well.
