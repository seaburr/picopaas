# PicoPaaS User Guide

This guide is for app owners. You deploy one Docker image, give it env vars and secrets, and optionally add a MySQL
database and an S3-compatible storage bucket. PicoPaaS serves your app over HTTPS at a generated hostname such as
`clever-robin-a1b2c3d.raisehellpraisedale.xyz`.

Platform URL: `https://platform.raisehellpraisedale.xyz` (the CLI default).

## 1. Getting an account

1. Open the platform URL and click **Request access**. Enter an email and a password (8 to 256 characters), then
   accept the Privacy Policy and Acceptable Use. This is a dev/test platform. Through the API, send `accept_terms: true`.
2. New accounts start as `pending`. You can log in, but the API refuses app and key calls with `403` ("Your account is
   waiting for administrator approval...") until an administrator approves you. The UI shows a waiting page and
   checks every 20 seconds, so you land on your apps as soon as you're approved.
3. Once approved (`active`) you can create apps and API keys. The first account registered on a fresh platform becomes
   the administrator (role `uberadmin`) and is active immediately.

If you request access with an email that already has an account, you see the same response as a new request, so
nobody can use the form to check who has an account. Log in instead.

**Forgot your password?** Ask the administrator to reset it. They give you a temporary password. Log in with it and
change it right away on the **Account** page.

A suspended account gets `403` ("Your account is suspended..."), and its apps are scaled to zero until the
administrator restores the account.

## 2. Platform rules

| Rule | Value |
|---|---|
| User | The image must not run as root. `root`, `0` or an empty `USER` is rejected with 422. A named non-root user (for example `USER app`) is run as UID 10001, so files the image owns as that user may not be writable. Baking in a numeric `USER` (for example `USER 10001`) avoids that. |
| Image size | Summed compressed layers at most **256 MB** (checked against the registry manifest on create and update). |
| Image visibility | Public images only. A missing or private image gives `422` ("Couldn't find that image, or it's private..."). |
| CPU / memory | Each app gets a limit of **100m CPU / 384Mi** (request 64m / 128Mi). One replica. |
| Scratch disk | **250 MB** of local disk per app (container filesystem, `/tmp`, logs). Over it, the pod is evicted and restarted. Store files in the storage bucket. |
| Port | Your app must listen on the `port` in your spec, on all interfaces. The platform also sets env `PORT=<port>` unless you define `PORT` yourself in `env`. |
| Apps per user | **1** by default. An administrator can raise it. Over the limit gives `403` ("You've reached your limit of N apps..."). Administrators are exempt. |
| Database | Optional MySQL database, 50 MB. |
| Storage | Optional storage bucket, 100 MB. |
| Spec size | A YAML spec is at most 64 KB. |
| Hardening | Pods run with seccomp `RuntimeDefault`, no privilege escalation, all capabilities dropped. There is no persistent disk; use the database or bucket. |

Example Dockerfile:

```dockerfile
FROM python:3.12-slim
USER 10001:10001
CMD ["python", "-m", "http.server", "8080"]
```

## 3. Deploying

You can deploy four ways. All of them save the same app record, and the reconciler (it runs every 15 seconds)
makes the cluster match it.

### 3a. UI form

1. Open **Apps**, then **New app**, and stay on the **Form** tab.
2. Enter a name, image and container port.
3. Tick **Database** and **Storage** if you need them. You can't change these after you create the app.
4. Add env vars. Tick **secret** on any value you want encrypted.
5. Optional: open **Advanced: health check** and turn it on.
6. Click **Create app**.

Editing an app (image, port, env, health check) redeploys it. The app page has **Rotate database password** and
**Rotate storage keys** buttons under **Credentials**.

### 3b. YAML in the UI

Open **Apps**, then **New app**, then the **YAML (advanced)** tab. Paste a spec (see the reference in section 4) and
enter secret values under **Secret values**. Applying a name you already use updates that app.

### 3c. API with curl

Create an API key under **API keys** in the UI. The key starts with `pp_sk_` and is shown once.

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
`created: true|false` (it is `200` whether it creates or updates).

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

# Update (any subset of name, image, port, env, health_check_enabled, health_check_path, auto_update)
curl -sS -X PATCH "$PP/apps/1" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"env": [{"key": "APP_VERSION", "value": "1.0.1", "is_secret": false}]}'

