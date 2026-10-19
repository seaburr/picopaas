# testapps

Tiny images for exercising a live PicoPaaS deployment end to end (deploy, env refs, DB/Spaces
provisioning, quotas, network isolation). Each is non-root (`USER 10001:10001`), listens on `$PORT`
(default `8080`), and serves `GET /healthz` -> `200 "ok"`. Built and pushed by
`.github/workflows/testapps.yml` to:

- `ghcr.io/seaburr/picopaas-testapp-hello:latest`
- `ghcr.io/seaburr/picopaas-testapp-probe:latest`
- `ghcr.io/seaburr/picopaas-testapp-toolbox:latest`
- `ghcr.io/seaburr/picopaas-testapp-webpage:latest`

(also tagged with the short commit SHA).

## hello

No dependencies, stdlib only. `GET /` -> `{"hello": "picopaas", "hostname": ..., "version": <$APP_VERSION>}`.
Smoke-tests a basic deploy.

```yaml
# app.yaml
apiVersion: picopaas/v1
name: hello
image: ghcr.io/seaburr/picopaas-testapp-hello:latest
port: 8080
healthCheck: {enabled: true, path: /healthz}
env:
  APP_VERSION: "1.0.0"
```

## probe

Exercises DB and Spaces connectivity and quotas. Deps: `pymysql`, `boto3` (pinned in
`requirements.txt`).

Endpoints (all JSON, `{"ok": false, "error": ...}` + HTTP 500 on failure — never a stack trace or
credential):

- `GET /db` - connect, `CREATE TABLE IF NOT EXISTS probe`, insert a row, count rows ->
  `{"ok": true, "rows": n, "tls": "<cipher or null>"}`.
- `GET /s3` - put an object under `probe/<uuid>`, read it back, compare, list, delete ->
  `{"ok": true, "objects": n}`.
- `GET /db/fill?mb=N` / `GET /s3/fill?mb=N` - write ~N MB (for quota tests) ->
  `{"ok": bool, "written_mb": x, "error": ...}`.

```yaml
# app.yaml
apiVersion: picopaas/v1
name: probe
image: ghcr.io/seaburr/picopaas-testapp-probe:latest
port: 8080
resources: {db: true, storage: true}
healthCheck: {enabled: true, path: /healthz}
env:
  DB_HOST: ${{ db.host }}
  DB_PORT: ${{ db.port }}
  DB_NAME: ${{ db.name }}
  DB_USER: ${{ db.user }}
  DB_PASSWORD: ${{ db.password }}
  DB_CA_CERT: ${{ db.ca_cert }}
  S3_ENDPOINT: ${{ storage.endpoint }}   # full URL incl. https://
  S3_REGION: ${{ storage.region }}
  S3_BUCKET: ${{ storage.bucket }}
  S3_ACCESS_KEY: ${{ storage.access_key }}
  S3_SECRET_KEY: ${{ storage.secret_key }}
```

`DB_CA_CERT` is optional; when set, probe writes it to a temp file and connects to MySQL with TLS
verification (`ssl_verify_cert` + `ssl_verify_identity`).

## toolbox

The pentest "attacker" image used in `docs/PENTEST.md` to probe PicoPaaS's own network isolation
from inside a tenant app's pod/namespace. Includes `nmap`, `curl`, `dnsutils` (`dig`/`nslookup`),
a `mariadb`/`mysql` client, and Python with `pymysql`/`boto3` — but the HTTP API only runs a fixed,
validated set of named checks; there is no arbitrary command execution.

All requests require `Authorization: Bearer $TOOLBOX_TOKEN`. If `TOOLBOX_TOKEN` is unset, every
request is refused (401).

- `GET /checks` -> `{"checks": [...]}`
- `POST /run {"check": name, "args": {...}}` -> `{"check", "result": "open|closed|allowed|denied|error", "detail": "..."}`

Checks: `tcp_connect`, `port_scan` (<=100 ports), `http_get` (status + <=2KB body), `k8s_api`
(reachability of `kubernetes.default.svc:443` + whether a service-account token file exists),
`metadata` (`169.254.169.254`), `dns_lookup`, `dns_srv_enum` (in-cluster service name patterns),
`mysql_login`/`mysql_enum` (allowed/denied + MySQL error code, no cleartext creds in the response),
`s3_access` (list/get/put), `smtp_egress`.

```yaml
# app.yaml
apiVersion: picopaas/v1
name: toolbox
image: ghcr.io/seaburr/picopaas-testapp-toolbox:latest
port: 8080
healthCheck: {enabled: true, path: /healthz}
secrets: [TOOLBOX_TOKEN]   # value supplied at apply time, never written in the spec
```

Never give toolbox real tenant credentials in a shared/long-lived env var; pass them per-request in
`POST /run` args instead, and rotate `TOOLBOX_TOKEN` after a pentest run.

## webpage

Stdlib-only server that serves a real page (HTML + CSS + JS, no CDNs) to prove rendering works
through the platform. Strict `Content-Security-Policy: default-src 'self'`.

- `GET /` - page with `#css-check` (CSS colours it `rgb(12, 34, 56)`), `#js-check` ("JS pending" -> "JS OK"),
  and `#info` (hostname from `/api/info`); JS sets `data-ok="1"` on `<body>` when done.
- `/style.css`, `/app.js`, `/favicon.svg` - assets with `Cache-Control`.
- `GET /api/info` -> `{"hostname", "version": <$APP_VERSION>, "time"}`; `GET /healthz` -> `ok`.

```yaml
# app.yaml
apiVersion: picopaas/v1
name: webpage
image: ghcr.io/seaburr/picopaas-testapp-webpage:latest
port: 8080
healthCheck: {enabled: true, path: /healthz}
env:
  APP_VERSION: "1.0.0"
```

Live test: `tests/e2e/test_webpage.py` (browser part needs `pip install playwright && python -m playwright install chromium`).
