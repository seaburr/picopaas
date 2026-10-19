# Work Ledger

Append-only. Every agent adds a line when it starts and when it finishes a chunk, so work survives
token exhaustion. Format: `- YYYY-MM-DD [agent] STATUS: what (files / commit)`.
STATUS ∈ START, DONE, BLOCKED, HANDOFF. **Next session: read "Now / Next" first.**

## Now / Next
- NEXT (backend-dev): scheduler imports `app.providers.spaces.SpacesStore` (optional `from_config()`); rename/add it when Spaces lands (current stub is `SpacesObjectStore`). Until then storage reconcile/quota is skipped with a log.
(Session paused 2026-10-18 night. Resume here.)
- BLOCKED on Chas: request 0001 (variant choice, DO token, Spaces key, Terraform Cloud workspace + TF_API_TOKEN).
  Expected 2026-10-19. Infra apply waits on it. Code work does not.
- DONE: backend scaffold + providers/manifests/registry/API keys (68 tests), frontend scaffold, infra
  Terraform (validated). All committed, CI green.
- DONE (backend-dev, 2026-10-18): horizontal DB scaling baked into the model — `DbServer`/`AppDatabase`
  models + migration, `app/db_placement.py::choose_server`, real `app/providers/mysql.py::MySQLAdminClient`
  (constructed per `DbServer`). See Log entry below for detail. `max_databases` default is 25 (manager's
  call: 1 GiB managed MySQL's 75-connection cap ÷ 5 per app user, with headroom for oversubscription).
- DONE (backend-dev, 2026-10-18): `app/reconciler.py` — `reconcile_app`/`delete_app`/`reconcile_all`,
  dependency-injected (kube/mysql_admin_factory/object_store/settings), fakes-only tests (7 cases:
  happy path db+storage w/ resolved `DATABASE_URL` in the rendered Secret, least-loaded placement,
  no-capacity→error, idempotent second reconcile, provider failure→error w/o leaking creds into
  `status_detail`, deletion cleanup, `reconcile_all` sweep). Added `App.status_detail` (nullable str)
  and a minimal `AppStorage` model (mirrors `AppDatabase`: secret_key Fernet-encrypted, rest plain) +
  migration `c3a9f5d2b1e4` (upgrade/downgrade round-trip verified against a throwaway sqlite file).
  See Log entry for detail.
- NEXT (backend-dev): wire `reconcile_all` into an actual APScheduler interval job with the *real*
  providers — still TODO'd in `app/reconciler.py`. Needs real `kube.py` (add the `kubernetes`
  dependency) and `spaces.py` first (both are still NotImplementedError stubs); `MySQLAdminClient`
  is already real. Then quota polling (DB 100MB via `db_size_bytes`/`set_read_only`, bucket 250MB).
  `mysql_cidr` is passed into `reconcile_app` via `settings` but nothing populates a real one yet
  (defaults to a dummy `/32`) — needs to come from the `DbServer`'s actual network once kube.py exists.
- NEXT (backend-dev): spike the DO API for per-bucket Spaces keys (ARCHITECTURE.md §2 ⚠). Needs the DO token.
- DONE (frontend-dev, 2026-10-18): API-key management UI at `/keys` (NavBar link).
- DONE (infra-ops, 2026-10-18): `deploy/ingress-nginx/values.yaml` (LB mode default, DO annotations,
  named "picopaas"; commented hostPort/DaemonSet alt), `deploy/cert-manager/` (values.yaml installCRDs,
  clusterissuer.yaml letsencrypt-prod+staging DNS-01 via `digitalocean-dns`/`access-token` secret —
  not in repo, command documented, certificate.yaml wildcard `*.seaburr.io`+apex →
  `wildcard-seaburr-io-tls` in ns `ingress-nginx`), `deploy/platform/` (namespace.yaml PSA restricted,
  rbac.yaml SA+ClusterRole+ClusterRoleBinding, deployment.yaml/service.yaml/ingress.yaml). All YAML
  validated with `ruby -ryaml` (kubeconform not installed, not installed it — budget). Not applied.
  `deploy/README.md` has install order + by-hand secrets + a flagged RBAC exposure (see next line).
