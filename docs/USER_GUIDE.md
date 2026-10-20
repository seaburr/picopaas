# PicoPaaS User Guide

For app owners. Deploy one Docker image, give it env vars and secrets, and optionally get a MySQL database and a
Spaces (S3-compatible) bucket. Your app is served over HTTPS at a generated hostname such as
`clever-robin-a1b2c.raisehellpraisedale.xyz`.

Platform URL: `https://platform.raisehellpraisedale.xyz` (the CLI default).

## 1. Getting an account

1. Open the platform URL, click **Register**, enter an email and a password (8 to 256 characters), and accept the terms (this is a dev/test platform; the API requires `accept_terms: true`).
2. New accounts are `pending`. You can log in, but the API refuses app and key calls with
   `403 account pending approval` until the uberadmin approves you. The UI shows a "pending" page.
3. Once approved (`active`) you can create apps and API keys. The first account ever registered on a fresh
   platform becomes the `uberadmin` and is active immediately.

A suspended account gets `403 account suspended`, and its apps are scaled to zero until the uberadmin unsuspends it.

## 2. Platform rules

| Rule | Value |
|---|---|
| User | The image must not run as root. `root`, `0` or an empty `USER` is rejected with 422. A named non-root user (for example `USER app`) is run as UID 10001, so files the image owns as that user may not be writable. Baking in a numeric `USER` (for example `USER 10001`) avoids that. |
| Image size | Summed compressed layers at most **256 MB** (checked against the registry manifest on create and update). |
| Image visibility | Public images only. A missing or private image gives `422` "Couldn't find that image, or it's private...". |
| CPU / memory | Each app gets a limit of **100m CPU / 384Mi** (request 64m / 128Mi). One replica. |
| Port | Your app must listen on the `port` in your spec, on all interfaces. The platform also sets env `PORT=<port>` unless you define `PORT` yourself in `env`. |
| Apps per user | **1** by default. The uberadmin can raise it. Over the limit gives `403` "You've reached your limit of N apps...". The uberadmin is exempt. |
| Database | Optional, 50 MB. |
| Storage | Optional, 100 MB bucket. |
| Spec size | A YAML spec is at most 64 KB. |
| Hardening | Pods run with seccomp `RuntimeDefault`, no privilege escalation, all capabilities dropped. There is no persistent disk; use the database or bucket. |

Dockerfile example:

```dockerfile
FROM python:3.12-slim
USER 10001:10001
CMD ["python", "-m", "http.server", "8080"]
```

## 3. Deploying

There are four ways in. All of them end up as the same app record, and the reconciler (runs every 15 seconds)
makes the cluster match it.

### 3a. UI form

**Apps -> New app**, **Form** tab: name, image, port, optional Database / Storage checkboxes, env vars (mark
secrets as secret), and an optional health check. Database and storage cannot be changed after the app is
created. Editing an app (image, port, env, health check) redeploys it. The edit page also has **Rotate DB
password** and **Rotate storage keys** buttons.

### 3b. YAML in the UI

**Apps -> New app**, **YAML** tab (create only). Paste a spec (see the reference below) and supply secret values
in the secrets box. Submitting an existing app name updates that app.

### 3c. API with curl

Create an API key under **API keys** in the UI (shown once, starts with `pp_sk_`).

```bash
export PP=https://platform.raisehellpraisedale.xyz
export TOKEN=pp_sk_...

# Deploy (create or update, matched on spec name) from a YAML file
curl -sS -X POST "$PP/apps/spec" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "$(jq -n --rawfile spec app.yaml '{spec: $spec, secrets: {API_TOKEN: "s3cret"}}')"
```

Body shape for `POST /apps/spec`: `{"spec": "<yaml text>", "secrets": {"NAME": "value"}}`. `secrets` is optional,
and every key must be listed in the spec's `secrets` list. The response is `200` with the app plus
`created: true|false` (also `200` when it creates the app).

Plain JSON endpoints:

