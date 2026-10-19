# PicoPaaS CLI

`cli/picopaas.py` (needs `httpx`). Create a `pp_sk_` API key in the UI, then:

```bash
export PICOPAAS_TOKEN=pp_sk_...            # PICOPAAS_URL defaults to https://platform.raisehellpraisedale.xyz
python cli/picopaas.py apply -f app.yaml --secret API_TOKEN=s3cret
python cli/picopaas.py apps
python cli/picopaas.py status my-app
python cli/picopaas.py rotate my-app --db      # or --storage
```

Example `app.yaml` (max 64KB, no YAML anchors/aliases, unknown keys rejected):

```yaml
apiVersion: picopaas/v1
name: my-app
image: ghcr.io/me/app:1.2
port: 8080
healthCheck: { enabled: true, path: /healthz }   # optional; off by default, path defaults to /healthz
resources: { db: true, storage: false }
env:
  LOG_LEVEL: info
  DATABASE_URL: ${{ db.url }}
secrets:
  - API_TOKEN
```

`image` above is a placeholder: use your own public image (or `ghcr.io/seaburr/picopaas-testapp-hello:latest`
to try it), otherwise `apply` fails with `Error: 422: image not found or not public`.

`apply` creates the app, or updates it if you already have one with that `name`, and prints
`created|updated <name>: https://<hostname> (<status>)`. On update, a secret without `--secret` keeps its
stored value; anything removed from the file (env, secrets, healthCheck) is removed from the app. `rotate`
prints `rotated db credentials for <name>`. On an API error every command prints the status and detail
(for example `error: 409: storage not provisioned yet`) and exits 1. Env values must be strings (quote numbers and booleans).

`healthCheck` (optional, unknown keys rejected): `enabled` (bool, default false) turns on HTTP readiness
and liveness probes against `path` on your app's port; a 2xx/3xx response is healthy. `path` defaults to
`/healthz`, must start with `/` (max 255 chars, letters, digits and `._~!$&()*+,;=:@/%-` only, no `..`
segments, spaces or quotes). When disabled, readiness is a plain TCP connect and there is no liveness probe.
Changing it redeploys the app.

`PORT` is set to your app's port unless you set it yourself in `env`.

## Connecting to your database

`${{ db.url }}` is a plain `mysql://user:pw@host:port/db` URL with no driver-specific parameters. Managed
MySQL requires TLS, so also map the CA and TLS flag:

```yaml
env:
  DATABASE_URL: ${{ db.url }}
  DB_CA_CERT: ${{ db.ca_cert }}   # PEM text; empty string if the server has no CA
  DB_TLS: ${{ db.tls }}           # "required" if the server has a CA, else "off"
```

Python (pymysql): write the PEM to a file at startup, then pass it as `ssl`.

```python
import os, ssl, pymysql
ca = os.environ.get("DB_CA_CERT", "")
ctx = None
if ca:
    open("/tmp/db-ca.pem", "w").write(ca)
    ctx = ssl.create_default_context(cafile="/tmp/db-ca.pem")  # verifies chain + hostname
    # Python 3.13+ rejects DO's CA in strict mode ("Basic Constraints of CA cert not marked critical").
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
conn = pymysql.connect(host=..., user=..., password=..., database=..., ssl=ctx)
```

Node (mysql2):

```js
const conn = mysql.createConnection({
  host, user, password, database,
  ssl: process.env.DB_CA_CERT ? { ca: process.env.DB_CA_CERT } : undefined,
});
```

mysql CLI (save the PEM to `ca.pem` first):

```bash
mysql --ssl-mode=VERIFY_CA --ssl-ca=ca.pem -h HOST -P PORT -u USER -p DBNAME
```
