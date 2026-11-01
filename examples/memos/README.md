# Memos (notes) on PicoPaaS

[Memos](https://usememos.com/) is a lightweight, self-hosted notes and microblog app. This recipe stores
everything in the platform's **PostgreSQL** database, so it needs no volume.

## The image: one line on top of upstream

The official `neosmemo/memos` image starts as root only to fix file ownership, then drops to its own
UID 10001 user. PicoPaaS rejects root images, so `Dockerfile` here is `FROM neosmemo/memos:26.10.0` plus
`USER 10001:10001`. The upstream entrypoint skips the root-only step when it is not root. `examples.yml`
publishes it as `ghcr.io/seaburr/picopaas-example-memos`.

## Deploy

1. Create an app with a **database**, engine **PostgreSQL**, port **5230**, health check `/healthz`, and the env
   from `memos.yaml` (or `picopaas apply -f memos.yaml`).
2. Open the app URL right away and create your account: **the first account becomes the admin.** Then turn
   off sign-ups in Settings.

## Notes

- `MEMOS_DSN=${{ db.url }}` is a `postgres://` URL with `sslmode=require` (encrypted, not CA-verified).
- Attachments are stored in the database by default and count toward the 50 MB quota. For more than a few
  images, set S3 storage in Memos' settings (use a bucket on the app) or keep attachments small.
- No email (SMTP is blocked).
- **Verified live 2026-11-01:** first account is ADMIN, memo create and list over the REST API (`/api/...`; the
  old `/api/v1` paths are gone in 26.x), data kept across a restart. About 24 MiB RAM, schema 8.8 MB of the 50 MB DB.
- Sign-ups stay open until you turn them off: a second sign-up gets an ordinary user account.