```bash
# Create
curl -sS -X POST "$PP/apps" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{
  "name": "hello",
  "image": "ghcr.io/seaburr/picopaas-testapp-hello:latest",
  "port": 8080,
  "want_db": false, "want_storage": false,
  "env": [{"key": "APP_VERSION", "value": "1.0.0", "is_secret": false}],
  "health_check_enabled": true, "health_check_path": "/healthz"
}'

# Update (any subset of name, image, port, env, health_check_enabled, health_check_path)
curl -sS -X PATCH "$PP/apps/1" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"env": [{"key": "APP_VERSION", "value": "1.0.1", "is_secret": false}]}'

curl -sS "$PP/apps"      -H "Authorization: Bearer $TOKEN"   # list yours
curl -sS -X DELETE "$PP/apps/1" -H "Authorization: Bearer $TOKEN"
```

Notes on `PATCH`: `env`, if present, **replaces the whole list**. To keep an existing secret unchanged, send it
back with `"value": "********"` and `"is_secret": true`. Unknown fields are rejected with 422. Full route list:
[API.md](API.md).

### 3d. CLI

`cli/picopaas.py` needs `httpx`. See [CLI.md](CLI.md).

```bash
export PICOPAAS_TOKEN=pp_sk_...     # PICOPAAS_URL defaults to https://platform.raisehellpraisedale.xyz
python cli/picopaas.py apply -f app.yaml --secret API_TOKEN=s3cret
python cli/picopaas.py apps
python cli/picopaas.py status my-app
python cli/picopaas.py rotate my-app --db       # or --storage
```

`apply` prints `created|updated <name>: https://<hostname> (<status>)`. On update, a secret without `--secret`
keeps its stored value.

## 4. YAML spec reference

Unknown keys are rejected anywhere. No YAML anchors or aliases. Maximum 64 KB.

```yaml
apiVersion: picopaas/v1
name: my-app
image: ghcr.io/me/app:1.2
port: 8080
healthCheck: { enabled: true, path: /healthz }
resources: { db: true, storage: false }
env:
  LOG_LEVEL: info
  DATABASE_URL: ${{ db.url }}
secrets:
  - API_TOKEN
```

| Field | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `apiVersion` | string | yes | | Exactly `picopaas/v1`. |
| `name` | string | yes | | 1 to 255 chars. Identifies the app: applying the same name again updates it. The name is display only; the hostname is generated and does not change. |
| `image` | string | yes | | 1 to 512 chars. Public, non-root `USER`, at most 256 MB. |
| `port` | integer | **yes** | none | 1 to 65535. There is no default. |
| `resources.db` | boolean | no | `false` | Provisions a MySQL database. Cannot be removed from an existing app via spec (422). |
| `resources.storage` | boolean | no | `false` | Provisions a Spaces bucket. Same removal rule. |
| `healthCheck.enabled` | boolean | no | `false` | Turns on HTTP readiness and liveness probes. |
| `healthCheck.path` | string | no | `/healthz` | Must start with `/`, max 255 chars, only letters, digits and `._~!$&()*+,;=:@/%-`, no `..` segments (also `%2e%2e`), no spaces or quotes. |
| `env` | map string to string | no | `{}` | Keys match `^[A-Z_][A-Z0-9_]*$`. Values must be strings (quote numbers and booleans: `"1.0"`, `"true"`). |
| `secrets` | list of strings | no | `[]` | Names only; values never go in the file. No duplicates, and a name may not also be in `env`. Names follow the same key rule as `env`. |

Strict typing: `port: "8080"` (a string) or `db: "yes"` is rejected.

### Secrets on create and update

- Create: every name in `secrets` needs a value (`secrets` in the API body, `--secret K=V` in the CLI), otherwise
  `422 missing values for secrets: ...`.
- Update: a secret you do not resupply keeps its stored value. A new secret name needs a value.
- Update is declarative: the app's `image`, `port`, `env`, `secrets` and `healthCheck` become exactly what the
  spec says. An env var or secret missing from the spec is removed, and a spec without `healthCheck` turns the
  health check off.
