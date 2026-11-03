# IT-Tools on PicoPaaS

A static site: no database, no volume, no bucket, no env. About 20 MiB RAM idle.

## The image: a wrapper

The upstream image (`corentinth/it-tools`) is nginx **as root on port 80** (no `USER`, `EXPOSE 80`), which PicoPaaS rejects.
`Dockerfile` here copies its built `/usr/share/nginx/html` into `nginxinc/nginx-unprivileged:stable-alpine`
(port 8080) with a one-rule `default.conf` (SPA fallback to `index.html`). It starts fine as UID 10001.
`examples.yml` builds it and publishes `ghcr.io/seaburr/picopaas-example-it-tools`, which `it-tools.yaml` points at (available after the workflow runs on the pushed commit).

## Deploy

`picopaas apply -f it-tools.yaml` (or create an app with port 8080 and health check `/`).

## Verified

Local only (2026-11-03, linux/amd64 under emulation): `docker run --user 10001 -p ...:8080` serves `/` and a deep
link with 200, correct page title, ~20 MiB RAM. **Live test pending publish** of the wrapper image.

## Gotchas

- Upstream's last image (`latest`) is from 2024-10-22 (release v2024.10.22); a `nightly` exists (2026-02). The wrapper
  inherits that staleness: pin or build from a newer source tag to refresh. Wrapper image about 108 MB.
- Server sends nothing out; all tools run in the browser.
