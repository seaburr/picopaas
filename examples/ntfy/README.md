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

**Verified live 2026-11-03** (throwaway app, published GHCR wrapper, PostgreSQL via the CA-verified DSN):
- Boots and serves `/v1/health` (`{"healthy":true}`) with `sslmode=verify-full&sslrootcert=${{ db.ca_file }}`.
- Negative test: the same DSN with `sslrootcert=/etc/ssl/certs/ca-certificates.crt` (system bundle) makes ntfy exit with
  `FATAL database ping failed ... x509: certificate signed by unknown authority`, so verification is really on.
- Anonymous publish and poll return 403; wrong password 401; authenticated publish and poll work; an access token
  from `/v1/account/token` works.
- After a platform restart the message history, the token and the user still work (state is in PG).
- Numbers: about 15 MiB RAM (one metrics sample, 13m CPU); DB 8.2-8.4 MB of 50 MB after startup and one message.

## Gotchas

- Upstream has `ENTRYPOINT ["ntfy"]` and no `CMD`, and PicoPaaS has no command override, so the wrapper must set `CMD ["serve"]`
  (the first published wrapper lacked it and printed help).
- `status` stays `deploying` for a while after ntfy is already serving; judge by `/v1/health`.
