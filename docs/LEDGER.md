# Work Ledger

Append-only. Every agent adds a line when it starts and when it finishes a chunk, so work survives
token exhaustion. Format: `- YYYY-MM-DD [agent] STATUS: what (files / commit)`.
STATUS ∈ START, DONE, BLOCKED, HANDOFF. **Next session: read "Now / Next" first.**

## Now / Next
- **PENTEST #19 step 2 DONE in rbac.yaml (uncommitted/undeployed):** after deploy, verify secret rotation + a new app deploy still work (diag RBAC step).
### Next (2026-10-20)
- Re-run live quota tests against the fixed code (E2E_SLOW=1 pytest tests/e2e -k quota). Check the first over-cap WARNING log for whether doadmin can see/kill app sessions.
- Live check of Health's node disk conditions. Verify on prod that a scratch-disk eviction now produces a "Recent events" entry (kind `disk`) within a sweep.
- Floating-tag test: a GH Actions job that retags a test image (good, then oversized) and confirms auto-redeploy and rejection.
- npm audit: dev-only postcss-selector-parser bump.
- Open: #11 hairpin (CoreDNS rewrite, test off prod), PENTEST #19 (SA cluster-wide Secret read), #10 API under /api.
- Chas to-dos are in docs/QUESTIONS.md: bootstrap.md caps incl. 250MB scratch disk, E2E secrets, delete PICOPAAS_SECRET_KEY, Porkbun abuse@ forwarding.

