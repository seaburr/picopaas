# PicoPaaS CLI

`cli/picopaas.py` deploys and manages apps from your terminal. It needs Python 3 and `httpx` (`pip install httpx`). Create a `pp_sk_` API key on the **API keys** page, then:

```bash
export PICOPAAS_TOKEN=pp_sk_...            # PICOPAAS_URL defaults to https://platform.raisehellpraisedale.xyz
python cli/picopaas.py apply -f app.yaml --secret API_TOKEN=s3cret
python cli/picopaas.py apps
python cli/picopaas.py status my-app
python cli/picopaas.py rotate my-app --db      # or --storage
python cli/picopaas.py storage-delete my-app --all   # or --prefix uploads/old/
python cli/picopaas.py -v apps                  # -v adds HTTP status codes to error messages
```

Run `python cli/picopaas.py --help`, or add `--help` to any command, to list its flags. Output is plain text for people, so scripts
should check the exit code (0 ok, 1 error), not the wording.

| Command | What it does |
|---|---|
| `apply -f FILE [--secret NAME=value ...]` | Create or update an app from a YAML spec. Repeat `--secret` for each secret. |
| `apps` | List your apps. |
| `status NAME` | Show an app's state, URL and, if it failed, why. |
| `rotate NAME --db` or `--storage` | Rotate the database password or the storage bucket keys. |
| `storage-delete NAME --prefix PREFIX` or `--all` | Delete objects from the app's storage bucket. Use it to free an over-cap bucket. |
| `-v` (before the command) | Show HTTP status codes in error messages. |

`PICOPAAS_TOKEN` (required) is your API key. `PICOPAAS_URL` is optional and defaults to `https://platform.raisehellpraisedale.xyz`.

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

`image` above is a placeholder. Use your own public image, or `ghcr.io/seaburr/picopaas-testapp-hello:latest` to try it.
Otherwise `apply` fails with `error: Couldn't find that image, or it's private. Check the name and tag, and make the
image public in your registry.`

`apply` creates the app, or updates it if you already have one with that `name`. It prints what happened and a next step:

```
Created my-app (queued). It'll be live in about a minute at https://<hostname>
Check progress: picopaas status my-app
```

If the app has already failed, the first line says so and gives the reason. When you update an app, a secret without
`--secret` keeps its stored value. Anything you remove from the file (env, secrets, healthCheck) is removed from the app.

- `apps` prints a `NAME  URL  STATUS` table, or `No apps yet. Create one with: picopaas apply -f app.yaml`.
- `status <name>` prints `name`, `url`, `status`, and, when there is one, `detail` (why it failed and what to do) and
  `retry` (when it will be retried), then `image` and `health`. Statuses read `queued` (API `pending`) and `failed`
  (API `error`).
- `rotate <name> --db|--storage` prints `Rotated the database password for <name>. The app is restarting.` (or `Rotated the storage bucket keys for ...`).
- `storage-delete <name> --prefix P|--all` deletes up to 5000 objects per run and prints how many it deleted and how much is left.
  If it says more objects remain, run it again. Once the bucket is under its cap, writes are restored and the app restarts.

Errors print `error: <message>` and exit 1. The message is the server's plain-language explanation (for example
`error: Your storage bucket is still being created. Try again in a minute.`); `-v` prefixes the HTTP code. A missing
token, a 401, an unreachable server and a timeout each say what to check (`PICOPAAS_TOKEN`, `PICOPAAS_URL`, your
network). Env values must be strings (quote numbers and booleans).

`healthCheck` is optional and unknown keys are rejected. `enabled` (default false) turns on HTTP readiness and liveness
probes against `path` on your app's port; a 2xx or 3xx response is healthy. `path` defaults to `/healthz`. It must start
with `/` and be at most 255 characters, using only letters, digits and `._~!$&()*+,;=:@/%-` (no `..` segments, spaces or
quotes). When disabled, the app counts as ready once the port accepts a connection, and there is no liveness probe.
Changing it redeploys the app.

The platform sets the `PORT` env var to your app's port unless you set `PORT` yourself in `env`.

## Connecting to your database

See [USER_GUIDE.md](USER_GUIDE.md), section 6, for the TLS settings and Python, Node and `mysql` examples.
