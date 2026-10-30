# Kaisen in PicoPaaS (Preview)

Status: design, 2026-10-29. Owner: Chas. Supersedes running Kaisen as a separate tenant app (seaburr/kaisen `service/`).

> Kaisen needs to be baked into the platform as a first-class citizen. A user logs in and can either deploy an app or
> build an app in Kaisen, and it deploys alongside their other apps, in their personal project or a team project. They
> still manage their own config (repo, Anthropic key, ...) but deployment into PicoPaaS becomes seamless: it deploys into
> the project where the build was created. Marked Preview. Seamless with PicoPaaS and looks like PicoPaaS. (Chas)

## 1. Where it runs: inside for data and UI, alongside for the agent loop

| Part | Where | Why |
|---|---|---|
| API, data model, UI | Platform (`app/kaisen/`, `frontend/`), platform MySQL | One login, one UI, projects/roles/audit/limits reused as-is |
| Agent loop (model calls, tool execution) | New Deployment `kaisen-worker` in `picopaas-system`, **same platform image**, command `python -m app.kaisen.worker`, 1 replica | A build streams for minutes and holds the Anthropic SDK in memory; the platform pods (2 x 384 MB / 250m) also serve the API and run the reconciler, so a runaway build must not be able to OOM or starve them |
| Previews | Ordinary apps in the user's project, image `kaisen-runtime` | Same isolation, limits, logs, backups and delete flow as any app |
| Bundles | Platform route `GET /kaisen/bundles/{token}.tar.gz` (public, 256-bit token, hash stored, last 3 per build kept) | The runtime image downloads the code at boot; no image builds, no CI |

The worker gets a lease via MySQL `GET_LOCK('picopaas-kaisen-worker')` (same pattern as the reconciler) and re-checks it
before every model call, push and deploy. It needs DB access only; it never talks to Kubernetes (deploys go through the
same internal function as `POST /apps/spec`, run by the API side when the worker asks; see section 4). Resources:
requests 50m / 128Mi, limits 250m / 384Mi. No new paid resource (fits existing nodes; sre to confirm headroom).

`KAISEN_ENABLED` (env, default on) plus an admin platform setting `kaisen_enabled` gate everything; the UI shows
**Preview** badges wherever Kaisen appears.

## 2. Concepts

- **Build**: one Kaisen project (name, brief, files, runs, transcript, spend). Lives in a PicoPaaS **project** (personal or
  team), created by a member. "Build" avoids clashing with PicoPaaS "projects".
- **Build app**: the PicoPaaS app a build deploys to (`apps.kaisen_build_id`). It is a normal app: it shows in the apps list
  with a "Built with Kaisen · Preview" badge, has the usual health/settings pages, counts against the project's app limit.
  Deleting the app unlinks the build; deleting the build asks whether to delete the app too.
- **Kaisen settings** are per PicoPaaS project (the personal project's are the user's own): Anthropic API key, model,
  effort, caps (run/day/build), default GitHub repo + token + push mode. Secrets Fernet-encrypted with the platform key,
  write-only ("set, ends in ...abcd"). Owners edit; members see what is set (not values).
- Members (any role that can manage apps) can create and run builds; spend comes from the project's key.

## 3. Data model (Alembic migration)

`kaisen_settings(project_id PK/FK, anthropic_key_enc, model, effort, cap_run, cap_day, cap_build, github_repo,
github_branch, github_token_enc, output_mode, push_on_finish, open_pr, updated_by, updated_at)`
`kaisen_builds(id, project_id FK, created_by FK users, name, slug, brief MEDIUMTEXT, app_id FK apps NULL, status,
created_at, deleted_at)`
`kaisen_files(id, build_id FK, path, content MEDIUMTEXT, size; unique(build_id, path))`
`kaisen_runs(id, build_id FK, status, kind build|feedback|resume, messages JSON, pending JSON, summary, error, cost_usd,
lease_token, created_at, finished_at)`
`kaisen_events(id, run_id, build_id, project_id, model, input/cache_write/cache_read/output tokens, usd, at)`
`kaisen_inbox(id, build_id, run_id, kind question|budget|refusal|error|push|deploy, text, answer, status, created_at)`
`kaisen_bundles(id, build_id, token_hash, sha256, data LONGBLOB, created_at)`
`apps.kaisen_build_id NULL FK`.
Build and run status (built): `idle` (build only, never run), `queued`, `running`, `waiting` (a question is open), `done`, `stopped`, `error`; the build mirrors its latest run. Active = queued, running, waiting.
Retention: transcripts trimmed after 14 days, events kept 90 days, bundles last 3 per build; all deleted with the build.

