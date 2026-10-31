# PicoPaaS User Guide

This guide is for app owners. You deploy one Docker image, give it env vars and secrets, and optionally add a MySQL or PostgreSQL
database and an S3-compatible blob storage bucket. PicoPaaS serves your app over HTTPS at a generated hostname such as
`clever-robin-a1b2c3d.picopaas.xyz`.

Platform URL: `https://platform.picopaas.xyz` (the CLI default).

## 1. Getting an account

1. Open the platform URL and click **Request access**. Enter an email and a password (8 to 256 characters), then
   accept the Privacy Policy and Acceptable Use. This is a dev/test platform. Through the API, send `accept_terms: true`.
2. New accounts start as `pending`. You can log in, but the API refuses app and key calls with `403` ("Your account is
   waiting for administrator approval...") until an administrator approves you. The UI shows a waiting page and
   checks every 20 seconds, so you land on your apps as soon as you're approved.
3. Once approved (`active`) you can create apps and API keys.

If you request access with an email that already has an account, you see the same response as a new request, so
nobody can use the form to check who has an account. Log in instead.

**Forgot your password?** Ask the administrator to reset it. They give you a temporary password. Log in with it and
change it right away on the **Account** page.

A suspended account gets `403` ("Your account is suspended..."), and its apps are scaled to zero until the
administrator restores the account.

**Timezone.** On the **Account** page you can pick your timezone so times show in local time; leave it unset to use your browser's. Server-written text (audit CSV, notices) stays in UTC.

## 2. Platform rules

| Rule | Value |
|---|---|
| User | The image must not run as root. `root`, `0` or an empty `USER` is rejected with 422. A named non-root user (for example `USER app`) is run as UID 10001, so files the image owns as that user may not be writable. Baking in a numeric `USER` (for example `USER 10001`) avoids that. |
| Image size | Summed compressed layers at most **512 MB** (checked against the registry manifest on create and update). |
| Image visibility | Public images only. A missing or private image gives `422` ("Couldn't find that image, or it's private..."). |
| CPU / memory | Each app gets a limit of **200m CPU / 384Mi** (request 64m / 128Mi) and runs one replica. Only a platform administrator can raise CPU, memory, replicas (1 to 3), scratch disk, outbound bandwidth, database connections or quotas for a specific app; ask them if you need more. |
| Outbound bandwidth | **20 Mbit/s** (about 2.5 MB/s) per app, enforced on the pod. It applies to everything your app sends, including HTTP responses to visitors, so a download-heavy app is throttled. Inbound traffic is not capped. A platform administrator can raise it per app (up to 160 Mbit/s). |
| Scratch disk | **250 MB** of local disk per app (container filesystem, `/tmp`, logs). Over it, the pod is evicted and restarted. Store files in the blob storage bucket. |
| Port | Your app must listen on the `port` in your spec, on all interfaces. The platform also sets env `PORT=<port>` unless you define `PORT` yourself in `env`. |
| Apps per user | **1** by default. An administrator can raise it, but never above the platform-wide hard max of **5** apps per account (`MAX_APPS_PER_USER`). Platform administrators have their own limits (at least 5, up to 10). `app_limit` is the per-user setting, default 1; the hard max is the ceiling for it. Over the limit gives `403` ("You've reached your limit of N apps..."). |
| Database | Optional database, your choice of MySQL or PostgreSQL (see section 5a), 50 MB. |
| Blob storage | Optional blob storage bucket (S3-compatible), 100 MB. |
| Spec size | A YAML spec is at most 64 KB. |
| Hardening | Pods run with seccomp `RuntimeDefault`, no privilege escalation, all capabilities dropped. Without a persistent volume (section 7a) there is no persistent disk; use the database or blob storage. |

Example Dockerfile:

```dockerfile
FROM python:3.12-slim
USER 10001:10001
CMD ["python", "-m", "http.server", "8080"]
```

## 3. Deploying

You can deploy four ways. All of them save the same app record; the platform then rolls it out, usually within a minute.

### 3a. UI form

1. Open **Apps**, then **New app**, and stay on the **Form** tab.
2. Enter a name, image and container port.
3. Tick **Database** and **Blob storage** if you need them. If you tick **Database**, pick the engine (MySQL or PostgreSQL; see section 5a). You can't change these, or the engine, after you create the app.
4. Add env vars. Tick **secret** on any value you want encrypted.
5. Optional: open **Advanced: health check**, turn it on, and set the path and (optionally) a timeout.
6. Click **Create app**.

Each app has two pages. **App health** (`/apps/:id`) shows status, resource usage, quotas, logs, recent events and
versions. **Settings** (`/apps/:id/edit`) holds the deployment form (image, port, env, health check) plus **Custom domain**,
the **Danger zone** (rotate credentials, delete) and, for administrators, **Short address**. A button on each page links to
the other. Saving the deployment form redeploys the app.

### 3b. YAML in the UI

Open **Apps**, then **New app**, then the **YAML (advanced)** tab. Paste a spec (see the reference in section 4) and
enter secret values under **Secret values**. Applying a name you already use updates that app.

### 3c. API with curl

Create an API key under **Account → API keys** in the UI. The key starts with `pp_sk_` and is shown once.

```bash
export PP=https://platform.picopaas.xyz
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
  "name": "my-app",
  "image": "ghcr.io/me/app:1.2",
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
export PICOPAAS_TOKEN=pp_sk_...     # PICOPAAS_URL defaults to https://platform.picopaas.xyz
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
| `image` | string | yes | | 1 to 512 chars. Public, non-root `USER`, at most 512 MB. |
| `port` | integer | **yes** | none | 1 to 65535. There is no default. |
| `resources.db` | boolean | no | `false` | Provisions a database (engine chosen by `resources.db_engine`). Cannot be removed from an existing app via spec (422). |
| `resources.db_engine` | string | no | see note | `mysql` or `pg` (PostgreSQL). Chooses the engine for `db: true`. Immutable once the app has a database. The `${{ db.* }}` refs work the same either way. Specs written before PostgreSQL existed don't set it, so when it is omitted the platform provisions MySQL (backward compatibility); set it explicitly in new specs. |
| `resources.storage` | boolean | no | `false` | Provisions a blob storage bucket. Same removal rule. |
| `resources.volume` | map | no | none | Persistent volume (only where the platform has enabled them): `sizeGi` (integer, default 1, at most the platform limit) and `mountPath` (default `/data`). Cannot be removed once added. See section 7a. |
| `healthCheck.enabled` | boolean | no | `false` | Turns on HTTP readiness and liveness probes. |
| `healthCheck.timeoutSeconds` | integer | no | blank | 1 to 90. Probe timeout for both probes; blank uses the platform values (see section 8). `periodSeconds` is raised to at least the timeout. |
| `healthCheck.path` | string | no | `/healthz` | Starts with `/`, at most 255 characters. Allowed: letters, digits and `._~!$&()*+,;=:@/%-`. No `..` segments (also `%2e%2e`), spaces or quotes. |
| `env` | map string to string | no | `{}` | Keys match `^[A-Z_][A-Z0-9_]*$`. Values must be strings (quote numbers and booleans: `"1.0"`, `"true"`). |
| `secrets` | list of strings | no | `[]` | Names only; values never go in the file. No duplicates, and a name may not also be in `env`. Names follow the same key rule as `env`. |

Types are strict: `port: "8080"` (a string) or `db: "yes"` is rejected.

`project` (optional string): the slug of a project you belong to, see section 15. Default: your personal project.

### Secrets on create and update

- Create: every name in `secrets` needs a value (`secrets` in the API body, `--secret NAME=value` in the CLI). Otherwise you get
  `422` ("Missing values for these secrets: ...").
- Update: a secret you do not resupply keeps its stored value. A new secret name needs a value.
- Update is declarative: the app's `image`, `port`, `env`, `secrets` and `healthCheck` become exactly what the
  spec says. An env var or secret missing from the spec is removed, and a spec without `healthCheck` turns the
  health check off.
- Secret values are encrypted at rest and shown as `********` in API responses.

## 5. Env vars and references

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
| `${{ db.url }}` | `mysql://user:password@host:port/name` for MySQL, or `postgres://user:password@host:port/name` (with `?sslmode=require` when the server has TLS) for PostgreSQL |
| `${{ db.ca_cert }}` | CA certificate PEM text; empty string if the server has none |
| `${{ db.tls }}` | `required` if the server has a CA cert, else `off` |
| `${{ storage.endpoint }}` | Full URL, for example `https://atl1.digitaloceanspaces.com` |
| `${{ storage.region }}` | For example `atl1` |
| `${{ storage.bucket }}` | Your bucket name |
| `${{ storage.access_key }}` | Bucket access key |
| `${{ storage.secret_key }}` | Bucket secret key |
| `${{ app.url }}` | Your app's own public URL — a custom domain or reserved subdomain if one is set, otherwise the generated host like `https://myapp-ab12cd.example.com` (always available, no database or bucket needed) |

You pick your own variable names. The platform does not inject `DATABASE_URL` and friends by itself.

## 5a. Choosing a DB engine

MySQL and PostgreSQL are equal choices on PicoPaaS: pick whichever your app supports best. Set `resources.db_engine`
(`mysql` or `pg`) in the spec or choose it in the form. The engine is fixed after the database is created; to switch,
create a new app and move your data. If a spec omits `db_engine`, the platform provisions MySQL (kept for older specs).

| | MySQL | PostgreSQL |
|---|---|---|
| Spec value | `mysql` | `pg` |
| Connections per app | **10** (`MAX_USER_CONNECTIONS`) | **5** (role `CONNECTION LIMIT`) |
| Over the 50 MB quota | Write privileges are revoked and sessions closed; reads, `DELETE`, `TRUNCATE`, `DROP` keep working. Writes return within about a minute of dropping under the cap. | Stage 1 at 50 MB: the role goes read-only and sessions are closed (your app can still `DELETE`, `TRUNCATE`, `DROP`). Stage 2 at 2x the cap (100 MB): `CONNECT` is revoked until the size is back under the cap. |
| TLS | Required, CA-verified (`${{ db.ca_cert }}`) | Required, CA-verified (`sslmode=verify-full`) |
| Extensions | Not applicable | Trusted extensions only (for example `pgcrypto`, `citext`, `pg_trgm`); extensions that need superuser or file access are blocked |
| Primary keys | Not required: the platform's managed MySQL runs with `sql_require_primary_key=OFF` (request 0009), so migrations that create keyless tables work (Ghost 6 needs this) | No such requirement |
| Other limits | none | Statement and idle-in-transaction timeouts of 60 s |
| Recipes | Ghost, Grafana, Shlink, Directus | n8n |

Both engines run on managed servers in the same private network; the `${{ db.* }}` refs are identical. Check your app's
documentation for which engines it supports, and keep its connection pool at or below the cap for the engine you pick.

## 6. Database connections

The database server requires TLS, so map the CA certificate and the TLS flag. The refs are the same for both engines; pick the client block for your engine (`resources.db_engine`):

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

**MySQL.** Python (pymysql). Python 3.13 enables `VERIFY_X509_STRICT`, which rejects DigitalOcean's CA ("Basic Constraints
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

**PostgreSQL** (`resources.db_engine: pg`). Same `env:` mapping as above. The server listens on port 25060 and `${{ db.url }}` takes the form `postgres://user:password@host:port/name` (with `?sslmode=require` when the server has TLS). To verify the server certificate, write the CA out and use `sslmode=verify-full`. Python (psycopg v3):

```python
import os, psycopg
open("/tmp/db-ca.pem", "w").write(os.environ["DB_CA_CERT"])
conn = psycopg.connect(host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]),
                       user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
                       dbname=os.environ["DB_NAME"],
                       sslmode="verify-full", sslrootcert="/tmp/db-ca.pem")
```

**Connection limit (PostgreSQL).** Your app's database role is capped at 5 simultaneous connections (`CONNECTION LIMIT 5`). Keep your pool's maximum size at or below this.

**Connection limit (MySQL).** Your database user is capped at 10 simultaneous connections (platform setting `MAX_DB_USER_CONNECTIONS`). If your app uses a connection pool, keep its maximum size at or below this, otherwise MySQL returns `exceeded the 'max_user_connections' resource`. For example, set Knex/Ghost `pool.max` to 10 or less.

## 7. Blob storage (S3)

PicoPaaS has two ways to keep files. Pick by how your app reads them:

| | Blob storage | Persistent volume |
|---|---|---|
| What it is | S3-compatible bucket, reached over the network API | Disk mounted into the container (section 7a) |
| Survives restarts | Yes | Yes |
| Replicas | Any | Single replica only |
| Default size | 100 MB | 1 GiB |

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

### Uploading files to your bucket

You never need the bucket keys to move files in or out from your own machine. With an API key (a read-only key can download only):

```bash
picopaas storage upload myapp ./site.tar.gz --key restore/site.tar.gz
picopaas storage download myapp restore/site.tar.gz -o site.tar.gz
```

The CLI asks PicoPaaS for a short-lived link (10 minutes) for that one object and streams the file directly to blob storage. An upload is
refused if it would not fit in the bucket's remaining quota, or while the bucket is over its cap (see Recovering from over-cap).

## 7a. Persistent volume

Normally an app's local disk is scratch space and is wiped on every restart. A persistent volume is a small disk that
survives restarts, deploys and node moves, for apps that keep files on disk (SQLite databases, uploads, plugins, a CMS).
It is optional and only available when your administrator has enabled it; if you don't see the option, it is off.

```yaml
resources: { volume: { sizeGi: 1, mountPath: /data } }
```

In the UI and API the same thing is `volume: {size_gi, mount_path}`. Rules to know before you use one:
- **One copy only.** A volume attaches to one pod, so the app always runs a single replica.
- **Short downtime on every deploy.** Updates stop the old pod before starting the new one (the volume can't be shared),
  so expect a gap of up to about a minute on each deploy, restart and when the platform moves your app to another machine.
- **Your app writes to the mount path.** The volume is writable by the non-root user your image runs as; put the data
  under `mountPath` (default `/data`). Don't mount over directories your image needs to start, unless it copies them in.
- **Size.** 1 GiB by default; an administrator can grow it. Volumes only grow, never shrink. Growing a volume after it's created is supported by the API (DigitalOcean's CSI driver expands the filesystem online) but is not yet tested live on this platform, so plan the size up front.
- **No detaching.** To drop a volume, delete the app; the data is destroyed with it.
- **Backed up when your administrator allows it.** A volume is part of your backups once the platform has volume backups turned on (they need a bucket-scoped backup key; see Backups, section 7b). If the Backups card says "Volume backups need a bucket-scoped backup key; ask the admin", the volume is not backed up yet: treat it as one copy of the data.

## 7b. Backups

When your administrator has turned backups on, you can back up an app with a **database**, **blob storage** or a **persistent volume**. One backup is one **package**: a "Back up now" click or one scheduled slot produces a single backup, made of one part per thing your app has (database, blob storage, volume). The parts are taken one after another, not at exactly the same instant, so a busy app can differ slightly between them. You see one row per backup, download one file, and delete it as one. Scheduled backups are
**opt-in**: turn them on for an app and it is backed up once a day (a bit after 03:00 UTC, staggered per app); until you do, nothing runs
by itself, but you can start a backup by hand at any time. Each backup is encrypted and stored outside the cluster. PicoPaaS keeps the latest daily backup plus one
weekly and one monthly copy. A backup you start by hand replaces that day's daily copy. Because only those few copies are kept,
**download** anything you need for the long term (see below). What is covered:
- **Database: yes.** A full dump of your app's database (MySQL or PostgreSQL).
- **Persistent volume: yes, when enabled.** Every file on the volume is copied (read-only, while your app keeps running) and stored as the `volume` part. The files are read as your app's user, so files that user cannot read make the volume part fail ("some files on the volume could not be read"); the other parts still succeed and the backup shows as **partial** with the error. It is a file-level copy, not an atomic snapshot: an app that keeps a database file on the volume can be caught mid-write. Restoring a volume is not self-service yet (ask an administrator; restore is planned).
- **Blob storage: yes.** Every object in your bucket is copied and stored the same way (as its own backup, shown as `bucket`
  in the status). A bucket larger than 512 MB is skipped with an error. Because the backup reads your bucket with your app's own key,
  it sees exactly what your app sees.
- DigitalOcean also backs up the database servers themselves; that is the platform's safety net, not something you restore from.

On the app's health page, the **Backups** card shows the schedule status (daily, weekly or off, and the next run), a **Back up now** button, a "Last backup" line, and one row per backup: its date, an **automatic**, **manual** or **before delete** label, and a status: **running**, **succeeded**, **partial** (some parts failed; the row names the failed part and its error) or **failed**. Small chips under it list the parts (database, blob storage, volume) with the new data each added. If your app has a volume but volume backups are off, the card says so. Click the download icon on a finished backup (automatic backups too); you can leave the page while it prepares, then come back and click it again within an hour (3 downloads per hour, counted per backup). The trash icon deletes the whole backup for good: it is marked "deleting" for a minute or two, then disappears (or comes back with an event if part of the delete failed). You can't delete or download a backup while it is still being taken or while another backup, restore or download of the same kind is running, and a download you already prepared from it is removed too. Deleting is also `DELETE /apps/{id}/backups/sets/{id}`; the older per-part `DELETE /apps/{id}/backups/{run_id}` still works. Turn scheduled backups on or off and choose daily or weekly under **Settings**, in the **Backups** section of "Other settings" (the **Backup settings** button on the card jumps there); changes save immediately.

**Turning backups on or off, and limits.** You can turn scheduled backups on or off for your app at any time (Settings, or `PATCH /apps/{id}/backups`). New apps start with them on or off depending on a platform setting. A platform admin can also **disable backups for an app**: then no scheduled or manual backup runs and you cannot turn them back on ("Backups for this app were disabled by a platform admin"); the reason is shown, and your existing backups are kept and stay downloadable. The platform keeps at most a set number of backups per app (default 14, manual ones included) and has a storage ceiling per app: when you reach either, the oldest backups roll off, failed or partial backups first, and the newest good backup is never removed to make room. If the app's storage is still full, a new backup is refused with "Backup storage for this app is full (X of Y)" and a scheduled one is skipped and listed as failed with that reason; delete a backup you no longer need. You can start one backup per app per hour ("You can start 1 backup per hour"), and prepare 3 backup downloads per hour ("You can prepare 3 backup downloads per hour"); a download that failed does not count, and the reason it failed is shown on the Backups card.

Check the status with `GET /apps/{id}/backups` (last backup, size, recent runs). Start one now with `POST /apps/{id}/backups`
(one per hour; it works whether or not scheduled backups are on). Turn scheduled backups on or off, or switch to weekly, with
`PATCH /apps/{id}/backups`. A failed backup adds an event to your app and
shows in the status. Backup storage has its own cap (2 GiB per app) and does not count against your database or storage quota.

```bash
# opt in to daily backups, run one now, list them
curl -X PATCH -H "Authorization: Bearer $PICOPAAS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"enabled": true}' https://platform.picopaas.xyz/apps/ID/backups
curl -X POST -H "Authorization: Bearer $PICOPAAS_TOKEN" https://platform.picopaas.xyz/apps/ID/backups
curl -H "Authorization: Bearer $PICOPAAS_TOKEN" https://platform.picopaas.xyz/apps/ID/backups
# or with the CLI
picopaas backup enable myapp      # picopaas backup disable myapp
picopaas backup run myapp
picopaas backup list myapp
```

**Downloading a backup.** A download is ONE `.tar.gz` package (`<app>-backup<id>.tar.gz`) holding `MANIFEST.json` (app name, backup id, when it was taken,
the database engine, the PicoPaaS version, and each part with its size and sha256) plus the parts that succeeded: `database.sql.gz` (or `database.pgdump` for
PostgreSQL), `blob-storage.tar.gz` and `volume.tar.gz` (the volume's files under a top-level `volume/` folder). `picopaas backup download myapp` saves the newest
finished backup; add a backup id (from `backup list`) for a specific one and `-o file` to name the file. Over the API:
`POST /apps/{id}/backups/sets/{set_id}/download` starts preparing the package, then poll `GET /apps/{id}/backups/exports/{export_id}` until
`status` is `ready` and fetch the `url` (valid 10 minutes; ask again for a fresh one). The prepared file is deleted after an hour. You can
prepare 3 downloads per app per hour. The download is prepared in the background; you can leave and come back within an hour: `GET /apps/{id}/backups/exports` lists your prepared downloads, and each backup in `GET /apps/{id}/backups` (`backups[]`) carries an `export` field ("Ready to download"). A volume bigger than 1 GiB is too big for a package; fetch the other parts. You can still download a single part with `POST /apps/{id}/backups/{run_id}/download` (CLI: `backup download myapp --run RUN_ID`).

When you delete an app the platform takes a final backup **only if scheduled backups are on for it**, and keeps it for 14 days,
then deletes it. If they are off, no final backup is taken, but any backups you made earlier by hand are kept for the same 14 days and
then deleted, so download what you need before deleting. Restoring is done by an
administrator: ask them to restore a backup run into your app (database tables or the whole bucket are replaced with the backup's
contents) or into a fresh copy of it.

## 8. Health checks

Health checks are off by default. With them off, the platform uses a single TCP readiness probe (initial delay 2 s,
period 5 s): the app counts as ready once your port accepts a connection, and there is no liveness probe.

With `healthCheck.enabled: true`, the platform sends an HTTP GET to `path` on your port. A 2xx or 3xx response is healthy.

| Probe | Initial delay | Period | Timeout | Failures before action |
|---|---|---|---|---|
| Readiness | 5 s | 10 s | 3 s | 3 (pod removed from traffic) |
| Liveness | 30 s | 20 s | 5 s | 3 (container restarted) |

**Timeout.** Set `healthCheck.timeoutSeconds` (1 to 90; blank keeps the table above) if your app answers slowly, for
example a slow-starting or CPU-throttled app. The value applies to both probes. If it is larger than a probe's period,
the period is raised to match (period is at least the timeout). The other values (delay, period, failure threshold)
are fixed for now and may become adjustable later. An app that needs longer than the liveness window (30 s plus three
failed 20 s probes) to start should leave the health check off.

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

### Versions and rollback

PicoPaaS keeps your last few image versions (digests) per app. The **Recent versions** list on the app page shows them,
and **Roll back** redeploys an older one (`GET /apps/{id}/image-history`, `POST /apps/{id}/rollback` with the digest).
A rollback stops auto-update from the tag until a newer image is pushed, so the old version isn't replaced at the next
check. The app restarts.

Auto-rollback: if a new image fails to start (the rollout never becomes ready), the platform reverts to the last version
that worked and records why on the app page. It won't retry the rejected digest, so it doesn't loop. Fix the image, push
a new one, and it deploys normally.

## 9. Credential rotation

Rotate the database password or the blob storage keys from the **Danger zone** on the app's **Settings** page, with
`picopaas rotate NAME --db` (or `--storage`), or with `POST /apps/{id}/rotate-credentials` and
`{"target": "db"}` or `{"target": "storage"}`. The old credentials stop working, and the app restarts with the new ones,
so expect a short interruption.

Rotation can fail in two ways:

- `409` ("Your database is still being created..." or the blob storage equivalent): the database or bucket isn't ready yet,
  or the app never had one. Try again in a minute.
- `502` ("Couldn't rotate the credentials, so the old ones still work..."): the provider call failed. Your old
  credentials are kept.

If an app shows "Your saved secrets can't be read", rotating fixes the database and blob storage credentials. Secret env vars
marked "can't be read" are not fixed by rotation: open **Edit**, type their values again and save.

## 10. Quotas

Database size is checked every minute and blob storage every 5 minutes, so a limit is enforced with some delay (a fast writer can
overshoot a little before the next check). Both are shown on the app page (**Usage**), in `picopaas status <app>`, and in
`GET /apps/{id}` under `quota`; an app over a cap gets an **over cap** badge in the list.

| Resource | Limit | At the limit |
|---|---|---|
| Database | 50 MB | Behaviour depends on the engine (section 5a). MySQL: INSERT, UPDATE, CREATE and ALTER are revoked and open connections are closed, so your app reconnects read-only. PostgreSQL: the role is made read-only and sessions are closed; at 2x the cap `CONNECT` is revoked. Either way SELECT, DELETE, TRUNCATE and DROP work so you can clean up, and writes come back automatically within about a minute of usage dropping under 50 MB. See "Recovering from over-cap" below. |
| Bucket | 100 MB | The key is swapped for a read-only one and the app restarts with it, so reads keep working but writes fail and **your app cannot delete objects itself**. Free space through the platform (see "Recovering from over-cap" below). The read-write key comes back right away once you are under 100 MB. |
| Image | 512 MB | Deploy is rejected. |
| Scratch disk | 250 MB | The pod is evicted and restarted. Kubernetes starts a replacement pod within seconds, so the app usually stays **Running**; a "Restarted" notice appears on the app page (see "Recent events"). Files on the container filesystem or in `/tmp` are lost on restart. |

### Recovering from over-cap

Find out which resource is over with the **Usage** section on the app page, `picopaas status <app>` or `GET /apps/{id}` (`quota`).

- **Database:** free space with `TRUNCATE TABLE t` or `DROP TABLE t` (or `DELETE`, but InnoDB on MySQL keeps the freed
  pages, so the reported size may not shrink until you `TRUNCATE`, `DROP` or run `OPTIMIZE TABLE`; on PostgreSQL use
  `VACUUM FULL` or `TRUNCATE`). On PostgreSQL a session your app keeps open may stay read-only; reconnect it. Within about a minute the platform
  re-measures and restores writes. You don't need to click anything. `GET /apps/{id}/database/usage` shows the live size.
- **Blob storage:** your app only holds a read-only key while over the cap, so delete through the platform instead: on the **App
  health** page click **Free up space** (delete everything, or everything under a prefix), run
  `picopaas storage-delete <app> --prefix uploads/old/` (or `--all`), or call `POST /apps/{id}/storage/delete` with
  `{"prefix": "..."}` or `{"all": true}`. Each call deletes up to 5000 objects. Repeat it if the response says `truncated`. As soon
  as the bucket is back under 100 MB the read-write key is restored and the app restarts to pick it up.
  `GET /apps/{id}/storage/usage` shows the live size, object count and the biggest prefixes.
- **Scratch disk:** nothing to clean up by hand. The pod is evicted and restarted, which empties the scratch space. If it
  keeps happening, your app writes too much to the container filesystem or `/tmp`: write to the database, blob storage or a
  persistent volume instead, or delete temp files as you go.
- **Persistent volume:** the platform does not block writes to a full volume. It is an ordinary disk, so once it fills,
  your app's own writes fail with the usual "no space left on device" error. Delete files from inside your app (the platform
  has no file browser). To make it bigger, raise `resources.volume.size_gi` (volumes only grow). Owners can go up to the
  platform's per-app volume limit, which is 1 GiB by default, so on a default instance you must ask an administrator, who
  can grow it to a higher ceiling (10 GiB by default). Growth is untested live, so plan the size up front. Usage shows
  under `quota.volume` once the platform has measured the disk (about every 15 minutes, only where volume backups are on).
- **Backup storage:** backups are metered separately from the caps above (default 2 GiB per app; the last 1 daily, 1
  weekly and 1 monthly restore point per repository are kept). When it is full, new backups are refused and the **Back up now**
  button is disabled; a scheduled backup is skipped and shows as a failed backup with the reason. The platform clears this
  on its own: every minute or so it deletes the app's failed and partial backups first, then the oldest successful ones,
  but never the newest successful one. You can do it yourself by clicking the trash icon on old backups (or
  `DELETE /apps/{id}/backups/sets/{id}`). **Free up space** does not apply here; it only deletes blob storage objects.
  Space is freed when the delete finishes, which takes a minute or two (the backup shows "deleting"); a failed backup
  holds nothing and disappears at once. Backups share unchanged data, so deleting one frees only what no other backup uses. If one backup is simply larger than the ceiling, ask an administrator to raise
  your app's backup ceiling (see the [Admin Guide](ADMIN_GUIDE.md)). If backups are locked by an administrator, existing
  backups can still be downloaded and deleted.

## 11. Networking and egress

- Inbound: only HTTPS through the platform ingress to your `port`. Apps cannot talk to each other.
- DNS: allowed.
- Internet: allowed on all TCP ports **except SMTP (25, 465, 587)**, plus all UDP. To send mail, call a mail provider's
  HTTPS API instead of using SMTP.
- Private ranges are blocked: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16` (this includes
  the cloud metadata address and other cluster services).
- Exceptions: your own database server (its host and port) and your own bucket's address on port 443.

## 11a. Custom domains

Your app is always reachable at its generated `*.picopaas.xyz` hostname. You can also attach your own
domain and we'll issue a free TLS certificate for it automatically.

**Attach, verify and remove custom domains only through the PicoPaaS UI or its REST API** (`PUT`, `POST .../verify`
and `DELETE /apps/{id}/domain`; same effect either way). Don't hand-edit the underlying routing: the platform manages
it and will revert manual changes.

1. On the app's **Settings** page, open **Custom domain**, enter a subdomain you control (e.g. `blog.example.com`) and click
   **Add domain**. Apex domains (bare `example.com`) can't be used unless your DNS provider supports CNAME
   flattening/ALIAS — use a subdomain.
2. Create the **two DNS records** shown:
   - a **CNAME** from your host to the platform load balancer (`lb.picopaas.xyz`), and
   - a **TXT** at `_picopaas-challenge.<your-host>` with the token shown (this proves you own the domain).
3. Click **Verify DNS**. Once both records are visible (DNS can take a few minutes), the domain goes **active**, a
   certificate is issued, and your app is served on it over HTTPS alongside the generated subdomain.

Notes: if your domain has a `CAA` record, make sure it permits `letsencrypt.org` or issuance will fail. Removing the
domain stops serving it and cleans up its certificate; your generated subdomain is unaffected.

## 12. App statuses

The UI and CLI show friendly names. The API uses the raw value in the second column.

| Status | API value | Meaning |
|---|---|---|
| Queued | `pending` | Saved; the platform picks it up within about 15 seconds. Also set after any change to image, port, env or health check, and after a credential rotation. |
| Deploying | `deploying` | Applied to the cluster. The new pod isn't ready yet, or a rollout is still in progress. |
| Running | `running` | At least one ready pod is serving the current version. |
| Failed | `error` | Something went wrong. Read the message on the app page (`status_detail` in the API). Typical causes: image pull failures, crash loops, a port that doesn't answer, no free database capacity. The platform retries on its own after 1, 5, 15, then every 60 minutes. The message says when the next retry happens, or that it won't retry until you change something. Saving the app or clicking **Restart** retries right away. |
| Suspended | `suspended` | Stopped (scaled to zero) by an administrator: the app itself, its project, or your account. You can't edit, rotate or delete it. |
| Deleted | `deleted` | Torn down. Deleted apps normally disappear from your list. |

Deleting an app removes its namespace, database and blob storage bucket. The UI asks you to type the app's name before it deletes.

**App page details.** Each app's URLs (the generated one and any custom domain) are listed, with the primary one marked. The
page shows how long the app has been up (`uptime_seconds` in the API). A restart or image-update notice can be dismissed with
its X button; it stays hidden until a new restart or a different update error occurs (`POST /apps/{id}/alerts/{kind}/dismiss`,
see API.md). Database and storage sizes are shown in MB.

### Viewing logs

The **App health** page shows the last 200 lines of your container's output. Click **Refresh logs**, or tick **Auto-refresh**.
Through the API, call `GET /apps/{id}/logs?tail=N` (default 200, at most 1000). Logs are only available while a pod exists.

### Resource usage

The **App health** page has a **Resource usage** panel with CPU and memory sparklines: one point a minute, about the last 3 hours,
drawn against your limits and turning amber near them. The same data is at `GET /apps/{id}/metrics-history`. Use it
to tell a CPU-bound app (the 200m limit throttles rather than kills) from one nearing the 384 MB memory limit.

## 13. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `403` "Your account is waiting for administrator approval" | Wait for an administrator to approve you. |
| `403` "Your account is suspended..." | Contact the administrator. |
| `422` "Image runs as root, which isn't allowed..." | Add a non-root `USER` to your Dockerfile, preferably numeric such as `USER 10001`. Rebuild, push and save the app again. |
| `422` "Couldn't find that image, or it's private..." | The name or tag is wrong, or the image is private. Check the name and make the image public. |
| `422` "Image is N MB; the limit is 512 MB..." | Slim the image down: use a `-slim` or alpine base, or a multi-stage build. |
| `403` "You've reached your limit of N apps..." | Delete an existing app or ask an administrator to raise your limit. The limit is checked before the image and env checks, so a new app at the limit gets this `403` even if the spec has other problems. |
| `422` "... needs a database, but this app doesn't have one" | Add `resources: { db: true }` (or `storage: true`), or remove the reference. |
| `422` "A value containing '${{' must be exactly one reference..." | Use one `${{ db.x }}` or `${{ storage.x }}` from the table in section 5, as the whole value. |
| `422` "Missing values for these secrets" | Pass `--secret NAME=value` (CLI), the `secrets` object (API), or fill in **Secret values** (UI). |
| `422` "... isn't a valid env var name" | Env names use capital letters, digits and underscores, and don't start with a digit. |
| 502 or 503 from your hostname | The app isn't ready yet, the `port` is wrong, or the app isn't listening on all interfaces. Bind to `0.0.0.0`. |
| Stuck in **Deploying** | Readiness isn't passing. Check the health check path, the port, and that the app starts within a few seconds. After 10 minutes the app moves to **Failed** with the reason. |
| **Failed** with a message | Read the message and the logs on the app page. The platform retries automatically (see section 12). To retry now, click **Restart** or save the app. |
| "My app restarted" (a **Restarted ...** notice on the app page, or a gap in the logs) | Open **Recent events** on the app page (or `GET /apps/{id}/events`, or `picopaas status`). It says why: more than 250 MB of local disk (files, `/tmp`, logs; local files are lost, so keep files in the storage bucket), out of memory (384 MB limit; reduce memory use), or the app exited with a code (check the logs; the previous container's last lines are shown above the current ones). The notice stays on the app page for 24 hours; the last 20 events are kept. |
| TLS error from your database | Use the CA certificate from `${{ db.ca_cert }}`. For MySQL on Python 3.13, see the strict-flag note in section 6. |
| Database writes fail with a read-only or permission error | You hit the 50 MB cap (on PostgreSQL, past 100 MB `CONNECT` is refused). The app page shows it. Run `TRUNCATE TABLE` or `DROP TABLE` to free space. Writes return within a minute. |
| Bucket writes fail (`AccessDenied`) and the app page says the bucket is over its cap | Click **Free up space** (or run `picopaas storage-delete`). Your app's own key is read-only and can't delete. |
| The app restarts with "used more than 250 MB of local disk" | The app wrote too much to the container filesystem, `/tmp` or logs. Store files in the blob storage bucket and log less. |
| `OSError: No username set in the environment` | A non-root image running as a numeric UID with no passwd entry can hit this from `getpass` on Python 3.13+ (PyMySQL triggers it on import). Set `ENV USER=app HOME=/tmp` in your Dockerfile, or pass values explicitly. |
| `User ... has exceeded the 'max_user_connections' resource` | MySQL: your pool is bigger than the per-app connection cap (10). Lower the pool's max size. |
| PostgreSQL: `too many connections for role "app_<id>"` | Your pool is bigger than the per-app role's `CONNECTION LIMIT` (5). Lower the pool's max size. |
| MySQL: `Table must have a primary key` (`sql_require_primary_key`) | Should not happen on PicoPaaS: the managed MySQL no longer requires primary keys (request 0009). If you see it, tell an admin; a different MySQL server may still enforce it. |
| SMTP connection times out | SMTP is blocked on purpose. Use a mail provider's HTTPS API. |
| "Your saved secrets can't be read" | The platform's encryption key changed. Re-enter your secret values and save, or rotate the credentials. |

## 14. Templates

The **Templates** page (`/templates`, or "Browse templates" on the New app page) lists ready-made apps: Ghost, WordPress, Directus, Grafana (MySQL or PostgreSQL card), n8n (PostgreSQL), Shlink, a Gatus status page and a static nginx site, each with a link to its example README. **Use this template** opens the new-app form prefilled (`/apps/new?template=<id>`), including add-ons, database engine, health check, volume (only if the instance has volumes on), env vars and the names of the secrets you must set (the card says how to generate each value). Cards warn when the instance has volumes, databases or blob storage turned off. Nothing is created until you click **Create app**, and the one-app default limit still applies. See [Recipes](RECIPES.md) for the Ghost walkthrough.

## 15. Projects (teams)

A project owns apps; its members manage them together. Everyone has a **personal project** (your apps until you choose
otherwise). Create a shared project from the project switcher, then invite people.

- **Roles.** A *member* can deploy, edit, restart, back up and delete the project's apps. An *owner* can also invite, remove people,
  change roles, rename and delete the project, and move apps. A project always keeps at least one owner.
- **Invites.** Owners create an invite link (optionally for one email address). No email is sent: copy the link and send it yourself.
  A link works once and expires after 7 days. The person must log in with an approved account (use the account whose email you entered, if you did)
  and open the link. Owners can revoke open invites.
- **Names.** A project name must be unique among the projects you belong to (ignoring case and surrounding spaces). Creating or renaming to a name you already use gives a 409 "You already have a project named ...". Other people's project names are never checked or shown.
- **Leaving.** Any member can leave (owners need another owner first). Removing someone ends their access immediately, including their API keys.
- **Limits.** Each project has its own app limit (default `PROJECT_DEFAULT_APP_LIMIT`; an administrator can change it). Your personal project uses your account limit.
- **Deleting a project** is refused while it still has apps: delete them or move them (owner of both projects) first.
- **CLI and YAML.** `picopaas apply --project my-team-ab12 -f app.yaml`, `PICOPAAS_PROJECT=my-team-ab12`, or `project: my-team-ab12` in the spec. `picopaas projects` shows the slugs.
- Databases and buckets stay with their app. Suspending a member's account does not stop the team's apps. Project owners (not the app's creator) are the contacts administrators see for a team app.
- API keys act as you, but cannot invite, remove or change roles. Do that in the browser.
- **Suspended projects.** If an administrator suspends a project (for example after an abuse report), all its apps stop and the project becomes
  read-only for everyone in it, owners included. You can still open the project, its apps and Kaisen builds and read the logs and settings,
  and a banner says it is suspended (owners also see the reason). Anything that changes something (deploy, edit, delete, invite, Kaisen builds and runs)
  returns `403` (owners get the reason in the message). Existing backups can still be downloaded. Leaving the project stays possible. Kaisen runs in the project stop. If one of your team's
  members is suspended, the project keeps running for everyone else, but runs that member started stop. Contact an administrator to get a
  project restored; only the apps that suspension stopped come back.


### Invite someone to a project

1. Open **Account**. The **Projects** card lists every project you belong to with its role and counts. Shared projects you own show an invite (person) button and a manage (gear) button; members see only manage.
2. Pick the invite button, or open the project and go to **Invite people to this project** (`/projects/<id>#invite`). Your personal project cannot be shared: choose **New project** first.
3. Optionally enter the person's email. With an email you can also choose the role (*Member* or *Owner*); without one the link is a member link that any approved account can use.
4. Press **Create invite** and copy the link now; it is shown once. No email is sent, so paste it into a message yourself.
5. The person logs in with an approved account (register first if needed) and opens the link. It works once and expires after 7 days. If you entered an email, they must use that account.
6. Open invites are listed under **Pending invites**, where you can revoke them. After accepting, the person appears in **Members**.

## 16. Kaisen

Kaisen is in preview. You describe a small web app in plain language; Kaisen writes the code, runs it and deploys it for you, then changes it when you ask. It shows as "Kaisen" in the nav and as "Build with Kaisen" on **New app** when an administrator has it on. Expect rough edges: generated apps are throwaway-grade.

### What a built app is

A built app is a normal app in the build's project. It appears in the apps list with a "Built with Kaisen · Preview" badge, has the usual health and settings pages, and counts against the project's app limit. Kaisen apps run on a shared runtime image and read a `kaisen.json` at the project root:

```json
{"runtime": "python", "start": "uvicorn main:app --host 0.0.0.0 --port $PORT", "health": "/", "database": "postgres", "bucket": true, "volume": {"path": "/data"}, "test": "python -m unittest discover -s tests"}
```

`runtime` is `static`, `python` or `node`; `start` is needed for python (node defaults to `npm start`); servers listen on `0.0.0.0:$PORT` (8080). The other keys are optional:

- `database`: `mysql` or `postgres`. The app gets its own database and the `DB_*` and `DATABASE_URL` variables (see "Kaisen limits").
- `bucket`: `true` gives the app a blob storage bucket and the `S3_ENDPOINT`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY` and `S3_SECRET_KEY` variables.
- `volume`: `{"path": "/data"}` gives the app a persistent volume mounted there. It needs volumes to be turned on for this instance, and limits the app to one instance.
- `test`: a command that runs the app's tests (see "Generated tests").

`static` apps have no server code, so they can't use a database, bucket or volume. Once added, a database (and its engine), bucket or volume can't be removed. You rarely write this file yourself; Kaisen does.

### Before you start

Kaisen uses your project's own Anthropic API key, so model cost is billed to you (or your team), not to PicoPaaS. Open **Kaisen > Settings** (`/kaisen/settings`). Settings belong to the project, so a team project has its own key, model and caps. In a team project, owners edit settings; members can build and spend the project's key. Saved keys and tokens are encrypted and write-only: the page shows only "set, ends in ...abcd".

1. Open console.anthropic.com > API keys and create a key named `picopaas-kaisen`.
2. Set a spend limit on the Anthropic side too. PicoPaaS caps are a second guard, not a replacement.
3. Paste the key into Kaisen settings and save, then press **Test Anthropic** ("Key valid. Selected model available.").

Also in settings:

- **Model.** `claude-haiku-5-5` (default: in our evals it built every starter brief, including a login-gated CRM, at about a fifth of Sonnet's cost), `claude-sonnet-5-5` (fewer steps, faster on bigger builds) or `claude-opus-5-5`. Existing projects keep the model they saved.
- **Effort.** Low, medium (default) or high.
- **Spend caps (US dollars).** Per run (default $2), per day (default $8) and per build in total (default $30); each can be raised up to $1000. A build that reaches a cap stops and says so in its inbox. Resume it after raising the cap.
- **Member limits.** Owners can cap what each person spends. See "Kaisen limits".
- **Web access** (off by default) and **Reviewer** (off by default). See "Web access" and "Reviewer step".
- **GitHub token (optional).** See "Download or push to GitHub".

### Your first build

1. Choose **New app > Build with Kaisen**, or **Kaisen > New build**.
2. Give it a name (up to 60 characters), a brief (up to 8000 characters) and a project. A project holds at most 20 builds.
3. Press **Start building**. Creating the build starts it. If the project has no Anthropic key or a cap is reached, the build is created but not started, and the page says why.
4. Watch the build page: while a run is going, a progress strip shows the run type, elapsed time, what the run has cost so far and how much of the build cap is used. The status is `idle` (not started), `queued`, `running`, `waiting` (needs your answer), `done`, `stopped` or `error`. A stopped or failed build can be run again and resumes where it left off; after a long run, Resume starts a fresh model context from `PLAN.md` and the files instead of the whole conversation. Work happens in steps of up to 15 minutes and 40 model calls; a long build continues on its own in a fresh context (the plan, brief and files carry over) for up to 4 steps before it stops and waits for you; the transcript is redacted and trimmed 14 days after a run ends.
5. If Kaisen has a question, it appears in the build's inbox with the status `waiting`. Answer it (up to 4000 characters) and the build continues.
6. When it is `done`, open the app from the **Live app** card.

### Ask for changes

Type what to change (up to 4000 characters) in **Tell Kaisen what to change**. Kaisen edits the files and redeploys the same app. Feedback needs a first run to have happened. One change per message works best; every message is a run, and runs cost money. A hint under the box shows what earlier runs on this build cost (for example "Usually $0.04-$0.09 on claude-haiku-5-5 (capped at $2.00 per run)"), or just the cap before the first run.

- **Next ideas.** When a run finishes, up to three suggestion chips ("Next: ...") appear for the latest run. Pressing one copies it into the box so you can edit and send it; nothing runs until you send.
- **Fix it.** When the preview app is in `error` or `retrying`, the Live app card shows **Fix it**. It sends a standard change request asking Kaisen to read the app's recent logs and fix the cause, so it costs a run like any message. If the generated tests fail, the Tests row has its own **Fix it**.
- **Starter cards.** On **New build**, the starter chips fill the name and brief from the examples below. Press **Undo** to get your text back.

### Write a good brief

- Say what the screen looks like: panes, where things sit, what is collapsible.
- Say what is saved and where. If the app must remember anything between visits, name MySQL; otherwise say "browser only" (localStorage).
- List the must-have interactions (drag and drop, keyboard alternatives, export).
- Say what is out of scope, so Kaisen does not build it anyway.
- Name the storage you want: MySQL or PostgreSQL for data, a bucket for uploaded files, a volume for files on disk (for example SQLite), or "browser only".
- Remember the limits: one web process only (no background workers or cron), no email sending. Outside API keys go in as build secrets (see "Build secrets").
- Fold changes you already know you want into the brief. One bigger first run is cheaper than three small follow-ups.

### Starter briefs

Paste one into the brief box and edit to taste. Costs vary by model, effort and how many questions Kaisen asks; the figures below are what we saw, not a promise.

**Fancy Docs (Markdown notes with a page tree)**

```
I want a Markdown note-taking application that saves to a MySQL database. The editor is on the left and the rendered Markdown is on the right, with a nav tree in the center. Pages can be nested underneath other pages, and the nav tree can be used to move pages around in the tree (drag and drop, plus buttons for keyboard users). The editor pane can be collapsed. Support image uploads that are stored in the MySQL database and can be referenced in the Markdown. Include a dark mode toggle for the entire page, remembered in the browser.
```

This rebuilt in one run for about five US cents on Claude Haiku 5.5 (the original took three runs, about 7 cents).

**Team standup board**

```
Build a small standup board with Python and MySQL. Anyone can open the page, type their name and post three short fields for a chosen date: yesterday, today and blockers. There is no login. The main screen is a day view: a date picker with previous and next day buttons, and one card per person who posted that day, with blockers highlighted. Posting again for the same person and date replaces the earlier post. A second page shows one person's history, newest first, reachable by clicking a name. Save everything in MySQL. Include a dark mode toggle remembered in the browser. Out of scope: accounts, passwords, email or chat notifications, editing other people's posts.
```

**Link shortener with click counts**

```
Build a link shortener with Python and MySQL. The home page has a form to paste a long URL and optionally choose a short code; if no code is given, generate a random 6-character one. Only accept http and https URLs, and show a clear error for anything else or for a code that is already taken. Visiting /<code> redirects to the long URL and adds one to that link's click count. A stats page lists every link with its code, target, created date and click count, sorted by clicks, with a copy button for each short URL. Also expose a JSON API: POST /api/links with JSON {"url": "...", "code": "..."} (code optional) returns 201 with {"code": "...", "url": "..."}, 400 for a URL that is not http or https, and 409 for a code that is already taken; GET /api/links returns a JSON list of links, each with code, url and clicks. Save everything in MySQL. Out of scope: accounts, QR codes, expiry, analytics beyond the count.
```

**Habit tracker with streaks**

```
Build a habit tracker as a static page that saves to the browser's localStorage, with no server. I can add and remove habits. Each habit shows a month grid where I click a day to tick it on or off, with buttons to move to the previous and next month. Next to each habit show the current streak (consecutive ticked days ending today or yesterday) and the best streak ever. Add Export and Import buttons for all data as a JSON file, and validate the file on import. Support a dark mode toggle remembered in the browser. Out of scope: reminders, accounts, syncing between devices.
```

**Recipe scaler**

```
Build a recipe scaler as a static page with no server and nothing saved. I paste a recipe's ingredient list, one ingredient per line (like "1 1/2 cups flour"), and set the original and wanted number of servings. The page shows the scaled list, handling fractions and decimals and showing results as friendly fractions. A toggle converts between metric and US units (cups, tablespoons, teaspoons, ounces, pounds, grams, millilitres) and leaves lines it cannot parse unchanged. Include a print view that shows only the scaled recipe in a clean layout. Light theme only. Out of scope: saving recipes, importing from web pages, nutrition information.
```

### Download or push to GitHub

- **Download** (`.tar.gz`) is always available from the build page. It holds the files plus `CONVERSATION.md`: your brief, feedback and answers, and Kaisen's replies, questions and summaries for every run (no internal agent detail; saved keys are redacted).
- **Push**: each build has its own **Output** card (also an optional section on New build). Choose *Download and push to GitHub*, enter the repository (`https://github.com/owner/repo`) and a branch (optional; blank = `kaisen/<build>`), then pick the auth: *Use the project's GitHub token* or *Use a token for this build only*. Options: push when a build finishes, and open a pull request. **Test** checks push access to that repository with the chosen token; **Push now** pushes immediately. The download's `CONVERSATION.md` is not pushed.
- The project token is shared: anyone who can run builds in the project can push with it to any repository it can write, so use a fine-grained token limited to the repositories you mean.

To make a token: GitHub > Settings > Developer settings > Fine-grained tokens > Generate new token. Name it `kaisen`, expire it in 90 days, pick the account that owns the repository, choose **Only select repositories** and pick just that one. Under repository permissions set **Contents** and **Pull requests** to Read and write (Metadata read-only is added for you) and leave the rest at No access. Copy the token (it starts with `github_pat_`) and paste it into Kaisen settings or the build's Output card; GitHub shows it once. For an organization, an owner may need to approve it.

### Web access

Off by default. In **Kaisen > Settings > Web access** an owner can let the builder search the web and read pages, for example to check a library's documentation or a link in your brief. It works with all three models.

- **Allowed sites.** Leave the list empty to allow any site, or enter up to 20 domains, one per line, without `https://` (a path such as `github.com/owner` is allowed). The settings page says in words what is in force.
- **Untrusted content.** A page can contain text that tries to steer the builder. Kaisen treats web content as data, and the builder has no secrets and can only touch its own build's files, but an allow-list is the safest setting. A build that has secrets never gets the web tools, with or without an allow-list, because injected page text could make the builder write code that sends the secrets out. Page text is not shown in the transcript or `CONVERSATION.md`; they say only "searched for ..." or "read ...".
- **Builds with secrets.** A build that has [build secrets](#build-secrets) only uses the web when the allowed-sites list is set; with an empty list it runs without web tools, so a hostile page can't talk the builder into shipping code that sends your secrets elsewhere.
- **Cost.** Each web search costs a flat $0.01 on top of the tokens it adds. Reading a page costs tokens only. Searches count toward every cap.

### Build secrets

The **Secrets** card on the build page holds environment values the built app needs, such as an API key. Anyone in the project who can run the build can add one.

1. Enter a name (UPPER_SNAKE_CASE) and a value, then save. Saving an existing name replaces its value.
2. Press **Deploy now**. Secrets reach the running app on its next deploy. Deleting a secret removes it from the running app at once.

Kaisen sees the names only and is told to read them from the environment; values are encrypted, masked in the page and API and redacted from transcripts, logs and downloads. Limits are under "Kaisen limits".

### Generated tests

Kaisen writes a small test suite and puts its command in `kaisen.json` as `test`. The runtime runs that command after installing dependencies and before the app starts (a static site has nothing to install, so its test runs just before the site is served; use `"test": "node --test"`, which runs every `*.test.js` file; Node is in the runtime image, and `node --test tests/` fails on Node 22 and later), with a time limit and without any platform credentials in the environment. The app starts whatever the result. The build page shows a **Tests** row (pass, fail, timed out, running) with the output tail, and Kaisen reads the result when it checks the preview, fixing failures before it finishes. Results appear once the runtime has run them; until then the row says there is no result yet (static-site tests need the updated runtime image, which an administrator rolls out; before that the row stays at no result). When Kaisen checks the preview it requests the `health` path from `kaisen.json` (default `/`). Any 2xx or 3xx answer counts as the app answering, so a login-gated app that redirects to `/login` is fine; 4xx and 5xx do not. If the runtime log shows the new version has not started yet, the check says the answer may come from the previous version instead of reporting OK.

### What the builder can check

Kaisen edits files in place (one exact piece of text at a time) and can search across them, so changes to a bigger app cost fewer tokens. Each time it writes a file it is told about Python syntax errors, invalid JSON and `kaisen.json` mistakes straight away, without running anything. After a preview is up it can also send requests (GET, POST and so on) to the app's own endpoints and check the answers before it finishes; it can only reach that build's own preview, never another site, and the answers are shown to it as untrusted data. Within one step it keeps the app's cookies between requests (so it can sign up, log in and then test logged-in pages; it sees cookie names, never values, and can clear them to test as a logged-out or second user); a new step starts with none. Before it finishes it is also asked once to tick or explain any open items in `PLAN.md`. The reviewer can search files and fetch (GET only) from the preview too.

### Reviewer step

Off by default. With **Reviewer** on, a second, cheaper model (default `claude-haiku-5-5`, any listed model allowed) reads the finished files and approves, or reports up to five findings. Findings trigger exactly one fix round by the builder; there is never a second review. In that fix round the builder must answer each finding ("fixed: ..." or "disagree: ..."); if it finishes without changing a file and without an answer it is sent back once. The run shows the verdict, findings, the builder's answer ("no files changed after review" when nothing but PLAN.md changed) and a "Reviewer" label in its transcript. The review is a model call, so it adds at least one extra call per run and is billed to your key and counted in the caps. If a cap stops it, the verdict is "skipped".

### Restore points

Kaisen saves a restore point of the files each time a run starts (a first build, a change request or a resume), as long as the files differ from the last restore point. The newest 5 per build are kept; older ones are dropped. The **Restore points** card lists them with the run they belong to, and each run can show **what changed** as a diff (secrets are hidden).

**Restore** puts the files of a restore point back. It is allowed for any project member and only while no run is queued, running or waiting (stop the run first). It never deploys: the live app keeps running the old version until you press **Deploy**, and the build page then shows the preview as out of date. Restoring is undoable: just before replacing the files, Kaisen saves the current ones as a "before restore" point, and restoring that point brings them back. The next change request starts from the restored files.

### Offline, redeploy, delete

**Take offline** on the build page deletes the app, and its database and data if it has one, but keeps the build, its files and its conversation. **Deploy again** brings the app back with a new address and an empty database. Deleting the app from its own pages does the same as taking it offline. Deleting the build asks whether to delete the app too.

### Kaisen limits

Everything that caps what Kaisen can do, in one place. Defaults come from the platform and are shown in the settings page; administrators can't raise the fixed numbers below without a platform change.

**Spend (US dollars)**

- Model cost is billed to your Anthropic account; the project's caps stop a build early. Defaults: $2 per run, $8 per day, $30 per build; each can be set from 0 to $1000. A cap that is reached stops the run and leaves a note in the inbox; raise the cap and resume.
- **Member limits.** In **Kaisen > Settings > Member limits** an owner sets, for each person, a daily and a per-build limit; blank means no limit. These apply on top of the project caps. Spend is counted against the person who started the run. The daily amount counts all builds in the project and resets at 00:00 UTC. Members see only their own row and can't edit it. A run that would exceed a limit isn't started, or stops mid-run, with a message naming the limit.
- **Costs that are extra.** Web search is $0.01 per search. The reviewer adds at least one model call per run. Every call counts toward all caps.

**Builds and files**

- A project holds at most 20 builds. A name is up to 60 characters, a brief up to 8000, a change request or an answer up to 4000.
- A run works in steps of up to 15 minutes and 40 model calls each, and chains up to 4 steps by itself, each in a fresh context seeded from `PLAN.md` (spend caps, the wall-clock limit and the repeat detector still stop it). Resume of a history over 30 messages also starts fresh from `PLAN.md`. A build holds files up to 200 KB each, 300 files and 5 MB in total.
- Transcripts are trimmed 14 days after a run ends.

**Built apps**

- A built app uses one of the project's app slots and follows the same limits as any app (memory, CPU, image, quotas).
- A database has the same 50 MB limit as any app; MySQL and PostgreSQL are equal choices. Variables: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_CA_CERT` (certificate text) and `DATABASE_URL` (PostgreSQL also sets `DB_TLS`). TLS is required, and the first deploy takes a minute or two longer.
- A bucket has the platform's bucket size limit; a volume has the platform's default volume size and needs volumes to be enabled on this instance.
- The platform owns the variable names starting with `KAISEN_` or `DB_` and these too: `DATABASE_URL`, `PORT`, `PATH`, `HOME`, `HOSTNAME`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `NODE_OPTIONS`, `S3_ENDPOINT`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`.

**Build secrets**

- Up to 20 per build. A name is UPPER_SNAKE_CASE (capital letters, digits and underscores, not starting with a digit), up to 64 characters, and not one of the reserved names above.
- A value is 6 to 4000 characters (shorter ones can't be hidden reliably in logs) and can't contain `${{`.

**Web, tests and reviewer**

- Web access: 20 allowed domains; up to 5 searches and 5 page reads per model request; a page read is capped at about 20,000 tokens.
- Tests: the command is up to 300 characters, runs for up to 120 seconds, and only the last part of its output (about 4 KB) is kept. The test can't see database, bucket or platform variables.
- Reviewer: up to 5 findings of 400 characters and 8 model calls per review; one fix round.
- Previews are one web process: no background workers or cron, and no email.

### Not supported yet

- Test results show up only once the runtime image on this instance has been updated; until then the Tests row says there is no result yet.
- Background workers, cron jobs and email sending in built apps.
- Removing a database, bucket or volume from a built app, or changing a database's engine, after it has been added.
