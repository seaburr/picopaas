# PicoPaaS API

Base URL: `https://platform.raisehellpraisedale.xyz`. JSON in, JSON out. FastAPI also serves interactive docs at
`/docs` and the schema at `/openapi.json`.

## Authentication

| Method | How |
|---|---|
| Session cookie | `POST /auth/login` or `/auth/register` sets an HttpOnly `session` cookie (JWT, 7 days, `SameSite=Lax`, `Secure` outside dev). The UI uses this. |
| API key | `Authorization: Bearer pp_sk_...` (created on the **API keys** page — browser only). A `full` key works on every endpoint that accepts a user; a `read` key is limited to safe methods (see API keys below). The cookie is checked first. |

Auth levels used below:

- **public**: no auth.
- **user**: any logged-in, non-deleted user (including pending and suspended).
- **active**: user with status `active`. Pending gets `403` "Your account is waiting for administrator approval..."; anything
  else gets `403` "Your account is suspended and your apps are stopped...".
- **admin**: the `platform_admin` role (`403` "This needs administrator access." otherwise). Missing auth is `401` "You're logged
  out. Log in again, or check that your API key (PICOPAAS_TOKEN) is valid and not revoked."

Error `detail` strings are written to be shown to people as-is (what happened, why, what to do) and may be reworded;
match on the status code, not the text.

Request bodies reject unknown fields (422). Common errors: 401 not logged in, 403 forbidden, 404 not found
(also used for other people's apps), 409 conflict, 422 validation, 502 an upstream service (registry, database server,
bucket) failed.

An example call that works with an API key:

```bash
curl -sS https://platform.raisehellpraisedale.xyz/apps -H "Authorization: Bearer $PICOPAAS_TOKEN"
```

## SPA navigation and path overlap (issue #10)

