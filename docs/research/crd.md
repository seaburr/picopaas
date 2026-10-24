# Research: how close is the PicoPaaS spec to a CRD?

Status: research only (2026-10-23). No code, cluster, or kubectl context was touched. Prompted by Chas: "Our YAML spec
looks a lot like a Kubernetes spec. How close are we to a CRD?"

Sources read: `app/spec.py`, `app/routers/spec.py`, `app/reconciler.py`, `app/k8s_manifests.py`, `app/models.py`,
`docs/ARCHITECTURE.md`, `docs/USER_GUIDE.md` section 4.

## Short answer

Close in shape, far in substance. The *document* is one afternoon from being CRD-shaped. The *system* is not: today the
platform DB is the source of truth, tenants have no Kubernetes credentials, and quotas, auth, placement, secrets and
cleanup of external resources (managed DB, Spaces bucket) all live in Python behind the API. A CRD only pays off if
something other than our API writes it (kubectl, Argo CD, Flux). Recommendation: phase 1 = option C (CRD-shaped
spec, accepted alongside the old one), phase 2 = option B (read-only mirror) only if Chas actually wants `kubectl get apps` /
GitOps visibility. Do not build a full operator (A) unless the product goal changes to "tenants use kubectl".

## 1. Gap analysis

### 1.1 What we have vs a CRD

