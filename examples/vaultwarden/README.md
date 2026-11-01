# Vaultwarden (password manager) on PicoPaaS

[Vaultwarden](https://github.com/dani-garcia/vaultwarden) is a lightweight server that speaks the Bitwarden
API, so the official Bitwarden apps and browser extensions work with it. Its SQLite database, attachments
and signing key live on a persistent volume at `/data`.

> PicoPaaS is for throwaway and test projects. Don't keep passwords you can't afford to lose or leak here.

## The image: one line on top of upstream

The official `vaultwarden/server` image has no `USER` (it runs as root), which PicoPaaS rejects. `Dockerfile`
here is `FROM vaultwarden/server:1.37.4-alpine` plus `USER 10001:10001` and `ROCKET_PORT=8080` (the
upstream default port is 80). Nothing in its start script needs root. `examples.yml` publishes it as
`ghcr.io/seaburr/picopaas-example-vaultwarden`.

## Deploy

1. Create an app with port **8080**, health check `/alive`, a **1 GiB volume at `/data`**, no database, and
   the env from `vaultwarden.yaml` (or `picopaas apply -f vaultwarden.yaml`).
2. Open the app URL and create your account straight away.
3. Set `SIGNUPS_ALLOWED` to `false` and save. The app redeploys; your vault stays on the volume.

## Notes

- Use an authenticator app for two-factor login. Email (invitations, email 2FA, password hints by mail) is
  not available because SMTP is blocked.
- The `/admin` page is off. To turn it on, add an `ADMIN_TOKEN` secret holding an Argon2 hash from
  `vaultwarden hash`, never the plain password.
- Volume backups cover `/data`. Do a test restore before you rely on them.