The Vue UI and the API share paths (`/admin`, `/admin/health`, `/apps`, `/apps/new`, ...). Because the API is not
under an `/api` prefix (issue #10 proposes moving it), the server decides per request:

- A **browser navigation** (GET/HEAD with `Sec-Fetch-Mode: navigate`, or, when that header is absent, an `Accept`
  starting with `text/html`) gets `index.html` so refreshes and deep links work.
- Everything else (the SPA's own `fetch` calls, curl, the CLI) gets the JSON API.
- Never swapped: `/static/*`, `/healthz`, `/docs`, `/redoc`, `/openapi.json`.
- Unknown non-API paths fall back to the SPA; unknown paths starting with `auth`, `apps`, `admin`, `api`,
  `healthz` or `static` return a JSON 404.
- A request whose `Accept` lists `application/json` first never gets the SPA, even with `Sec-Fetch-Mode: navigate`.
- Caching: the SPA HTML is `Cache-Control: no-cache`; every JSON response is `Cache-Control: no-store`. Both carry
  `Vary: Sec-Fetch-Mode, Accept`, because one URL can return either (a cache must not replay one for the other).

If you script against the API, do not send `Accept: text/html` or a `Sec-Fetch-Mode: navigate` header on GET.
The only route that already has an `/api` prefix is `/api/keys`.

## Health

| Method | Path | Auth | Response |
|---|---|---|---|
| GET | `/healthz` | public | `{"status": "ok"}` |

## Auth (`/auth`)

| Method | Path | Auth | Body | Response |
|---|---|---|---|---|
| POST | `/auth/register` | public | `{email, password, accept_terms}` (email valid, password 8 to 256, `accept_terms` must be `true` else 422) | 201 `User`; sets cookie. The first user is `platform_admin`/`active`; later users are `user`/`pending`. Enumeration-safe: registering an email that already has an account also returns 201 with a `pending` `User` and a cookie, but the cookie authenticates nobody (`/auth/me` is 401) and nothing changes; the owner just logs in. No email is sent. |
| POST | `/auth/login` | public | `{email, password}` | 200 `User`; sets cookie. 401 "Email or password is wrong..." (same for unknown emails and deleted users; failures are audited as `login.failed`). |
| POST | `/auth/logout` | public | none | 204; clears cookie. |
| GET | `/auth/me` | user | none | `User` plus `features` and `limits` |
| POST | `/auth/change-password` | user (any status) | `{current_password, new_password}` (new: 8 to 256, must differ) | 204. 400 if `current_password` is wrong; 422 for a bad new password. Clears `must_change_password`. Audited as `user.change_password`. |

`User`: `id, email, role ("user"|"platform_admin"), app_limit, status ("pending"|"active"|"suspended"|"deleted"),
created_at, must_change_password`. New users get `app_limit` 1. `must_change_password` is set by an admin password
reset and informational only (the server does not block other calls); the UI should send the user to change it.

`/auth/me` adds:
- `features: {databases, storage}`: `databases` is true when a new database could be placed now (an `active` database
  server with a free slot; draining and full servers don't count). `storage` is true when the platform has bucket
  credentials configured. Hide the matching checkbox when false.
- `limits: {cpu_limit_m, mem_limit_mi, cpu_request_m, mem_request_mi, image_mb, db_mb, bucket_mb, ephemeral_mi,
  override: {cpu_min_m, cpu_max_m, mem_min_mi, mem_max_mi}}`: the platform caps (from `app/config.py`; `ephemeral_mi` is
  the scratch disk) and the range an administrator may override per app.

## API keys (`/api/keys`, active)

Keys have one of two access tiers (`scope`): **`full`** (default — everything your account can do) or **`read`**
(read-only: only safe methods `GET`/`HEAD`/`OPTIONS`; a mutating request returns **403**). Key management itself is
**browser-session only** — an API key, at any scope, cannot create or revoke keys (so a leaked key can't mint more or
change a scope). Pre-existing keys are `full`.

| Method | Path | Response |
|---|---|---|
| POST | `/api/keys` | Optional body `{"name": "...", "scope": "read"\|"full"}` (name trimmed, printable, max 64, blank = unnamed; scope defaults `full`; unknown fields and other scope values rejected 422). 201 `{id, key, prefix, name, scope, created_at}`. The raw `key` is shown **once**. **Session only** (403 if called with an API key). |
| GET | `/api/keys` | List of `{id, prefix, name, scope, created_at, last_used_at, revoked_at}` for your keys, newest first. `last_used_at` is updated at most every 5 minutes. |
| DELETE | `/api/keys/{key_id}` | 204. Revokes (idempotent). **Session only** (403 if called with an API key). 404 if not yours. |

## Apps (`/apps`, active; scoped to the caller's own apps)

`App` response: `id, owner_id, name, hostname (full, e.g. clever-robin-a1b2c.raisehellpraisedale.xyz), image, port,
image_digest, auto_update, image_checked_at, image_update_error, want_db, want_storage, health_check_enabled, health_check_path, status, status_detail, status_reason, retry_at, created_at, last_event, resources, quota, custom_domain, env[]`.
`custom_domain` is null when none is attached, else `{host, status (pending|active|error), verified_at, error, challenge_host, challenge_token, cname_target}` — the last three are the DNS records the user must create. `status_detail` is a sentence for people; `status_reason` is a machine code for `error` (for example `image_pull`, `crash_loop`, `oom`, `not_ready`, `disk`, `timeout`).
`quota` is `{db, storage}`, each `{size_bytes, limit_bytes, over_quota}` or null when the app has no such resource; it comes from the last quota poll (DB every 60s, storage every 5 min), no live calls. `last_event` is `{at, kind, message}` for the newest restart event (`disk`, `evicted`, `oom`, `crashed`) if it is under 24 hours old, else null. `retry_at` is when an app in `error` will next be retried (or null). `resources` is
`{cpu_request_m, cpu_limit_m, mem_request_mi, mem_limit_mi, overridden, exceeds_platform_caps}`.
Each `env` item: `{key, value, is_secret, undecryptable}`. Secret values are returned as `********`. If a stored
value cannot be decrypted (platform key changed) `undecryptable` is true and `value` is empty.

Statuses: `pending, deploying, running, error, suspended, deleted`.

| Method | Path | Body | Response / notes |
|---|---|---|---|
| POST | `/apps` | `AppCreate` | 201 `App` (status `pending`). 403 "You've reached your limit of N apps..." (administrators are exempt); 422 for image or env problems; 409 if it couldn't reserve a hostname (retry). |
| GET | `/apps` | none | List of `App`, newest first. |
| GET | `/apps/{app_id}` | none | `App`; 404 "We can't find that app..." (also for other people's apps). |
| PATCH | `/apps/{app_id}` | `AppUpdate` | `App`. 403 "This app is suspended by an administrator..." Changing image, port, env or health check sets status `pending` and redeploys. |
| DELETE | `/apps/{app_id}` | none | 204. Soft delete; the reconciler tears down the namespace, database and bucket. 403 if the app is suspended. |
| POST | `/apps/{app_id}/rotate-credentials` | `{"target": "db"\|"storage"}` | `App` (status `pending`). 409 "Your database is still being created..." or the storage equivalent (also when the app has no database or bucket); 502 "Couldn't rotate the credentials, so the old ones still work..."; 403 if suspended. |
| GET | `/apps/{app_id}/database/usage` | none | Live `{size_bytes, limit_bytes, over_quota}`. 404 if the app has no database; 502 if the DB server is unreachable. Owner or admin. |
| GET | `/apps/{app_id}/storage/usage` | none | Live `{size_bytes, limit_bytes, over_quota, objects, top_prefixes[{prefix, size_bytes, objects}]}` (top 5 first-path-segment prefixes). 404 if no bucket. Owner or admin. |
| POST | `/apps/{app_id}/storage/delete` | `{"prefix": "..."}` or `{"all": true}` (exactly one; 422 otherwise) | `{deleted, size_bytes, truncated, over_quota}`. Deletes with the platform's credentials (an over-cap app's own key is read-only), at most 5000 objects per call (`truncated` = more remain). Re-measures; if now under the cap the read-write key is restored immediately. Audit-logged as `app.storage.delete`. Owner or admin. |
| GET | `/apps/{app_id}/events` | none | Recent restart events, newest first, at most 20: `[{at, kind, message}]`. `kind` is `disk` (over the 250 MB local disk), `evicted` (other eviction), `oom` (out of memory) or `crashed` (non-zero exit; the message has the code). Recorded once per occurrence by the reconciler. Owner or admin; others get 404. |
| GET | `/apps/{app_id}/logs?tail=N` | none | `{lines[], pod, note}`. `tail` is 1 or more, default 200, capped at 1000. `lines` is empty and `note` explains why when there is no pod or the logs can't be read. Owner or admin. |
| POST | `/apps/{app_id}/restart` | none | `App`. Owner or admin. 409 if suspended. Re-resolves the image tag first ("pull latest"); a new image that fails the checks is skipped and the old one restarted (`image_update_error` is set). |
| POST | `/apps/{app_id}/check-image` | none | `App`. Owner or admin. Re-resolves the tag now and redeploys if it changed and passes the checks. 502 if the registry can't be reached; 409 if suspended. |
| PUT | `/apps/{app_id}/domain` | `{"host": "blog.example.com"}` | `App`. Attaches/replaces a custom domain (owner). Generates the ownership token; returns it in `custom_domain`. 422 for an invalid host or one of our own domains; 409 if the host is attached to another app; 403 if suspended; 404 if custom domains are disabled platform-wide. |
| POST | `/apps/{app_id}/domain/verify` | none | `App`. Checks the ownership TXT and that the host resolves to the platform LB. On success `custom_domain.status` is `active` and the app requeues (cert issues, host served). On failure returns 200 with `custom_domain.error` set and status still `pending`. 400 if no domain is set. |
| DELETE | `/apps/{app_id}/domain` | none | `App`. Detaches the custom domain (owner); if it was live the app requeues so the custom Ingress + cert are torn down. The generated subdomain is unaffected. |
| POST | `/apps/spec` | `{spec, secrets}` | Create-or-update from YAML; see below. |

`AppCreate`:

| Field | Type | Default | Constraints |
|---|---|---|---|
| `name` | string | required | 1 to 255 |
| `image` | string | required | 1 to 512 |
| `port` | int | required | 1 to 65535 |
| `want_db` | bool | `false` | |
| `want_storage` | bool | `false` | |
| `env` | list of `EnvVar` | `[]` | no duplicate keys |
| `health_check_enabled` | bool | `false` | |
| `health_check_path` | string | `/healthz` | max 255; see the path rules in USER_GUIDE.md |
| `auto_update` | bool | `true` | redeploy when the image tag changes (ignored for digest refs) |

`AppUpdate`: optional `name, image, port, env, health_check_enabled, health_check_path, auto_update` (same constraints).
`want_db` and `want_storage` cannot be changed. `env`, when sent, replaces the whole list; send a secret back with
value `********` to keep it.

`EnvVar` (input): `{key, value, is_secret=false}`. `key` matches `^[A-Z_][A-Z0-9_]*$`. A `value` that is exactly
`${{ ref }}` is a reference; allowed refs: `db.host, db.port, db.name, db.user, db.password, db.url, db.ca_cert,
db.tls, storage.endpoint, storage.region, storage.bucket, storage.access_key, storage.secret_key, app.url`. `app.url` is always available (the app's public `https://<hostname>.<domain>` URL). `db.*` needs
`want_db`, `storage.*` needs `want_storage`.

Image checks on create, update (when `image` is sent) and spec: registry must serve it publicly (422 "Couldn't find that image, or it's private..."), the USER must be non-root (422; a named user runs as UID 10001), and the summed compressed layers at most 512 MB
(422 "Image is N MB; the limit is 512 MB...").

### Floating tags (`:latest`)

The image is resolved to a manifest digest (the linux/amd64 one for multi-arch images) on create and update, and the
Deployment runs `repo@sha256:...`; `image_digest` is that digest and the tag you typed stays in `image`. Every
10 minutes the platform re-resolves tags of apps with `auto_update=true` (not suspended, not digest refs). A changed
digest is re-checked (size, non-root); if it passes, the app goes `pending` and rolls out (audit `app.image_updated`). If it
fails, the previous version keeps running and `image_update_error` explains why until a good image is pushed.
`image_checked_at` is the last successful registry check. To pin, use a digest reference (`repo@sha256:...`) or set
`auto_update=false` and deploy changes yourself with `check-image`, `restart` or a PATCH.

### `POST /apps/spec`

Request: `{"spec": "<YAML, max 64KB>", "secrets": {"NAME": "value"}}` (`secrets` optional; each key must be in the
spec's `secrets` list).

Response: 200 `App` plus `created: bool` (200 for both create and update). The app is matched by
`(you, spec.name)`; if it exists it is updated, declaratively: env, secrets and health check become exactly what
the spec says (missing ones are removed; a secret still listed but not resupplied keeps its value).
Errors: 422 for YAML/spec errors (message lists `field: problem` in plain words, e.g. `port: is required`),
values given for undeclared secrets, missing secret values, trying to remove a database or bucket from an existing
app, or an invalid `${{ }}` reference; 403 when the app is suspended or the app limit is reached. On create the app
limit is checked first, so at the limit you get the 403 before any image or env error. The YAML format is in USER_GUIDE.md section 4.

## Admin (`/admin`, admin only)

| Method | Path | Body | Response / notes |
|---|---|---|---|
| GET | `/admin/users` | none | List of `{id, email, role, app_limit, app_count, status, created_at, terms_accepted_at}` (excludes deleted). |
| PATCH | `/admin/users/{id}` | `{app_limit: int >= 0}` | Updated user. Audited as `user.update`. |
| POST | `/admin/users/{id}/approve` | none | `pending` to `active`; 409 if the user is not pending (the message says whether they were already approved). |
| POST | `/admin/users/{id}/reset-password` | none | 200 `{"temporary_password": "..."}`. Sets a random 16-character password, shown once, and flags the user `must_change_password`. Audited as `user.reset_password` (the password is never logged). Existing sessions are not revoked. Hand the password over out of band. |
| POST | `/admin/users/{id}/suspend` | none | User `suspended`; all their live apps `suspended` (scaled to zero). 400 for the platform admin. |
| POST | `/admin/users/{id}/unsuspend` | none | User `active`; apps suspended through the user go back to `pending`. 409 if the user is not suspended. |
| DELETE | `/admin/users/{id}` | none | 204. Soft-deletes the user, their apps (reconciler tears down) and revokes their API keys. 400 for the platform admin. |
| GET | `/admin/apps` | none | All live apps: `{id, name, hostname, status, owner_id, owner_email, image, created_at, resources}`. |
| PATCH | `/admin/apps/{id}/resources` | any of `{cpu_request_m, cpu_limit_m, mem_request_mi, mem_limit_mi}` (ints; `null` resets a field to the default) | Updated admin app. CPU must be 10 to 1000m and memory 32 to 2048Mi. 422 if a request is above its limit. Sets the app to `pending` so it redeploys (a suspended app picks it up when unsuspended). Audited as `app.resources`. |
| POST | `/admin/apps/{id}/suspend` | none | App `suspended`. |
| POST | `/admin/apps/{id}/unsuspend` | none | App to `pending`; 409 if the app is not suspended. |
| DELETE | `/admin/apps/{id}` | none | 204; works even on suspended apps. |
| GET | `/admin/audit?limit=100` | none | Newest first; `limit` 1 to 1000. Items: `{id, at, actor_email, action, target_type, target_id, detail}`. |
| GET | `/admin/db-servers` | none | `{id, name, host, port, status, max_databases, used, pct, warn}`; `warn` at 80% or more. |
| POST | `/admin/db-servers` | `{name, host, port=3306, admin_user, admin_password, max_databases, ca_cert?}` | 201 server. 409 if the name exists. Status starts `active`. `ca_cert` is optional PEM text (max 20000 characters). Audited as `dbserver.create`. |
| PATCH | `/admin/db-servers/{id}` | `{status?: active\|draining\|full, max_databases?: int > 0, ca_cert?: string}` | Updated server. An empty `ca_cert` clears it. `full` is a legacy value that the UI hides; it stops new placements, same as `draining`. Audited as `dbserver.update`. |
| GET | `/admin/health` | none | Cluster dashboard JSON, cached 15 s: `generated_at, metrics_available, errors, cluster{node_count,max_nodes,pods_total}, nodes, apps[], platform[], db_servers[]`. The payload may gain fields; see `app/health.py`. |

Audit actions recorded: `user.register`, `user.register_duplicate`, `login.failed`, `user.change_password`,
`user.reset_password`, `user.update`, `user.approve`, `user.suspend`, `user.unsuspend`, `user.delete`, `apikey.create`,
`apikey.revoke`, `app.create`, `app.update`, `app.delete`, `app.rotate`, `app.restart`, `app.image_updated`,
`app.resources`, `app.storage.delete`, `app.suspend`, `app.unsuspend`, `dbserver.create`, `dbserver.update`. Never contains secrets; `detail` is capped at 255 characters.

## Not an API

The reconciler (every 15 s), the database quota poll (every 60 s), the storage quota poll (every 5 min) and the image watcher (every 10 min) run inside the platform process when
`RECONCILER_ENABLED=1`; there are no endpoints to trigger them.
