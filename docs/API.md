# PicoPaaS API

Base URL: `https://platform.picopaas.xyz`. JSON in, JSON out. FastAPI also serves interactive docs at
`/docs` and the schema at `/openapi.json`.

## Endpoint index

Auth: **public**, **user** (any logged-in non-deleted user), **active** (status `active`), **owner** (active and the
app is yours; administrators can also use the ones marked "owner or admin"), **admin** (`platform_admin`). API keys work
everywhere a user is accepted except where marked "session only".

| Method | Path | Auth |
|---|---|---|
| GET | `/healthz` | public |
| POST | `/auth/register`, `/auth/login`, `/auth/logout` | public |
| GET | `/auth/me` | user |
| POST | `/auth/change-password` | user |
| POST, GET, DELETE | `/api/keys`, `/api/keys`, `/api/keys/{key_id}` | active (create and revoke: session only) |
| GET | `/templates` | active |
| POST, GET | `/apps`, `/apps` | active |
| POST | `/apps/spec` | active |
| GET, PATCH, DELETE | `/apps/{app_id}` | owner |
| POST | `/apps/{app_id}/rotate-credentials` | owner |
| PUT, DELETE | `/apps/{app_id}/domain` | owner |
| POST | `/apps/{app_id}/domain/verify` | owner |
| GET | `/apps/{app_id}/logs`, `/events`, `/metrics-history`, `/image-history` | owner or admin |
| GET | `/apps/{app_id}/database/usage`, `/storage/usage` | owner or admin |
| POST | `/apps/{app_id}/storage/delete` | owner or admin |
| POST | `/apps/{app_id}/restart`, `/check-image`, `/rollback` | owner or admin |
| GET | `/admin/users`, `/admin/apps`, `/admin/audit`, `/admin/db-servers`, `/admin/health`, `/admin/docs/admin-guide` | admin |
| PATCH | `/admin/users/{user_id}` | admin |
| POST | `/admin/users/{user_id}/approve`, `/suspend`, `/unsuspend`, `/reset-password` | admin |
| DELETE | `/admin/users/{user_id}`, `/admin/apps/{app_id}` | admin |
| POST | `/admin/apps/{app_id}/suspend`, `/unsuspend` | admin |
| PUT, DELETE | `/admin/apps/{app_id}/platform-host` | admin |
| PUT | `/admin/apps/{app_id}/replicas` | admin |
| PATCH | `/admin/apps/{app_id}/resources` | admin |
| POST, PATCH | `/admin/db-servers`, `/admin/db-servers/{server_id}` | admin |

## Authentication

| Method | How |
|---|---|
| Session cookie | `POST /auth/login` or `/auth/register` sets an HttpOnly `session` cookie (JWT, 7 days, `SameSite=Lax`, `Secure` outside dev). The UI uses this. |
| API key | `Authorization: Bearer pp_sk_...` (created on the **API keys** page — browser only). A `full` key works on every endpoint that accepts a user; a `read` key is limited to safe methods (see API keys below). The cookie is checked first. |

Auth levels used below:

- **public**: no auth.
- **user**: any logged-in, non-deleted user (including pending and suspended).
- **admin**: role `platform_admin`; anyone else gets `403` "This needs administrator access." (API keys of an
  administrator work too; a `read` key can only call the GET endpoints.)
- **active**: user with status `active`. Pending gets `403` "Your account is waiting for administrator approval..."; anything
  else gets `403` "Your account is suspended and your apps are stopped...".

Missing auth is `401` "You're logged out. Log in again, or check that your API key (PICOPAAS_TOKEN) is valid and not revoked."

Error `detail` strings are written to be shown to people as-is (what happened, why, what to do) and may be reworded;
match on the status code, not the text.

Request bodies reject unknown fields (422). Common errors: 401 not logged in, 403 forbidden, 404 not found
(also used for other people's apps), 409 conflict, 422 validation, 502 an upstream service (registry, database server,
bucket) failed.

An example call that works with an API key:

```bash
curl -sS https://platform.picopaas.xyz/apps -H "Authorization: Bearer $PICOPAAS_TOKEN"
```

## Scripting the API