- DONE (infra-ops, 2026-10-19): `deploy/mysql/` (ns, StatefulSet, NetworkPolicy, nightly backup CronJob), `deploy/dns-sync/`, kubeconform CI job, `deploy.yml` + manual plan-only `terraform.yml`, full runbook + restore in `deploy/README.md`. kubeconform clean locally. Not applied.
- FLAG (infra-ops → scm-security): `deploy/platform/rbac.yaml`'s ClusterRole grants `secrets`
  get/list/watch/create/update/patch/delete at cluster scope (needed because the reconciler manages
  arbitrary per-app namespaces that don't exist yet), so a compromised platform pod can read every
  Secret in every app namespace cluster-wide, not just ones it created. Detail + rationale in
  `deploy/README.md` "Known RBAC exposure" — please fold into `docs/PENTEST.md`.
- DONE (infra-ops, 2026-10-18): Dockerfile (multi-stage, non-root `USER 10001:10001`), `.dockerignore`,
  `entrypoint.sh`, `.github/workflows/image.yml` (GHCR build/push). Local `docker build` not verified —
  Docker daemon not running on this machine; CI's `image.yml` will be the first real build. The platform
  Deployment manifest (next bullet) should point at `ghcr.io/seaburr/picopaas:<sha>`.
- DONE (frontend-dev, 2026-10-19): admin view, YAML deploy tab, status_detail + list polling, rotate-credentials buttons. Needs backend `AppOut.status_detail` and `POST /apps/{id}/rotate-credentials` to exist to function.
- DECIDED (Chas, 2026-10-18): NO root images. `RUN_AS_NON_ROOT=True` stays.
- DONE: root-image check at create/update (`Registry.inspect` reads the config blob's `User`; empty/0/root → 422,
  a non-numeric user → 422 because kubelet can't verify it).
- DONE (frontend-dev, 2026-10-18): `USER 10001` hint + verbatim image-field 422s added to AppFormView.

- NEXT (scm-security, once dev env is up): run docs/PENTEST.md (tenant isolation, incl. "even with creds").
- DECIDED: request 0002 → option A + private DB server (k8s API public is fine for now; Spaces can't be made private
  on DO at this budget). NEXT (backend-dev): credential-rotation action for DB/storage.

- DONE (backend-dev chunk B, 2026-10-19): real `SpacesStore` (`app/providers/spaces.py`, `from_env()` needs
  DIGITALOCEAN_TOKEN, SPACES_ACCESS_KEY_ID, SPACES_SECRET_ACCESS_KEY, SPACES_REGION=atl1), `rotate_password`/`rotate_key`,
  `POST /apps/{id}/rotate-credentials`, `app/quota.py` (`poll_quotas`, `capacity_summary`). boto3 added.
- GAP (Spaces per-bucket keys): DO API `POST /v2/spaces/keys` DOES support per-bucket `grants` (read/readwrite),
  but PUT/PATCH can only rename a key; grants are immutable. So read-only toggle and rotation = create new key,
  delete old, rename. The app's access key therefore CHANGES on quota flip/rotation: `ObjectStore.set_read_only`
  now returns new creds (or None), quota.py persists them and sets app.status='pending'. NEXT (backend-dev/reconciler):
  make sure a re-render with changed Secret actually rolls the pod (e.g. checksum annotation); wire `poll_quotas`
  into the scheduler with `MySQLAdminClient` + `SpacesStore.from_env()`; wire `get_object_store`/
  `get_mysql_admin_factory` in routers/apps.py are the injection points.

## Log
- 2026-10-18 [infra-ops] DONE: see Now/Next above — `deploy/ingress-nginx/`, `deploy/cert-manager/`,
  `deploy/platform/`, `deploy/README.md`. Not committed (not applied, per rules).
- 2026-10-18 [infra-ops] START: write `deploy/` manifests (ingress-nginx values, cert-manager +
  wildcard cert via DO DNS-01, platform namespace/RBAC/Deployment/Service/Ingress) per the
  "NEXT (infra-ops): `deploy/` manifests" bullet. Hard 10-min budget; validating with ruby YAML
  parse (no kubeconform installed, not installing it). Nothing applied.
- 2026-10-18 [manager] DONE: bootstrap. Researched DO pricing, picked the lean architecture (~$30/mo),
  wrote CLAUDE.md, ARCHITECTURE.md, request 0001, agent definitions, .gitignore, CI secret scan.
- 2026-10-18 [infra-ops] START: write (not apply) Terraform for request 0001 in `infra/`, plus a
  `terraform` CI job.
- 2026-10-18 [infra-ops] DONE: wrote `infra/` (versions.tf, variables.tf, network.tf, cluster.tf,
  storage.tf, database.tf, firewall.tf, dns.tf, outputs.tf, terraform.tfvars.example, README.md) —
  VPC, DOKS (version via data source, auto-upgrade, maintenance window), private platform Spaces
  bucket, optional managed MySQL + DB firewall trusting the k8s cluster, optional node firewall for
  the no-LB case, `seaburr.io` domain as a data source only (no TXT records touched), conditional
  wildcard A record with a TODO for wiring the LB IP, S3 backend pointed at a manually-created
  `picopaas-tfstate` Spaces bucket. `terraform fmt -recursive` and `terraform init -backend=false &&
  terraform validate` both pass locally. Added a `terraform` job to `.github/workflows/ci.yml`
  (hashicorp/setup-terraform; fmt -check + init -backend=false + validate in `infra/`), secret-scan
  job untouched. Nothing applied; no secrets written. Not committed.
- 2026-10-18 [backend-dev] START: scaffold `app/` (FastAPI, SQLAlchemy 2 models, dual auth,
  apps CRUD, alembic, pytest) per ARCHITECTURE.md §2.
- 2026-10-18 [backend-dev] DONE: scaffolded `app/` — config.py (env-driven DATABASE_URL/SECRET_KEY/
  FERNET_KEY, required outside ENVIRONMENT=dev), db.py, models.py (User, ApiKey, App, AppEnvVar),
  security.py (bcrypt + JWT), crypto.py (Fernet env-var encryption), naming.py (hostname generator,
  DB-uniqueness retry), env_refs.py (env key regex + `${{ db.* / storage.* }}` ref validation against
  ARCHITECTURE.md's allowed list, gated on want_db/want_storage), dependencies.py (dual cookie/
  `pp_sk_` Bearer auth), routers/auth.py (register — first user ⇒ uberadmin — login/logout/me),
  routers/apps.py (owner-scoped CRUD, app_limit enforcement, secret masking, soft delete; status
  stays 'pending', no k8s calls). `alembic/` initialized and wired to app.config.DATABASE_URL;
  one autogenerated initial migration, verified with `alembic upgrade head` against a throwaway
  sqlite file. Had to bump SQLAlchemy to 2.0.55 (from 2.0.36) — 2.0.36 crashes on `Mapped[Optional[...]]`
  under the Homebrew Python 3.14 in this environment; 2.0.55 fixes it and is still SQLAlchemy 2.
  `tests/` (conftest with per-test in-memory SQLite + independent TestClients sharing one DB,
  real register/login flow, no auth mocks): first-user-uberadmin, second-user-is-user, duplicate
  email, login/logout/me, app_limit enforcement (uberadmin unlimited), cross-user 404 isolation,
  hostname format, secret masking (create + refetch), bad env key rejection, ref validation (missing
  want_db, unknown ref, valid ref). Created `.venv` (gitignored, added `data/*.db` to .gitignore too).
  `python -m pytest tests/ -q` → **17 passed**. Updated CLAUDE.md Commands section with real
  setup/run/test/migration commands and the required-env-vars note. Not committed (scm-security's job).
  Out of scope for this chunk, left for later: `app/providers/` (k8s/mysql/spaces), reconciler loop,
  API-key management endpoints (model + dual-auth support exist; no issue/revoke endpoint yet),
  schemas.py `AppUpdate` doesn't support partial env-var patch (replaces the whole list).
- 2026-10-18 [frontend-dev] START: scaffold `frontend/` (Vue 3 + TS + Vite + Pinia + vue-router +
  Tailwind) per CLAUDE.md layout and the seaburr/tribal `frontend/` style reference; wire FastAPI
  to serve the built SPA.
- 2026-10-18 [frontend-dev] DONE: scaffolded `frontend/` (package.json, vite.config.ts — outDir
  `../static`, base `/static/`, dev proxy for `/auth`, `/apps`, `/api` — tailwind.config.ts,
  postcss.config.cjs, tsconfig*.json, index.html). `src/api/client.ts` (typed fetch wrapper,
  cookie-auth, surfaces FastAPI `{"detail": ...}` as `ApiError`), `src/api/auth.ts` + `src/api/apps.ts`
  matching `app/schemas.py`/`app/routers/{auth,apps}.py` exactly (checked again after backend-dev's
  concurrent api_keys/providers additions — AppOut/AppCreate/AppUpdate/MASKED_VALUE/ALLOWED_REFS all
  unchanged). `src/stores/auth.ts`, `src/stores/apps.ts` (Pinia, setup style). Views: LoginView,
  RegisterView, AppsListView (hostname link `https://<hostname>`, status badge, image, in-page
  `ConfirmDialog` for delete — no `window.confirm`), AppFormView (create + edit; want_db/want_storage
  locked on edit since `AppUpdate` doesn't carry them; `EnvVarEditor` component with a per-row secret
  toggle, a masked/disabled display + explicit "Change" button for untouched secrets, and a
  click-to-insert reference-variable panel (`${{ db.* }}` / `${{ storage.* }}`, shown only when
  want_db/want_storage is set) that targets the last-focused value field). The app-limit 403 from
  `routers/apps.py` (`"app limit reached (N); contact the uberadmin to raise it"`) is surfaced
  verbatim as the form's error banner — no frontend-side copy of that message.
  KNOWN GAP (flagged in `src/components/EnvVarEditor.vue` too): `AppUpdate.env` replaces the whole
  env list and the API never returns a secret's real value, so an edit that doesn't touch a given
  secret re-sends the masked placeholder string as if it were the value. Harmless today (reconciler
  isn't live — see backend-dev's NEXT above, apps stay 'pending'), but will overwrite real secrets
  once it is; needs a partial env-patch endpoint from backend-dev before that.
  `app/main.py`: re-read immediately before editing (backend-dev had concurrently added the
  `api_keys` router) and made a minimal additive change — mount `StaticFiles` at `/static` and add a
  catch-all `GET /{full_path:path}` that serves `static/index.html` for any path not under
  `auth/`, `apps/`, `api/`, `healthz`, or `static/` (client-side routing fallback). Both degrade
  gracefully (404, not a crash) when `static/` doesn't exist, so `backend-tests` CI doesn't need a
  frontend build. Verified via TestClient: `GET /` → 200 html, `GET /some/spa/route` → 200 html,
  `GET /static/assets/missing.js` → 404, `GET /apps/999` (unauth) still → 401 (API routes untouched).
  `.venv/bin/python -m pytest tests/ -q` → **57 passed** (backend-dev's concurrent additions included).
  Added a `frontend` job to `.github/workflows/ci.yml` (actions/setup-node@v4, `npm ci`, `npm run
  typecheck`, `npm run build`, working-directory `frontend`); other jobs (`secret-scan`, `terraform`,
  `backend-tests`) untouched. `npm install` generated `frontend/package-lock.json`; verified `npm ci`
  (clean node_modules), `npm run typecheck`, and `npm run build` (→ `../static`) all pass.
  `node_modules/`, `frontend/dist/`, and `static/` were already gitignored from bootstrap — confirmed,
  not re-added. Not committed (scm-security's job). Out of scope for this chunk, left for later:
  API-key management UI (backend endpoints just landed concurrently), partial env-var patch support
  (backend), real-time app status updates (polling or websocket — status just shows whatever the API
  returns).
- 2026-10-18 [backend-dev] START: `app/k8s_manifests.py` (pure manifest rendering), `app/env_refs.py`
  `resolve()`, `app/providers/` (interfaces + fakes + real registry client + stubs), image-size-cap
  enforcement, and `/api/keys` endpoints, per ARCHITECTURE.md §2.
- 2026-10-18 [backend-dev] DONE: `app/k8s_manifests.py` — pure, no-cluster-access functions building
  Namespace (pod-security.kubernetes.io/enforce=restricted + picopaas.io/app-id label),
  ResourceQuota, LimitRange, 5 NetworkPolicies (default-deny + allow-ingress-nginx + allow-egress-dns
  + allow-egress-internet [0.0.0.0/0 minus RFC1918/link-local] + allow-egress-mysql[cidr param]),
  Secret, Deployment (runAsNonRoot/allowPrivilegeEscalation=false/drop ALL caps/seccomp
  RuntimeDefault/automountServiceAccountToken=false/tcpSocket readiness), ClusterIP Service, and
  Ingress (class nginx, host `<hostname>.seaburr.io` via new `app.config.APP_DOMAIN`, tls hosts-only
  so it rides the ingress-nginx default wildcard cert — no secretName/per-app issuance) —
  `render_app_manifests()` + `flatten_manifests()` tie them together. `RUN_AS_NON_ROOT = True` is a
  documented module constant with a TODO (it'll reject root-user images); added a "Now / Next" bullet
  above asking for a decision before the reconciler applies these. `app/env_refs.py`: added
  `resolve(env_vars, refs)` (substitutes `${{ x.y }}` placeholders with real values; unknown ref →
  `EnvValidationError`). `app/providers/`: `base.py` (ABCs — KubeProvider, MySQLAdmin, ObjectStore,
  Registry — plus their error types), `fake.py` (in-memory fakes for all four, for tests), `registry.py`
  (real: Docker Registry v2 HTTP API via httpx, anonymous Bearer-token flow for Docker Hub/GHCR-style
  registries, sums compressed layer sizes, resolves manifest lists/OCI indexes to linux/amd64), and
  `kube.py`/`mysql.py`/`spaces.py` (stubs raising NotImplementedError with TODOs — no `kubernetes`
  client dependency added, per this chunk's scope). Image cap: `app/dependencies.py::get_registry`
  (real RegistryClient by default, overridable via `app.dependency_overrides` as the other deps are);
  `routers/apps.py` calls it on create and on update-when-image-changes, rejecting >512MB or any
  RegistryError with 422 ("image not found or not public" for the latter) — update without an image
  change never touches the registry. `tests/conftest.py`'s `make_client` now also overrides
  `get_registry` with a small-size `FakeRegistry` by default, so existing image-agnostic tests didn't
  need changes. API keys: `app/schemas.py` (`ApiKeyCreateOut`, `ApiKeyOut`), `app/routers/api_keys.py`
  (`POST/GET /api/keys`, `DELETE /api/keys/{id}`, owner-scoped, registered in `app/main.py` — re-read
  first, found frontend-dev's concurrent SPA-fallback edit already there and already including this
  router, so no further main.py change was needed). New tests: `test_k8s_manifests.py` (caps, security
  fields, netpol isolation rules incl. private-range exclusion and CIDR normalization),
  `test_env_refs.py` (resolve), `test_providers_fake.py`, `test_registry.py` (httpx.MockTransport —
  token flow, layer sum, manifest-list→amd64, 404/auth-denied→RegistryError, image-ref parsing),
  `test_image_cap.py` (over/under cap, registry-error mapping, update skip-when-unchanged),
  `test_api_keys.py` (plaintext-once, list masking, revoke, cross-user revoke→404, revoked
  key→401 via the real Bearer flow). `.venv/bin/python -m pytest tests/ -q` → **67 passed**. Not
  committed (scm-security's job). Out of scope for this chunk, left for later: real `kube.py`/
  `mysql.py`/`spaces.py` + the reconciler loop (apps still stay 'pending'), the Spaces per-bucket-key
  spike, private registry auth, partial env-var patch (frontend-dev's known gap, noted above).
- 2026-10-18 [manager] DONE: reviewed + committed frontend and backend chunk 2. Fixed two bugs found
  in review: (1) PATCH env re-sent the masked "********" and overwrote stored secrets, so the mask now means
  "unchanged"; (2) PATCH env reusing an existing key hit the unique constraint (flush the deletes first).
  Regression test added. 68 tests passing.
- 2026-10-18 [manager] DONE: recorded Chas's decision: no root images (ARCHITECTURE.md, k8s_manifests.py).
- 2026-10-18 [infra-ops] START: containerize platform (Dockerfile multi-stage, .dockerignore, entrypoint.sh, image.yml GHCR workflow).
- 2026-10-18 [infra-ops] DONE: containerize platform. `Dockerfile` (node:22-slim builds frontend/ -> ../static, python:3.13-slim final stage, non-root `USER 10001:10001`, ENV ENVIRONMENT=production so SECRET_KEY/FERNET_KEY are required), `.dockerignore`, `entrypoint.sh` (alembic upgrade head && exec uvicorn), `.github/workflows/image.yml` (buildx + build-push-action, GHA cache, PR=build-only, main push=ghcr.io/seaburr/picopaas:latest + short-sha). `/healthz` already existed in app/main.py (no edit needed); added `tests/test_healthz.py` since no test covered it. `.venv/bin/python -m pytest tests/ -q` -> 69 passed. Did not touch routers/apps.py or providers/registry.py. Not committed (scm-security's job). Docker daemon not running locally so `docker build .` was not actually exercised; flagged in Now/Next for CI to verify.
- 2026-10-18 [manager] DONE: root-image preflight (registry.py inspect, apps.py _check_non_root, tests/test_non_root.py);
  Terraform backend moved from S3 to Terraform Cloud (org seaburr, workspace picopaas), per Chas. 79 tests passing.
- 2026-10-18 [frontend-dev] START: API-key management UI (`/keys`) and image-field UX (non-root hint +
  verbatim 422s) in `frontend/`.
- 2026-10-18 [frontend-dev] DONE: `src/api/keys.ts`, `src/stores/apiKeys.ts`, `src/views/ApiKeysView.vue`
  (list prefix/created/revoked state; create shows the one-time plaintext key in a copyable box with a
  "won't see it again" note and a Dismiss action; revoke uses the existing `ConfirmDialog`, not
  `window.confirm`), route `/keys` in `src/router/index.ts`, NavBar link. `src/types.ts` got `ApiKey`/
  `ApiKeyCreated` (mirrors `app/schemas.py` ApiKeyOut/ApiKeyCreateOut exactly, re-read before adding).
  `AppFormView.vue`: added the `USER 10001` hint under the image field, plus an `imageError` computed that
  detects backend 422 `detail` strings for the image field (`app/routers/apps.py` `_check_non_root`/
  `_check_image_cap` — root user, non-numeric UID, size cap, not-found/not-public all start with "image")
  and renders them verbatim next to the field instead of the generic bottom banner; other errors (e.g. app
  limit, duplicate env key) still use the bottom banner unchanged. Re-read `app/routers/api_keys.py` and
  `app/schemas.py` first to get the real shapes; no backend files touched. `npm run typecheck` and `npm run
  build` (→ `../static`) both pass. Not committed (scm-security's job). Out of scope for this chunk: no
  "copy" fallback UI beyond the box itself if `navigator.clipboard` is unavailable (key is still visible for
  manual copy).
- 2026-10-18 [backend-dev] START: bake horizontal DB scaling into the model — `DbServer`/`AppDatabase`,
  `app/db_placement.py`, real `app/providers/mysql.py::MySQLAdminClient` (PyMySQL), placement + mysql
  integration tests, `mysql-integration` CI job.
- 2026-10-18 [backend-dev] DONE: `app/models.py` — `DbServer` (name unique, host/port, admin creds
  Fernet-encrypted via `app/crypto`, `max_databases` default 25 per manager's call — DO's 1 GiB managed
  MySQL caps at 75 connections, ÷5 per app user ≈ 25 lightly-used apps with headroom — status
  active/draining/full) and `AppDatabase` (app_id unique FK, db_server_id FK, db_name/db_user,
  password_encrypted, size_bytes, over_quota). Alembic migration `8870a895c11e` (autogenerated; sanity
  checked with upgrade → downgrade → upgrade round-trip against a throwaway sqlite file). `app/db_placement.py`
  — `choose_server(session)`: among `status='active'` `DbServer` rows, picks the one with the most free
  slots (`max_databases` − its `AppDatabase` count), ties broken by lowest id; raises `NoDbCapacity` if
  none qualify. Pure SQLAlchemy, no provider calls. `app/providers/mysql.py`: real `MySQLAdminClient`,
  constructed per `DbServer` (host/port/admin creds come from `__init__`, not per call — updated
  `MySQLAdmin`'s docstring in `base.py` to say so, and gave `FakeMySQLAdmin` an optional unused `server`
  param for the same calling convention without breaking its existing no-arg test). `create_db_user`
  creates `app_<id>` db + user with a random 32-char password and `MAX_USER_CONNECTIONS 5`, grants ALL
  scoped to that one db; `drop` does `DROP DATABASE/USER IF EXISTS`; `db_size_bytes` sums
  `information_schema.TABLES.(data_length+index_length)`; `set_read_only` revokes/restores only
  INSERT/UPDATE/CREATE/ALTER (SELECT/DELETE/DROP always stay granted so a quota-exceeding app can still
  read and clean up). Identifiers are our own `app_<id>` generation but still regex-validated before
  SQL interpolation (MySQL has no parameterized identifiers); passwords go through query parameters
  (`IDENTIFIED BY %s`) since PyMySQL escapes those client-side. Tests: `tests/test_db_placement.py`
  (7 cases, plain SQLite, no providers) and `tests/test_mysql_admin.py` (`@pytest.mark.mysql`, module-level
  skip unless `MYSQL_ADMIN_URL` is set — create+grant, cross-app isolation via `USE`, size reporting,
  read-only blocks INSERT but allows SELECT/DELETE, drop). Registered the `mysql` marker in new
  `pytest.ini`. CI: added a `mysql-integration` job to `.github/workflows/ci.yml` with a `mysql:8.4`
  service (root password `test`, healthcheck via `mysqladmin ping`), `MYSQL_ADMIN_URL=mysql://root:test@127.0.0.1:3306`,
  running `pytest tests/ -m mysql -q`; `backend-tests` untouched and still self-skips the mysql tests
  (no `MYSQL_ADMIN_URL` there). Docker wasn't running locally so only the SQLite suite was exercised here:
  `.venv/bin/python -m pytest tests/ -q` → **86 passed, 1 skipped** (the skip is the whole
  `test_mysql_admin.py` module). Not committed (scm-security's job). Out of scope for this chunk, left for
  later: nothing calls `db_placement`/`MySQLAdminClient` yet — that's the reconciler's job (see Now/Next);
  no admin UI/endpoint to register a `DbServer` row yet either (today there'd be zero active servers, so
  `choose_server` would raise `NoDbCapacity` — fine until the reconciler and a seed/admin path exist).
- 2026-10-18 [manager] DONE: scaling design (ARCHITECTURE.md §3), autoscaler max_nodes var (default 2, needs Chas OK),
  reviewed + committed DB placement/MySQL admin (6 real-MySQL CI tests pass) and the API keys UI. Session end.
  Next session: reconciler loop wiring placement + MySQLAdmin + k8s manifests (real kube.py), then deploy/ manifests.
- 2026-10-18 [manager] DONE: wrote docs/PENTEST.md and request 0002 (credential binding) per Chas.
- 2026-10-18 [manager] DONE: recorded 0002 decision (A + private infra); added a DOKS control-plane firewall var to Terraform.
- 2026-10-18 [manager] DONE: corrected per Chas: k8s API may stay public (firewall var kept, off by default); only DB/Spaces should be private.
- 2026-10-18 [backend-dev] START: `app/reconciler.py` (reconcile_app/delete_app/reconcile_all,
  dependency-injected providers) per Now/Next. Hard 10-minute budget this chunk.
- 2026-10-18 [backend-dev] DONE: `app/reconciler.py` — `reconcile_app(session, app, *, kube,
  mysql_admin_factory, object_store, settings)`: provisions an `AppDatabase` via
  `db_placement.choose_server` + `mysql_admin_factory(server).create_db_user` (NoDbCapacity ->
  status='error'/status_detail='no database capacity') and an `AppStorage` via
  `object_store.create_bucket_and_key`, both skipped if already provisioned (idempotent); builds
  `db.*`/`storage.*` refs (decrypting stored secrets via `app/crypto`), decrypts env vars, resolves
  `${{ }}` refs via `app/env_refs.resolve`, renders manifests via `app/k8s_manifests`, and
  `kube.apply()`s them — status goes 'deploying' then whatever `kube.get_status()` returns (e.g.
  'running'). Any provider exception anywhere in the flow sets status='error' with a fixed,
  hand-written `status_detail` string (never `str(exc)`) so a provider's exception text — which could
  echo a password/connection string — can never leak into status_detail; verified in
  `test_provider_failure_sets_error_without_leaking_secrets`. `delete_app(...)`: best-effort
  `kube.delete_namespace` + mysql `drop` + storage `delete` + row cleanup for soft-deleted apps,
  independent try/excepts so one failure doesn't block the others. `reconcile_all(...)`: sweeps
  pending/deploying apps through `reconcile_app` and deleted-but-not-status='deleted' apps through
  `delete_app`. Model changes: `App.status_detail` (nullable String(255)) and a new minimal
  `AppStorage` model (one-to-one with App, mirrors `AppDatabase`'s pattern — `secret_key_encrypted`
  Fernet via `app/crypto`, bucket/endpoint/region/access_key plain, size_bytes/over_quota for later
  quota polling) + relationship on `App`. Migration `c3a9f5d2b1e4` (adds `apps.status_detail`,
  creates `app_storage`) — upgrade/downgrade/upgrade round-trip verified against a throwaway sqlite
  file. `tests/test_reconciler.py` (7 cases, fakes only, plain SQLite session — no HTTP layer):
  happy path db+storage (asserts the rendered k8s Secret's `DATABASE_URL` is the real resolved
  `mysql://...` URL, no `${{ }}` leftover), placement onto the least-loaded `DbServer`, no capacity ->
  error, idempotency (second `reconcile_app` call creates no second `AppDatabase`/`AppStorage` row
  and calls the fake provider's create exactly once), provider failure -> error status with a
  secret-free detail, deletion cleanup (namespace/db/storage all gone), `reconcile_all` sweep over a
  mix of a pending and a deleted app. `.venv/bin/python -m pytest tests/ -q` -> **93 passed, 1
  skipped** (the skip is the pre-existing `test_mysql_admin.py` module, no `MYSQL_ADMIN_URL` here).
  Not committed (scm-security's job). Out of scope for this chunk, per the hard time budget: no
  APScheduler wiring (left as a TODO in the module docstring, per the chunk spec), no real
  `kube.py`/`spaces.py` (still stubs), no quota polling, `mysql_cidr` for the rendered NetworkPolicy
  is threaded through `settings` but nothing populates a real value yet (placeholder `/32` default) —
  all flagged in Now/Next above for the next backend-dev chunk.
- 2026-10-19 [frontend-dev] START: admin view, YAML deploy tab, status_detail + polling, rotate-credentials buttons (frontend/)
- 2026-10-19 [backend-dev] START: chunk C — admin API, YAML spec deploy (POST /apps/spec), CLI (app/routers/admin.py, spec.py, app/spec.py, cli/picopaas.py)
- 2026-10-19 [frontend-dev] DONE: /admin view (users app_limit, DB servers capacity/status/add, warn banner), YAML tab + SpecEditor on /apps/new, status_detail + 5s polling in list, rotate buttons in edit; typecheck+build pass. Types aligned with admin.py/spec.py; AppOut.status_detail and POST /apps/{id}/rotate-credentials not yet in backend at check time (frontend/src/{api,views,components,types.ts})
- 2026-10-19 [backend-dev] DONE: chunk C — /admin (users, db-servers), POST /apps/spec (strict pydantic, aliases rejected, 64KB cap, secrets kept on update; removing db/storage on update is refused), cli/picopaas.py + docs/CLI.md, PyYAML pinned, tests/test_admin_spec_cli.py. Imported private helpers from apps.py (no edits). Rotate CLI relies on the other agent's /apps/{id}/rotate-credentials. 102 tests pass.
- 2026-10-19 [backend-dev] START: chunk A — real KubeProvider, MYSQL_EGRESS_MODE, scheduler, DbServer bootstrap
- 2026-10-19 [backend-dev] DONE: chunk A — app/providers/kube.py (SSA via dynamic client, status mapping), app/scheduler.py (APScheduler, MySQL GET_LOCK leader), lifespan in app/main.py (RECONCILER_ENABLED=1 only), mysql egress in-cluster|cidr (k8s_manifests/reconciler), reconciler maps "error: x" status to status=error+detail, config/requirements/CLAUDE.md, tests (kube, scheduler, manifests). Full suite passes.
- 2026-10-19 [infra-ops] START: deploy/mysql, backup, dns-sync, kubeconform CI, deploy.yml, runbook.
- 2026-10-19 [infra-ops] DONE: same; files under deploy/, .github/workflows/{ci,deploy,terraform}.yml. Not committed/applied.
- 2026-10-19 [backend-dev] START: chunk B — SpacesStore, credential rotation, quota polling.
- 2026-10-19 [backend-dev] DONE: chunk B — app/providers/{spaces,base,fake,mysql}.py, app/quota.py, rotation endpoint in
  app/routers/apps.py, tests/test_{spaces,quota,rotation}.py + rotate test in test_mysql_admin.py (CI-only). 122 passed, 1 skipped. Not committed.
- 2026-10-19 [scm-security] START: secret gate, security review, commit+push wave, watch CI
- 2026-10-19 [scm-security] DONE: wave committed+pushed; review filed #1 (mysql root@% reachable from tenant ns), #2 (non-atomic rotation); deploy.yml sha input now validated hex. Reviewed OK: admin authz, spec parsing, dns-sync (A-only), workflow perms.
- 2026-10-19 [infra-ops] START: issue #1 — mysql root localhost-only, picopaas_admin/picopaas_backup users (deploy/mysql, README, PENTEST #20)
- 2026-10-19 [infra-ops] DONE: issue #1 fix in working tree (deploy/mysql/{statefulset,backup-cronjob,networkpolicy}.yaml, deploy/README.md, docs/PENTEST.md); kubeconform OK. Existing MySQL needs manual user SQL (README). Not committed.
- 2026-10-19 [backend-dev] START: fix issue #2 (atomic credential rotation)
- 2026-10-19 [backend-dev] DONE: issue #2 — Spaces: create new key -> commit -> revoke old (no rename; `revoke_other_keys`/`delete_key` added to ObjectStore); MySQL: commit new password first, then `set_password` (ALTER), restore old stored pw on failure; quota.py same + retries revoke each poll. Failure-injection tests in test_{rotation,quota,spaces}.py, real-MySQL test in test_mysql_admin.py. 136 passed, 1 skipped. Not committed.
- 2026-10-19 [scm-security] START: commit+push fixes for #1/#2, watch CI (mysql-integration as picopaas_admin)
