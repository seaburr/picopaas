# Ghost on PicoPaaS

A real, mainstream CMS (Ghost 6; Ghost 5 is still built until the live blogs are cut over) running on PicoPaaS within the platform's constraints — proof that the
platform can host a genuine app, not just toy test images. Live demo: deployed as app `ghost-blog`.

## What works
- **Ghost serves**: homepage, `/ghost/` admin, and `/rss/` all return 200.
- **Persistent**: all content lives in the PicoPaaS **MySQL** database, so it survives restarts/redeploys
  (verified: restarted the app — pod replaced, ephemeral disk wiped — and the site + data were intact).
- **Fits the caps**: image ~**185 MiB** (< 512 MB image cap), boots and runs within **384 MB RAM** (0 restarts),
  schema uses ~3 MB of the 50 MB DB.

## Why a custom image (not the stock `ghost` image)
The official `ghost:N-alpine` image starts as **root** and `su-exec`'s down to `node`; PicoPaaS rejects root
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
   `...-ghost:6` and `:6-<sha>` (Ghost 6), plus `:latest` and `:<sha>` (still Ghost 5 until cutover; make the GHCR package **public** — PicoPaaS pulls anonymously).
2. Create an app with a **database** and a **blob storage bucket**, port **2368**, image
   `ghcr.io/seaburr/picopaas-example-ghost:6` (new blogs; existing blogs still run `:latest` = Ghost 5), and these env refs:
   `DB_HOST=${{ db.host }}`, `DB_PORT=${{ db.port }}`, `DB_NAME=${{ db.name }}`, `DB_USER=${{ db.user }}`,
   `DB_PASSWORD=${{ db.password }}`, `DB_CA_CERT=${{ db.ca_cert }}`, `S3_ENDPOINT=${{ storage.endpoint }}`,
   `S3_REGION=${{ storage.region }}`, `S3_BUCKET=${{ storage.bucket }}`, `S3_ACCESS_KEY=${{ storage.access_key }}`,
   `S3_SECRET_KEY=${{ storage.secret_key }}`, `SITE_URL=${{ app.url }}`.
3. **Single step**: `${{ app.url }}` gives Ghost its public URL at deploy time, so there is no second deploy.
4. Finish setup at `/ghost/`.

## Migrating from WordPress
See [MIGRATING-FROM-WORDPRESS.md](MIGRATING-FROM-WORDPRESS.md) (approach, limits) and the hands-on
[WORDPRESS-TO-GHOST-WALKTHROUGH.md](WORDPRESS-TO-GHOST-WALKTHROUGH.md). Script: `tools/wp-to-ghost.py`.

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

## Upgrading to Ghost 6 (runbook)

### Image tags
| Tag | Ghost | Who uses it |
|---|---|---|
| `:latest`, `:<sha>` | 5 (`ghost:5-alpine`, `GHOST_MAJOR=5`) | blog.picopaas.xyz (app 98) and blog.chasberndt.com (app 164) until cut over |
| `:6`, `:6-<sha>` | 6 (`ghost:6-alpine`, Dockerfile default) | new blogs and the rehearsal app |

Pushing to `main` therefore never upgrades a live blog. Final step, only after both blogs run 6 and are verified:
delete the `ghost_major: "5"` row in `.github/workflows/examples.yml` and set the Ghost 6 row's `tag_main`/`tag_sha_prefix`
to `latest`/empty (or leave blogs pinned to `:6`, which is the safer long-term choice: re-point apps to `:6`, never to a moving `:latest`).

### Facts (checked 2026-10-25)
- Ghost 6.0 shipped 2025-08-04 (ghost.org/changelog/6). Ghost 5 is now the LTS line and its end of life is January 2026 (docs.ghost.org/faq/major-versions-lts): treat 5 as unsupported and insecure.
- Requirements: Node 22 only, MySQL 8 only (docs.ghost.org/changes). The base image carries Node 22; DO managed MySQL 8 is fine.
- Removed: AMP, `GET /admin/session/` (use `/users/me/`), `?limit=all` (max 100 per page, paginate), DB columns `created_by`/`updated_by`, extension-less theme files from the theme root. Admin API URL and `Accept-Version` behave as in 5 (no change in 6.0).
- ActivityPub and Tinybird analytics are separate optional services; the stock image runs without them. Self-hosted analytics needs a Tinybird account, so it stays off here. Mail is blocked on PicoPaaS, so nothing changes for staff verification (entrypoint still disables it).
- The Docker Compose install is a preview and is not our model (we run the single `ghost` image with external MySQL).
- Upgrade: Ghost runs DB migrations on boot, migrations are one-way, and the docs do not name a minimum 5.x. Take the latest 5.x first (the `ghost:5-alpine` base is rebuilt weekly, 5.x latest) and expect a direct 5 -> 6 jump to work; the rehearsal proves it on our data.
- S3 adapter: `ghost-storage-adapter-s3@2.8.0` (latest, aws-sdk v2) has no 6-specific docs. Local test of the 6.72.0 image against MySQL 8: boots, migrates, serves `/`, `/ghost/`, admin site API with `storage__active=s3`, and the adapter module loads. Uploads to a real bucket are checked in the rehearsal. Image: ~207 MiB compressed with S3 (cap 512 MB).
- The favicon `IMAGE_SIZE_URL` error in logs when browsing `http://localhost` is a local self-fetch artifact, not an app fault.

### Cutover plan (per blog: do app 98 first, then 164)
Needs an admin full-scope key; backups must be enabled for the platform. The restore endpoint is admin-only.
1. Fresh backup: `POST /apps/{id}/backups` (db + bucket); poll `GET /apps/{id}/backups` until the new set is `succeeded`.
   Do not continue unless it is. Also record the current image (`:latest`, note the digest from GHCR) and the app's env.
   Optionally download the package (`POST /apps/{id}/backups/sets/{set_id}/download`) and keep it off-platform.
2. Rehearse on a throwaway app: create an app with a DB and a bucket, image `ghcr.io/seaburr/picopaas-example-ghost:6`,
   the env refs from "Deploy it" (port 2368), then `POST /admin/backups/{run_id}/restore` with `{target_app_id}` for the db run and the bucket run
   (the restore replaces contents), restart/redeploy so Ghost 6 migrates the restored DB. SITE_URL must be the throwaway's URL.
   Check: `/ghost/` admin login; posts and pages render; images load from the bucket and a new upload persists across a restart;
   Admin API works with the release-blogger token (mint JWT as in deploy/ghost/README.md; GET `/ghost/api/admin/site/` and `/posts/?limit=1`, and a draft create+delete);
   RSS; theme (check Admin > Design for gscan warnings; the custom code injection is stored in the DB and carries over).
   Delete the throwaway app when done.
3. Switch the real app: change its image to `ghcr.io/seaburr/picopaas-example-ghost:6` (PATCH the app / redeploy). Ghost migrates the DB on first boot (watch the logs, allow a few minutes; 384 MiB RAM is tight, so watch for OOM).
   Verify: homepage, `/ghost/` login, a recent post, an image, RSS, `blog.*` TLS, Admin API with the blogger token; then re-run the branding PUT only if the code injection looks lost.
4. Rollback: migrations are one-way, so Ghost 5 cannot read the upgraded DB. Set the image back to the recorded Ghost 5 ref, AND restore the pre-upgrade backup into the same app
   (`POST /admin/backups/{run_id}/restore` with `overwrite_source: true`; the app must be suspended first), then redeploy and verify.
   Keep the pre-upgrade backup set undeleted until the blog has run on 6 for at least a week.
5. After both blogs are on 6, retire the Ghost 5 build (see "Image tags").