- Secret values are encrypted at rest and shown as `********` in API responses.

## 5. Env vars, secrets and `${{ }}` references

User env vars (plain or secret) are put into a Kubernetes Secret in your app's namespace and injected into the
container as environment variables. Changing them redeploys (restarts) the app.

A value that is **exactly** `${{ ref }}` (whole value, optional inner spaces) is replaced at deploy time. You
cannot embed a reference inside a longer string. An unknown or embedded reference is a
`422 invalid reference in value: ...` (the message lists the allowed references), and `db.*` requires
`resources.db: true` (`storage.*` requires `resources.storage: true`), otherwise
`422 ${{ db.url }} references a database, but this app did not request one (set want_db)`.

| Reference | Value |
|---|---|
| `${{ db.host }}` | DB server hostname |
| `${{ db.port }}` | DB server port |
| `${{ db.name }}` | Your database name |
| `${{ db.user }}` | Your database user |
| `${{ db.password }}` | Your database password |
| `${{ db.url }}` | `mysql://user:password@host:port/name` (no driver parameters) |
| `${{ db.ca_cert }}` | CA certificate PEM text; empty string if the server has none |
| `${{ db.tls }}` | `required` if the server has a CA cert, else `off` |
| `${{ storage.endpoint }}` | Full URL, for example `https://atl1.digitaloceanspaces.com` |
| `${{ storage.region }}` | For example `atl1` |
| `${{ storage.bucket }}` | Your bucket name |
| `${{ storage.access_key }}` | Bucket access key |
| `${{ storage.secret_key }}` | Bucket secret key |

You pick your own variable names. The platform does not inject `DATABASE_URL` and friends by itself.

## 6. Connecting to MySQL (TLS)

Managed MySQL requires TLS, so map the CA and the TLS flag:

```yaml
resources: { db: true }
env:
  DATABASE_URL: ${{ db.url }}
  DB_HOST: ${{ db.host }}
  DB_PORT: ${{ db.port }}
  DB_NAME: ${{ db.name }}
  DB_USER: ${{ db.user }}
  DB_PASSWORD: ${{ db.password }}
  DB_CA_CERT: ${{ db.ca_cert }}
  DB_TLS: ${{ db.tls }}
```

Python (pymysql). Python 3.13 enables `VERIFY_X509_STRICT`, which rejects DigitalOcean's CA ("Basic Constraints
of CA cert not marked critical"), so clear that one flag. Chain and hostname checks stay on.

```python
import os, ssl, pymysql
ca = os.environ.get("DB_CA_CERT", "")
ctx = None
if ca:
    open("/tmp/db-ca.pem", "w").write(ca)
    ctx = ssl.create_default_context(cafile="/tmp/db-ca.pem")
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
conn = pymysql.connect(host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]),
                       user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
                       database=os.environ["DB_NAME"], ssl=ctx)
```

Node (mysql2):

```js
const conn = mysql.createConnection({
  host: process.env.DB_HOST, port: +process.env.DB_PORT, user: process.env.DB_USER,
  password: process.env.DB_PASSWORD, database: process.env.DB_NAME,
  ssl: process.env.DB_CA_CERT ? { ca: process.env.DB_CA_CERT } : undefined,
});
```

mysql CLI (save the PEM to `ca.pem`; only reachable from inside the cluster, the DB is not public):

```bash
mysql --ssl-mode=VERIFY_CA --ssl-ca=ca.pem -h HOST -P PORT -u USER -p DBNAME
```

## 7. Connecting to Spaces (S3)

```yaml
resources: { storage: true }
env:
  S3_ENDPOINT: ${{ storage.endpoint }}
  S3_REGION: ${{ storage.region }}
  S3_BUCKET: ${{ storage.bucket }}
  S3_ACCESS_KEY: ${{ storage.access_key }}
  S3_SECRET_KEY: ${{ storage.secret_key }}
```

