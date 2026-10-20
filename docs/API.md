# PicoPaaS API

Base URL: `https://platform.raisehellpraisedale.xyz`. JSON in, JSON out. FastAPI also serves interactive docs at
`/docs` and the schema at `/openapi.json`.

## Authentication

| Method | How |
|---|---|
| Session cookie | `POST /auth/login` or `/auth/register` sets an HttpOnly `session` cookie (JWT, 7 days, `SameSite=Lax`, `Secure` outside dev). The UI uses this. |
| API key | `Authorization: Bearer pp_sk_...` (create under `POST /api/keys`). Works on every endpoint that accepts a user. The cookie is checked first. |

Auth levels used below:

- **public**: no auth.
- **user**: any logged-in, non-deleted user (including pending and suspended).
- **active**: user with status `active`. Pending gets `403` "Your account is waiting for admin approval..."; anything
  else gets `403` "Your account is suspended...".
- **admin**: the `uberadmin` role (`403` "This needs admin access." otherwise). Missing auth is `401` "You're logged
  out. Log in again, or check that your API key (PICOPAAS_TOKEN) is valid and not revoked."

Error `detail` strings are written to be shown to people as-is (what happened, why, what to do) and may be reworded;
match on the status code, not the text.

Request bodies reject unknown fields (422). Common errors: 401 unauthenticated, 403 forbidden, 404 not found
(also used for other people's apps), 409 conflict, 422 validation.

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
| POST | `/auth/register` | public | `{email, password, accept_terms}` (email valid, password 8 to 256, `accept_terms` must be `true` else 422) | 201 `User`; sets cookie. The first user is `uberadmin`/`active`; later users are `user`/`pending`. Enumeration-safe: registering an email that already has an account also returns 201 with a `pending` `User` and a cookie, but the cookie authenticates nobody (`/auth/me` is 401) and nothing changes; the owner just logs in. No email is sent. |
| POST | `/auth/login` | public | `{email, password}` | 200 `User`; sets cookie. 401 "Email or password is wrong..." (same for unknown emails and deleted users; failures are audited). |
| POST | `/auth/logout` | public | none | 204; clears cookie. |
| GET | `/auth/me` | user | none | `User` plus `features` and `limits` |
| POST | `/auth/change-password` | user (any status) | `{current_password, new_password}` (new: 8 to 256, must differ) | 204. 400 if `current_password` is wrong; 422 for a bad new password. Clears `must_change_password`. Audited as `user.change_password`. |

`User`: `id, email, role ("user"|"uberadmin"), app_limit, status ("pending"|"active"|"suspended"|"deleted"),
created_at, must_change_password`. New users get `app_limit` 1. `must_change_password` is set by an admin password
reset and informational only (the server does not block other calls); the UI should send the user to change it.

`/auth/me` adds:
- `features: {databases, storage}`: `databases` is true when at least one DB server exists; `storage` is true when
  Spaces credentials are configured. Hide the matching checkbox when false.
- `limits: {cpu_limit_m, mem_limit_mi, cpu_request_m, mem_request_mi, image_mb, db_mb, bucket_mb,
  override: {cpu_min_m, cpu_max_m, mem_min_mi, mem_max_mi}}`: the platform caps (from `app/config.py`) and the
  range an admin may override per app.

## API keys (`/api/keys`, active)

| Method | Path | Response |
|---|---|---|
| POST | `/api/keys` | 201 `{id, key, prefix, created_at}`. The raw `key` is shown **once**. No body. |
| GET | `/api/keys` | List of `{id, prefix, created_at, revoked_at}` for your keys, newest first. |
| DELETE | `/api/keys/{key_id}` | 204. Revokes (idempotent). 404 if not yours. |

## Apps (`/apps`, active; scoped to the caller's own apps)

`App` response: `id, owner_id, name, hostname (full, e.g. clever-robin-a1b2c.raisehellpraisedale.xyz), image, port,
want_db, want_storage, health_check_enabled, health_check_path, status, status_detail, retry_at, created_at, resources, env[]`.
`retry_at` is when an app in `error` will next be retried (or null). `resources` is
`{cpu_request_m, cpu_limit_m, mem_request_mi, mem_limit_mi, overridden, exceeds_platform_caps}`.
Each `env` item: `{key, value, is_secret, undecryptable}`. Secret values are returned as `********`. If a stored
value cannot be decrypted (platform key changed) `undecryptable` is true and `value` is empty.

Statuses: `pending, deploying, running, error, suspended, deleted`.

| Method | Path | Body | Response / notes |
|---|---|---|---|
| POST | `/apps` | `AppCreate` | 201 `App` (status `pending`). 403 `app limit reached (N)...` (uberadmin exempt); 422 for image/env problems; 409 `hostname collision, please retry`. |
| GET | `/apps` | none | List of `App`, newest first. |
| GET | `/apps/{app_id}` | none | `App`; 404 `app not found`. |
| PATCH | `/apps/{app_id}` | `AppUpdate` | `App`. 403 `app suspended`. Changing image, port, env or health check sets status `pending` and redeploys. |
| DELETE | `/apps/{app_id}` | none | 204. Soft delete; the reconciler tears down namespace, DB and bucket. 403 `app suspended`. |
| POST | `/apps/{app_id}/rotate-credentials` | `{"target": "db"\|"storage"}` | `App` (status `pending`). 409 `database|storage not provisioned yet` (also when the app has no DB/bucket); 502 `credential rotation failed`; 403 if suspended. |
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

`AppUpdate`: optional `name, image, port, env, health_check_enabled, health_check_path` (same constraints).
`want_db` and `want_storage` cannot be changed. `env`, when sent, replaces the whole list; send a secret back with
value `********` to keep it.

`EnvVar` (input): `{key, value, is_secret=false}`. `key` matches `^[A-Z_][A-Z0-9_]*$`. A `value` that is exactly
`${{ ref }}` is a reference; allowed refs: `db.host, db.port, db.name, db.user, db.password, db.url, db.ca_cert,
db.tls, storage.endpoint, storage.region, storage.bucket, storage.access_key, storage.secret_key`. `db.*` needs
`want_db`, `storage.*` needs `want_storage`.

Image checks on create, update (when `image` is sent) and spec: registry must serve it publicly (422 `image not
found or not public`), the USER must be non-root (422; a named user runs as UID 10001), and the summed compressed layers at most 256 MB
(422).

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
| GET | `/admin/users` | none | List of `{id, email, role, app_limit, app_count, status, created_at}` (excludes deleted). |
| PATCH | `/admin/users/{id}` | `{app_limit: int >= 0}` | Updated user. Not audited. |
| POST | `/admin/users/{id}/approve` | none | `pending` to `active`; 409 if the user is not pending. |
| POST | `/admin/users/{id}/reset-password` | none | 200 `{"temporary_password": "..."}`. Sets a random 16-character password, shown once, and flags the user `must_change_password`. Audited as `user.reset_password` (the password is never logged). Existing sessions are not revoked. Hand the password over out of band. |
| POST | `/admin/users/{id}/suspend` | none | User `suspended`; all their live apps `suspended` (scaled to zero). 400 for the uberadmin. |
| POST | `/admin/users/{id}/unsuspend` | none | User `active`; apps suspended through the user go back to `pending`. 409 if the user is not suspended. |
| DELETE | `/admin/users/{id}` | none | 204. Soft-deletes the user, their apps (reconciler tears down) and revokes their API keys. 400 for the uberadmin. |
| GET | `/admin/apps` | none | All live apps: `{id, name, hostname, status, owner_id, owner_email, image, created_at}`. |
| POST | `/admin/apps/{id}/suspend` | none | App `suspended`. |
| POST | `/admin/apps/{id}/unsuspend` | none | App to `pending`; 409 if the app is not suspended. |
| DELETE | `/admin/apps/{id}` | none | 204; works even on suspended apps. |
| GET | `/admin/audit?limit=100` | none | Newest first; `limit` 1 to 1000. Items: `{id, at, actor_email, action, target_type, target_id, detail}`. |
| GET | `/admin/db-servers` | none | `{id, name, host, port, status, max_databases, used, pct, warn}`; `warn` at 80% or more. |
| POST | `/admin/db-servers` | `{name, host, port=3306, admin_user, admin_password, max_databases}` | 201 server. 409 name exists. Status starts `active`. Not audited. |
| PATCH | `/admin/db-servers/{id}` | `{status?: active\|draining\|full, max_databases?: int > 0}` | Updated server. Not audited. |
| GET | `/admin/health` | none | Cluster dashboard JSON, cached 15 s: `generated_at, metrics_available, errors, cluster{node_count,max_nodes,pods_total}, nodes, apps[], platform[], db_servers[]`. The payload may gain fields; see `app/health.py`. |

Audit actions recorded: `user.register`, `login.failed`, `apikey.create`, `apikey.revoke`, `app.create`,
`app.update`, `app.delete`, `app.rotate`, `user.approve`, `user.suspend`, `user.unsuspend`, `user.delete`,
`app.suspend`, `app.unsuspend`, `user.register_duplicate`, `user.change_password`, `user.reset_password`. Never contains secrets; `detail` is capped at 255 characters.

## Not an API

The reconciler (every 15 s) and quota poller (every 5 min) run inside the platform process when
`RECONCILER_ENABLED=1`; there are no endpoints to trigger them.