## 4. Deploy path (seamless: no PicoPaaS API key)

The worker records "deploy wanted" for a run; the platform deploys through `routers/spec.deploy_spec()` **as the build's
creator, into the build's project** (same limits, same 403/422 messages, audit `kaisen.deploy` + the usual app events).
Spec: name = build name, image = pinned `KAISEN_RUNTIME_IMAGE`, port 8080, healthCheck off, env
`KAISEN_BUNDLE_URL`/`KAISEN_BUNDLE_SHA256`, and, when `kaisen.json` says `"database": "mysql"`, `resources.db` + the
`DB_*`/`DATABASE_URL` refs. The `deploy_preview` tool then polls the app status, probes the URL. **Built state:** app logs are
NOT yet available to `deploy_preview` (it returns "(logs are not available yet)"; TODO route through the platform's pod-log path,
redacted, control chars stripped, 4 KB cap), because the worker makes no kube calls.
Implementation note: the simplest correct split is that the worker calls the same Python functions directly (it has the
platform code and DB) and only the k8s side stays with the reconciler, which already picks up queued apps. So the worker
never needs cluster credentials.

## 5. API (all under the existing auth; API keys work, read-scope keys read only)

- `GET/PUT /projects/{id}/kaisen` settings (owner for PUT; secrets write-only); `POST /projects/{id}/kaisen/test/{anthropic|github}`
- `GET /kaisen/builds?project_id=` · `POST /kaisen/builds {project_id, name, brief}` · `GET /kaisen/builds/{id}`
  (build, files list, latest run, transcript (redacted), inbox, spend, app link) · `DELETE /kaisen/builds/{id}?delete_app=`
- `POST /kaisen/builds/{id}/run` (build or resume) · `POST .../feedback {text}` · `POST .../inbox/{item}/answer {text}`
  · `POST .../deploy` · `GET .../files/{path}` · `GET .../download` (tar.gz) · `POST .../push`
- `GET /kaisen/bundles/{token}.tar.gz` (public)
- `GET /kaisen/spend?project_id=`
Errors follow the platform's plain-language style.

## 6. UI (must look like PicoPaaS: same components, Tailwind tokens, IconButton, cards, 390px)

- **Nav**: "Kaisen" link with a small Preview badge.
- **New app** (`/apps/new`): two choices at the top: *Deploy an image* (today's form) and *Build with Kaisen (Preview)*.
- **Kaisen home** (`/kaisen`): builds in the current project (switcher applies): name, status, app link, spend; "New build".
  Empty state explains Kaisen in two lines and links to settings if the project has no Anthropic key.
- **New build** (`/kaisen/new`): name + brief (textarea with examples) + project select (like the app form).
- **Build page** (`/kaisen/:id`): header with status + Preview badge; "Live app" card (URL, status, link to the app's
  health page); "Tell Kaisen what to change" box; questions from the builder answered inline; run history with a
  collapsible, redacted transcript; files list with viewer; Download / Push. Two-page IA rule: the build page links to the
  app's pages with buttons, no tabs.
- **App pages**: built apps show "Open in Kaisen" on the health page and the badge in the list.
- **Kaisen settings**: `/kaisen/settings` for the current project (owners edit), with the Anthropic and GitHub
  instructions from the standalone app; Test buttons; spend this month and caps.
ui-ux-designer produces a mockup first (memory: layout changes start with a mockup).

## 7. Migration from the standalone Kaisen

Port `seaburr/kaisen/service/kaisen/{builder,llm,previews,export,github,redact,prompts}` into `app/kaisen/` adapted to
platform models/auth; move `runtime/` to `images/kaisen-runtime/` with its own workflow (published by the platform repo,
pinned like `BACKUP_IMAGE`). seaburr/kaisen keeps the agent kit (agents/process/templates) and is marked as such.
The standalone test instance (`kaisen` + its 2 preview apps on the E2E user account) is deleted once parity is reached
(ask Chas first). No data migration (test data only).

## 8. Security (security-reviewer before ship)

Same tool scope and path rules as standalone; model never sees secrets; per-project keys encrypted; transcripts redacted
on read; generated apps are ordinary tenants (no secrets, optional own DB). New: bundle route is public on the platform
host (rate-limited, constant-time lookup); worker holds the decrypted Anthropic key only per call; members of a team can
spend the team's key (documented); audit events for settings changes, builds, deploys, pushes.

## 9. Out of scope for this preview

Logs in `deploy_preview` (next), Running generated tests in CI, a reviewer role, per-member spend limits, billing, Kaisen for apps with storage/volumes.