curl -sS "$PP/apps"      -H "Authorization: Bearer $TOKEN"   # list yours
curl -sS -X DELETE "$PP/apps/1" -H "Authorization: Bearer $TOKEN"
```

About `PATCH`: `env`, if present, **replaces the whole list**. To keep an existing secret unchanged, send it
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

`apply` prints what happened and a next step, for example:

```
Created my-app (queued). It'll be live in about a minute at https://<hostname>
Check progress: picopaas status my-app
```

On update, a secret without `--secret` keeps its stored value. To free an over-cap bucket, run
`picopaas storage-delete my-app --all` (see section 10).

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
| `resources.storage` | boolean | no | `false` | Provisions a storage bucket. Same removal rule. |
| `healthCheck.enabled` | boolean | no | `false` | Turns on HTTP readiness and liveness probes. |
| `healthCheck.path` | string | no | `/healthz` | Starts with `/`, at most 255 characters. Allowed: letters, digits and `._~!$&()*+,;=:@/%-`. No `..` segments (also `%2e%2e`), spaces or quotes. |
| `env` | map string to string | no | `{}` | Keys match `^[A-Z_][A-Z0-9_]*$`. Values must be strings (quote numbers and booleans: `"1.0"`, `"true"`). |
| `secrets` | list of strings | no | `[]` | Names only; values never go in the file. No duplicates, and a name may not also be in `env`. Names follow the same key rule as `env`. |

Types are strict: `port: "8080"` (a string) or `db: "yes"` is rejected.

### Secrets on create and update

- Create: every name in `secrets` needs a value (`secrets` in the API body, `--secret NAME=value` in the CLI). Otherwise you get
  `422` ("Missing values for these secrets: ...").
- Update: a secret you do not resupply keeps its stored value. A new secret name needs a value.
- Update is declarative: the app's `image`, `port`, `env`, `secrets` and `healthCheck` become exactly what the
  spec says. An env var or secret missing from the spec is removed, and a spec without `healthCheck` turns the
  health check off.
- Secret values are encrypted at rest and shown as `********` in API responses.

## 5. Env vars, secrets and `${{ }}` references

Your env vars (plain or secret) reach your container as environment variables. Changing them redeploys (restarts)
the app.

A value that is **exactly** `${{ ref }}` (the whole value, optional spaces inside the braces) is replaced at deploy
time. You can't put a reference inside a longer string. An unknown or embedded reference gives `422` ("A value
containing '${{' must be exactly one reference and nothing else. Allowed: ..."). A `db.*` reference needs
`resources.db: true` (API: `want_db`), and a `storage.*` reference needs `resources.storage: true` (`want_storage`).
Otherwise you get `422` ("${{ db.url }} needs a database, but this app doesn't have one...").

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

The database server requires TLS, so map the CA certificate and the TLS flag:

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

mysql CLI (save the PEM to `ca.pem`). The database is only reachable from inside the cluster, so run this from your app's container, not your laptop:

```bash
mysql --ssl-mode=VERIFY_CA --ssl-ca=ca.pem -h HOST -P PORT -u USER -p DBNAME
```

## 7. Connecting to the storage bucket (S3)

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

The key works for your bucket only. `storage.endpoint` already includes `https://`.

## 8. Health checks

Health checks are off by default. Without one, the app counts as ready once your port accepts a TCP connection, and there is no liveness probe.

With `healthCheck.enabled: true`, the platform sends an HTTP GET to `path` on your port. A 2xx or 3xx response is healthy.

| Probe | Initial delay | Period | Timeout | Failures before action |
|---|---|---|---|---|
| Readiness | 5 s | 10 s | 3 s | 3 (pod removed from traffic) |
| Liveness | 30 s | 20 s | 5 s | 3 (container restarted) |

Changing the health check redeploys the app. Make sure the path returns 2xx or 3xx, or the app never becomes ready.

### Floating tags (`:latest`)

