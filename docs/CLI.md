# PicoPaaS CLI

`cli/picopaas.py` deploys and manages apps from your terminal. It is a thin client for the platform's HTTP API: it
talks only to PicoPaaS (`PICOPAAS_URL`) and never connects to your database server or your blob storage bucket directly.
It needs Python 3 and `httpx` (`pip install httpx`).

## Setup

Create a `pp_sk_` API key on the **API keys** page, then:

```bash
export PICOPAAS_TOKEN=pp_sk_...            # PICOPAAS_URL defaults to https://platform.picopaas.xyz
python cli/picopaas.py apps
```

`PICOPAAS_TOKEN` (required) is your API key. `PICOPAAS_URL` is optional. Run `python cli/picopaas.py --help`, or add
`--help` to any command, to list its flags. `-v` (before the command) adds HTTP status codes to error messages.
Output is plain text for people, so scripts should check the exit code (0 ok, 1 error), not the wording.

| Command | What it does |
|---|---|
| `apply -f FILE [--secret NAME=value ...]` | Create or update an app from a YAML spec. |
| `apps` | List your apps. |
| `status NAME` | Show state, URL, health, usage and, if it failed, why. |
| `logs NAME [--tail N] [--pod POD]` | Print recent logs. |
| `restart NAME` | Restart, pulling the latest image for the tag. |
| `check-image NAME` | Re-check the image tag now and roll out a new image. |
| `events NAME` | Show recent restart events. |
| `versions NAME` / `rollback NAME DIGEST` | List earlier image versions / redeploy one. |
| `export NAME [-o FILE]` | Print the app as a YAML spec. |
| `usage NAME` | Show database and blob storage usage. |
| `rotate NAME --db` or `--storage` | Rotate the database password or bucket keys. |
| `storage upload NAME FILE [--key K]` | Upload a file to the app's bucket (default key: the file name). |
| `storage download NAME KEY [-o FILE]` | Download one object. |
| `storage-delete NAME --prefix P` or `--all` | Delete objects from the app's bucket. |
| `domain NAME [--set HOST \| --verify \| --remove]` | Manage the custom domain. |
| `delete NAME --yes` | Permanently delete the app. |

Every command takes the app's `name`, not its numeric id. A name you don't own prints `No app named X`.

## apply

```bash
python cli/picopaas.py apply -f app.yaml --secret API_TOKEN=s3cret
```

Creates the app, or updates it if you already have one with that `name`, then prints what happened and a next step:

```
Created my-app (queued). It'll be live in about a minute at https://<hostname>
Check progress: picopaas status my-app
```

If the app has already failed, the first line says so and gives the reason. When you update an app, a secret without
`--secret` keeps its stored value. Anything you remove from the file (env, secrets, healthCheck) is removed from the
app. Repeat `--secret` for each secret; each must be declared under `secrets:` in the spec.

Example `app.yaml` (max 64KB, no YAML anchors/aliases, unknown keys rejected):

```yaml
apiVersion: picopaas/v1
name: my-app
image: ghcr.io/me/app:1.2
port: 8080
healthCheck: { enabled: true, path: /healthz }   # optional; off by default, path defaults to /healthz
resources: { db: true, db_engine: pg, storage: false }   # db_engine: mysql (default) or pg; fixed once created
env:
  LOG_LEVEL: info
  DATABASE_URL: ${{ db.url }}
secrets:
  - API_TOKEN
```

`image` above is a placeholder. Use your own public image, or `ghcr.io/seaburr/picopaas-testapp-hello:latest` to try
it. Otherwise `apply` fails with `error: Couldn't find that image, or it's private. Check the name and tag, and make
the image public in your registry.` Env values must be strings (quote numbers and booleans). `port` is 1024-65535.

`healthCheck.enabled` (default false) turns on HTTP readiness and liveness probes against `path` on your app's port;
a 2xx or 3xx response is healthy. `path` must start with `/` and be at most 255 characters, using only letters, digits
and `._~!$&()*+,;=:@/%-`. Changing it redeploys the app. The platform sets the `PORT` env var to your app's port
unless you set `PORT` yourself. The full format is in [USER_GUIDE.md](USER_GUIDE.md), section 4.

## apps and status

`apps` prints a `NAME  URL  STATUS` table, or `No apps yet. Create one with: picopaas apply -f app.yaml`.