| Aspect | Today (`app/spec.py`) | Proper CRD | Gap |
|---|---|---|---|
| Identity | `apiVersion: picopaas/v1` (no group, not `group/version`) | `picopaas.io/v1alpha1` | Cosmetic. Group must be a DNS name; `picopaas/v1` is not valid for a CRD. The platform domain is `picopaas.xyz`, so the group should arguably be `picopaas.xyz` (own the domain), not `.io` (we don't own it as far as the repo shows). Chas to confirm; this doc uses `picopaas.io` as he wrote it |
| Kind | none | `kind: App` | Add one constant field |
| Name | `name`: free text, 1-255 chars, display only, unique per owner | `metadata.name`: DNS-1123 subdomain (<=253, lowercase, no spaces), unique per namespace | **Real mismatch.** Existing names may contain uppercase/spaces/underscores. Map to `metadata.name` only when valid, otherwise keep as `spec.displayName` and derive `metadata.name` (e.g. from hostname) |
| metadata | none | name, namespace, labels, annotations, uid, generation, finalizers, ownerReferences | New. Platform-managed labels (`owner`, `app-id`) belong here |
| spec / status split | one flat doc; status lives in DB columns (`status`, `status_detail`, `status_reason`, `error_count`, `volume_phase`, ...) and is returned by the REST API | `spec` (user-writable) and `status` (controller-written, separate subresource) | Spec side is trivial: everything in `AppSpec` goes under `spec`. Status side is a mapping exercise (1.4) |
| Schema | Pydantic strict models, `extra="forbid"`, custom validators, plain-English errors | OpenAPI v3 *structural* schema (`type` everywhere, `additionalProperties: false` or `x-kubernetes-preserve-unknown-fields`), CEL `x-kubernetes-validations` | Most rules map 1:1 (1.2). A few cannot (registry lookups, image size, quota, "can't remove db once added", path checks needing regexes) and stay in the platform/operator |
| Defaults | Pydantic defaults | `default:` in the schema (applied by the API server on write/read) | 1:1, except `db_engine` which is deliberately *not* defaulted for backward compatibility |
| Printer columns | n/a | `additionalPrinterColumns` | Easy (1.3) |
| Subresources | n/a | `status` subresource (so `spec` writes and `status` writes don't race), optional `scale` | Needed; `scale` could expose `replicas` (admin-only today), skip |
| Finalizers | `reconciler.delete_app` tears down namespace, DB + user, Spaces bucket + key, volume; retries until done, then `status="deleted"` | `metadata.finalizers: [picopaas.io/cleanup]`; controller removes it when external cleanup is done | Same idea, different trigger (`deletionTimestamp` instead of `deleted_at`). The retry logic ports directly |
| Secrets | `secrets:` list of names; values arrive out-of-band (`POST /apps/spec` body), Fernet-encrypted in the platform DB | A CR is plain etcd data (and Git, under GitOps); it can only reference Secrets | Names-only list becomes `secretRefs` (or `envFrom`-style `valueFrom.secretKeyRef`). Value ingestion needs a different path (1.5) |
| Reference variables | `${{ db.url }}` expanded at reconcile time into the app's k8s Secret | CRDs have no templating | Keep as an opaque string in `spec.env`; the controller resolves it. Fine, but it is a platform convention, not Kubernetes-native (an alternative is a structured `valueFrom.ref: db.url`, see 1.5) |
| Immutability rules | "db can't be removed", "db_engine immutable once DB exists", "volume can't be removed" (checked against DB row in the router) | CEL transition rules (`oldSelf`) | Expressible in CEL, so the API server can enforce them without our code |
| Cross-field rules | `secrets` unique; `secrets` and `env` disjoint | CEL | Expressible (1.2) |
| Auth / tenancy | session cookie or `pp_sk_` key, `owner_id`, per-user app limits, per-user `MAX_APPS_PER_USER` | Kubernetes RBAC (namespace scoped), admission webhooks/ResourceQuota for counts | **Largest gap.** See section 2 |
| Size | 64 KB cap in `parse_spec`; aliases rejected | etcd object limit ~1.5 MiB (request limit default ~3 MiB) | Not a problem at our sizes; the API server also rejects YAML anchors implicitly (it parses to JSON) |

### 1.2 Pydantic rules to schema/CEL

| Rule in `app/spec.py` | CRD equivalent |
|---|---|
| `extra="forbid"` | structural schema with `additionalProperties: false`, or leave pruning on (default) and use `kubectl --validate=strict` / server-side field validation `Strict` (2025+ clients default to it). Unknown fields are *pruned silently* unless the client asks for strict validation, which is a behaviour change vs our hard 422 |
| `image` 1-512 | `minLength: 1`, `maxLength: 512` |
| `port` 1024-65535 integer | `type: integer, minimum: 1024, maximum: 65535` |
| `resources.db_engine` enum | `enum: [mysql, pg]` |
| `volume.sizeGi` 1..`OVERRIDE_VOLUME_MAX_GI` (env-overridable, default 10) | `minimum: 1`; the *max* is deployment-configurable, so it must not be baked in the CRD. Either a generous static ceiling in schema plus platform enforcement, or a ValidatingAdmissionPolicy parameterised by a ConfigMap (`paramKind`) |
| `volume.mountPath` (`validate_mount_path`, reserved paths) | `pattern` plus CEL `!self.startsWith('//')` etc. The reserved-path list can be CEL, but the Python function is the single source of truth today; duplicating it is a drift risk (test: generate CRD schema from Pydantic `model_json_schema()` and diff in CI) |
| `healthCheck.path` charset, no `..`, `%2e%2e` | `pattern` for charset plus CEL `!self.contains('..') && !self.lowerAscii().contains('%2e%2e')` |
| `healthCheck.timeoutSeconds` 1..90 | min/max from `HEALTH_TIMEOUT_*` |
| `env` keys `^[A-Z_][A-Z0-9_]*$` | `propertyNames` is not allowed in structural schemas; use CEL `self.all(k, k.matches('^[A-Z_][A-Z0-9_]*$'))`. Better: model env as a list of `{name, value}` (k8s-idiomatic, pattern on `name`, uniqueness via `x-kubernetes-list-type: map`) |
| `secrets` no duplicates | `x-kubernetes-list-type: set` |
| `secrets` disjoint from `env` | CEL: `!has(self.secrets) || !has(self.env) || self.secrets.all(s, !(s in self.env))` (with the list shape: `self.secrets.all(s, !self.env.exists(e, e.name == s))`) |
| `${{ ref }}` must be the whole value; `db.*` needs `resources.db` | CEL on `env[*].value` (`startsWith('${{')` implies `matches('^\\$\\{\\{ *(db|storage)\\.[a-z_]+ *\\}\\}$')`); the cross-field "needs db" rule is CEL too. CEL cost limits apply to maps with unbounded size, so bound `maxProperties`/`maxItems` and string `maxLength` |
| image public, non-root, <=512 MB, registry digest pin | **Not expressible.** Needs the network. Controller-side only, reported as a status condition (`ImageResolved=False`, reason `RootUser`/`TooLarge`) rather than a 422 at apply time. UX regression: errors appear in `status`, not at `kubectl apply` (unless we add a validating webhook, which can do network calls but adds an availability dependency) |
| per-user app count, name uniqueness per owner | Not expressible. Namespace `ResourceQuota` with `count/apps.picopaas.io` covers the count if each tenant has a namespace |

### 1.3 Draft CRD (abridged)

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: apps.picopaas.io
spec:
  group: picopaas.io
  scope: Namespaced
  names: { kind: App, listKind: AppList, plural: apps, singular: app, shortNames: [pp] }
  versions:
    - name: v1alpha1
      served: true
      storage: true
      subresources:
        status: {}
      additionalPrinterColumns:
        - { name: Status,   type: string,  jsonPath: .status.phase }
        - { name: Reason,   type: string,  jsonPath: .status.reason }
        - { name: Image,    type: string,  jsonPath: .spec.image, priority: 1 }
        - { name: URL,      type: string,  jsonPath: .status.url }
        - { name: DB,       type: string,  jsonPath: .spec.resources.dbEngine, priority: 1 }
        - { name: Ready,    type: string,  jsonPath: ".status.conditions[?(@.type=='Ready')].status" }
        - { name: Age,      type: date,    jsonPath: .metadata.creationTimestamp }
      schema:
        openAPIV3Schema:
          type: object
          required: [spec]
          properties:
            spec:
              type: object
              required: [image, port]
              x-kubernetes-validations:
                - rule: "!has(self.resources) || !has(self.resources.dbEngine) || has(self.resources.db)"
                  message: "resources.dbEngine only applies when resources.db is true"
                # transition rules (immutability), the API server enforces these on UPDATE
                - rule: "!has(oldSelf.resources) || !oldSelf.resources.db || (has(self.resources) && self.resources.db)"
                  message: "a database can't be removed once added"
                - rule: "!has(oldSelf.resources) || !has(oldSelf.resources.volume) || (has(self.resources) && has(self.resources.volume))"
                  message: "a persistent volume can't be removed once added"
              properties:
                displayName: { type: string, minLength: 1, maxLength: 255 }   # old `name`
                image:       { type: string, minLength: 1, maxLength: 512 }
                port:        { type: integer, minimum: 1024, maximum: 65535 }
                healthCheck:
                  type: object
                  properties:
                    enabled: { type: boolean, default: false }
                    path:
                      type: string
                      default: /healthz
                      maxLength: 255
                      pattern: '^/[A-Za-z0-9._~!$&()*+,;=:@/%-]*$'
                      x-kubernetes-validations:
                        - { rule: "!self.contains('..') && !self.lowerAscii().contains('%2e%2e')", message: "no .. segments" }
                    timeoutSeconds: { type: integer, minimum: 1, maximum: 90 }
                resources:
                  type: object
                  properties:
                    db:       { type: boolean, default: false }
                    dbEngine:
                      type: string
                      enum: [mysql, pg]
                      x-kubernetes-validations:
                        - { rule: "self == oldSelf", message: "dbEngine is immutable", optionalOldSelf: false }
                    storage:  { type: boolean, default: false }          # blob storage bucket
                    volume:
                      type: object
                      properties:
                        sizeGi:    { type: integer, minimum: 1, maximum: 100, default: 1 }  # real ceiling: platform config
                        mountPath: { type: string, maxLength: 255, default: /data }
                env:
                  type: array
                  maxItems: 128
                  x-kubernetes-list-type: map
                  x-kubernetes-list-map-keys: [name]
                  items:
                    type: object
                    required: [name]
                    properties:
                      name:  { type: string, pattern: '^[A-Z_][A-Z0-9_]*$', maxLength: 255 }
                      value: { type: string, maxLength: 4096 }   # may be a ${{ db.url }} style reference
                secrets:     # names only; values live in a Secret in the same namespace
                  type: array
                  maxItems: 64
                  x-kubernetes-list-type: set
                  items: { type: string, pattern: '^[A-Z_][A-Z0-9_]*$', maxLength: 255 }
            status:
              type: object
              x-kubernetes-preserve-unknown-fields: false
              properties:
                observedGeneration: { type: integer, format: int64 }
                phase:   { type: string, enum: [Pending, Deploying, Running, Error, Suspended, Deleting] }
                reason:  { type: string }          # machine code: image_pull, crash_loop, oom, quota, disk, ...
                message: { type: string }
                url:     { type: string }
                imageDigest: { type: string }
                volume:  { type: object, properties: { phase: { type: string } } }
                database: { type: object, properties: { engine: {type: string}, sizeBytes: {type: integer, format: int64}, overQuota: {type: boolean} } }
                storage:  { type: object, properties: { sizeBytes: {type: integer, format: int64}, readOnly: {type: boolean} } }
                conditions:
                  type: array
                  x-kubernetes-list-type: map
                  x-kubernetes-list-map-keys: [type]
                  items:
                    type: object
                    required: [type, status]
                    properties:
                      type: { type: string }
                      status: { type: string, enum: ["True", "False", "Unknown"] }
                      reason: { type: string }
                      message: { type: string }
                      lastTransitionTime: { type: string, format: date-time }
                      observedGeneration: { type: integer, format: int64 }
```

Notes on the draft: `name` becomes `displayName` (see the metadata.name mismatch); `env` becomes a list so key patterns and
uniqueness are native; the `${{ }}` reference stays a string convention. Admin-only knobs (cpu/mem limits, replicas, quotas,
egress, `db_max_connections`) are deliberately *absent* from `spec`: a tenant must not be able to write them. If we expose
them at all, they belong in a separate cluster-scoped `AppOverride` (admin RBAC only) or as annotations stripped/validated by an
admission policy. Today they are DB columns only an admin can PATCH, and the spec endpoint does not accept them; keep that.

### 1.4 Status mapping (DB columns to conditions)

| Platform field | CRD status |
|---|---|
| `status` = pending / deploying / running / error / suspended / deleted | `phase` (plus `Ready` condition; `deleted` has no CR to show, `Deleting` while the finalizer is pending) |
| `status_reason` (machine code) + `status_detail` | `Ready.reason` and `Ready.message` (CamelCase reason: `ImagePull`, `CrashLoop`, `OOMKilled`, `NotReady`, `Quota`, `UnreadableSecrets`, `Timeout`, `Disk`) |
| `error_count`, `last_attempt_at`, `error_retryable` | `status.retry: {count, lastAttemptTime, retryable}` (optional) |
| `volume_phase` (Pending/Bound) | `VolumeBound` condition + `status.volume.phase` |
| `app_databases.over_quota`, `size_bytes` | `DatabaseWithinQuota` condition + `status.database` |
| storage read-only flip | `StorageWithinQuota` condition + `status.storage` |
| `custom_domain_status` pending/active/error | `CustomDomainVerified` condition |
| `image_digest`, `image_update_error`, `rejected_digest` | `status.imageDigest`, `ImageResolved` condition |
| `rendered_hash` | stays internal (annotation on the Deployment, not on the CR) |
| `observedGeneration` | new; falls out of `metadata.generation` for free |

Things that do not map: `AppEvent` rows (eviction/OOM history) become Kubernetes `Event` objects (TTL 1h by default, so
long history stays in our DB), `AppMetricSample` (time series) stays in the DB, never in status (write amplification in etcd).

### 1.5 One existing spec as a CR

From USER_GUIDE section 4 (with the volume added to exercise more fields):

```yaml
# today
apiVersion: picopaas/v1
name: my-app
image: ghcr.io/me/app:1.2
port: 8080
healthCheck: { enabled: true, path: /healthz }
resources: { db: true, db_engine: pg, storage: false, volume: { sizeGi: 1, mountPath: /data } }
env:
  LOG_LEVEL: info
  DATABASE_URL: ${{ db.url }}
secrets:
  - API_TOKEN
```

```yaml
# as a CR
apiVersion: picopaas.io/v1alpha1
kind: App
metadata:
  name: my-app                 # DNS-1123; derived from `name` when valid, else generated
  # namespace: <tenant-ns>     # see section 2; omitted in a file POSTed to the API
spec:
  displayName: my-app
  image: ghcr.io/me/app:1.2
  port: 8080
  healthCheck: { enabled: true, path: /healthz }
  resources:
    db: true
    dbEngine: pg               # camelCase; db_engine accepted as an alias on the API side
    storage: false
    volume: { sizeGi: 1, mountPath: /data }
  env:
    - { name: LOG_LEVEL,    value: info }
    - { name: DATABASE_URL, value: "${{ db.url }}" }
  secrets: [API_TOKEN]         # values: Secret `my-app-secrets`, key API_TOKEN (see 2A)
status:                        # written by the controller, never by users
  observedGeneration: 3
  phase: Running
  url: https://brave-otter-a1b2c3d.picopaas.xyz
  conditions:
    - { type: Ready, status: "True", reason: Available, lastTransitionTime: "2026-10-23T12:00:00Z" }
    - { type: VolumeBound, status: "True", reason: Bound }
```

Two details need a decision when renaming keys: (a) `db_engine` is snake_case in the existing spec while everything else
is camelCase (`healthCheck`, `timeoutSeconds`, `mountPath`); a v1alpha1 spec can fix that to `dbEngine`; (b) `env` map to list
changes how people write the file; the converter handles both.

## 2. Architecture options

### Option A: CRD is the source of truth, with an operator

The API server (kube-apiserver) stores `App` objects; an operator reconciles them; PicoPaaS becomes a thin UI over CRs.

**Operator language.**
- *kopf (Python):* reuses `reconcile_app`/`delete_app`, `k8s_manifests.py`, the DB/Spaces providers and the fakes. Smallest
  rewrite. Costs: kopf is a lightly maintained single-maintainer project, leader election is via peering objects (less
  standard than Lease), and the controller would still need the platform DB (for `db_servers` placement, encrypted
  admin creds, quotas), so it is not actually independent of our app.
- *controller-runtime (Go):* the ecosystem default (kubebuilder, envtest, generated CRD from Go types). But it means a rewrite of
  ~2,500 lines of reconciler/manifest logic plus MySQL/PG/Spaces admin providers in a language this repo does not use, and
  duplicates the Python code during any migration. Highest quality ceiling, highest cost.

**What happens to what we have today:**

| Concern | Under A |
|---|---|
| Platform DB | `apps` table either shrinks to an index/cache (owner, hostname, ids, quota flags) or is dropped in favour of CRs. The DB still must hold per-app *provisioning* state that is not spec: `app_databases` (server placement, encrypted password), `app_storage`, audit log, users, API keys, metric samples, events, throttle table. Realistically: CR for desired state, DB for everything operational. That is two sources of truth with a sync problem, unless the DB is explicitly derived |
| Auth | Users authenticate to *our* API (cookie/`pp_sk_`). To make CRs the truth the API either (i) writes CRs on the user's behalf using its own service account (tenants still have no kube creds; CRD adds nothing for them) or (ii) tenants talk to the kube-apiserver directly, which needs kube credentials per user. (i) is just B/C with extra steps; (ii) is what "real CRD" means and is the security cost in section 3 |
| Quotas | App-count limit (`app_limit`, `MAX_APPS_PER_USER`) is application logic today. CR-native: `ResourceQuota count/apps.picopaas.io` per tenant namespace. Per-user override by admin = patch the quota. Admin-only resource knobs (cpu/mem/replicas) need either a second admin-only object or a ValidatingAdmissionPolicy checking the requester's group, which is more moving parts than the DB columns they replace |
| Multi-tenancy | Where do CRs live? (1) *One platform namespace* (`picopaas-system`): names must be globally unique (prefix with owner id), and RBAC cannot restrict a user to "their" CRs (Kubernetes RBAC has `resourceNames` but no label/owner selectors), so tenants would have to go through our API anyway. (2) *A namespace per tenant* (`tenant-<userid>`): clean RBAC (Role/RoleBinding per user) and natural `ResourceQuota`, but requires the platform to create namespaces and RoleBindings on signup approval, and a way for users to get a kube credential (OIDC, or short-lived ServiceAccount tokens from our API, or a kubeconfig generator). Each tenant namespace sits *next to* the per-app namespaces `app-<id>`, which stay (PSA, quota, NetworkPolicy isolation do not change). (3) *CR in the app namespace itself* (`app-<id>/app`): the controller needs the namespace to exist first; a tenant who can create the CR cannot create the namespace, so this does not work for bootstrap. Option 2 is the only sound multi-tenant choice, and it is a real build-out |
| Secrets | A CR must never contain secret values (it lands in etcd unencrypted by default on DOKS unless secret/CR encryption is configured, in `kubectl get -o yaml`, in audit logs, and in Git under GitOps). Replace `secrets: [NAME]` with Secret references: the CR names a `Secret` in the tenant namespace; the operator copies/merges the keys into the app's namespace Secret. Under GitOps use SealedSecrets / External Secrets / SOPS (a new component, not in budget today). Our Fernet-in-DB model is *better* for the "tenant pastes a value in a form" flow; CRs force a second secret path |
| `${{ db.* }}` refs | Keep as opaque strings the controller resolves from provisioning state (like `reconciler._db_refs`). Works, but the CRD schema can only validate the shape. A structured form (`valueFrom: {platformRef: db.url}`) is more k8s-idiomatic and lets CEL check "needs db" without regex, but breaks the one-line `${{ db.url }}` ergonomics |
| External resources | Managed DB + user, Spaces bucket + key. Finalizer-driven. Naming collisions on re-create of the same CR name: the DB/bucket names derive from `app.id` today, so the operator needs a stable id (use CR `metadata.uid` hash) |
| Failure mode | Control plane (kube-apiserver) outage means no deploys at all and the UI cannot list apps; today the API reads its own DB and only the *apply* needs Kubernetes |
| Backup/DR | Today a MySQL dump captures everything. Under A, CRs live in etcd on DOKS (managed, no direct access); we would need Velero or an export job to back up desired state, otherwise a cluster replacement (which has happened: see key management in ARCHITECTURE) loses every app definition |

### Option B: read-only mirror (projection)

The existing reconciler, after each pass, server-side-applies an `App` CR per app into a platform-owned namespace (or into
`app-<id>`), with `spec` copied from the DB and `status` filled from reconcile results. Nobody but the platform writes CRs
(RBAC: only the API's service account has write; tenants get nothing; admins may get read via `kubectl`).

- Buys: `kubectl get apps -A` with printer columns for the operator (Chas), ecosystem tools that read CRDs (Argo CD UI can display them,
  Grafana/kube-state-metrics custom resource state metrics, Backstage), a cheap way to see everything an admin cares about without opening the UI, and
  a dry run of the schema (CEL validation against real data catches drift between Pydantic and the CRD).
- Does not buy: GitOps write path, anything for tenants.
- Costs: one CRD, ~150 lines in `k8s_manifests`/reconciler to render and apply the CR and update `status`; an RBAC rule; ownership
  conventions (CR deleted when the app is deleted; the reconciler already has that flow). Staleness: status lags up to the 15 s
  reconcile interval. Write amplification: update status only on change (we already hash rendered manifests).
  There is no feedback loop, so editing a CR by hand does nothing, which must be made obvious (a `picopaas.io/managed-by: api, read-only` annotation, and
  optionally a ValidatingAdmissionPolicy that denies writes from anyone but the platform service account, keeping the mirror honest).
- Risk: confusion. People will try to `kubectl edit` it. The admission policy answers that.

### Option C: keep the API as truth, make the YAML spec CRD-shaped

Accept `apiVersion: picopaas.io/v1alpha1` + `kind: App` + `metadata` + `spec` in `POST /apps/spec` and the UI editor, and `export`
in the same shape, in addition to `picopaas/v1` (sections 4 and 5). No cluster changes at all. The platform just adopts the
envelope, the key names, and the list-shaped `env`.

- Buys: the file is identical to what a future operator, Argo CD, or Flux would consume, so users' files never change again
  ("write once"). Familiar to anyone who knows Kubernetes. A JSON Schema for editors comes for free: we can publish the CRD's
  OpenAPI schema (or `AppSpec.model_json_schema()`) at `/schema/app.json` for IDE validation in `yaml-language-server`.
- Optional thin bridge to GitOps without an operator: a small CLI/Action, `picopaas apply -f app.yaml` (the CLI exists), run
  from a GitHub Actions workflow in the user's repo, a pull-style GitOps equivalent. Argo CD can also run it as a
  PostSync hook. This covers "deploy on merge" without CRDs in the cluster.
- Thin *controller* variant (the "applied later by Argo/Flux via a thin controller" idea): install the CRD, run a small
  controller that watches `App` CRs and calls our own REST API (`POST /apps/spec`) with an API key (it holds a `pp_sk_` key per
  tenant, kept in a Secret in the tenant namespace). The controller owns no provisioning logic; the platform stays the truth and
  the CR is a *client* of it. This keeps auth, quotas and placement in one place and gives tenants GitOps without
  giving them anything beyond a namespace and a key. It is the middle path between B and A, and the only route to
  Argo/Flux that does not rewrite the platform. Costs: status sync back to the CR (poll `GET /apps/{id}`), drift semantics
  (what if someone edits in the UI? Declare "CR-managed apps are read-only in the UI", a per-app flag), deletion semantics
  (finalizer calls DELETE).
- Costs of C proper (no controller): small. `app/spec.py` gains a normalising layer (accept both shapes into the same `AppSpec`),
  new tests, docs. The `name` vs `metadata.name` question needs a rule.

### Option D: don't

The current model works: one source of truth, a simple tenant model, one place where every limit is enforced. The only unmet
need is "kubectl/GitOps visibility", which B or the CLI+Actions pattern of C already cover. Reasonable if nobody asks for GitOps.

## 3. What a CRD buys and what it costs

### Buys
- **GitOps:** Argo CD / Flux reconcile Git to `App` objects; PR review becomes the deploy process; drift detection; rollbacks by `git revert`.
- **`kubectl get apps`:** printer columns, `kubectl wait --for=condition=Ready app/foo`, `kubectl describe`, events on the object. Good for the operator (Chas), good for CI scripts.
- **Admission validation at the API server:** structural schema and CEL rules reject bad specs at `kubectl apply`, before our code runs, with no webhook to keep alive. Transition rules enforce "can't remove db".
- **Ecosystem tooling:** kube-state-metrics custom resource state, Backstage/Headlamp/Lens views, `kubectl explain apps.spec` from the schema, IDE validation, policy engines (Kyverno/Gatekeeper) and, with an operator, owner references giving automatic garbage collection of child objects.
- **Reconcile loop semantics for free:** watches, resync, leader election, work queues, events, `observedGeneration`. Our APScheduler + `GET_LOCK` loop replicates these by hand (15 s polling, DB lock).

### Costs
- **Operator complexity:** a second controller (or our reconciler re-homed) with watch handling, retries, finalizer bugs (a stuck finalizer leaves `kubectl delete` hanging), RBAC, leader election, upgrades. Our reconciler is already ~800 lines plus ~800 of manifest generation; porting it faithfully is most of the effort in every estimate below.
- **etcd limits:** objects max about 1.5 MiB (our 64 KB cap is far below that). The real concern is write volume: status updates for metrics or per-pod data must not go into `status` (keep them in the DB). Number of CRs (hundreds at most) is irrelevant for etcd.
- **CRD versioning:** `v1alpha1` is a promise-free version, but once `storage: true` objects exist, moving to `v1beta1`/`v1` needs a conversion strategy. `strategy: None` only allows apiVersion rewrites with identical schemas; any field rename (e.g. `db_engine` to `dbEngine`, `env` map to list) needs a **conversion webhook**: a service that must be up, with a CA bundle (cert-manager is already installed, which helps). Plan for it only when we really need a second version; for now, design v1alpha1 once and keep it additive.
- **Tenants needing kube access (security):** today a tenant has zero kube credentials: an API compromise of a tenant account yields only that tenant's apps. Giving users kube credentials adds: the kube-apiserver (public on DOKS) as a tenant-reachable attack surface, per-tenant namespace/RBAC provisioning, credential issuance/rotation/revocation (OIDC is not available on DOKS without a proxy; ServiceAccount tokens are long lived or need the TokenRequest API), RBAC mistakes becoming cross-tenant escapes (a Role with `*` verbs on `apps`, or a bound Secret read), and CR write access implying the power to cause *our controller* to act (confused deputy: a CR must not be able to name another tenant's DB or Secret). Cluster-wide `list` on the CRD kind leaks other tenants' specs if scoped wrong. Nothing here is unsolvable, but it is a security review and a recurring burden, directly against the current "no kube credentials" model and pentest posture (see `docs/PENTEST.md`).
- **Testing:** today: pytest + fakes, real login flow, fast. Operator testing needs envtest (Go) or kind/k3d in CI (slow, flaky), plus CRD schema tests (apply fixtures, expect CEL rejections). Fakes in `app/providers/` still work for kopf unit tests, but integration of the finalizer/status loops needs a real API server. CI minutes are free for the public repo, but maintenance is not.
- **Dual validation drift:** Pydantic and CEL must say the same thing. Mitigate by generating the CRD schema from `model_json_schema()` where possible and a CI test that runs a fixture corpus through both.
- **Error UX regression:** today's plain-English 422s (`_plain_msg`) become CEL `message` strings and conditions; registry/image/quota checks move from synchronous 422 to asynchronous status.
- **DOKS specifics:** managed control plane means no etcd access/backups of CRs from our side, and CRD installs need cluster-admin (the bootstrap workflow already holds that).

## 4. Recommendation

Do C now, B if Chas wants visibility, and revisit A only on a product change. Do not build a full operator.

| Phase | What | Effort (agent-days) | Risk |
|---|---|---|---|
| 0 | Decide group name (`picopaas.io` vs `picopaas.xyz`), `metadata.name` rule, env list shape; add a `docs/QUESTIONS.md` entry for Chas | 0.25 | none |
| 1 | **CRD-shaped spec (C):** `app/spec.py` accepts both `picopaas/v1` and `picopaas.io/v1alpha1` (`kind: App`, `metadata.name/labels`, `spec.*`, `env` as map *or* list, `db_engine`/`dbEngine`), normalised into the existing internal `AppSpec`; `GET /apps/{id}/export` gains `?format=v1alpha1` (old stays default until deprecation); publish JSON Schema at `/schema/app.json`; UI editor example switch; CLI `apply -f`; tests incl. error text for both shapes; USER_GUIDE section 4 | 2 to 3 | low |
| 2a | **Mirror (B):** generate the CRD YAML from the Pydantic model (+ hand-written CEL/printer columns), `deploy/` manifest and bootstrap step, reconciler writes `App` + status (conditions mapped per 1.4), RBAC rule, admission policy making it read-only, delete on app delete; fake provider and tests. Needs a request in `docs/requests/`? No: no paid resource, but needs cluster-admin on bootstrap, so flag it to Chas | 3 to 4 | low to medium (status write volume, apiserver load is trivial at hundreds of apps) |
| 2b | **Thin GitOps controller (C-bridge), only if asked:** kopf controller (~300 lines) that turns `App` CRs into `POST /apps/spec` calls with a per-tenant `pp_sk_` key, mirrors status back, finalizer calls DELETE; tenant namespaces + RBAC + SealedSecrets/External Secrets for secret values; docs and Argo CD example | 8 to 12 | medium (tenant namespace/RBAC onboarding, secret flow, UI-vs-CR ownership flag) |
| 3 | **Full operator (A):** kopf port of the reconciler with CR as source of truth, tenant namespaces + RBAC + quotas via `ResourceQuota`, Secret refs, conversion webhook plan, envtest/kind CI, DR (Velero), migration of existing apps and an audit-log story (kube audit vs our `audit` table) | 20 to 30 with kopf (35 to 50 in Go) | high; changes the security model and needs a pentest pass. Not recommended |

Sequencing: 0 then 1 (about 3 days total) is the only thing worth doing unprompted. It is cheap, reversible, and makes
every later phase easier because user files stop changing. 2a is a nice-to-have of about 3 to 4 days that gives Chas
`kubectl get apps`. 2b only if a user (or Chas) genuinely wants GitOps; prefer first pointing them at `picopaas apply` from a GitHub Action.

### Backward compatibility for `apiVersion: picopaas/v1`
- Accept both indefinitely. A single normaliser (`apiVersion` + `kind` dispatch) maps either shape to the one internal model, so the router,
  reconciler, DB and tests do not change.
- `picopaas/v1` documents do not carry `kind`; treat missing `kind` as `App` only for `picopaas/v1`. For `picopaas.io/v1alpha1`
  require `kind: App` (reject any other kind with a clear 422).
- Strictness stays the same: unknown fields are errors in both shapes (a CRD would silently prune them; we keep our stricter behaviour at the API).
- Keep `export` default as `picopaas/v1` for one release, then flip, with a deprecation note in the docs and a response header. Never auto-rewrite a user's saved spec.
- Name rule: `picopaas/v1` `name` (free text) maps to `metadata.name` if it is already DNS-1123 and to `spec.displayName` always; in a CR, `metadata.name` is
  the matching key for update-by-name (same owner scope as today), and `displayName` defaults to it. Existing apps keep their `name`; no data migration.
- Any future `v1beta1`/`v1`: additive-only fields in v1alpha1 so no conversion webhook is ever needed for the mirror; if a breaking change is needed, bump the version and add the webhook then.

## Open questions for Chas (to add to `docs/QUESTIONS.md`)
1. Is the goal GitOps, `kubectl` visibility for you, or tenants using kubectl? (Decides between C, B, and A.)
2. API group: `picopaas.io` (not confirmed as owned) or `picopaas.xyz`?
3. OK to introduce a CRD and its read-only admission policy through the bootstrap workflow (needs cluster-admin; no new cost)?