When you save an app, PicoPaaS looks up the exact image behind your tag and runs that version (its digest, shown on
the app page). Pushing a new `:latest` therefore doesn't change a running app by surprise. Every 10 minutes the
platform re-checks the tag. A new image that is under the size limit and runs as non-root is rolled out
automatically. A new image that fails those checks is ignored, your previous version keeps running, and the app page
shows why. To check right away, click **Check for new image** on the app page. **Restart** also re-checks the tag
first.

To pin an exact version, use a digest reference (`ghcr.io/me/app@sha256:...`) or an immutable tag, and untick
**Redeploy automatically when this tag changes** (`auto_update: false` in the API).

## 9. Credential rotation

Rotate the database password or the storage keys from the **Credentials** section of the app page, with
`picopaas rotate NAME --db` (or `--storage`), or with `POST /apps/{id}/rotate-credentials` and
`{"target": "db"}` or `{"target": "storage"}`. The old credentials stop working, and the app restarts with the new ones,
so expect a short interruption.

Rotation can fail in two ways:

- `409` ("Your database is still being created..." or the storage equivalent): the database or bucket isn't ready yet,
  or the app never had one. Try again in a minute.
- `502` ("Couldn't rotate the credentials, so the old ones still work..."): the provider call failed. Your old
  credentials are kept.

Rotation also repairs an app that shows "Your saved secrets can't be read".

## 10. Quotas

Database size is checked every minute and storage every 5 minutes, so a limit is enforced with some delay (a fast writer can
overshoot a little before the next check). Both are shown on the app page (**Usage**), in `picopaas status <app>`, and in
`GET /apps/{id}` under `quota`; an app over a cap gets an **over cap** badge in the list.

| Resource | Limit | At the limit |
|---|---|---|
| Database | 50 MB | Writes that add data are blocked (INSERT, UPDATE, CREATE and ALTER are revoked) and your open database connections are closed, so your app reconnects read-only. SELECT, DELETE, TRUNCATE and DROP keep working so you can clean up. Writes come back automatically within about a minute of usage dropping under 50 MB. See "Recovering from an over-cap resource" below. |
| Bucket | 100 MB | The key is swapped for a read-only one and the app restarts with it, so reads keep working but writes fail and **your app cannot delete objects itself**. Free space through the platform (see "Recovering from an over-cap resource" below). The read-write key comes back right away once you are under 100 MB. |
| Image | 256 MB | Deploy is rejected. |
| Scratch disk | 250 MB | The pod is evicted and restarted. The app shows **Failed** with reason `disk` and is retried automatically. Files on the container filesystem or in `/tmp` are lost on restart. |

### Recovering from an over-cap resource

- **Database:** free space with `TRUNCATE TABLE t` or `DROP TABLE t` (or `DELETE`, but InnoDB keeps the freed pages, so the
  reported size may not shrink until you `TRUNCATE` or `DROP` or run `OPTIMIZE TABLE`). Within about a minute the platform
  re-measures and restores writes. You don't need to click anything. `GET /apps/{id}/database/usage` shows the live size.
- **Storage:** your app only holds a read-only key while over the cap, so delete through the platform instead: on the app
  page click **Free up space** (delete everything, or everything under a prefix), run
  `picopaas storage-delete <app> --prefix uploads/old/` (or `--all`), or call `POST /apps/{id}/storage/delete` with
  `{"prefix": "..."}` or `{"all": true}`. Each call deletes up to 5000 objects. Repeat it if the response says `truncated`. As soon
  as the bucket is back under 100 MB the read-write key is restored and the app restarts to pick it up.
  `GET /apps/{id}/storage/usage` shows the live size, object count and the biggest prefixes.

## 11. Networking and egress

- Inbound: only HTTPS through the platform ingress to your `port`. Apps cannot talk to each other.
- DNS: allowed.
- Internet: allowed on all TCP ports **except SMTP (25, 465, 587)**, plus all UDP. To send mail, call a mail provider's
  HTTPS API instead of using SMTP.