```python
import os, boto3
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT"],          # https://<region>.digitaloceanspaces.com
    region_name=os.environ["S3_REGION"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
)
s3.put_object(Bucket=os.environ["S3_BUCKET"], Key="hello.txt", Body=b"hi")
```

The key is scoped to your bucket only. The endpoint is HTTPS (`storage.endpoint` already includes `https://`).

## 8. Health checks

Off by default. Without it, readiness is a TCP connect to your port and there is no liveness probe.

With `healthCheck.enabled: true`, an HTTP GET to `path` on your port; a 2xx or 3xx is healthy.

| Probe | Initial delay | Period | Timeout | Failures before action |
|---|---|---|---|---|
| Readiness | 5 s | 10 s | 3 s | 3 (pod removed from traffic) |
| Liveness | 30 s | 20 s | 5 s | 3 (container restarted) |

Changing the health check redeploys the app. Make sure the path really returns 2xx/3xx, or the pod never becomes
ready.

### Floating tags (`:latest`)

PicoPaaS looks up the exact image behind your tag when you save and runs that specific version (its digest, shown on the app page). Re-pushing
`:latest` therefore does not change a running app by surprise. Every 10 minutes the platform re-checks the tag; a new
image that is under the size limit and runs as non-root is rolled out automatically. A new image that fails those checks is
ignored (your previous version keeps running) and the app page shows why. You can also use the "Check for new image" button, or Restart,
which pulls the latest too.

To pin an exact version, use a digest reference (`ghcr.io/me/app@sha256:...`) or an immutable tag, and untick "Redeploy
automatically when this tag changes" (`auto_update: false` in the API).

## 9. Credential rotation

Rotate DB password or storage keys from the app edit page, with `picopaas rotate NAME --db|--storage`, or
`POST /apps/{id}/rotate-credentials` with `{"target": "db"}` or `{"target": "storage"}`. The app moves to
`pending`, is re-rendered with the new credentials and restarts. It returns `409` "Your database is still being created..."
(or the storage bucket equivalent) if the DB or bucket is not provisioned yet or the app never asked for one, and `502` "Couldn't rotate the credentials, so the old ones still work..." if the provider call failed (the old credentials are kept).
Rotation also repairs an app showing "stored secrets unreadable".

## 10. Quotas

Sizes are polled every 5 minutes, so a limit is enforced with some delay.

| Resource | Limit | At the limit |
|---|---|---|
| Database | 50 MB | Writes that add data are blocked (INSERT/UPDATE/CREATE revoked); SELECT and DELETE keep working so you can clean up. Access is restored automatically once usage drops back under 50 MB. |
| Bucket | 100 MB | The key is swapped for a read-only one and the app restarts with it. Delete objects to get back under 100 MB and the read-write key is restored. |
| Image | 256 MB | Deploy is rejected. |

## 11. Networking and egress