`status NAME` prints `name`, `url`, `status`, and, when there is one, `detail` (why it failed and what to do),
`retry` (when it will be retried) and `event` (a recent restart), then `image` and `health`. It also shows
`replicas` when more than one, the database `engine` (MySQL or PostgreSQL) and database and blob storage usage against
their caps. Statuses read `queued` (API `pending`) and `failed` (API `error`).

## logs

`logs NAME` prints the last 200 lines per pod. `--tail N` sets lines per pod (1 to 1000). An app with several
replicas prints every pod, grouped under `=== pod NAME ===` headers, plus a note on stderr listing the pod names;
pass `--pod NAME` for one. If there is no running pod, or logs can't be read, the reason is printed on stderr.

## restart and check-image

`restart NAME` restarts the app. It first re-resolves the image tag ("pull latest"); a new image that fails the checks
is skipped and the old one restarts. `check-image NAME` re-resolves the tag now and rolls out a changed image
if it passes the checks (apps also check on their own every 10 minutes unless `auto_update` is off). It says whether
a rollout started, the new image was skipped (with the reason), or the app is already current. A suspended app
can't be restarted (409).

## events

`events NAME` lists recent restart events, newest first, as `WHEN  KIND  MESSAGE`. Kinds are `disk`, `evicted`,
`oom` (out of memory) and `crashed`.

## versions and rollback

`versions NAME` lists up to five earlier image versions that ran successfully (digest, age, image ref), marking
the current one. `rollback NAME DIGEST` redeploys one of them; a digest that isn't in the list is rejected.

## export

`export NAME` prints the app as a `picopaas/v1` spec on stdout, ready to edit and `apply`; `-o FILE` writes it to a
file instead. Secret values are never exported, only their names under `secrets:`, so pass them again with `--secret`
when you apply. The CLI builds the spec from the app's current settings.

## usage

`usage NAME` asks the platform to measure the database and the blob storage bucket right now and prints
`size / cap MB` for each, flagging `OVER CAP, read-only`. For a bucket it also lists the largest prefixes. An app with
neither says so.

## rotate

`rotate NAME --db` or `--storage` replaces the database password or the bucket access keys. It prints
`Rotated the database password for NAME. The app is restarting.` (or `Rotated the blob storage bucket keys for ...`).
If the database or bucket is still being created, or the app doesn't have one, it fails with a 409 explanation.

## storage upload / download

`storage upload NAME FILE [--key K]` streams a local file into the app's bucket through a presigned link (the CLI never sees bucket
credentials). The size must fit in the bucket's remaining quota and the bucket must not be over its cap. `storage download NAME KEY
[-o FILE]` streams one object to disk (a read-only API key works). Needs a full-scope key for upload.

## storage-delete

`storage-delete NAME --prefix P` deletes objects in the app's bucket whose key starts with `P`; `--all` deletes every
object. Use it to free an over-cap bucket, which is read-only until it is under its cap. It deletes up to 5000 objects
per run and prints how many it deleted and how much is left. If it says more objects remain, run it again. Once the
bucket is under its cap, writes are restored and the app restarts. This only touches the bucket. There is no CLI
command for database contents (see below).

## domain

`domain NAME` shows the app's custom domain, if any. `--set HOST` attaches one and prints the DNS records to create
(a TXT ownership proof and a CNAME to the platform). `--verify` checks them: on success the domain turns `active`;
otherwise `problem:` says what is missing and it stays `pending`. `--remove` detaches it; the generated address keeps
working. Custom domains can be disabled platform-wide (the command then prints the server's explanation).

## delete

`delete NAME --yes` permanently deletes the app and destroys its database and blob storage data. Without `--yes` it
refuses and changes nothing.

## Database access

The CLI does not connect to your database server. Your database is reachable only from your own running app: put
`DATABASE_URL: ${{ db.url }}` (or `db.host`, `db.port`, `db.name`, `db.user`, `db.password`, `db.ca_cert`) under `env:`
in the spec and read it in your code. To change data or schema, do it from the app (for example a migration at
startup). To check size, use `usage`; if the database is over its cap, delete rows or drop tables from the app, and
writes return once it's under the limit. Connection settings and TLS examples per engine are in
[USER_GUIDE.md](USER_GUIDE.md), section 6.

## Errors

Errors print `error: <message>` and exit 1. The message is the server's plain-language explanation (for example
`error: Your blob storage bucket is still being created. Try again in a minute.`); `-v` prefixes the HTTP code. A missing
token, a 401, an unreachable server and a timeout each say what to check (`PICOPAAS_TOKEN`, `PICOPAAS_URL`, your
network).
