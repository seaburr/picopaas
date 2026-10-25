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
| GET | `/apps/{app_id}/logs`, `/pods/{pod}/events`, `/events`, `/metrics-history`, `/image-history` | owner or admin |
| GET | `/apps/{app_id}/database/usage`, `/storage/usage` | owner or admin |
| POST | `/apps/{app_id}/storage/delete` | owner or admin |
| POST | `/apps/{app_id}/storage/presign` | owner or admin; PUT needs a full-scope key |
| POST | `/apps/{app_id}/restart`, `/check-image`, `/rollback` | owner or admin |
| GET, POST, PATCH | `/apps/{app_id}/backups` | owner or admin; POST and PATCH need a full-scope key |
| DELETE | `/apps/{app_id}/backups/{run_id}`, `/apps/{app_id}/backups/sets/{set_id}` | owner or admin; needs a full-scope key |
| POST | `/apps/{app_id}/backups/sets/{set_id}/download` | owner or admin; full-scope key |
| POST, GET | `/apps/{app_id}/backups/{run_id}/download`, `/apps/{app_id}/backups/exports`, `/apps/{app_id}/backups/exports/{export_id}` | owner or admin; POST needs a full-scope key |
| GET, PUT | `/admin/apps/{app_id}/backups` | admin; PUT needs a full-scope key (lock backups off for an app, per-app ceiling) |
| POST | `/admin/backups/{run_id}/restore` | admin, full-scope key |
| GET | `/admin/users`, `/admin/apps`, `/admin/audit`, `/admin/audit/export`, `/admin/db-servers`, `/admin/health`, `/admin/health/backups`, `/admin/health/logs[/pods|/download]`, `/admin/backups`, `/admin/docs/admin-guide` | admin |
| PATCH | `/admin/users/{user_id}` | admin |
| POST | `/admin/users/{user_id}/approve`, `/suspend`, `/unsuspend`, `/reset-password`, `/promote`, `/demote` | admin |
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
- `features: {databases, storage, volumes, db_engines}`: `volumes` is true when persistent volumes are enabled
  (`VOLUMES_ENABLED`); hide the volume option when false. (The `databases`, `storage` and `db_engines` flags:) `databases` is true when a new database could be placed now (an `active`
  database server with a free slot; draining and full servers don't count). `db_engines` lists the engines that have room
  now, a subset of `["mysql", "pg"]` (`databases` is true when it is non-empty); use it for the engine picker. `storage` (blob storage) is true when the platform has bucket
  credentials configured. Hide the matching checkbox when false.
- `limits: {cpu_limit_m, mem_limit_mi, cpu_request_m, mem_request_mi, image_mb, db_mb, bucket_mb, ephemeral_mi, egress_mbit,
  volume_max_gi, volume_default_gi, volume_override_max_gi, volume_default_mount_path, override: {cpu_min_m, cpu_max_m, mem_min_mi, mem_max_mi}}`: the platform defaults (`image_mb`, `db_mb`, `bucket_mb`
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
wants_db, db_engine, wants_storage, env{KEY: value}, secrets[{key, hint}], health_check{path, timeout_seconds}|null,
volume{size_gi, mount_path, required}|null, notes, docs_url}` (`env` values may be `${{ ref }}` references; `secrets` are
names to create as empty secret env vars, `hint` says how to generate the value; `docs_url` is the example README on GitHub). Today:
`ghost`, `wordpress`, `directus`, `grafana`, `grafana-pg`, `n8n`, `shlink`, `status`, `static-site`. A template is just a prefilled `POST /apps` body; nothing is created.

## Apps (`/apps`, active; scoped to the caller's own apps)

`App` response: `id, owner_id, name, hostname (full, e.g. clever-robin-a1b2c.picopaas.xyz), image, port,
image_digest, auto_update, image_checked_at, image_update_error, want_db, db_engine (`mysql`|`pg`), want_storage, health_check_enabled, health_check_path, health_check_timeout_seconds, replicas, volume, status, status_detail, status_reason, retry_at, created_at, last_event, resources, quota, custom_domain, platform_subdomain, platform_host, env[]`.
`replicas` is set by an administrator only (default 1). `volume` is null, or `{size_gi, mount_path, phase}` (`phase` is the
PVC phase, `Pending` or `Bound`, null until the first deploy applied it). Request one with `volume: {size_gi?, mount_path?}`
on `POST /apps` and `PATCH /apps/{id}` (defaults `1` GiB and `/data`; the whole feature is behind `VOLUMES_ENABLED`, else 422
`Persistent volumes aren't enabled on this platform.`). `mount_path` must be absolute, without `..`, not `/` and not under
`/proc /sys /dev /etc /usr /bin /sbin /lib /tmp /run`, at most 255 characters. Rules: `size_gi` is capped at `limits.volume_max_gi`
(422) and only grows (shrinking is 422; a value an administrator granted can be re-sent); `volume: null` is 422 (volumes are never
detached, delete the app instead); an app with replicas above 1 can't get a volume (409), and a volume app can't be scaled above 1
(409, also on `PUT /admin/apps/{id}/replicas`); when the platform already holds `VOLUMES_MAX_TOTAL` volumes, adding one is 409.
A volume app rolls with the `Recreate` strategy, so each deploy has a short gap. Changing the size or mount path re-deploys. `platform_subdomain` / `platform_host` are an admin-assigned fixed label and its full host (for example `blog.picopaas.xyz`), or null; the generated `hostname` keeps working. `db_engine` is only meaningful when `want_db` is true.
`custom_domain` is null when none is attached, else `{host, status (pending|active|error), verified_at, error, challenge_host, challenge_token, cname_target}` — the last three are the DNS records the user must create. `status_detail` is a sentence for people; `status_reason` is a machine code for `error` (for example `image_pull`, `crash_loop`, `oom`, `not_ready`, `disk`, `timeout`, `root_image`, `config`, `quota`, `env_invalid`, `unreadable_secrets`, `db_capacity`, `db_failed`, `storage_failed`, `unknown`); it is null unless `status` is `error`.
`quota` is `{db, storage, volume}`, each `{size_bytes, limit_bytes, over_quota}` or null when the app has no such resource; it comes from the last quota poll (DB every 60s, blob storage every 5 min), no live calls. `volume` is the measured used bytes of the persistent volume (`df` of the mount, taken by a tiny `MODE=stat` Job about every 15 min and by every volume backup; limit = volume size); null when there is no volume or it has not been measured yet. `last_event` is `{at, kind, message}` for the newest restart event (`disk`, `evicted`, `oom`, `crashed`) if it is under 24 hours old, else null. `retry_at` is when an app in `error` will next be retried (or null). `resources` is
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
| POST | `/apps/{app_id}/rotate-credentials` | `{"target": "db"\|"storage"}` | `App` (status `pending`). 409 "Your database is still being created..." or the blob storage equivalent (also when the app has no database or bucket); 502 "Couldn't rotate the credentials, so the old ones still work..."; 403 if suspended. |
| GET | `/apps/{app_id}/database/usage` | none | Live `{size_bytes, limit_bytes, over_quota}`. 404 if the app has no database; 502 if the DB server is unreachable. Owner or admin. |
| GET | `/apps/{app_id}/storage/usage` | none | Live `{size_bytes, limit_bytes, over_quota, objects, top_prefixes[{prefix, size_bytes, objects}]}` (top 5 first-path-segment prefixes). 404 if no bucket. Owner or admin. |
| POST | `/apps/{app_id}/storage/delete` | `{"prefix": "..."}` or `{"all": true}` (exactly one; 422 otherwise) | `{deleted, size_bytes, truncated, over_quota}`. Deletes with the platform's credentials (an over-cap app's own key is read-only), at most 5000 objects per call (`truncated` = more remain). Re-measures; if now under the cap the read-write key is restored immediately. Audit-logged as `app.storage.delete`. Owner or admin. |
| GET | `/apps/{app_id}/events` | none | Recent events, newest first, at most 20: `[{at, kind, message}]`. `kind` is `disk` (over the app's local scratch disk, 250 MB by default), `evicted` (other eviction), `oom` (out of memory) or `crashed` (non-zero exit; the message has the code) or `rollback` (a new image failed to start and the previous version was restored). Recorded once per occurrence by the reconciler. Owner or admin; others get 404. |
| GET | `/apps/{app_id}/logs?tail=N&pod=NAME` | none | `{lines[], pod, note, pods[], sections[{pod, lines[], note, restarts}], pod_info[{name, created_at, phase, ready, current}]}`. `pods`/`pod_info` list live (non-finished) pods only, current first, then Running, then newest; Succeeded/Failed (e.g. Evicted) pods are left out unless no live pod exists, then the newest finished one is shown so crash logs stay reachable. `pod_info` has one entry per pod in `pods`; `current` is true for running, ready pods of the newest rollout (false for old, terminating or crash-looping pods). Multi-pod aware: with no `pod`, every pod is read; `sections` is per pod, `pods` lists the names you can pass as `?pod=` (unknown name: 404), and `lines` is the flat view grouped under `=== pod NAME ===` headers (`pod` is null when several pods are merged, the name when one). `tail` is per pod, 1 or more, default 200, capped at 1000. `lines` is empty and `note` explains why when there is no pod or the logs can't be read. Owner or admin. |
| GET | `/apps/{app_id}/pods/{pod}/events?format=text\|json` | none | Kubernetes Events about one pod, oldest first. Default `text/plain` download (`Content-Disposition: attachment; filename="<app-name>-<pod>-events.txt"`): a first `#` line noting Kubernetes keeps events only about 1 hour, then one line per event: `<ISO time>  <type>  <reason>  x<count>  <message>`. `?format=json` returns `[{at, type, reason, count, message}]`. 404 if the pod isn't one of the app's pods. Owner or admin. |
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

Spec fields: `apiVersion: picopaas/v1`, `name`, `image`, `port` (1024 to 65535), `resources{db, db_engine (mysql|pg), storage, volume{sizeGi (1 to 10, default 1), mountPath (default /data)}}`,
`healthCheck{enabled, path, timeoutSeconds (1 to 90)}`, `env{}`, `secrets[]`.

Response: 200 `App` plus `created: bool` (200 for both create and update). The app is matched by
`(you, spec.name)`; if it exists it is updated, declaratively: env, secrets and health check become exactly what
the spec says (missing ones are removed; a secret still listed but not resupplied keeps its value).
Errors: 422 for YAML/spec errors (message lists `field: problem` in plain words, e.g. `port: is required`),
values given for undeclared secrets, missing secret values, trying to remove a database or bucket from an existing
app, trying to change the `resources.db_engine` of an existing app, removing `resources.volume` from an app that has one, an invalid volume request (see `POST /apps`; 409 when out of volume capacity), or an invalid `${{ }}` reference; 403 when the app is suspended or the app limit is reached. On create the app
limit is checked first, so at the limit you get the 403 before any image or env error. The YAML format is in USER_GUIDE.md section 4.

## Backups (`/apps/{app_id}/backups`, owner or admin)

Off unless the administrator enabled `BACKUPS_ENABLED` (see ADMIN_GUIDE). Covers the app's database (`db`), blob storage bucket (`bucket`) and
persistent volume (`volume`, only while the admin has set `BACKUP_KEY_SCOPED=1`; otherwise a `volume` kind is 422 "Volume backups need a bucket-scoped backup key; ask the admin." and a default backup simply leaves the volume out).
**One backup is a set**: everything one trigger produced (manual click, scheduled slot, final backup), as one run per kind linked by `set_id`. Use the set endpoints
(`backups[]`, `sets/{set_id}/download`, `DELETE sets/{set_id}`); the per-run endpoints below keep working for single parts. Scheduled backups are opt-in (`enabled` starts false unless the operator sets `BACKUP_DEFAULT_ENABLED=1`); a manual `POST` works either way. Download is below; restore is admin-only.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/apps/{app_id}/backups` | none | `{feature_enabled, configured, enabled (always false while `admin_disabled`), admin_disabled, admin_disabled_reason, admin_disabled_message, max_held, held, manual_per_hour, export_per_hour, storage_full, cap_override, schedule, state, kinds[{kind, last_backup_at, last_status, last_error, last_bytes, repo_bytes}], last_backup_at, last_success_at, last_status, last_error, next_backup_at, repo_bytes (this app's backup storage in use), cap_bytes (its effective ceiling: the admin's per-app override, else `backup_max_bytes_per_app`), backups[], volume_backups{has_volume, available, note}, runs[]}`. `backups[]` is the app's backups, newest first (30), each `{id, trigger (scheduled, manual, final), created_at, finished_at (null while a part is queued or running), status, size_bytes, parts[{run_id, kind (db, bucket, volume), status, bytes_added, error}], export}`. `status` is `running` (any part queued or running), `succeeded` (all parts), `partial` (some failed: see each part's `error`), `failed` (all), or `deleting`; deleted backups are not listed. `size_bytes` is the new data the succeeded parts stored (restic deduplicates, so later backups add little). `export` is the set's latest live package download `{id, status (queued, running, ready), available_until}` or null; `failed_export` `{id, error, at}` is the latest download that FAILED in the last hour (only when `export` is null): show its `error`. `volume_backups.note` explains why a volume is left out. `runs[]` is the same parts flat (each also has `set_id`). `state` is `disabled` (scheduled backups off), `pending`, `ok`, `stale` or `failing`. A run is `{id, kind, trigger (scheduled, manual, final, restore), status (queued, running, succeeded, failed), created_at, started_at, finished_at, bytes_added, repo_bytes, snapshot_id, error, source_run_id, export}`, newest first (30). `export` is the run's latest live download `{id, status (queued, running, ready), available_until (once ready)}` or null; `failed_export` as on a backup. Works with `feature_enabled: false` when backups are off. Read-scope keys may call it. |
| POST | `/apps/{app_id}/backups` | `{kinds?: ["db"\|"bucket"\|"volume"]}` (default: everything the app has and can back up) | 202, a list of queued runs (one set; each run has `set_id`). 422 backups off, or nothing to back up; 503 enabled but not configured; 403 a platform admin disabled backups for the app ("Backups for this app were disabled by a platform admin. Existing backups are kept."); 409 a run is already queued or running, or this app's backup storage is full ("Backup storage for this app is full (X of Y)."; the oldest backup is rolled off to make room, try again shortly); 429 (with `Retry-After`) past `backup_manual_per_hour` manual backups per app per hour ("You can start N backups per hour for this app.") or past 5x that across the account's apps. Audit `backup.create` (`trigger=manual set=<id> kinds=...`). Not for read-scope keys (403). |
| POST | `/apps/{app_id}/backups/sets/{set_id}/download` | none | Prepare the WHOLE backup as ONE `.tar.gz` package: `MANIFEST.json` (`{format, app, set_id, created_at, engine, picopaas_version, parts[{kind, file, snapshot, bytes, sha256}]}`) plus `database.sql.gz` or `database.pgdump`, `blob-storage.tar.gz`, `volume.tar.gz` (volume files under `volume/`) for the parts that succeeded. 202 in the export shape below with `set_id` set, `run_id` 0 and `kind: "package"`; poll `GET .../exports/{export_id}` exactly like a single-run download (fresh 10-minute presigned URL per call, file kept one hour). Counts as one of the 3 downloads per app per hour. Asking again while a package is live returns it. 404 unknown, deleted or another app's set; 409 a part is still running, the set is being deleted or another download is being prepared; 410 repository purged; 422 no part succeeded; 429 limit; 503 not configured. A volume over `BACKUP_VOLUME_EXPORT_MAX_BYTES` (1 GiB) makes the Job fail with "the volume backup is too big to download as one file". Audit `backup.download` (`set=<id> kinds=... export=<id>`). Not for read-scope keys. |
| DELETE | `/apps/{app_id}/backups/sets/{set_id}` | none | Delete the whole backup: each succeeded part's snapshot is forgotten and pruned (failed parts just disappear). 202 `{set_id, status: "deleting"\|"deleted", delete_run_ids[]}`. The set shows `status: "deleting"` until the last Job ends, then disappears; if some part could not be deleted the set returns with the parts that remain, and the app gets a `backup_failed` event. 404 unknown/deleted; 409 a part is queued or running, a download of it is being prepared, or another backup/restore/download/delete of the same kind is active; 410 repository purged; 403 read-scope key. A ready package of the set is removed with it. Audit `backup.delete` (`set=<id> runs=... kinds=...`), `backup.export_deleted`, then per part `backup.deleted` / `backup.delete_failed` (system, with `set=<id>`). |
| PATCH | `/apps/{app_id}/backups` | `{enabled?: bool, schedule?: "daily"\|"weekly"\|"off"}` | The same payload as GET. Audit `backup.config_changed`. 422 if backups are off or the app has no database or volume. 403 when a platform admin locked backups (turning them on, or a daily/weekly schedule); turning them off is always allowed. |
| POST | `/apps/{app_id}/backups/{run_id}/download` | none | 202 `{export_id, run_id, kind, status, filename, size_bytes, url, url_expires_in, available_until, error, created_at}`. `run_id` must be a succeeded `db`, `bucket` or `volume` run of this app (`.sql.gz` / `.pgdump` / `.tar.gz`). `status` is `queued`, `running`, `ready`, `failed` or `expired`; `url` is only set on `ready`. Asking again for a snapshot with a live export returns the same export. 404 unknown run; 409 another download is being prepared for this app; 410 repository purged; 422 backups off or the run is not downloadable; 429 (with `Retry-After`) past `backup_export_max_per_hour` downloads in an hour ("You can prepare N backup downloads per hour for this app"; FAILED downloads don't count); 503 not configured. A failed export's `error` says why (an out-of-date backup image on the platform is reported as such). Audit `backup.download` (then `backup.export_succeeded` / `backup.export_failed`, actor system). Not for read-scope keys. |
| GET | `/apps/{app_id}/backups/exports` | none | List of the app's downloads that have not expired (queued, running, ready, and failed ones from the last hour), newest first, in the download shape above with `url` always null. Call `GET .../exports/{export_id}` for a fresh 10-minute link; it works again and again until `available_until` (one hour after the file was prepared). Scheduled and manual runs download alike. Read-scope keys may call it. Another user's app is 404. |
| GET | `/apps/{app_id}/backups/exports/{export_id}` | none | The same object. When `status` is `ready`, `url` is a fresh presigned GET valid `url_expires_in` (600) seconds and `filename` is the suggested name (`<app>-<kind>-backup<run>.sql.gz` / `.pgdump` / `.tar.gz`). The file is deleted `available_until` (one hour after it was prepared); after that `expired`. Read-scope keys may call it. |
| DELETE | `/apps/{app_id}/backups/{run_id}` | none | Delete one backup (its restic snapshot, then prune). 202 `{run_id, status: "deleting", delete_run_id}`. Only a succeeded `db`, `bucket` or `volume` backup (trigger `manual`, `scheduled` or `final`) that has a snapshot: 404 for an unknown run, another app's run or one already deleted, 422 for anything else (failed, queued, running, restore, export), 403 for a read-scope key. 409 while a backup, restore (into this app, or from this repo into another), download or delete is queued or running for that repo of that kind, or if a download of this run is still being prepared. A finished download of the run is removed with it (its link stops working; the export shows `expired`). While the Job runs the run shows `status: "deleting"` in `GET /apps/{app_id}/backups`; on success it disappears from `runs[]` (the row is kept internally as `deleted`), on failure it returns to `succeeded` and the app gets a `backup_failed` event. Audit `backup.delete` (you), then `backup.deleted` / `backup.delete_failed` (system). |

`PUT /admin/apps/{app_id}/backups` (admin, full-scope key): body (send only what changes) `{admin_disabled?: bool, reason?: string (required, max 255, when disabling; shown to the owner), max_bytes?: int|null (per-app storage ceiling, at least 1 MiB and not above `backup_max_bytes_total`; null clears it)}`. While disabled no scheduled or manual backup runs, the owner cannot turn backups on, and existing backups are kept (still downloadable and deletable). Returns the same payload as `GET /apps/{id}/backups`. `GET /admin/apps/{app_id}/backups` returns that payload without changing anything. Audit `backup.admin_disabled` / `backup.admin_enabled` / `backup.ceiling_set`. Owner toggles are audited as `backup.config_changed`.

Retention (system audit rows): after each tick an app holding more than `backup_max_held_per_app` backups, or at its storage ceiling, has backups rolled off (`backup.rolled_off`) through the owner-delete machinery: failed and partial backups first (oldest first), then the oldest successful ones; the newest successful backup is never rolled off. A scheduled backup that cannot start because the app or the platform is at its ceiling is recorded as a failed backup carrying the reason.

`GET /admin/backups` (admin): `{enabled, configured, cap_per_app_bytes, cap_total_bytes, total_repo_bytes, warning, counts{ok, pending, stale, failing, disabled}, apps[], retained[]}`; `retained` lists deleted apps whose repositories are still within their 14 days. `GET /admin/health` also carries a compact `backups` block.

`POST /admin/backups/{run_id}/restore` (admin, full-scope key): body `{target_app_id: int, overwrite_source?: bool}`. Restores a succeeded `db` or `bucket` run into the target app's database or bucket, replacing its contents; 202 with the queued run (`trigger: "restore"`, `source_run_id`), which belongs to the target app and shows in `GET /apps/{target}/backups`. 403 for non-admins and read-scope keys; 404 unknown run or target; 409 target is the source app (unless `overwrite_source` is true and that app is suspended) or the target has a backup/restore active; 410 the repository was purged; 422 backups off, the run did not succeed, kind not restorable (`volume`), or the target lacks a database/bucket (or its bucket is over quota); 503 not configured. Audit `backup.restore`, then `backup.restore_succeeded` or `backup.restore_failed`.

## Background jobs

The platform reconciles apps, polls database and blob storage usage, and re-checks image tags on its own
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
| POST | `/admin/users/{user_id}/promote` | none | `AdminUser` with `role: platform_admin`. 409 unless the user is `active`, or if they are already an administrator. Audited as `user.promote`. |
| POST | `/admin/users/{user_id}/demote` | none | `AdminUser` with `role: user`. 409 if they aren't an administrator, or if it would leave zero active administrators. Demoting yourself works only when another active administrator exists. Audited as `user.demote`. |
| DELETE | `/admin/users/{user_id}` | none | 204. Revokes their API keys and deletes their apps. 400 for any administrator (demote first); 409 for the last active administrator. |

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
| `volume_size_gi` (persistent volume, GiB) | 1 to 10 (`OVERRIDE_VOLUME_MAX_GI`), **grow-only**, app must already have a volume | 1 (user ceiling `VOLUME_MAX_GI`) |

422 if a request exceeds its limit (CPU or memory). Response (`AdminAppOut`): `id, name, hostname, status, owner_id,
owner_email, image, created_at, replicas, volume (`{size_gi, mount_path, phase}` or null), want_db, want_storage, db_engine (null without a database), platform_host,
custom_domain (the host string or null), exceeds_platform_caps, restarts_app, resources`. `resources` holds the
effective value of every field above plus `overridden`, `exceeds_platform_caps` and `defaults` (the platform default of
each field for this app's engine). `exceeds_platform_caps` and `restarts_app` are set only on the PATCH response;
`volume_size_gi` is not nullable (a volume can't be reset or shrunk: 422) and growing it re-applies the claim without rolling pods (`restarts_app` false). `restarts_app` is true when the change alters the pod spec (CPU, memory, scratch disk, outbound bandwidth) so pods roll. Changing a quota
re-checks usage immediately (a raised quota restores a read-only database or bucket right away).
`AppOut.quota.*.limit_bytes` and `/apps/{id}/database|storage/usage` reflect the app's effective quotas.

### Recovery CLI

Not an HTTP API. Break-glass commands, run in the API pod (`kubectl exec -it deploy/<api> -- python -m app.manage ...`):
`grant-admin <email>` (promote an existing user and activate them), `list-admins`, `reset-password <email>` (prints a
temporary password once and sets `must_change_password`). They use the app's `DATABASE_URL` and audit as actor `manage-cli`.

### Audit log

`GET /admin/audit?limit=100` (1 to 1000, newest first): `[{id, at, actor_email, action, target_type, target_id, detail}]`.

`GET /admin/audit/export?format=csv|json` (default `csv`): download of up to 50,000 rows, newest first, same fields as above, as an attachment named `picopaas-audit-YYYYMMDD.csv|json`. CSV cells starting with `=`, `+`, `-`, `@`, tab or CR get a leading `'` (spreadsheet formula injection). The response is streamed (same shape as before; JSON is one array). Each download is audited as `audit.export` before streaming starts (`rows=` is the count to be sent, capped at 50,000). Rows older than `AUDIT_RETENTION_DAYS` (default 90; 0 = keep forever) are deleted daily by the leader, which writes one `audit.purge` row (actor `system`, detail `cutoff=<iso> rows=<n>`). Admin only.

### Database servers

`DbServer`: `id, name, host, port, status (active|draining|full), engine (mysql|pg), max_databases, used, pct, warn`
(`warn` is true at 80% or more).

| Method | Path | Body | Response / notes |
|---|---|---|---|
| GET | `/admin/db-servers` | none | List of `DbServer`. |
| POST | `/admin/db-servers` | `{name, host, port=3306, engine="mysql"\|"pg", admin_user, admin_password, max_databases, ca_cert?}` | 201 `DbServer` (status `active`). The port default is 3306 whatever the engine, so send 5432 for `pg`. `ca_cert` must be PEM (422). 409 if the name exists. The admin password is stored encrypted and never returned. Before saving, the platform connects as `admin_user` (TLS, `ca_cert` as in provisioning, 5 s timeout) and runs `SELECT 1`; on failure 422 with a classified detail (`Couldn't connect to host:port as user: authentication failed` / `connection timed out` / `connection refused` / `TLS handshake failed` / `host name could not be resolved`) and nothing is saved. |
| PATCH | `/admin/db-servers/{server_id}` | any of `{status, max_databases, ca_cert, admin_password}` (`ca_cert: ""` clears it; `admin_password` is write-only, min 1 char) | `DbServer`. 404 if unknown. `engine` can't be changed. Changing `admin_password` or `ca_cert` runs the same connection check as create (422 on failure, nothing saved, old password kept); the new password is stored encrypted, never returned, and the audit row says `admin_password=changed` without the value. Cached health metrics for the server are dropped. |

### Health

`GET /admin/health`: the platform dashboard payload. `generated_at, warming, metrics_available, errors[], cluster{node_count,
max_nodes, pods_total}, nodes[], apps[] (per app: status, pods, restarts, CPU and memory used, per-pod limits,
replicas, aggregate capacity, pod_usage[]), platform[] (pods in the platform namespaces), db_servers[] (`DbServer`
plus live metrics), users{total, active, pending, suspended, admins} (all non-deleted accounts, admins included; `admins` is a subset), pending_users[], workloads{apps_total, by_status, with_db,
with_storage, by_db_engine{mysql, postgres}}`. The cluster snapshot is cached about 15 seconds; `warming` is true until the first one lands.

### Health: backups and platform logs (admin)

| Method | Path | Returns |
|---|---|---|
| GET | `/admin/health/backups?days=7` | `{enabled, configured, days, storage{used_bytes, cap_bytes, used_pct, apps_with_backups, over_cap, top_apps[{app_id, name, owner_email, deleted, purge_after (ISO, deleted apps only, else null), repo_bytes, db_bytes, volume_bytes, bucket_bytes, cap_bytes, full, admin_disabled}], warning, note}, failures[{run_id, app_id, app_name, app_deleted, owner_email, kind (db/bucket/volume), trigger, status, error, at, still_failing}], stuck[{... same fields, age_seconds}], sweeper{last_run_at, cleaned{counts by type}, errors[], orphan_repos[app ids]}}`. `sweeper` is the last orphan-sweep result (`last_run_at` null until the first sweep). `days` 1 to 30. Failures skip deleted apps; purged or empty deleted apps are left out of `top_apps`. Sizes are the repository sizes recorded by each app's last backup (deduplicated; excludes download exports and bucket overhead). `still_failing` is false once a newer backup of that app and kind succeeded. |
| GET | `/admin/health/logs/pods` | `{namespaces[], pods[{namespace, name, phase, ready, restarts, containers[]}]}`: pods in platform namespaces, discovered from the cluster (every namespace except `app-*`, `kube-*` and `default`). |
| GET | `/admin/health/logs?namespace=&pod=&container=&tail=200&since_seconds=&previous=false` | `{namespace, pod, container, previous, lines[], truncated}`. `tail` 1 to 2000, `since_seconds` 1 to 604800, output capped at 512 KiB (newest kept). |
| GET | `/admin/health/logs/download?...` | Same query, `tail` up to 10000 (default 10000), capped at 5 MiB; `text/plain` attachment. Audited as `platform_logs.download` (namespace/pod, container, line count; never the content). |

Names are validated strictly (lowercase DNS-1123); a tenant (`app-*`) or system namespace returns 403, an unknown pod or container 404, a malformed value 422.

### Admin guide

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/admin/docs/admin-guide` | none | `{"markdown": "<docs/ADMIN_GUIDE.md>"}`. 404 if the guide isn't in this build. |

### Settings

Global platform settings (limit ceilings, defaults, backup limits). Env vars in `app/config.py` now only set the
**default**; an admin override stored in the database wins, and "reset" returns to the default. Changes reach all
replicas within about 5 seconds. Every change is audit-logged (`settings.update`).

| Method | Path | Body | Response / notes |
|---|---|---|---|
| GET | `/admin/settings` | | `{groups: [{name, settings: [{key, label, description, type ("int"/"bool"), unit, value, default, overridden, min, max, updated_at, updated_by}]}]}` |
| PUT | `/admin/settings` | `{settings: {key: value-or-null, ...}}` | Partial update; `null` resets that key to its default. Same shape as GET. 422 (string `detail`) for unknown keys, wrong type, out of `min`/`max`, or a broken rule (`volume_max_gi` <= `override_volume_max_gi`, `default_app_limit` <= `max_apps_per_user`, `backup_max_bytes_per_app` <= `backup_max_bytes_total`). Nothing is saved on error. |

Lowering a ceiling doesn't change values already set on apps; it limits future changes.

## Presigned object transfer

`POST /apps/{app_id}/storage/presign` body `{"method": "PUT"|"GET", "key": "...", "content_length": int, "expires_seconds": int}`
returns `{"url", "method", "headers", "expires_at", "key"}`. The bucket is always the app's own; bucket credentials are never
returned (the URL is signed for that one bucket and key). `key` is 1 to 512 characters, no leading `/`, no `..`, no backslash or
control characters; repeated `/` collapse and the normalised `key` is echoed back. `expires_seconds` defaults to 600, max 3600.
For `PUT`, `content_length` (bytes) is required and is signed into the URL: send the body with exactly that `Content-Length` (use
the returned `headers`) or the blob storage service rejects it. A size above the bucket's remaining quota gives 413. Errors: 404 (no
such app, not yours, or no bucket), 409 (bucket is over its cap and read-only, or full), 403 (read-scope API key asked for a PUT;
GET is allowed), 422 (bad key, expiry or missing size), 429 (30 presigns per minute per user, per process). Every presign is
audited as `storage.presign` (`method`, `key`, `size`; never the URL).

```bash
curl -X PUT -H "Content-Length: 3000" --data-binary @bundle.tar.gz "$URL"
```
