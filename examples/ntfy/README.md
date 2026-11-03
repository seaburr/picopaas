# ntfy (push notifications) on PicoPaaS

[ntfy](https://ntfy.sh/) is a simple HTTP pub/sub notification server. This recipe keeps the message cache, users and
tokens in the platform's **PostgreSQL** database, so it needs no volume.

## The image: one line on top of upstream

`binwiederhier/ntfy` sets no `USER`, and the platform check rejects it ("Image runs as root, which isn't allowed").
`Dockerfile` here is `FROM binwiederhier/ntfy:v2.37.1` plus `USER 10001:10001`; the Go binary needs no root on port 8080.
`examples.yml` builds it and publishes `ghcr.io/seaburr/picopaas-example-ntfy`, which `ntfy.yaml` points at. The image
exists only after that workflow has run on the pushed commit; until then the spec cannot pull.

## Deploy

1. Make a bcrypt hash of a password (`htpasswd -nbBC 10 x pw`, or `ntfy user hash` interactively) and build
   `NTFY_AUTH_USERS` as `name:hash:admin`.
2. `picopaas apply -f ntfy.yaml --secret 'NTFY_AUTH_USERS=name:$2a$10$...:admin'`
3. Publish with `curl --user "$NTFY_USER" -d "deployed" https://<app>/mytopic` (set `NTFY_USER` to `name:password`), read back with `curl --user "$NTFY_USER" "https://<app>/mytopic/json?poll=1&since=all"`.

## Notes

- `NTFY_DATABASE_URL` uses `sslmode=verify-full&sslrootcert=${{ db.ca_file }}`. When it is set, do NOT set the cache, auth or
  web-push file options (ntfy then writes no files). Set `pool_max_conns=3` to stay inside the per-role connection budget.
- `NTFY_AUTH_DEFAULT_ACCESS=deny-all` so anonymous publish/subscribe gets 403; an open ntfy is an abuse magnet.
- Provisioned users are deleted if removed from `NTFY_AUTH_USERS` at the next start.
- No SMTP (email in/out unusable).

## Verified

Local only (2026-11-03, ntfy 2.37.1 amd64 under emulation, `--user 10001`, Postgres 16 container with `sslmode=disable`):
healthy in about 1 s; anonymous publish 403; authenticated publish and poll OK; access token created via
`/v1/account/token`; after `docker restart` the message history, the token and the user all still work (state is in PG);
about 65 MiB RAM with 3 pool connections; 12 tables, DB 8.3 MB of 50 MB. **Live test not run:** the wrapper image
is unpublished, the platform refuses the root upstream image, and pushing a temporary public image was blocked by the
permission system. TLS verify-full against the platform CA is therefore still unproven for ntfy (Miniflux/Memos prove the same pattern).
