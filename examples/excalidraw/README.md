# Excalidraw on PicoPaaS

A static site: no database, no volume, no bucket, no env. About 20 MiB RAM idle.

## The image: a wrapper

The upstream image (`excalidraw/excalidraw`) is nginx **as root on port 80** (no `USER`, `EXPOSE 80`), which PicoPaaS rejects.
`Dockerfile` here copies its built `/usr/share/nginx/html` into `nginxinc/nginx-unprivileged:stable-alpine`
(port 8080) with a one-rule `default.conf` (SPA fallback to `index.html`). It starts fine as UID 10001.
`examples.yml` builds it and publishes `ghcr.io/seaburr/picopaas-example-excalidraw`, which `excalidraw.yaml` points at (available after the workflow runs on the pushed commit).

## Deploy

`picopaas apply -f excalidraw.yaml` (or create an app with port 8080 and health check `/`).

## Verified

**Verified live 2026-11-03** (throwaway app, published GHCR wrapper): running with the health check on, `/` and a deep link
(`/base64-string-converter`) both return 200 with the right page title. Also checked locally with `docker run --user 10001`: about 20 MiB RAM idle.

## Gotchas

- Upstream image `excalidraw/excalidraw:latest` was pushed 2026-05-06 (repo release v0.18.1, 2026-04-21). Wrapper image about 159 MB.
- Live collaboration and the library browser call external services (egress 443 is allowed); drawing works without them.