- 2026-10-20 [infra-ops] START+DONE: DB host cpu/mem/disk % on Health DB card via DO scrapable metrics (app/db_host_metrics.py, health._host_metrics 45s cache, DB_METRICS_ENABLED/PORT/CA, HealthView line, tests/test_db_host_metrics.py). No netpol change (picopaas-system has no egress policy). Live scrape unverified until deploy. Uncommitted.
- 2026-10-20 [frontend-dev] START+DONE: table overflow re-audit (Admin/Health/Apps/Keys/Guide/Detail at 1440+390, Playwright) + app cap no longer revealed (removed 'of N max'/'platform max' UI, PATCH 422 text now number-free). Fixes: AdminView users grid cols/nowrap, HealthView node cards min-w-0+wrap and DB/apps col widths, HostLink sr-only span caused 390px page overflow on Apps list. Uncommitted.
- 2026-10-20 [backend-dev] START+DONE: hard per-account app cap MAX_APPS_PER_USER (default 5) for everyone incl. admins; effective_app_limit in app/resources.py; admin PATCH clamp (422); /auth/me max_apps + app_limit_effective; admin list app_limit_effective; AdminView/AppsListView use effective; e2e harness requests 5; tests + docs. Uncommitted.
- 2026-10-19 [backend-dev] DONE: platform caps lowered (bucket 100MB, DB 50MB, image 256MB, CPU 100m, RAM 384Mi) via single source app/config.py; code, tests, docs, e2e sizes updated. Uncommitted. bootstrap.md pending Chas.
- 2026-10-19 INCIDENT: ~50 min outage 19:50–20:45 UTC. Applying the DO LB hostname annotation (hairpin fix, #11) stopped the LB passing
  traffic; it happened during a usage-limit pause, so nobody noticed. Reverted (577f793). Rule: never test LB changes on the prod Service.
- 2026-10-19 STATUS: e2e 12/12 green; pentest 33 pass / 1 fail (#11 hairpin) / 8 skipped (need kubectl). NEXT: E2E_SLOW quota tests;
  kubectl-based pentest items via diag; PENTEST #19 (platform SA cluster-wide Secret read); #11 via CoreDNS rewrite; issues #8 #9 #10.
- 2026-10-19 [infra-ops] DONE: Traefik Service gets do-loadbalancer-hostname=lb.raisehellpraisedale.xyz (pod->own-LB hairpin fix); bootstrap upserts A `lb`, LB IP now read from doctl. Uncommitted; not applied.
- LATER: add a platform PodDisruptionBudget (minAvailable 1) once the cluster runs ≥2 nodes. On 1 node it would block drains/upgrades (Chas, 2026-10-19).
- 2026-10-19 UNBLOCKED (manager): cause = the region size list (plain sizes have no capacity in nyc3); now s-1vcpu-2gb-amd + preflight check. Was BLOCKED: DOKS create 422 "insufficient capacity" in atl1+nyc3 at all sizes, versions 1.37/1.36 and
  minimal request. MySQL (db-s-1vcpu-1gb) + VPC are up in nyc3. Waiting on Chas + DO support. Temporary debug settings
  to revert once the cluster exists: max_nodes=1 (approved 2) and the DOKS 1.36 pin in infra/cluster.tf.
  Ready to run after the cluster is up: bootstrap.yml → Chas makes the uberadmin + test account → tests/e2e + tests/pentest.
- DONE (infra-ops, 2026-10-19): ingress-nginx -> Traefik 42.0.0 (ns traefik, IngressClass traefik, default TLSStore -> wildcard secret in ns traefik). Uncommitted; not applied.
- 2026-10-19 (manager): Chas answered QUESTIONS.md. DONE: MySQL slug `db-amd-1vcpu-1gb` + plan-time slug check in
  terraform.yml; LE email; adopt bucket `pico-paas-s3-a` (import block); gitleaks pre-commit hook + CI no-allowlist guard
  + CLAUDE.md rule. Chas ran the history rewrite (main force-pushed, CI green) and approved the LB variant. NEXT: TFC plan → apply → deploy/README.md bring-up.
- **Chas's open questions/to-dos live in `docs/QUESTIONS.md`. Check it first.**
- DONE (backend-dev, 2026-10-19): issues #3/#4/#5 fixed + gitleaks test-value rename (migration e5b2c8d1f3a7). Uncommitted.
- DONE (backend-dev, 2026-10-19): signup approval + user/app suspend/delete + audit log + SMTP egress block (migration d4e1a7b9c2f6; see Log). Frontend can build against it.
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
- 2026-10-19 [scm-security] DONE: pushed 1f5c1bc (#1), bea2c4e (#2). CI red: mysql-integration 'MySQL tests as picopaas_admin' - 7 fail on 'CREATE DATABASE IF NOT EXISTS app_9000x' -> 1044 Access denied for picopaas_admin@% (the GRANT ALL ON app\_%.* from deploy/mysql initdb isn't matching app_90001; suspect escaping of the db pattern in 10-users.sh, not a missing privilege). BLOCKED for infra-ops: verify SHOW GRANTS FOR picopaas_admin in CI. #1/#2 left open. All other jobs green.
- START/DONE (infra-ops, 2026-10-19): domain move seaburr.io -> raisehellpraisedale.xyz in infra/, deploy/ (cert secret wildcard-raisehellpraisedale-xyz-tls), ARCHITECTURE.md (+Abuse & moderation), request 0003 (new), 0001 pointer. terraform validate + kubeconform OK. Uncommitted. Waiting on Chas: NS move + add domain in DO.
- 2026-10-19 [frontend-dev] START: account approval/suspension UI, admin apps + audit
- 2026-10-19 [frontend-dev] DONE: /pending status page + router guard, admin pending/approve/suspend/delete users, All apps table, audit log w/ filter, suspended badge + disabled edit; typecheck+build pass. Coded to contract; backend /admin/{apps,audit,users/*} not yet present when checked, re-align types (frontend/src/types.ts) once landed.
- 2026-10-19 [backend-dev] START: moderation chunk (user status, admin approve/suspend/delete, audit log, SMTP egress block)
- 2026-10-19 [backend-dev] DONE: users.status (pending/active/suspended/deleted) + audit_log, migration d4e1a7b9c2f6; `get_active_user` gate on /apps*, /api/keys*, spec; /admin/users/{id}/approve|suspend|unsuspend, DELETE, /admin/apps*, /admin/audit; app status 'suspended' -> replicas 0 (reconciler marks status_detail='scaled to zero'); allow-egress-internet excludes TCP 25/465/587; app/audit.py; tests/test_moderation.py; register_and_login auto-approves (approve=False for pending). Full suite passes. Not committed.
- 2026-10-19 [scm-security] START: secret gate, security review of moderation wave, commit+push, watch CI
- 2026-10-19 [scm-security] DONE: committed domain move + moderation (api, frontend) + docs; review filed #3 (delete+recreate evades app suspension), #4 (first-user race), #5 (unsuspend revives admin-suspended apps). Reviewed OK: get_active_user on all /apps,/api/keys,/spec routes; deleted users blocked (login, cookie, keys); admin routes uberadmin-only; uberadmin protected; audit has no secrets; SMTP port ranges gap-free (UDP all allowed, 2525 etc. still open).
- 2026-10-19 [backend-dev] START: fix issues #3 (owner delete of suspended app 403), #4 (platform_init singleton for first uberadmin), #5 (App.suspended_by_user), gitleaks false positive in tests/test_moderation.py
- 2026-10-19 [backend-dev] DONE: same; app/routers/{apps,admin,auth}.py, app/models.py, alembic e5b2c8d1f3a7, tests/test_moderation.py (+3 tests), tests/test_reconciler.py (uuid email, flaky id() collision). 152 pass. Not committed.
- 2026-10-19 [scm-security] START: commit #3/#4/#5 fixes, merge fix/mysql-admin-grants, review procedures, push, CI
- 2026-10-19 [backend] DONE: #1 root cause = MySQL 8.4 GRANT ignores wildcard-db grants for the grantor check (1044 even for SELECT). Fix: picopaas_ops SECURITY DEFINER procs + MYSQL_ADMIN_USE_PROCEDURES=1; branch fix/mysql-admin-grants, CI green run 39242817676 (not merged).
- 2026-10-19 [scm-security] note: .gitleaksignore fingerprint for historical test dummy password (commit 58e2897 line 146); current tree no longer contains it
- 2026-10-19 [scm-security] DONE: pushed 58c56b5 (#3/#4/#5), merge 3722fb8 (#1), gitleaks ignore; CI all green; procedures reviewed OK (int-validated ids, QUOTE() for passwords); issues 1,3,4,5 closed; branch deleted
- 2026-10-19 [manager] DONE: added docs/QUESTIONS.md as Chas's answer file; CLAUDE.md points every session at it.
- 2026-10-19 [scm-security] START: commit/push terraform.yml gate working-directory fix, dispatch workflow
- 2026-10-19 [scm-security] DONE: pushed ff9dc0d; terraform run 39270616109 FAILED at plan: DO API 401 + Spaces creds not configured (TF workspace vars/credentials issue, BLOCKED for infra owner/Chas)
- 2026-10-19 [scm-security] START: commit terraform plan+apply workflow and LB-approved docs, push, watch CI (terraform workflow NOT dispatched)
- 2026-10-19 [scm-security] DONE: pushed 0d6f851 (ci) + 2a31d66 (docs); CI + image green; deploy run 39271027882 FAILED (BLOCKED, expected: doctl 'no cluster goes by the name picopaas-atl1', cluster not provisioned until TFC creds fixed); terraform workflow not dispatched
- 2026-10-19 [scm-security] START: commit terraform local-exec workflow change, push, dispatch plan-only (apply=false)
- 2026-10-19 [scm-security] DONE: pushed e1fbe9e; CI green; terraform plan-only run 39271775789 success (1 import, 4 add, 1 change, 0 destroy); apply NOT run
- 2026-10-19 [scm-security] START: commit MySQL 8.4 / AMD node infra change, push, dispatch plan-only
- 2026-10-19 [scm-security] DONE: pushed e4a53c4; CI FAILED: 'terraform fmt -check' flags infra/database.tf (BLOCKED, infra owner: run terraform fmt); terraform plan-only run 39273533943 success: 3 add, 0 change, 0 destroy; apply NOT run
- 2026-10-19 [infra-ops] START: .github/workflows/bootstrap.yml (managed MySQL + LB bring-up, steps 2-8), cert-manager crds values, deployment CA mount
- 2026-10-19 [infra-ops] DONE: bootstrap.yml (ingress-nginx 4.15.1, cert-manager v1.21.2), deploy/cert-manager/values.yaml (crds.enabled), deploy/platform/deployment.yaml (db-ca mount), infra/dns.tf comment, deploy/README.md. YAML + bash -n + kubeconform OK; not run. APP GAP: MySQLAdminClient (app/providers/mysql.py) has no ssl= for pymysql.connect, so managed-MySQL app provisioning/BOOTSTRAP admin will fail until it uses the CA at /etc/picopaas/db-ca/ca.crt. Not committed.
- 2026-10-19 [backend] START: MYSQL_SSL_CA TLS for MySQLAdminClient + bootstrap.yml literal
- 2026-10-19 [backend] DONE: MYSQL_SSL_CA in config.py, ssl+verify in MySQLAdminClient._connect (only pymysql.connect in app/), bootstrap.yml literal, tests/test_mysql_ssl.py. 154 passed, 1 skipped. Tenant DATABASE_URL (reconciler.py:54 mysql://u:p@host:port/db) has no TLS param; apps may need ssl-mode. Not committed.
- 2026-10-19 [scm-security] DONE: committed fmt, MYSQL_SSL_CA TLS, bootstrap workflow (reviewed: no secret echo, masks present, DNS limited to A '*'/picopaas); issues filed for tenant DB TLS + ingress-nginx retirement
- 2026-10-19 [backend] START: ingress-nginx -> Traefik in app/ (ingressClassName, NetworkPolicy ns, settings)
- 2026-10-19 [backend] DONE: Traefik in app/k8s_manifests.py + config.py (INGRESS_CLASS, INGRESS_NAMESPACE), policy renamed allow-ingress-controller, tests updated; 154 passed, 1 skipped. No nginx annotations existed. Not committed.
- 2026-10-19 [infra-ops] START: replace ingress-nginx with Traefik (deploy/traefik, bootstrap.yml, cert-manager ns, docs)
- 2026-10-19 [infra-ops] DONE: deploy/traefik/values.yaml (chart 42.0.0, CRDs kept for TLSStore default cert), bootstrap.yml, cert-manager certificate ns traefik, platform ingress class traefik, deploy/README, ARCHITECTURE decision, infra comments. helm template + kubeconform + yaml OK; not applied, not committed.
- 2026-10-19 [backend] START: testapps/ (hello, probe, toolbox) + .github/workflows/testapps.yml
- 2026-10-19 [backend] START: #6 tenant DB TLS (DbServer.ca_cert, db.ca_cert/db.tls refs, bootstrap seed+backfill, docs)
- 2026-10-19 [backend] DONE: migration f6c3d9e2a4b8, models/env_refs/reconciler/scheduler, frontend DB_REFS, docs/CLI.md "Connecting to your database", tests/test_db_ca_cert.py. Not committed.
- 2026-10-19 [backend] DONE: testapps/hello (stdlib only), testapps/probe (pymysql+boto3, DB_CA_CERT TLS
  support, /db /s3 /db/fill /s3/fill, scrubbed errors), testapps/toolbox (nmap/curl/dnsutils/mariadb-client +
  python; bearer-token-gated /checks /run with 11 fixed named checks, arg validation, no arbitrary exec,
  scrubbed secrets), .github/workflows/testapps.yml (matrix build+push to
  ghcr.io/seaburr/picopaas-testapp-{hello,probe,toolbox}, triggers on testapps/** push + workflow_dispatch),
  testapps/README.md (app.yaml examples with db./storage. env refs incl. DB_CA_CERT). All non-root
  (10001:10001), listen on $PORT, GET /healthz. Verified: py_compile on all .py; hello and probe servers run
  locally and return correct JSON (probe returns clean {"ok":false,"error":...} with no stack trace or leaked
  creds when DB/S3 unreachable or deps missing — fixed a scrub() bug that corrupted error text on short
  values like DB_USER=u); toolbox tested locally (401 when TOOLBOX_TOKEN unset/wrong, /checks and /run
  tcp_connect work with correct token). Not verified: actual Docker builds (Docker Desktop not started, per
  instructions) — image sizes/</512MB and USER directive correctness in the built image are unverified beyond
  Dockerfile review. Uncommitted.
- 2026-10-19 [backend] START: live e2e + pentest harness (tests/e2e, tests/pentest)
- 2026-10-19 [backend] DONE: tests/e2e/{harness,conftest,test_live}.py + README, tests/pentest/{test_isolation.py,README.md}, pytest markers e2e/pentest in pytest.ini. Skipped unless E2E_USER_TOKEN+E2E_ADMIN_TOKEN set; 159 pass, 56 skipped offline. Never run against the live env yet. Not committed.
- 2026-10-19 [scm-security] DONE: committed+pushed infra SKUs, db.ca_cert/db.tls refs (#6), testapps + workflow; tests/e2e, tests/pentest, pytest.ini left uncommitted (other agent).
- 2026-10-19 [frontend] START: admin Health view (/admin/health)
- 2026-10-19 [frontend] DONE: HealthView.vue, DbCapacityBar.vue (shared w/ AdminView), types/api/router/NavBar; typecheck+build pass; needs GET /admin/health. Uncommitted.
- 2026-10-19 [backend-dev] START: GET /admin/health (admin dashboard backend)
- 2026-10-19 [backend-dev] DONE: GET /admin/health, 15s in-process cache (app/health.py, routers/admin.py, dependencies.get_kube); KubeProvider.cluster_snapshot (base/kube/fake); MAX_NODES config + bootstrap.yml literal (2); RBAC read-only nodes + metrics.k8s.io nodes/pods get/list; tests/test_admin_health.py. Uncommitted.
- 2026-10-19 [frontend] START: per-app health check fields (form, list, spec sample)
- 2026-10-19 [frontend] DONE: types, AppFormView section+validation, AppsListView line, SpecEditor sample; typecheck+build pass. Uncommitted.
- 2026-10-19 [backend-dev] START: per-app HTTP health checks (opt-in, custom path) + PORT env injection
- 2026-10-19 [backend-dev] DONE: App.health_check_enabled/health_check_path (migration a7d4e0c3b9f1), app/health_path.py validator, schemas/spec (`healthCheck`)/routers/reconciler/k8s_manifests probes; PORT=<port> container env unless user env defines PORT; PATCH and spec update set status=pending on health change so the reconciler rolls the pod; docs/CLI.md; tests/test_health_check.py; test_db_ca_cert downgrade target fixed. Uncommitted.
- 2026-10-19 [backend-dev] START: e2e bugfixes (updates not rolling out, MySQL egress port, Docker Hub registry)
- 2026-10-19 [backend-dev] DONE: PATCH/spec set status=pending on image/port/env/health change (no-ops don't); rotation already sets pending (test added); allow-egress-mysql uses DbServer.port (3306 fallback); registry: SSRF guard (non-global IPs refused, https realm only), redirects followed on manifests, hub aliases, attestation-safe amd64 fallback, failure reasons logged; tests/test_rollout.py + test_registry.py additions. 228 passed, 56 skipped. Not committed.
- 2026-10-19 [infra-ops] START: bootstrap installs metrics-server; platform pods roll on picopaas-env changes
- 2026-10-19 [infra-ops] DONE: bootstrap.yml metrics-server step (chart 3.14.0, deploy/metrics-server/values.yaml, 1 replica 20m/48Mi, no insecure-tls, waits for APIService Available); deployment.yaml has `picopaas.io/env-checksum` pod annotation placeholder, sed'd from sha256 of live picopaas-env data in both bootstrap.yml and deploy.yml; deploy/README.md note. helm template ok; nothing applied. Uncommitted.
- 2026-10-19 [frontend] START: UI polish from Chas's screenshots (Health n/a cells, nodes card, node headroom note, Admin DB-server spacing, gutters)
- 2026-10-19 [frontend] DONE: HealthView usage cells ("—" + limit / "37m / 250m (15%)"), Nodes card w/o "?", amber scheduling-headroom note (>=85% requested), AdminView DB-server spacing + select width + scrollable table wrappers, Admin/Health/Apps share max-w-5xl px-4 sm:px-6. typecheck+build pass. Uncommitted.
- 2026-10-19 [backend-dev] START: webpage testapp (HTML/CSS/JS) + e2e test
- 2026-10-19 [backend-dev] DONE: testapps/webpage (+Dockerfile, CSP), workflow matrix, harness IMG, tests/e2e/test_webpage.py, README. Local curl verified; 237 passed, 58 skipped. Uncommitted.
- 2026-10-19 [backend-dev] START: rollout quota headroom + stuck-rollout status + https storage.endpoint
- 2026-10-19 [backend-dev] DONE: app-quota sized 2 pods, Deployment RollingUpdate surge 1/unavail 0, kube.get_status reports deploying while updatedReplicas<replicas or observedGeneration<generation; storage.endpoint now https:// (spaces.py, fake, reconciler._storage_refs normalises old rows), docs + tests. Uncommitted.
- 2026-10-19 [backend-dev] START: reconciler drift sweep (rendered_hash) for running apps
- 2026-10-19 [backend-dev] DONE: apps.rendered_hash (migration a1b2c3d4e5f6); reconcile_all re-applies running apps whose rendered manifest hash changed (no deploying flip; failures retried next tick). Uncommitted.
- 2026-10-19 [backend-dev] START: DR sweep (re-apply running apps whose namespace is missing) + provisioning-failure logging
- 2026-10-19 [backend-dev] DONE: KubeProvider.list_app_namespaces() (label picopaas.io/app-id; kube.py + fake), reconcile_all lists once per tick and forces re-apply of running apps with missing app-<id> ns (list failure = no forcing); reconciler `_provider_error_summary` (class + pymysql code/msg or botocore Code, secrets scrubbed, <=300) logged on db/storage provisioning failure, status_detail unchanged; tests added. Uncommitted.
- 2026-10-19 [backend-dev] START: undecryptable-ciphertext resilience + Spaces private-IP egress policy
- 2026-10-19 [backend-dev] DONE: crypto.try_decrypt; EnvVarOut.undecryptable (placeholder "" + warning log; EnvVarIn ignores echoed flag); PATCH/spec diffing tolerant; reconciler marks error "stored secrets unreadable (platform key changed); re-enter secrets or rotate credentials" without aborting the sweep; rotation/delete never decrypt old creds. Reconciler resolves storage endpoint (5-min cache), private IPs -> `allow-egress-storage` NetworkPolicy (/32, TCP 443) via render_app_manifests(storage_ips=); DNS failure = no policy. tests/test_undecryptable.py. Uncommitted.
- 2026-10-19 [backend-dev] START: /admin/health users + workloads counts
- 2026-10-19 [frontend-dev] START: uberadmin lands on Health; Deploy button; users/workloads/pending-approval on Health
- 2026-10-19 [frontend-dev] DONE: / redirects by role (uberadmin -> /admin/health, users -> /apps; list now at /apps, nav 'My apps'); Health: Deploy button, users/pending/workloads cards, approve list (fields optional). Uncommitted.
- 2026-10-19 [backend-dev] DONE: /admin/health adds users, pending_users, workloads (health.db_counts, uncached, merged per request); test_platform_counts. Uncommitted.
- 2026-10-19 [backend-dev] START: docs — USER_GUIDE.md, OPERATIONS.md, API.md (verified against code)
- 2026-10-19 [backend-dev] DONE: docs/USER_GUIDE.md, docs/OPERATIONS.md, docs/API.md + README link line. Docs only. Uncommitted.
- 2026-10-19 [backend-dev] START: review fixes (teardown retry, admin audit, db ca_cert, testapps README, cost docs)
- 2026-10-19 [backend-dev] DONE: review fixes: delete_app keeps rows+soft-delete on failed drop/bucket delete (not-found = ok, logged via _provider_error_summary); PATCH /admin/users audited + 404 deleted; db-server create/patch audited; ca_cert on create/patch (x509 validated, "" clears); testapps README valid specs + test; cost docs ≈$60 (+$28 2nd node). Tests pass. Uncommitted.
- 2026-10-19 [infra-ops] START: LB hairpin fix (do-loadbalancer-hostname annotation + lb A record)
- 2026-10-19 [infra-ops] DONE: deploy/traefik/values.yaml annotation; bootstrap.yml lb A record + IP via doctl (svc status has hostname now). externalTrafficPolicy/proxy-protocol unchanged. Uncommitted.
- START (backend): env ref validation (pentest #16) + pentest harness toolbox arg fixes
- DONE (backend): validate_env_value rejects any value containing `${{` unless exactly one allowed, available ref (422 lists allowed refs; create/PATCH/spec); tests in tests/test_env_refs.py; pentest port_scan now passes int list, mysql_login no longer sends empty ca_pem. Uncommitted.
- 2026-10-19 [backend-dev] START: issue #8 reconciler retry with backoff for errored apps
- 2026-10-19 [backend-dev] DONE: issue #8. App.error_count/last_attempt_at/error_retryable (migration b7e2d9a4c1f3, single head); reconcile_all retries retryable errors at 1m/5m/15m/60m (cap); env-invalid and unreadable-secrets are non-retryable; success/config change reset; AppOut.retry_at. Tests in tests/test_reconciler.py. Uncommitted; frontend "will retry at" display is a follow-up.
- 2026-10-19 [infra-ops] START: platform.* host + apex landing (DNS, ingress, host-routing middleware, reserved hostnames, URL defaults)
- 2026-10-19 [infra-ops] DONE: bootstrap.yml upserts A `@`/`platform` (+`*`,`picopaas`,`lb`); ingress routes platform/picopaas/apex; cert already listed the apex (same secret name, so TLSStore unchanged); config PLATFORM_HOST/APEX_HOST/LEGACY_HOSTS; host_routing middleware in app/main.py (legacy 301, apex landing/privacy, /privacy on platform); RESERVED_HOSTNAMES in app/naming.py; tests/test_host_routing.py; URL defaults/docs updated. Uncommitted; not applied. Needs landing.html/privacy.html from frontend.
- 2026-10-19 [frontend-dev] START: admin UX overhaul, per-app resource overrides UI, apps list link/retry, landing+privacy pages
- 2026-10-19 [frontend-dev] DONE: AdminView rewrite, ResourcesDialog.vue, AppsListView retry/ext link, NavBar Privacy link, frontend/public/{landing,privacy}.html; typecheck+build ok. Uncommitted.
- 2026-10-19 [infra-ops] DONE: hostname suffix 5->7 chars, word lists expanded (~150 each); existing apps unchanged; docs/tests updated.
- 2026-10-19 [backend-dev] START: health snapshot background refresh + per-app resource overrides
- 2026-10-19 [backend-dev] DONE: app/health.py caches only the k8s snapshot, refreshed every 15s per replica (scheduler job + warm-up thread; "warming": true while empty; sync fallback when no scheduler); app/resources.py, App cpu/mem override columns (migration c8d4e1f7a2b9), PATCH /admin/apps/{id}/resources, AppOut/AdminAppOut `resources`, manifests/quota(2x limits)/LimitRange max from effective values. Uncommitted.
- 2026-10-19 [frontend-dev] START: dev/test banner, register terms checkbox, privacy reword, noindex
- 2026-10-19 [frontend-dev] DONE: DevBanner (login/register/slim shell), landing+privacy banner, register accept_terms checkbox, privacy reword + abuse@ contact, noindex, public/404.html. typecheck+build ok. Uncommitted.
- 2026-10-19 [backend-dev] START: accept_terms at signup, robots.txt + X-Robots-Tag, unknown-app-host 404
- 2026-10-19 [backend-dev] DONE: users.terms_accepted_at (migration d9e5f2a8b3c1), register requires accept_terms, admin users list exposes it; /robots.txt + X-Robots-Tag via outermost middleware; unknown *.APP_DOMAIN hosts get static/404.html (inline fallback) 404; catch-all Ingress (priority 1) in deploy/platform/ingress.yaml; docs + tests. Uncommitted.
- 2026-10-19 [frontend-dev] START: public/suspended.html (503 page for suspended apps)
- 2026-10-19 [frontend-dev] DONE: frontend/public/suspended.html (matches 404.html); build ok, static/suspended.html present. Uncommitted.
- 2026-10-19 [backend-dev] DONE: suspended apps: reconciler omits Ingress and calls KubeProvider.delete_ingress (base/kube/fake); middleware serves static/suspended.html 503 (10s cached hostname lookup, indexed) vs 404; docs/tests. Uncommitted.
- 2026-10-19 [general-purpose] START: doc test vs live platform (USER_GUIDE/API/CLI/testapps README), fix doc inaccuracies
- 2026-10-19 [backend-dev] START: DB quota enforcement fix (stale stats, FLUSH, visibility)
- 2026-10-19 [backend-dev] DONE: db_size_bytes sets information_schema_stats_expiry=0 (also in deploy/mysql app_size proc; initdb only runs on fresh volume); set_read_only drops FLUSH PRIVILEGES and best-effort KILLs app user's connections on revoke; poll_quotas logs INFO on revoke/restore + per-run summary, WARNING (scrubbed) on failures; quota_job no longer skips silently when Spaces is unconfigured (uses _NoStorage). Leader lock verified shared/guarded. Tests in tests/test_quota.py. Uncommitted.
- 2026-10-19 [general-purpose] DONE: doc test vs live: JSON create/patch/list/delete, spec YAMLs (USER_GUIDE + testapps README incl. toolbox), CLI apply/apps/status/rotate, error cases all match. Fixed docs: spec returns 200 + is declarative, retry_at/resources fields, invalid-ref 422 text, app-limit-checked-first, rotate 409 wording, abacusaas port/path, CLI placeholder image + output/errors (docs/USER_GUIDE.md, docs/API.md, docs/CLI.md). Uncommitted.
- 2026-10-19 [frontend-dev] START: in-product docs (/guide), new caps in UI, landing page fixes
- 2026-10-19 [frontend-dev] DONE: /guide/{user,api,cli} public pages (GuideView.vue, marked+DOMPurify, ?raw from docs/ via @docs alias), NavBar/landing/"?" deep links, Dockerfile COPY docs/ + .dockerignore un-ignores docs, caps (100m/384Mi, 50MB DB, 100MB bucket, 256MB image) in landing + ResourcesDialog. Uncommitted.
- 2026-10-19 [backend-dev] START: apply lowered platform caps everywhere (app/config.py single source)
- 2026-10-19 [backend-dev] DONE: caps in app/config.py (env-overridable) used by k8s_manifests, quota, resources, image check; tests/docs/e2e updated; pytest green. Uncommitted.
- 2026-10-19 [backend-dev] START: allow named non-root USER (run as UID 10001), PSA restricted labels on app namespaces
- 2026-10-19 [backend-dev] DONE: named non-root USER accepted (apps.run_as_uid=10001 -> runAsUser/runAsGroup, migration e7a3c9b1d5f2); PSA enforce-version label on app namespaces (reapplied by reconciler via hash change); only Deployment is created in app ns (compliant); tests+docs+UI hint updated; 344 passed/58 skipped. Uncommitted.
- 2026-10-19 [backend-dev] START: single platform key (derive SECRET_KEY), MultiFernet rotation + startup re-encrypt, bootstrap.yml
- 2026-10-19 [backend-dev] DONE: FERNET_KEY only required (SECRET_KEY = hmac-sha256 derived unless set); crypto.py MultiFernet + FERNET_KEYS_PREVIOUS + rotate_all (env vars, DbServer, AppDatabase, AppStorage) run at startup before bootstrap_db_server; bootstrap.yml drops SECRET_KEY, keeps old key in FERNET_KEYS_PREVIOUS (max 3, fail closed on non-NotFound); deployment uses envFrom so it is picked up; docs + tests/test_key_rotation.py; 347+ passed. Uncommitted.
- 2026-10-19 [scm-security] START: commit+push key rotation chunk, watch ci/deploy, then bootstrap
- 2026-10-19 [scm-security] pytest green (347 passed); committing
- 2026-10-19 [frontend-dev] START: UX chunk B (foundation): tokens/components, NavBar, favicons, a11y, dialogs, toasts, api client, static pages. frontend/ only.
- 2026-10-19 [frontend-dev] DONE: UX chunk B foundation (frontend/ only, uncommitted): .btn-*/.input/.card/.alert-*/.badge-* classes in style.css, NavBar mobile sheet + icon + skip link, native-dialog ConfirmDialog/ResourcesDialog (useModal, require-text), toast store + ToastHost, apiFetch (Accept, ApiError.fieldErrors, 401->/login?next), route titles, favicons/manifest, static pages restyled (L1-L12, UX-14), new optional TS fields (features/limits/status_reason).
- 2026-10-19 [frontend-dev] START: UX chunk C1: apps list, app detail page (logs/restart/delete/credentials), app form, env editor, YAML tab. frontend/ only.
- 2026-10-19 [backend-dev] START: UX chunk A1: cache headers (UX-1), backend error copy, enumeration-safe register, /auth/me features+limits, admin reset-password + change-password, CLI wording
- 2026-10-19 [frontend-dev] START: UX chunk C2: login/register/pending, account page, API keys, admin tabs, resources dialog, health, guide mobile. frontend/ only.
- 2026-10-19 [backend-dev] START: UX chunk A2 (quota-sizing rollout bug, deploy timeout, FailedCreate detect, retryable unreadable secrets, logs+restart endpoints, status_reason + human status_detail). Files: reconciler, providers/kube+base+fake, k8s_manifests, crypto, routers/app_ops.py, models, schemas, migration f1a8c4d7e2b5, deploy/platform/rbac.yaml
- 2026-10-19 [backend-dev] DONE: UX chunk A1 (uncommitted): Cache-Control/Vary on SPA+JSON, JSON Accept never gets SPA; plain-language HTTPException/validation copy (app/routers, dependencies, spec, env_refs, health_path); enumeration-safe register (DECISION: a duplicate email returns the same 201 pending body + a session cookie for user 0, which authenticates nobody and grants/changes nothing; audited as user.register_duplicate; owner just logs in; no email sent. Residual: a follow-up /auth/me is 401 for the duplicate, 200 for a real new signup); /auth/me features+limits; POST /admin/users/{id}/reset-password (audited, flags users.must_change_password, migration a9f1c2d3e4b5, informational only, sessions not revoked) + POST /auth/change-password; CLI C1-C12; docs API.md/CLI.md; tests/test_ux_backend.py
- 2026-10-19 [backend-dev] DONE: UX chunk A2 (uncommitted). (1) Rollout-stuck bug: quota = new + max(new, live pod-template limits), read via KubeProvider.get_live_limits; steady-state rendered_hash is stored unless widened, so the sweep shrinks it back with no flapping; Deployment ReplicaFailure condition -> 'error: quota'/'replica_failure'; DEPLOY_TIMEOUT_MINUTES=10 (config) -> retryable 'timeout' error saying what we saw (App.deploying_since). (2) crypto.rotate_all summary at WARNING; unreadable secrets now retryable with backoff. (3) app/routers/app_ops.py: GET /apps/{id}/logs (tail<=1000, previous container marked), POST /apps/{id}/restart (App.restart_nonce pod-template annotation, 409 suspended, audited); registered in main.py. RBAC: pods/log get added to deploy/platform/rbac.yaml (deploy.yml applies rbac.yaml, so it ships on next deploy). (4) App.status_reason + human status_detail (STATUS_MESSAGES in reconciler.py); kube.get_status now returns 'error: <code>'. Migration f1a8c4d7e2b5 (down_revision a9f1c2d3e4b5 = A1's; single head). Tests: tests/test_rollout_recovery.py.
- 2026-10-19 [frontend-dev] DONE: UX chunk C1 (frontend/ only, uncommitted): AppsListView rewrite (rows, ⋯ menu, empty state/sample, n/limit, polling, failure text+actions), new AppDetailView at /apps/:id (status, logs, restart, config, connection details, credentials, danger zone), AppFormView (helper copy, image requirements, advanced health, inline errors, sticky actions, limits/features), EnvVarEditor, SpecEditor; new components AppStatusNote/CopyButton/HostLink/RowMenu/DeleteAppDialog; composables appUi/envKeys; api getAppLogs/restartApp. typecheck+build ok; screenshots 1440/390 no overflow.
- 2026-10-19 [frontend-dev] DONE: UX chunk C2 (frontend/ only, uncommitted): Login/Register/AccountStatus (poll), new /account page + PasswordBanner, API keys (tabs/snippets/leave warning), Admin tabs (?tab=) + stacked rows + reset-password + add-server dialog + setup checklist, Health verdict, Guide mobile sticky contents, ResourcesDialog link. Depends on A1 endpoints (reset-password, change-password, features, must_change_password).
- 2026-10-19 [scm-security] START: commit UX backend + frontend (2 commits), push, watch ci/image/deploy
- 2026-10-19 [scm-security] DONE: pushed 527df70 + a7d9c8a; ci/image/deploy green
- 2026-10-19 [backend-dev] START: fix quota sizing to use existing pods' limits (prod app 48 stuck: template already new, old pod bigger)
- 2026-10-19 [backend-dev] DONE: quota = max(2*new, sum(existing non-terminal pod limits)+new); KubeProvider.get_live_limits replaced by get_used_limits (kube/base/fake; fake uses `used_limits[ns]`); reconciler widens from it, drift-hash no-flap kept, shrinks to 2x once old pod gone; retryable error/quota apps recover via the existing retry sweep (regression test in tests/test_rollout_recovery.py). 390 passed/58 skipped. Uncommitted.
- 2026-10-19 [scm-security] START: commit quota-sizing fix, push, watch ci/image/deploy
- 2026-10-19 [scm-security] DONE: pushed ff6136f (quota sizing from existing pods); ci/image/deploy green
- 2026-10-19 [scm-security] DONE: SPA catch-all 404 route (unknown paths rendered a blank shell)
- 2026-10-19 [scm-security] DONE: reload once onto the target route when a lazy chunk from a previous build is gone (post-deploy dead links)
- 2026-10-19 [frontend-dev] START: UI density/icon-button/table/mono polish pass (frontend/)
- 2026-10-19 [scm-security] START: commit probe read/size/clear endpoints
- 2026-10-19 [scm-security] DONE: pushed 7c95ed4 (probe read/size/clear endpoints); testapps+ci green
- 2026-10-19 [frontend-dev] DONE: UI polish: 32px buttons/inputs, IconButton (lucide-vue-next 1.0.0), fixed-width grids/tables (admin, keys, health), .mono shared class, tighter spacing. Uncommitted. Shots: scratchpad/uxreview/polish/{before,after}
- 2026-10-19 [scm-security] START: commit UI polish pass
- 2026-10-19 [scm-security] DONE: pushed 66e4d40 (UI polish); ci/image/deploy green
- 2026-10-19 [scm-security] DONE: select chevron spacing; DB server status options labelled + explained (drain)
- 2026-10-19 [scm-security] DONE: DB server status select fits its column; Health verdict names not-ready platform pods
- 2026-10-19 [backend-dev] START: digest pinning + floating-tag watcher (image_digest, auto_update, check-image, restart re-resolve)
- 2026-10-19 [backend-dev] DONE: registry.resolve_digest + ImageInfo.digest; app/image_check.py (shared checks, refresh, watch_all); scheduler image_watch job (IMAGE_WATCH_MINUTES=10); Deployment runs repo@digest (tag in annotation); POST /apps/{id}/check-image; restart re-resolves; AppOut/AppCreate/AppUpdate fields; UI (digest, check button, warn alert, auto-update checkbox); docs; migration b4c7d1e8f2a6; tests/test_image_watch.py. Uncommitted.
- 2026-10-19 [scm-security] START: commit/push digest pinning + image watcher
- 2026-10-19 [backend-dev] START: 250MB ephemeral-storage cap (config, manifests, quota, eviction->disk, health, docs)
- 2026-10-19 [backend-dev] START: quota hardening (60s DB poll + immediate run, session kill, key revoke ordering, storage delete/usage endpoints, AppOut.quota, UI, CLI, docs, weekly quota e2e)
- 2026-10-19 [backend-dev] DONE: 250MB ephemeral-storage cap: config EPHEMERAL_LIMIT_MI, manifests/quota/LimitRange, get_used_limits, eviction->status_reason=disk, health node conditions + DiskPressure verdict, /auth/me limits.ephemeral_mi, docs, QUESTIONS to-do (bootstrap.md). Uncommitted.
- 2026-10-19 [backend-dev] DONE: quota hardening. Scheduler: db_quota_job every 60s (DB_QUOTA_POLL_SECONDS) + storage_quota_job every 300s, both run at startup (next_run_time=now), leader-only. Every quota flip / kill result logs at WARNING. DB: `set_read_only(True)` now returns {listed,killed,failed,list_error}; re-enforced (sessions killed again) on every poll while over; if kills can't be confirmed (failed>0, listing failed, or 0 sessions visible) the app's pod is rolled once on the flip as a fallback. New proc picopaas_ops.kill_app_sessions in deploy/mysql/statefulset.yaml (only applies on a fresh in-cluster data volume; SQL not run against a live server). UNVERIFIED: whether DO managed `doadmin` can KILL other users' threads or even SEE them in PROCESSLIST (no PROCESS => silently empty list); the code tolerates both and logs; QUESTIONS.md asks Chas to check the first prod over-cap log line. Storage key ordering: a swapped key's predecessor is revoked only once the app is out of `pending` (rollout done), so reads never break; cost = old read-write key (writes) survives rollout + <=1 storage poll (<=5 min), accepted since storage overshoot is cheap and a read outage is not. New: POST /apps/{id}/storage/delete, GET /apps/{id}/storage/usage, GET /apps/{id}/database/usage (app/routers/usage.py), ObjectStore.usage/delete_objects, AppOut.quota, UI Usage section + FreeStorageDialog + 'over cap' badge, CLI status usage + storage-delete, docs, .github/workflows/quota-e2e.yml (weekly; skips itself without E2E secrets), extended tests/e2e quota tests. Note: InnoDB DELETE does not shrink the reported size; docs say TRUNCATE/DROP. rotate-credentials still revokes the old key immediately (user-initiated, unchanged). No migration. Uncommitted.
- 2026-10-19 [scm-security] DONE: committed ephemeral-storage + quota hardening as two commits; pushing
- 2026-10-20 [backend-dev] START: API key name + last_used_at (throttled 5 min), migration c5d8e2f9a3b7
- 2026-10-20 [backend-dev] DONE: API key name (String(64), trimmed/printable, optional body) + last_used_at in list, throttled 5 min in dependencies.py; migration c5d8e2f9a3b7 (single head); docs/API.md; tests in test_api_keys.py. 434 passed/60 skipped. Uncommitted.
- 2026-10-20 [scm-security] START: commit API key names + db-servers features fix
- 2026-10-20 [backend-dev] DONE: features.databases means a new DB can be placed (active server with a free slot); 'Full' status hidden as legacy (same as draining); drain help corrected
- 2026-10-20 [backend-dev] START: kill over-cap DB sessions as the app's own user first (doadmin gets 1095)
- 2026-10-20 [backend-dev] DONE: over-cap DB sessions are killed as the app's own user first (MySQLAdminClient._kill_as_app_user; set_read_only gets app_password, result has 'path'), then admin kill, then pod roll. quota.py passes the decrypted password; tests added. QUESTIONS to-do resolved. Uncommitted.
- 2026-10-20 [infra-ops] START: docs update: actual cost (ARCHITECTURE §1, OPERATIONS §8, QUESTIONS to-do) + README rewrite with architecture diagrams
- 2026-10-20 [infra-ops] DONE: ARCHITECTURE §1 rewritten (baseline ~$60.15, worst ~$88.15; node $28 unverified from DO), OPERATIONS §8 points to it, QUESTIONS to-do for CLAUDE.md budget rule, README rewritten (Mermaid arch + deploy sequence, stack table). Uncommitted.
- 2026-10-20 [scm-security] START: commit app-user session kill (backend) + README/cost docs as two commits; push
- 2026-10-20 [scm-security] DONE: committed both; pushing
- 2026-10-20 [tech-writer] START: docs + in-app text review (README, guides, API, CLI, ops, frontend strings, backend messages, CLI output)
- 2026-10-20 [tech-writer] DONE: docs + in-app text review. Fixed stale facts in USER_GUIDE/API/CLI/OPERATIONS/ARCHITECTURE/README (retries, logs, resources, db-server CA, audit actions, replicas, $28 node); 'admin' -> 'administrator' in API messages (tests updated); UI/landing/privacy copy; secrets-unreadable and OOM status messages; README Mermaid reordered; variables.tf max_nodes text. Open questions added to QUESTIONS.md. Pytest, typecheck, build pass. Uncommitted.
- 2026-10-20 [scm-security] START: commit docs pass + copy pass as two commits; push; watch CI
- 2026-10-20 [backend-dev] START: app recent events (app_events table, detect evicted/OOM/crash, GET /apps/{id}/events, AppOut.last_event, UI, CLI, docs, tests)
- 2026-10-20 [backend-dev] DONE: recent events. `app_events` table (migration a3f7c1d9e5b2), `KubeProvider.app_pod_events()` (one cluster-wide pod list per sweep; fake: `pod_events`), `reconciler.record_app_events` (dedupe on pod uid + restartCount/finishedAt, keep 20), `GET /apps/{id}/events`, `AppOut.last_event` (<24h), UI (detail list + alert-info on detail and list), CLI status, USER_GUIDE/API docs, tests/test_app_events.py. Uncommitted.
- 2026-10-20 [scm-security] START: commit app events feature; push; watch CI/image/deploy
- 2026-10-20 [scm-security] DONE: 556eab8 pushed; CI, image, deploy all success
- 2026-10-20 [backend-dev] START: probe /db/tlscheck + py3.13; verify rotation-repairs-unreadable claim (tests/test_rotation_repair.py)
- 2026-10-20 [backend-dev] START: rename role uberadmin -> platform_admin (app, frontend, tests, docs, migration b4c8e2f6a1d3); signup wording; QUESTIONS cleanup
- 2026-10-20 [backend-dev] DONE: role renamed everywhere (User.is_platform_admin, require_platform_admin, router guard/NavBar/views, e2e harness, API/ops/user docs); data migration b4c8e2f6a1d3 (users.role is VARCHAR(16), no CHECK/enum; downgrade reverses). ROLLOUT NOTE: migration runs at deploy; old pods still check role == 'uberadmin', so Chas briefly loses admin in old pods until replaced (accepted). Signup wording applied (landing, privacy, dev banner, register/login/pending, README). bootstrap.md still says "uberadmin" (contract file, left for Chas). Uncommitted.
- 2026-10-20 [backend-dev] DONE: probe on python:3.13-slim + GET /db/tlscheck (db() already cleared VERIFY_X509_STRICT, unchanged); tests/test_rotation_repair.py (2 tests, both pass: rotation repairs DB/storage; env-var needs re-entry). No app code changed. 450 passed/60 skipped. Uncommitted.
- 2026-10-20 [scm-security] START: commit rename / rotation-doc / S3+probe+test as three commits; push; watch ci, image, deploy, testapps; verify
- 2026-10-20 [scm-security] DONE: 3 commits made (CLAUDE.md budget edit left uncommitted, not in scope); pushing
- 2026-10-20 [scm-security] DONE: 5ea84b8 pushed; ci/image/deploy/testapps success; /auth/me role platform_admin, healthz ok, demo 200
- 2026-10-20 [backend-dev] START: registry SSRF (manual redirects, pinned IP) + manifest/config digest verification
- 2026-10-20 [backend-dev] START: pentest fixes: auth rate limiting (DB-backed), security headers + CSP, DOMAIN_SUFFIX -> config.APP_DOMAIN (app/ratelimit.py, main.py, routers/auth|apps|admin.py, migration, tests)
- 2026-10-20 [backend-dev] DONE: registry hardening: manual redirects (https only, <=5 hops, per-hop public-host check, Authorization dropped cross-host), connect-time pinned-IP backend (resolve once, all-global check, connect to that IP; SNI unchanged), body cap 4MiB, manifest+config bytes verified against digest (header must match computed; pin = hashed digest). Decompressed-layer residual documented in code. Tests updated/added in test_registry/test_image_watch/test_non_root. 459 passed/60 skipped. Uncommitted.
- 2026-10-20 [backend-dev] DONE: auth throttling (app/ratelimit.py, auth_throttle migration c9d1e3f5a7b2), security headers + CSP in app/main.py, DOMAIN_SUFFIX removed (APP_DOMAIN), tests/test_auth_throttle_and_headers.py. 470 passed. Uncommitted; migration needs alembic upgrade head on deploy.
- 2026-10-20 [scm-security] START: commit+push registry hardening and auth hardening
- 2026-10-20 [scm-security] DONE: b9b8dee + ed32c68 pushed; ci/image/deploy success (auth_throttle migration deployed); /auth/me 200 with HSTS/CSP/nosniff/frame headers, healthz ok
- 2026-10-20 [infra-ops] START: PENTEST #19 step 1 (per-namespace RoleBinding for platform SA) + quota pod-count/surge fix.
- 2026-10-20 [infra-ops] DONE: reconciler now applies RoleBinding app-<id>/picopaas-platform -> ClusterRole picopaas-app-ns (rbac.yaml; `bind` limited by resourceNames, plus rolebindings get/list/create/patch); broad cluster-wide grant KEPT (step 2 is a separate later deploy, see Now/Next). Quota: found live cause of test_09 failure = ResourceQuota `pods` was fixed at 2 so a roll while the old pod still Terminating (storage key swap then recovery roll) was rejected; get_used_limits now returns `pods`, quota pods = max(2, existing+1), reconciler widens on that too. Files: app/k8s_manifests.py, app/reconciler.py, app/providers/kube.py, deploy/platform/rbac.yaml, tests. Uncommitted.
- 2026-10-20 [scm-security] START: commit+push RBAC step1 + quota fix; watch ci/image/deploy; verify RoleBinding on app-48
- 2026-10-20 [scm-security] DONE: 508dcb2 pushed; ci/image/deploy success; healthz ok, /auth/me 200. diag.yml cannot list RoleBindings (no rolebinding step), so app-48 binding NOT verified; suggest adding 'kubectl -n $NS get rolebinding' to diag.yml (owner: infra-ops)
- 2026-10-20 [infra-ops] START: pentest low-severity hardening (INF-5/6/7/9, ISO-2, diag RBAC/quota visibility).
- 2026-10-20 [infra-ops] DONE: deploy.yml job `if:` requires same-repo push + success for workflow_run (INF-5; deploy is the only workflow_run consumer); diag.yml steps printing cluster text wrapped in ::stop-commands:: (trap resumes) + new RBAC/quota step (INF-9); toolbox gets cryptography==43.0.3; all third-party actions SHA-pinned with version comments (INF-6); prevent_destroy on DOKS, managed MySQL, Spaces bucket (INF-7; terraform validate ok, not applied). Report-only: MYSQL_EGRESS_CIDR /32 narrowing, mysql:8.4 / gitleaks:latest tags. Uncommitted.
- 2026-10-20 [scm-security] START: commit+push CI/infra hardening; watch ci/image/deploy/testapps; verify RoleBinding app-48/app-60
- 2026-10-20 [scm-security] DONE: 6a60d2b pushed; ci/image/testapps/deploy success (deploy ran via workflow_run, not blocked by guard); picopaas-platform RoleBinding verified present in app-48 and app-60; healthz ok
- 2026-10-20 [infra-ops] START: PENTEST #19 step 2 (drop cluster-wide secrets/quotas/limitranges/services from picopaas-reconciler).
- 2026-10-20 [infra-ops] DONE: verified kube.py only touches those via apply() of app-<id> manifests (namespace, then RoleBinding, then rest); no cluster-wide/other-ns access (only cluster-wide ops: pods list, nodes, metrics, namespaces). Removed the rule from rbac.yaml; kept namespaces, rolebindings, bind, pods, pods/log, nodes, metrics, deployments, networkpolicies/ingresses. Uncommitted, not applied.
- 2026-10-20 [frontend] START: light polish of frontend/public/landing.html (Oxford comma, drop top limits strip, spacing/hierarchy).
- 2026-10-20 [frontend] DONE: landing.html only; serial comma fixed, top limits strip removed (numbers only in table), eyebrow tag, h2 rules, mono table values, deduped approval copy; no JS/external resources; scrollWidth == viewport at 1440 and 390. Uncommitted.
- 2026-10-20 [scm-security] DONE: 408876e pushed (landing polish + docs); ci/image/deploy success; live landing 200, serial-comma present, old strip absent. CLAUDE.md left unstaged.
- 2026-10-20 [backend-dev] START: probe /db 500 "No username set" on py3.13 (UID 10001)
- 2026-10-20 [backend-dev] DONE: root cause = pymysql calls getpass.getuser() at import; 3.13 raises OSError (not KeyError) with no passwd entry/USER env. DB_USER was fine. Fix: testapps/probe Dockerfile `ENV HOME=/tmp USER=app` + os.environ.setdefault in app.py; USER_GUIDE troubleshooting row. Verified live (existing image + USER/HOME app env): all endpoints 200, tlscheck matches guide. New image needs scm-security commit/push to rebuild. Uncommitted.
- 2026-10-20 [scm-security] START: commit+push probe UID/py3.13 fix; watching testapps + ci.
- 2026-10-20 [scm-security] DONE: 0f7da86 pushed; testapps+ci+image+deploy success; probe image ghcr.io/seaburr/picopaas-testapp-probe:0f7da86 (sha256:0dde23077c1cc00e68c4eefce160bc2dbf33555173958ffbe7bdd5eb29933fc2). CLAUDE.md left unstaged.
- 2026-10-20 [scm-security] START: commit+push Ghost CMS demo (examples/ghost image + examples workflow); watch build, make GHCR package public.
- 2026-10-20 [scm-security] DONE: Ghost CMS demo build committed (non-root UID 10001, env-translation entrypoint, MySQL content + S3 image storage); image ghcr.io/seaburr/picopaas-example-ghost. CLAUDE.md left unstaged.
- 2026-10-20 [scm-security] BLOCKED: 512d03f examples build (ghost) failed: Dockerfile pins ghost-storage-adapter-s3@3.0.1 which does not exist on npm (ETARGET); latest is 2.8.0. Owner: whoever owns examples/ghost. GHCR package not created yet, so visibility not set.
- 2026-10-20 [scm-security] START: commit+push ghost adapter pin fix (2.8.0); watching examples build.
- 2026-10-20 [scm-security] DONE: 1bf049f pushed; examples build (ghost) succeeded; image ghcr.io/seaburr/picopaas-example-ghost:1bf049f, anonymously pullable (public); compressed amd64 size 296.7 MiB (311 MB) EXCEEDS 256MB cap. CLAUDE.md left unstaged.
- 2026-10-20 [scm-security] START: commit+push Ghost S3 adapter opt-in (ARG WITH_S3=0); watching examples build.
- 2026-10-20 [scm-security] DONE: 997a705 pushed; examples build (ghost, WITH_S3=0) succeeded; image ghcr.io/seaburr/picopaas-example-ghost:997a705, anonymously pullable. BUT measured compressed amd64 size 268.1 MiB (281.1 MB) STILL EXCEEDS 256 cap (base is not ~168 MB as assumed). Owner: examples/ghost. CLAUDE.md left unstaged.
- 2026-10-20 [scm-security] START: commit+push ghost chown-only-content/HOME=/tmp size fix; watching examples build.
- 2026-10-20 [scm-security] DONE: 62fb596 pushed; examples build (ghost, WITH_S3=0) succeeded; ghcr.io/seaburr/picopaas-example-ghost:62fb596 anonymously pullable; compressed amd64 168.0 MiB (176.2 MB), UNDER 256 cap. CLAUDE.md left unstaged.
- 2026-10-20 [scm-security] START: commit+push ghost content.orig seeding fix; watching examples build.
- 2026-10-20 [scm-security] DONE: Ghost CMS live on PicoPaaS (app ghost-demo), content persists in MySQL across restart; S3-image variant over cap, follow-up. README at examples/ghost/README.md committed.
- 2026-10-20 [scm-security] START: commit+push platform PriorityClass (preemptionPolicy Never); verify deploy, 1 node, pods Running, healthz.
- 2026-10-20 [scm-security] DONE: cbbace4 pushed; CI/image/deploy green; PriorityClass picopaas-platform created, rollout complete; diag shows 1 node (no scale-up), 2 picopaas pods Running, healthz ok.
- 2026-10-20 [scm-security] START: commit+push hard app cap + Health UI fix.
- 2026-10-20 [scm-security] DONE: c5e6ff9 (app cap) + 1c38e4c (health UI) pushed; CI/image/deploy success; /auth/me max_apps=5 app_limit_effective=5; healthz ok. CLAUDE.md left unstaged.
- 2026-10-20 [backend] START+DONE: ${{ app.url }} env ref (https://<hostname>.<APP_DOMAIN>, always available); image cap 256->512 MB (config, docs, landing, UI fallback, tests); examples workflow builds Ghost with WITH_S3=1. NOTE: bootstrap.md still says 256MB; not edited (edit permission arrived only via agent message, not from Chas directly).
- 2026-10-20 [scm-security] START: commit+push image cap 512MB, app.url ref, Ghost S3 example.
- 2026-10-20 [scm-security] DONE: 346379b pushed; CI/image/examples/deploy success; /auth/me image_mb=512; healthz ok; ghost image :346379b built with WITH_S3=1.
- 2026-10-20 [scm-security] START: commit+push Ghost S3 assetHost fix (public Spaces URL).
- 2026-10-20 [scm-security] DONE: fd8497a pushed; examples workflow success; ghost image :fd8497a (WITH_S3=1). Not deployed.
- 2026-10-20 [scm-security] START: commit+push Ghost staffDeviceVerification=false (no SMTP lockout).
- 2026-10-20 [scm-security] DONE: c8d075e pushed; examples workflow success; ghost image :c8d075e (WITH_S3=1). Not deployed.
- 2026-10-20 [frontend] START: Recipes tab (docs/RECIPES.md, Ghost + BYO image lessons) in /guide; refresh examples/ghost/README.md.
- 2026-10-20 [frontend] DONE: Recipes tab added (GuideView, route regex, RECIPES.md, USER_GUIDE pointer, ghost README refreshed); typecheck+build pass; RECIPES in GuideView chunk. Uncommitted. Note: examples/ghost/docker-entrypoint.sh header comment still says no app.url / SITE_URL two-step; outside my scope.
- 2026-10-20 [scm-security] START: commit+push Recipes tab docs; watch ci/image/deploy/examples.
- 2026-10-20 [backend-dev] START: DB server SQL health metrics (connections, data size) on admin Health.
- 2026-10-20 [backend-dev] DONE: MySQLAdminClient.server_health, health.py per-server 15s cache, db_servers[] metrics, MANAGED_DB_DISK_MB, HealthView columns, tests, OPERATIONS. Uncommitted.
- 2026-10-20 [scm-security] START: commit+push DB health metrics; watch ci/image/deploy, verify /admin/health db_servers.
- 2026-10-20 [scm-security] DONE: DB health metrics pushed (21ebfe4); ci/image/deploy green; live db_servers metrics verified, error=null.
- 2026-10-20 [scm-security] START: commit+push table overflow pass / app-cap hiding; watch ci/image/deploy.
- 2026-10-20 [scm-security] DONE: table overflow/app-cap fix pushed (93f2313); ci/image/deploy green; healthz ok, / 200.
- 2026-10-20 [scm-security] START: commit+push DB host cpu/mem/disk metrics; watch ci/image/deploy, diagnose live host_metrics.
- 2026-10-20 [scm-security] DONE: host metrics pushed (94762f4); ci/image/deploy green; live host_metrics=null, error='metrics endpoint unreachable (ConnectError)'.
- 2026-10-20 [scm-security] START: commit+push host-metrics scrape off by default (#12); watch ci/image/deploy.
- 2026-10-20 [scm-security] DONE: fc37efb pushed; ci/image/deploy green; healthz ok; db_servers connections+data_size ok, host_metrics=null.
- 2026-10-20 [frontend] START: Download logs button on app detail Logs panel.
- 2026-10-20 [frontend] DONE: Download logs icon button (tail 1000, ANSI stripped, header line, <name>-<id>-logs.txt) in AppDetailView; typecheck+build pass. Uncommitted.
- 2026-10-20 [backend-dev] START: raise per-app MySQL MAX_USER_CONNECTIONS (Ghost app_93 hit the cap of 5).
- 2026-10-20 [backend-dev] DONE: config.MAX_DB_USER_CONNECTIONS (env, default 10); mysql.py CREATE USER uses it; new MySQLAdmin.set_user_connection_limit (ALTER USER, no-op in proc mode) called from reconcile_app on the running-app sweep so existing apps (app_93) are raised next sweep; errors logged, not fatal. deploy/mysql proc updated 5->10 (hardcoded). NOTE: production uses managed MySQL via the non-proc path, so the proc change only affects the in-cluster MySQL option and ships on a fresh data volume only. Docs: ARCHITECTURE, USER_GUIDE (pool size <= limit). tests/test_db_conn_limit.py. Uncommitted.
- 2026-10-20 [scm-security] DONE: committed docs/RECIPES.md (Ghost full YAML spec + picopaas apply / curl /apps/spec in Deploy section).
- 2026-10-20 [scm-security] DONE: committed docs/ROADMAP.md (custom domains + TLS tabled, in-progress list, Ghost load-test findings).
- 2026-10-20 [scm-security] START: commit templates catalog and image rollback/auto-rollback as two commits; push; verify deploy.
- 2026-10-20 [scm-security] DONE: committed templates catalog (/templates, GET /templates, ?template= prefill) and image history + rollback + deploy health-gate auto-rollback (migration b4e7a2c9d1f6). Pushed; see CI/deploy results.
- 2026-10-20 [scm-security] START: commit per-app CPU/memory history (app_metric_samples, metrics-history endpoint, sparklines); push; verify deploy + migration d2f6a8c0e4b9.
- 2026-10-20 [scm-security] DONE: committed per-app metrics history; scale-to-zero (item 7) deferred to backlog in ROADMAP. Pushed; see CI/deploy results.
- 2026-10-20 [scm-security] START: commit UI/UX pass on new features (app detail reorder, templates port wrap, inline prefill notice, apps-list count); push; verify deploy.
- 2026-10-20 [scm-security] DONE: committed UI/UX pass (frontend views only). Pushed; see CI/deploy results.
- 2026-10-20 [scm-security] START: commit RollbackRequest extra=forbid fix and landing/README/user-guide updates; push; verify deploy + landing content.
- 2026-10-20 [scm-security] DONE: committed as two commits (security fix; docs). Tests 503 passed, typecheck+build ok. Pushed; see CI/deploy results.
