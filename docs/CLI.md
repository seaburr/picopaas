# PicoPaaS CLI

`cli/picopaas.py` (needs `httpx`). Create a `pp_sk_` API key in the UI, then:

```bash
export PICOPAAS_TOKEN=pp_sk_...            # PICOPAAS_URL defaults to https://picopaas.raisehellpraisedale.xyz
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
resources: { db: true, storage: false }
env:
  LOG_LEVEL: info
  DATABASE_URL: ${{ db.url }}
secrets:
  - API_TOKEN
```

`apply` creates the app, or updates it if you already have one with that `name`. On update, a secret
without `--secret` keeps its stored value. Env values must be strings (quote numbers and booleans).

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
import os, pymysql
ca = os.environ.get("DB_CA_CERT", "")
if ca:
    open("/tmp/db-ca.pem", "w").write(ca)
conn = pymysql.connect(host=..., user=..., password=..., database=...,
                       ssl={"ca": "/tmp/db-ca.pem"} if ca else None)
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