The web UI and the API share the same host. When scripting, request JSON and do not send a browser-style
`Accept: text/html` or `Sec-Fetch-Mode: navigate` header on GET, or the request may return the UI's HTML instead of JSON.

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
| GET | `/auth/me` | user | none | `User` plus `max_apps`, `app_limit_effective`, `features` and `limits` |
| POST | `/auth/change-password` | user (any status) | `{current_password, new_password}` (new: 8 to 256, must differ) | 204. 400 if `current_password` is wrong; 422 for a bad new password. Clears `must_change_password`. Audited as `user.change_password`. |

`User`: `id, email, role ("user"|"platform_admin"), app_limit, status ("pending"|"active"|"suspended"|"deleted"),
created_at, must_change_password`. New users get `app_limit` 1. `must_change_password` is set by an admin password
reset and informational only (the server does not block other calls); the UI should send the user to change it.

`/auth/me` adds:
- `max_apps`: the platform ceiling on `app_limit`. `app_limit_effective`: the number of apps this user may have
  (administrators are exempt from the check on create).
- `features: {databases, storage, db_engines}`: `databases` is true when a new database could be placed now (an `active`
  database server with a free slot; draining and full servers don't count). `db_engines` lists the engines that have room
  now, a subset of `["mysql", "pg"]` (`databases` is true when it is non-empty); use it for the engine picker. `storage` is true when the platform has bucket
  credentials configured. Hide the matching checkbox when false.
- `limits: {cpu_limit_m, mem_limit_mi, cpu_request_m, mem_request_mi, image_mb, db_mb, bucket_mb, ephemeral_mi, egress_mbit,
  override: {cpu_min_m, cpu_max_m, mem_min_mi, mem_max_mi}}`: the platform defaults (`image_mb`, `db_mb`, `bucket_mb`
  and `ephemeral_mi` (the scratch disk) are per-app defaults an administrator can raise) and the CPU/memory range an
  administrator may override per app.

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

## Templates (`/templates`, active)

`GET /templates` returns the static catalog of one-click starters: a list of `{id, name, description, image, port,
wants_db, wants_storage, env{KEY: value}, notes, docs_url}` (`env` values may be `${{ ref }}` references). Today:
`ghost`, `static-site`, `podinfo`. A template is just a prefilled `POST /apps` body; nothing is created.

## Apps (`/apps`, active; scoped to the caller's own apps)

`App` response: `id, owner_id, name, hostname (full, e.g. clever-robin-a1b2c.picopaas.xyz), image, port,
image_digest, auto_update, image_checked_at, image_update_error, want_db, db_engine (`mysql`|`pg`), want_storage, health_check_enabled, health_check_path, health_check_timeout_seconds, replicas, status, status_detail, status_reason, retry_at, created_at, last_event, resources, quota, custom_domain, platform_subdomain, platform_host, env[]`.
`replicas` is set by an administrator only (default 1). `platform_subdomain` / `platform_host` are an admin-assigned fixed label and its full host (for example `blog.picopaas.xyz`), or null; the generated `hostname` keeps working. `db_engine` is only meaningful when `want_db` is true.
`custom_domain` is null when none is attached, else `{host, status (pending|active|error), verified_at, error, challenge_host, challenge_token, cname_target}` — the last three are the DNS records the user must create. `status_detail` is a sentence for people; `status_reason` is a machine code for `error` (for example `image_pull`, `crash_loop`, `oom`, `not_ready`, `disk`, `timeout`, `root_image`, `config`, `quota`, `env_invalid`, `unreadable_secrets`, `db_capacity`, `db_failed`, `storage_failed`, `unknown`); it is null unless `status` is `error`.
`quota` is `{db, storage}`, each `{size_bytes, limit_bytes, over_quota}` or null when the app has no such resource; it comes from the last quota poll (DB every 60s, storage every 5 min), no live calls. `last_event` is `{at, kind, message}` for the newest restart event (`disk`, `evicted`, `oom`, `crashed`) if it is under 24 hours old, else null. `retry_at` is when an app in `error` will next be retried (or null). `resources` is
`{cpu_request_m, cpu_limit_m, mem_request_mi, mem_limit_mi, ephemeral_limit_mi, db_max_connections, db_quota_mb,
storage_quota_mb, image_size_mb, egress_mbit, overridden, exceeds_platform_caps, defaults}`: the effective per-app values (platform
defaults unless an administrator overrode them) and `defaults`, the platform default of each field for this app's DB engine.
Each `env` item: `{key, value, is_secret, undecryptable}`. Secret values are returned as `********`. If a stored
value cannot be decrypted (platform key changed) `undecryptable` is true and `value` is empty.

Statuses: `pending, deploying, running, error, suspended, deleted`.

| Method | Path | Body | Response / notes |
|---|---|---|---|
| POST | `/apps` | `AppCreate` | 201 `App` (status `pending`). 403 "You've reached your limit of N apps..." (the limit is `app_limit_effective` from `/auth/me`; administrators are exempt); 422 for image or env problems; 409 if it couldn't reserve a hostname (retry). |
| GET | `/apps` | none | List of `App`, newest first. |
| GET | `/apps/{app_id}` | none | `App`; 404 "We can't find that app..." (also for other people's apps). |
| PATCH | `/apps/{app_id}` | `AppUpdate` | `App` (owner only; others, including administrators, get 404). 403 "This app is suspended by an administrator..." Changing image, port, env or health check sets status `pending` and redeploys. |
| DELETE | `/apps/{app_id}` | none | 204. Soft delete; the reconciler tears down the namespace, database and bucket. 403 if the app is suspended. Owner only (administrators use `DELETE /admin/apps/{app_id}`). |
| POST | `/apps/{app_id}/rotate-credentials` | `{"target": "db"\|"storage"}` | `App` (status `pending`). 409 "Your database is still being created..." or the storage equivalent (also when the app has no database or bucket); 502 "Couldn't rotate the credentials, so the old ones still work..."; 403 if suspended. |
| GET | `/apps/{app_id}/database/usage` | none | Live `{size_bytes, limit_bytes, over_quota}`. 404 if the app has no database; 502 if the DB server is unreachable. Owner or admin. |
| GET | `/apps/{app_id}/storage/usage` | none | Live `{size_bytes, limit_bytes, over_quota, objects, top_prefixes[{prefix, size_bytes, objects}]}` (top 5 first-path-segment prefixes). 404 if no bucket. Owner or admin. |
| POST | `/apps/{app_id}/storage/delete` | `{"prefix": "..."}` or `{"all": true}` (exactly one; 422 otherwise) | `{deleted, size_bytes, truncated, over_quota}`. Deletes with the platform's credentials (an over-cap app's own key is read-only), at most 5000 objects per call (`truncated` = more remain). Re-measures; if now under the cap the read-write key is restored immediately. Audit-logged as `app.storage.delete`. Owner or admin. |
| GET | `/apps/{app_id}/events` | none | Recent events, newest first, at most 20: `[{at, kind, message}]`. `kind` is `disk` (over the app's local scratch disk, 250 MB by default), `evicted` (other eviction), `oom` (out of memory) or `crashed` (non-zero exit; the message has the code) or `rollback` (a new image failed to start and the previous version was restored). Recorded once per occurrence by the reconciler. Owner or admin; others get 404. |
| GET | `/apps/{app_id}/logs?tail=N&pod=NAME` | none | `{lines[], pod, note, pods[], sections[{pod, lines[], note, restarts}]}`. Multi-pod aware: with no `pod`, every pod is read; `sections` is per pod, `pods` lists the names you can pass as `?pod=` (unknown name: 404), and `lines` is the flat view grouped under `=== pod NAME ===` headers (`pod` is null when several pods are merged, the name when one). `tail` is per pod, 1 or more, default 200, capped at 1000. `lines` is empty and `note` explains why when there is no pod or the logs can't be read. Owner or admin. |
| GET | `/apps/{app_id}/metrics-history` | none | `{points[{ts, cpu_m, mem_mi, pods, per_pod{POD:{cpu_m,mem_mi}}}], cpu_limit_m, mem_limit_mi, replicas, pods, cpu_capacity_m, mem_capacity_mi}`. Point `cpu_m`/`mem_mi` are SUMS over `pods` pods; `cpu_limit_m`/`mem_limit_mi` are PER POD. Draw usage against `cpu_capacity_m`/`mem_capacity_mi` (= max(replicas, latest pods) x per-pod limit); a past point's own capacity is `point.pods x` per-pod limit. `per_pod` is empty on older samples. Owner or admin. |
| POST | `/apps/{app_id}/restart` | none | `App`. Owner or admin. 409 if suspended. Re-resolves the image tag first ("pull latest"); a new image that fails the checks is skipped and the old one restarted (`image_update_error` is set). |
| POST | `/apps/{app_id}/check-image` | none | `App`. Owner or admin. Re-resolves the tag now and redeploys if it changed and passes the checks. 502 if the registry can't be reached; 409 if suspended. |
| GET | `/apps/{app_id}/image-history` | none | `[{digest, image_ref, created_at, current}]`: the last 5 image versions that reached `running`, newest first; `current` marks the deployed one. Owner or admin. |
| POST | `/apps/{app_id}/rollback` | `{"digest": "sha256:..."}` | `App` (status `pending`, redeploys that version). Owner or admin. 400 if the digest isn't in the history; 409 if suspended. The version you left is not auto-updated back to until a newer image appears. A failed rollout of a never-healthy image also rolls back automatically (event `rollback`, audit `app.auto_rollback`). |
| | | | **Custom domains: change them only via these endpoints (or the UI); the platform manages the underlying routing and reverts hand edits.** |
| PUT | `/apps/{app_id}/domain` | `{"host": "blog.example.com"}` | `App`. Attaches/replaces a custom domain (owner). Generates the ownership token; returns it in `custom_domain`. 422 for an invalid host or one of our own domains; 409 if the host is attached to another app; 403 if suspended; 404 if custom domains are disabled platform-wide. Owner only. |
| POST | `/apps/{app_id}/domain/verify` | none | `App`. Checks the ownership TXT and that the host resolves to the platform LB. On success `custom_domain.status` is `active` and the app requeues (cert issues, host served). On failure returns 200 with `custom_domain.error` set and status still `pending`. 400 if no domain is set. |
| DELETE | `/apps/{app_id}/domain` | none | `App`. Detaches the custom domain (owner); if it was live the app requeues so the custom Ingress + cert are torn down. The generated subdomain is unaffected. |
| POST | `/apps/spec` | `{spec, secrets}` | Create-or-update from YAML; see below. |

`AppCreate`:

| Field | Type | Default | Constraints |
|---|---|---|---|
| `name` | string | required | 1 to 255 |
| `image` | string | required | 1 to 512 |
| `port` | int | required | 1024 to 65535 (non-root containers can't bind lower ports) |
| `want_db` | bool | `false` | |
| `db_engine` | `"mysql"` or `"pg"` | `"mysql"` | Engine hosting the database when `want_db` is true; cannot be changed later. Offer only engines in `features.db_engines`. In a YAML spec: `resources.db_engine`. |
| `want_storage` | bool | `false` | |
| `env` | list of `EnvVar` | `[]` | no duplicate keys |
| `health_check_enabled` | bool | `false` | |
| `health_check_path` | string | `/healthz` | max 255; see the path rules in USER_GUIDE.md |
| `health_check_timeout_seconds` | int or null | `null` | 1 to 90. Probe timeout for both probes (null = readiness 3 s, liveness 5 s). The probe period is raised to at least the timeout. In a YAML spec: `healthCheck.timeoutSeconds`. |
| `auto_update` | bool | `true` | redeploy when the image tag changes (ignored for digest refs) |

`AppUpdate`: optional `name, image, port, env, health_check_enabled, health_check_path, health_check_timeout_seconds (null resets), auto_update` (same constraints).
`want_db`, `db_engine` and `want_storage` cannot be changed (and `replicas` is admin-only). `env`, when sent, replaces the whole list; send a secret back with
value `********` to keep it.

`EnvVar` (input): `{key, value, is_secret=false}`. `key` matches `^[A-Z_][A-Z0-9_]*$`. A `value` that is exactly
`${{ ref }}` is a reference; allowed refs: `db.host, db.port, db.name, db.user, db.password, db.url, db.ca_cert,
db.tls, storage.endpoint, storage.region, storage.bucket, storage.access_key, storage.secret_key, app.url`. `app.url` is always available (the app's public URL — an active custom domain or reserved subdomain if set, otherwise `https://<hostname>.<domain>`). `db.*` needs
`want_db`, `storage.*` needs `want_storage`.

Image checks on create, update (when `image` is sent) and spec: registry must serve it publicly (422 "Couldn't find that image, or it's private..."), the USER must be non-root (422; a named user runs as UID 10001), and the summed compressed layers at most the app's image limit (512 MB by default, `limits.image_mb`; an administrator can
raise it per app) (422 "Image is N MB; the limit is 512 MB...").

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

Spec fields: `apiVersion: picopaas/v1`, `name`, `image`, `port` (1024 to 65535), `resources{db, db_engine (mysql|pg), storage}`,
`healthCheck{enabled, path, timeoutSeconds (1 to 90)}`, `env{}`, `secrets[]`.

Response: 200 `App` plus `created: bool` (200 for both create and update). The app is matched by
`(you, spec.name)`; if it exists it is updated, declaratively: env, secrets and health check become exactly what
the spec says (missing ones are removed; a secret still listed but not resupplied keeps its value).
Errors: 422 for YAML/spec errors (message lists `field: problem` in plain words, e.g. `port: is required`),
values given for undeclared secrets, missing secret values, trying to remove a database or bucket from an existing
app, trying to change the `resources.db_engine` of an existing app, or an invalid `${{ }}` reference; 403 when the app is suspended or the app limit is reached. On create the app
limit is checked first, so at the limit you get the 403 before any image or env error. The YAML format is in USER_GUIDE.md section 4.

## Background jobs

The platform reconciles apps, polls database and storage usage, and re-checks image tags on its own
schedule. There are no API endpoints to trigger them.

## Admin (`/admin`, admin)

Every route under `/admin` needs a `platform_admin` (401 logged out, 403 otherwise). Unknown body fields are rejected (422).
Admins can also read and operate on any app through the `/apps/{app_id}/...` routes marked "owner or admin".

### Users

`AdminUser`: `id, email, role, app_limit, app_limit_effective, app_count, status, created_at, terms_accepted_at`.

| Method | Path | Body | Response / notes |
|---|---|---|---|
| GET | `/admin/users` | none | List of `AdminUser` (not deleted), by id. |
| PATCH | `/admin/users/{user_id}` | `{"app_limit": int >= 0}` | `AdminUser`. 422 above the platform maximum (`max_apps` in `/auth/me`). |
| POST | `/admin/users/{user_id}/approve` | none | `AdminUser`. 409 unless the user is `pending`. |
| POST | `/admin/users/{user_id}/suspend` | none | `AdminUser`. Suspends their apps too. 400 for the main administrator. |
| POST | `/admin/users/{user_id}/unsuspend` | none | `AdminUser`. 409 unless suspended; apps suspended with the account come back. |
| POST | `/admin/users/{user_id}/reset-password` | none | `{temporary_password}`, shown once; sets `must_change_password`. |
| DELETE | `/admin/users/{user_id}` | none | 204. Revokes their API keys and deletes their apps. 400 for the main administrator. |

All user routes return 404 for an unknown or deleted user. Each action is audited.

### Apps

| Method | Path | Body | Response / notes |
|---|---|---|---|
| GET | `/admin/apps` | none | List of `AdminAppOut` for every live app. |
| POST | `/admin/apps/{app_id}/suspend` | none | `AdminAppOut`. |
| POST | `/admin/apps/{app_id}/unsuspend` | none | `AdminAppOut`; status goes to `pending`. 409 unless suspended. |
| DELETE | `/admin/apps/{app_id}` | none | 204. Works on suspended apps too. |
| PUT | `/admin/apps/{app_id}/platform-host` | `{"subdomain": "blog"}` | `App`. Adds a fixed `blog.<domain>` host (1 to 63 chars, normalised; 422 if invalid or reserved; 409 if another app uses it as a platform host or generated address). The app requeues. |
| DELETE | `/admin/apps/{app_id}/platform-host` | none | `App`. Removes the fixed host. |
| PUT | `/admin/apps/{app_id}/replicas` | `{"replicas": 1..3}` | `App`. The ceiling is `MAX_REPLICAS` (3). Rolls the pods; a suspended app picks it up on unsuspend. |
| PATCH | `/admin/apps/{app_id}/resources` | see below | `AdminAppOut`. |

All return 404 for an unknown or deleted app.

#### Per-app limit overrides

`PATCH /admin/apps/{app_id}/resources` takes any of these (omit = unchanged, `null` = platform default; unknown fields 422):

| Field | Range | Default |
|---|---|---|
| `cpu_request_m`, `cpu_limit_m` | 10 to 1000 | 64 / 200 |
| `mem_request_mi`, `mem_limit_mi` | 32 to 2048 | 128 / 384 |
| `ephemeral_limit_mi` (scratch disk) | 64 to 2048 | 250 |
| `db_max_connections` | 1 to 25 | 10 (MySQL), 5 (PostgreSQL) |
| `db_quota_mb` | 1 to 1024 | 50 |
| `storage_quota_mb` | 1 to 51200 | 100 |
| `image_size_mb` | 1 to 2048 | 512 |
| `egress_mbit` (outbound bandwidth, Mbit/s) | 1 to 160 | 20 |

422 if a request exceeds its limit (CPU or memory). Response (`AdminAppOut`): `id, name, hostname, status, owner_id,
owner_email, image, created_at, replicas, want_db, want_storage, db_engine (null without a database), platform_host,
custom_domain (the host string or null), exceeds_platform_caps, restarts_app, resources`. `resources` holds the
effective value of every field above plus `overridden`, `exceeds_platform_caps` and `defaults` (the platform default of
each field for this app's engine). `exceeds_platform_caps` and `restarts_app` are set only on the PATCH response;
`restarts_app` is true when the change alters the pod spec (CPU, memory, scratch disk, outbound bandwidth) so pods roll. Changing a quota
re-checks usage immediately (a raised quota restores a read-only database or bucket right away).
`AppOut.quota.*.limit_bytes` and `/apps/{id}/database|storage/usage` reflect the app's effective quotas.

### Audit log

`GET /admin/audit?limit=100` (1 to 1000, newest first): `[{id, at, actor_email, action, target_type, target_id, detail}]`.

### Database servers

`DbServer`: `id, name, host, port, status (active|draining|full), engine (mysql|pg), max_databases, used, pct, warn`
(`warn` is true at 80% or more).

| Method | Path | Body | Response / notes |
|---|---|---|---|
| GET | `/admin/db-servers` | none | List of `DbServer`. |
| POST | `/admin/db-servers` | `{name, host, port=3306, engine="mysql"\|"pg", admin_user, admin_password, max_databases, ca_cert?}` | 201 `DbServer` (status `active`). The port default is 3306 whatever the engine, so send 5432 for `pg`. `ca_cert` must be PEM (422). 409 if the name exists. The admin password is stored encrypted and never returned. |
| PATCH | `/admin/db-servers/{server_id}` | any of `{status, max_databases, ca_cert}` (`ca_cert: ""` clears it) | `DbServer`. 404 if unknown. `engine` can't be changed. |

### Health

`GET /admin/health`: the platform dashboard payload. `generated_at, warming, metrics_available, errors[], cluster{node_count,
max_nodes, pods_total}, nodes[], apps[] (per app: status, pods, restarts, CPU and memory used, per-pod limits,
replicas, aggregate capacity, pod_usage[]), platform[] (pods in the platform namespaces), db_servers[] (`DbServer`
plus live metrics), users{total, active, pending, suspended}, pending_users[], workloads{apps_total, by_status, with_db,
with_storage}`. The cluster snapshot is cached about 15 seconds; `warming` is true until the first one lands.

### Admin guide

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/admin/docs/admin-guide` | none | `{"markdown": "<docs/ADMIN_GUIDE.md>"}`. 404 if the guide isn't in this build. |