- Private ranges are blocked: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16` (this includes
  the cloud metadata address and other cluster services).
- Exceptions: your own database server (its host and port) and your own bucket's address on port 443.

## 12. App statuses

The UI and CLI show friendly names. The API uses the raw value in the second column.

| Status | API value | Meaning |
|---|---|---|
| Queued | `pending` | Saved, waiting for the reconciler (it runs every 15 s). Also set after any change to image, port, env or health check, and after a credential rotation. |
| Deploying | `deploying` | Applied to the cluster. The new pod isn't ready yet, or a rollout is still in progress. |
| Running | `running` | At least one ready pod is serving the current version. |
| Failed | `error` | Something went wrong. Read the message on the app page (`status_detail` in the API). Typical causes: image pull failures, crash loops, a port that doesn't answer, no free database capacity. The platform retries on its own after 1, 5, 15, then every 60 minutes. The message says when the next retry happens, or that it won't retry until you change something. Saving the app or clicking **Restart** retries right away. |
| Suspended | `suspended` | Stopped (scaled to zero) by an administrator, or because your account was suspended. You can't edit, rotate or delete it. |
| Deleted | `deleted` | Torn down. Deleted apps normally disappear from your list. |

Deleting an app removes its namespace, database and storage bucket.

### Viewing logs

The app page shows the last 200 lines of your container's output. Click **Refresh logs**, or tick **Auto-refresh**.
Through the API, call `GET /apps/{id}/logs?tail=N` (default 200, at most 1000). Logs are only available while a pod exists.

## 13. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `403` "Your account is waiting for administrator approval" | Wait for an administrator to approve you. |
| `403` "Your account is suspended..." | Contact the administrator. |
| `422` "Image runs as root, which isn't allowed..." | Add a non-root `USER` to your Dockerfile, preferably numeric such as `USER 10001`. Rebuild, push and save the app again. |
| `422` "Couldn't find that image, or it's private..." | The name or tag is wrong, or the image is private. Check the name and make the image public. |
| `422` "Image is N MB; the limit is 256 MB..." | Slim the image down: use a `-slim` or alpine base, or a multi-stage build. |
| `403` "You've reached your limit of N apps..." | Delete an existing app or ask an administrator to raise your limit. The limit is checked before the image and env checks, so a new app at the limit gets this `403` even if the spec has other problems. |
| `422` "... needs a database, but this app doesn't have one" | Add `resources: { db: true }` (or `storage: true`), or remove the reference. |
| `422` "A value containing '${{' must be exactly one reference..." | Use one `${{ db.x }}` or `${{ storage.x }}` from the table in section 5, as the whole value. |
| `422` "Missing values for these secrets" | Pass `--secret NAME=value` (CLI), the `secrets` object (API), or fill in **Secret values** (UI). |
| `422` "... isn't a valid env var name" | Env names use capital letters, digits and underscores, and don't start with a digit. |
| 502 or 503 from your hostname | The app isn't ready yet, the `port` is wrong, or the app isn't listening on all interfaces. Bind to `0.0.0.0`. |
| Stuck in **Deploying** | Readiness isn't passing. Check the health check path, the port, and that the app starts within a few seconds. After 10 minutes the app moves to **Failed** with the reason. |
| **Failed** with a message | Read the message and the logs on the app page. The platform retries automatically (see section 12). To retry now, click **Restart** or save the app. |
| TLS error from MySQL | Use the CA certificate from `${{ db.ca_cert }}`. See the strict-flag note in section 6. |
| Database writes fail with a read-only or permission error | You hit the 50 MB cap. The app page shows it. Run `TRUNCATE TABLE` or `DROP TABLE` to free space. Writes return within a minute. |
| Bucket writes fail (`AccessDenied`) and the app page says the bucket is over its cap | Click **Free up space** (or run `picopaas storage-delete`). Your app's own key is read-only and can't delete. |
| The app restarts with "used more than 250 MB of local disk" | The app wrote too much to the container filesystem, `/tmp` or logs. Store files in the storage bucket and log less. |
| SMTP connection times out | SMTP is blocked on purpose. Use a mail provider's HTTPS API. |
| "Your saved secrets can't be read" | The platform's encryption key changed. Re-enter your secret values and save, or rotate the credentials. |

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

probe (exercises the database and storage bucket; endpoints include `/db`, `/s3`, `/db/fill?mb=N`, `/s3/fill?mb=N` and `/disk/fill`):

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

The default limit is one app per account, so delete a test app before you deploy the next one.