- Inbound: only HTTPS through the platform ingress to your `port`. Apps cannot talk to each other.
- DNS: allowed.
- Internet: allowed on all TCP ports **except SMTP (25, 465, 587)**, plus all UDP. Send mail through an HTTPS API
  (a mail provider's REST endpoint), not SMTP.
- Private ranges are blocked: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16` (this includes
  the cloud metadata address and other cluster services).
- Exceptions: your own database server (its host and port) and your own bucket's private address on port 443.

## 12. App statuses

| Status | Meaning |
|---|---|
| `pending` | Saved, waiting for the reconciler (it runs every 15 s). Also set after any change to image, port, env, health check, or a credential rotation. |
| `deploying` | Applied to the cluster; the new pod is not ready yet, or a rollout is still in progress. |
| `running` | At least one ready pod serving the current version. |
| `error` | See `status_detail`. Typical: image pull failures, crash loops, provisioning failed, no database capacity, or "stored secrets unreadable". |
| `suspended` | Stopped (scaled to zero) by the uberadmin, or because your account was suspended. You cannot edit, rotate or delete it. |
| `deleted` | Torn down (deleted apps normally disappear from your list). |

Deleting an app removes its namespace, database and bucket.

## 13. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `403 account pending approval` | Wait for the uberadmin to approve. |
| `422` "Image runs as root..." | Add a non-root `USER` (preferably numeric, e.g. `USER 10001`) to your Dockerfile. |
| `422` "Couldn't find that image, or it's private" | Wrong tag, or private image. Make the package public. |
| `422 ... over the 268435456 byte (256MB) platform cap` | Shrink the image. |
| `403` "You've reached your limit..." | Delete your existing app or ask the uberadmin to raise your limit. The limit is checked before the image and env checks, so a new app at the limit gets this 403 even if the spec has other problems. |
| `422 ... references a database, but this app did not request one` | Add `resources: { db: true }` (or `storage`). |
| `422 invalid reference in value` | Use exactly one `${{ db.x }}` / `${{ storage.x }}` from the list in section 5, as the whole value. |
| `422 missing values for secrets` | Provide `--secret NAME=value` / the `secrets` body. |
| `422 invalid env key` | Keys are uppercase letters, digits and underscores, not starting with a digit. |
| 502/503 from your hostname | App not ready yet, wrong `port`, or not listening on all interfaces (bind `0.0.0.0`). |
| Stuck in `deploying` | Readiness not passing: check the health path, port, and that the app starts within a few seconds. |
| `error` with `status_detail` | Read it. Note: apps in `error` are not retried automatically (open issue #8); change something (for example re-apply the spec with a small env change) or ask the uberadmin. The detail text can be terse (issue #9). |
| TLS error from MySQL | Use the CA from `${{ db.ca_cert }}` and the strict-flag note above. |
| DB writes fail with a read-only/permission error | You hit the 50 MB cap; delete rows. |
| SMTP connection times out | SMTP is blocked by design. |
| `stored secrets unreadable` | The platform key changed; re-enter your secrets or rotate credentials. |

## 14. Test apps

Public images built from `testapps/` that you can deploy to try the platform.

hello (smallest possible deploy):

```yaml
apiVersion: picopaas/v1
name: hello
image: ghcr.io/seaburr/picopaas-testapp-hello:latest
port: 8080
env:
  APP_VERSION: "1.0.0"
```

webpage (static page, health check on):

```yaml
apiVersion: picopaas/v1
name: webpage
image: ghcr.io/seaburr/picopaas-testapp-webpage:latest
port: 8080
healthCheck: { enabled: true, path: /healthz }
env:
  APP_VERSION: "1.0.0"
```

probe (exercises the database and Spaces; endpoints `/db`, `/s3`, `/db/fill?mb=N`, `/s3/fill?mb=N`):

```yaml
apiVersion: picopaas/v1
name: probe
image: ghcr.io/seaburr/picopaas-testapp-probe:latest
port: 8080
healthCheck: { enabled: true, path: /healthz }
resources: { db: true, storage: true }
env:
  DB_HOST: ${{ db.host }}
  DB_PORT: ${{ db.port }}
  DB_NAME: ${{ db.name }}
  DB_USER: ${{ db.user }}
  DB_PASSWORD: ${{ db.password }}
  DB_CA_CERT: ${{ db.ca_cert }}
  S3_ENDPOINT: ${{ storage.endpoint }}
  S3_REGION: ${{ storage.region }}
  S3_BUCKET: ${{ storage.bucket }}
  S3_ACCESS_KEY: ${{ storage.access_key }}
  S3_SECRET_KEY: ${{ storage.secret_key }}
```

abacusaas (a real third-party app; listens on 8080 and serves `/abacus`, the root path is a 404):

```yaml
apiVersion: picopaas/v1
name: abacusaas
image: ghcr.io/seaburr/abacusaas:latest
port: 8080
```

Remember the default limit is one app per account, so delete one test app before deploying the next.
