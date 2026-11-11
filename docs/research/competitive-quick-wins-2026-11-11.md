# Competitive quick wins: platform and Kaisen (2026-11-11)

Author: mba (product strategy). Research only, no code changed.

Scope: the 3-5 easiest additions per track. "Easy" means about a day or less on the existing code and no new paid
infrastructure. Kaisen items also state the model spend they add or save.

How to read the claims:

- Competitor facts with a link were read from that page on 2026-11-11.
- In the tables, `yes` is confirmed from a linked source, `(yes)` is general knowledge that was not re-checked
  today, and `?` was not checked.
- Effort figures are estimates from reading the code. Nothing was prototyped.

## Top 5 overall

| # | Addition | Track | Effort | Why it is first |
|---|---|---|---|---|
| 1 | Paste a `.env` block into the env var editor | Platform | 2-3 h, frontend only | Removes the slowest step of every first deploy. No backend, no risk. |
| 2 | Duplicate a build ("Fork") | Kaisen | about 0.5 day | Lets a user try a risky change without losing a working build. Costs no model spend. |
| 3 | Deploy hook URL per app, plus a CI snippet in the docs | Platform | about 0.5 day | Gives "push to main, app updates in a minute" without building images in the cluster. |
| 4 | Confirm the plan before building | Kaisen | 0.5-1 day | One cheap model turn up front avoids paying for a whole run built on a wrong reading of the brief. |
| 5 | Outbound notification webhook (Slack/Discord/generic) | Platform | about 1 day | Today a crash or failed backup is only visible if you open the app page. |

---

## Track A: the PaaS platform

### A.1 Feature table

| Feature | PicoPaaS | Render | Railway | Fly.io | Coolify | Dokploy | Vercel / Netlify | Koyeb |
|---|---|---|---|---|---|---|---|---|
| Redeploy by calling a secret URL (deploy hook) | no (API key + `POST /apps/{id}/check-image`; tag re-checked every 10 min) | yes [R1] | ? | ? | (yes) | yes [D1] | yes [V1] / (yes) | ? |
| Build from a Git repo | no (prebuilt public image only) | (yes) | (yes) | (yes) | (yes) | yes [D1] | (yes) | (yes) |
| Notifications to Slack/Discord/webhook | no | yes, email + Slack [R2] | (yes) | ? | yes, 6 channels [C1] | (yes) | (yes) | ? |
| Cron jobs | no (backlog) | yes [R3] | (yes) | (yes) | yes (scheduled tasks) [C1] | (yes) | (yes) | ? |
| Log search / filter | no (last 200 lines, pod picker, download) | (yes) | yes, query syntax [RW2] | (yes) | ? | ? | (yes) | ? |
| Paste `.env` into the variable editor | no (one row at a time) | (yes) | yes, Raw Editor [RW1] | n/a (CLI secrets) | (yes) | (yes) | (yes) | ? |
| HTTP basic auth in front of an app | no | ? | ? | ? | yes [C2] | yes [D2] | (paid plans) | ? |
| Stop / sleep an idle app | admin suspend only | (yes) | yes, auto sleep + wake [RW3] | (yes), autostop/autostart [F1, not read] | (yes) | (yes) | n/a | (yes) |
| "Deploy" button / prefilled link | partly (`?template=<id>` only) | (yes) | (yes) | no | ? | ? | (yes) | yes, query params [K1] |
| Status badge for a README | no | ? | ? | ? | ? | ? | ? / yes [N1] | ? |

Already at parity and not listed above: CLI, YAML spec apply, custom domains with TLS, rollback to the last 5
digests, auto-rollback on a failed rollout, health checks, CPU/memory history, one-click templates, backups,
reference variables, projects with members.

### A.2 Recommendations, easiest first

#### A1. Paste a `.env` block into the env var editor

- **What:** a "Paste .env" control in the env var editor. The user pastes `KEY=value` lines; the editor adds or
  updates rows. Handle `export KEY=`, quotes, `#` comments and blank lines. Show which keys were added, replaced
  or refused.
- **Who has it:** Railway's Raw Editor takes a whole `.env` or JSON block
  ([RW1](https://docs.railway.com/guides/variables)). Render, Vercel, Coolify and Dokploy are known to have the
  same; not re-checked today.
- **Why it matters here:** a technician moving a throwaway project already has a `.env`. Templates such as Ghost
  or Directus need 5-10 values. Typing them row by row is the slowest part of a first deploy.
- **Effort:** 2-3 h, frontend only. `frontend/src/components/EnvVarEditor.vue` (204 lines, no paste handling
  today) and the existing key validation in `frontend/src/composables/envKeys.ts` (`envKeyError`). The API
  already takes the full env list. Optional follow-up: `picopaas apply --env-file` in `cli/picopaas.py` (there is
  no such flag today).
- **Caveats:** pasted rows need a secret/plain choice. Default every pasted row to secret and let the user untick,
  so a pasted password is never stored as a visible value. The 8 KiB per-value cap and the `${{` reference rules
  still apply; report refused rows by name, never echo values in the error.

#### A2. Log filter in the UI and `logs --follow` in the CLI

- **What:** a filter box above the app log panel (substring or regex, client-side over the fetched lines), a tail
  size choice up to the existing 1000-line API limit, and `picopaas logs --follow` / `--grep` that polls the
  existing endpoint and prints only new lines.
- **Who has it:** Railway has keyword and attribute search with AND/OR/negation
  ([RW2](https://docs.railway.com/guides/logs)). Render, Fly.io and Vercel are known to have log search and live
  tail; not re-checked today.
- **Why it matters here:** the app page shows 200 lines with no way to find an error among them, and the CLI
  cannot follow. Debugging a crash loop means repeated manual refreshes.
- **Effort:** 3-4 h. `frontend/src/views/AppDetailView.vue` (log state around lines 131-215; no filter today),
  `cli/picopaas.py` (`logs` parser at line 702, `cmd_logs`). No backend change: `GET /apps/{id}/logs` in
  `app/routers/app_ops.py` already accepts `tail` up to `MAX_LOG_TAIL` and a `pod`.
- **Caveats:** no cost. This is a filter over what the pod still holds, not stored log search. Say so in the
  docs. `--follow` must poll at a fixed interval (for example 3 s) and stop on Ctrl-C; do not add a streaming
  endpoint.

#### A3. Deploy hook URL per app, plus a CI snippet

- **What:** each app gets an optional secret URL. A `POST` to it runs the same code as **Check for new image**:
  re-resolve the tag and roll out if the digest changed. Show it in Settings with **Regenerate**. Add a
  ten-line GitHub Actions example to the User Guide: build, push to GHCR, call the hook.
- **Who has it:** Render (secret URL per service, regenerate, meant for image-backed services that do not
  redeploy on push) ([R1](https://render.com/docs/deploy-hooks)); Vercel (URL is the credential, 60 triggers per
  hour) ([V1](https://vercel.com/docs/deploy-hooks)); Dokploy (webhook URL, Docker Hub tag match, or API)
  ([D1](https://docs.dokploy.com/docs/core/auto-deploy)).
- **Why it matters here:** PicoPaaS cannot build from Git and that is not a quick win (see A.3). The hook is
  the cheap equivalent. Today the choices are waiting up to 10 minutes for the tag check, or putting a full API
  key into CI. A hook can only redeploy that one app's configured tag.
- **Effort:** about 0.5 day. New column + Alembic migration on `apps` (store a hash, as `app/routers/api_keys.py`
  does for keys); new unauthenticated route that calls `image_check.refresh_app_image` (the body of
  `check_image` in `app/routers/app_ops.py:293`); Settings UI in `frontend/src/components/AppSettingsPanel.vue`;
  `docs/USER_GUIDE.md`, `docs/API.md`. The docs snippet alone (using an API key) is 30 minutes and can ship
  first.
- **Caveats:** the URL is a bearer secret. Use at least 128 bits of randomness, store only the hash, answer 404
  for unknown tokens, write an audit row, and rate-limit per app with `app/ratelimit.py`. The hook must not take
  parameters that change the image (Render allows a tag override; skip that). Each call hits the registry, so the
  limit also protects the registry client. Needs a security-reviewer pass.

#### A4. Outbound notification webhook

- **What:** one URL per app (or per project). The platform POSTs a small JSON message when an app event is
  recorded: restart for OOM, crash or disk, failed rollout with auto-rollback, failed backup, quota over cap.
  Accept a Slack or Discord incoming-webhook URL or any HTTPS endpoint.
- **Who has it:** Render (email and Slack; deploy failures, unhealthy services, disk at 80%)
  ([R2](https://render.com/docs/notifications)); Coolify (email, Discord, Telegram, Slack, Pushover, generic
  webhook) ([C1](https://coolify.io/docs/knowledge-base/notifications)). Railway, Dokploy and Netlify are known
  to have it; not re-checked today.
- **Why it matters here:** throwaway projects are not watched. The platform already detects and stores these
  events (`AppEvent`), but only shows them on the app page. Already listed in `docs/ROADMAP.md` backlog
  ("Outbound deploy webhooks").
- **Effort:** about 1 day. Hook the three places that insert `models.AppEvent` (`app/reconciler.py:262`,
  `app/backups.py:323`, `app/image_history.py:88`) through one helper; new encrypted column + migration; URL
  field in `AppSettingsPanel.vue` with a **Send test** button; reuse the public-address check in
  `app/providers/registry.py` (lines 91-110, `is_global`).
- **Caveats:** this is a server-side request to a user-supplied URL. Resolve once and connect to the checked
  address (DNS rebinding), HTTPS only, no redirects, 3 s timeout, no retry loop, send from the reconciler leader
  only. Store the URL encrypted: a Slack webhook URL is a credential. Confirm the API pod's egress policy allows
  outbound 443. Needs a security-reviewer pass. No cost.

#### A5. HTTP basic auth in front of an app

- **What:** a switch in Settings: "Require a password to open this app", with a user name and password. Traefik
  asks for credentials before any request reaches the pod.
- **Who has it:** Coolify (built-in setting that generates the Traefik middleware, bcrypt)
  ([C2](https://coolify.io/docs/core/networking/proxy/traefik/basic-auth)); Dokploy (Advanced > Security)
  ([D2](https://docs.dokploy.com/docs/core/applications/advanced)). Vercel and Netlify offer password protection
  on paid plans; not re-checked today.
- **Why it matters here:** every app gets a public hostname, and many throwaway tools (Adminer, IT-Tools, an
  internal dashboard, a Kaisen build with no login) have no auth of their own. This is the cheapest way to keep
  them private.
- **Effort:** about 1 day, the largest of the five. `app/k8s_manifests.py` (`ingress_manifest` at line 780 and
  the custom-domain Ingress at line 832: add a `Middleware` + `Secret` and the
  `traefik.ingress.kubernetes.io/router.middlewares` annotation, the same mechanism `deploy/platform/ingress.yaml`
  already uses), `app/reconciler.py`, `app/models.py` + migration, `app/schemas.py`, the YAML spec,
  `AppSettingsPanel.vue`, docs.
- **Caveats:** (1) the platform service account probably has no rule for `traefik.io` Middleware objects in app
  namespaces; I found Middleware only in static manifests under `deploy/`. If so, the ClusterRole needs one rule
  and a deploy; no paid resource. Check this first, it decides whether the item is "easy". (2) Store only a
  bcrypt hash. (3) A Kaisen build's `http_request` and `deploy_preview` checks go through the public host and
  would get 401, so either refuse the switch on Kaisen apps at first or have the builder send the credentials.
  (4) One shared credential, not user accounts; say so in the UI. Needs a security-reviewer pass.

### A.3 Considered but not easy (or easy but low value now)

- **Build from Git / Dockerfile.** The largest gap in the table. Needs build compute on a node that is already
  at 2470 of 3002 MiB requested (ledger 2026-11-10), plus a registry and build quotas. Already in the backlog as
  "not strictly free". A3 covers most of the workflow.
- **Cron jobs.** Render charges per run-second with a $1 monthly minimum
  ([R3](https://render.com/docs/cronjobs)). Here it is a k8s CronJob per app plus quota accounting, log access
  for finished Jobs, schedule validation and UI. Roughly 2-3 days. Already in the backlog.
- **Sleep on idle with wake on request.** Railway detects idle from outbound packets and wakes on the first
  request, with a possible 502 ([RW3](https://docs.railway.com/reference/app-sleeping)). Needs an activator in the
  request path. Chas deferred the whole item on 2026-10-20. A manual Stop/Start is buildable without infra, but
  it adds an owner-controlled stopped state next to `suspended`, which touches every status check in the
  reconciler; about 1-2 days, and it was deferred with the rest.
- **Preview environments per pull request.** Depends on build from Git.
- **Env var groups shared across apps.** Railway has shared variables
  ([RW1](https://docs.railway.com/guides/variables)). Needs a project-level store and reference resolution in
  `app/env_refs.py`. About 2 days. Low demand while most projects hold 1-3 apps.
- **Uptime checks from outside the cluster.** Needs a second vantage point, which is new infrastructure.
- **Deploy button / prefilled link** (`/apps/new?image=...&port=...`). Easy (2-3 h in
  `frontend/src/views/AppFormView.vue`, which already reads `?template=`). Koyeb's version passes image, name,
  ports and env as query parameters ([K1](https://www.koyeb.com/docs/build-and-deploy/deploy-to-koyeb-button)).
  Low value while sign-ups are approval-gated; worth doing when self-hosted instances exist. Never accept secret
  values in the query string.
- **Status badge SVG.** Easy (about 3 h, one public route). Netlify's shows success, building, canceled, failed
  ([N1](https://docs.netlify.com/monitor-sites/status-badges/)). Low value for throwaway apps, and a public
  endpoint that reveals app state must be opt-in with an unguessable id.

---

## Track B: Kaisen (Preview)

### B.1 Feature table

Firebase Studio is being shut down (new sign-ups closed 2026-06-22, shutdown 2027-03-22, per third-party
reports [FS1]), so it is left out of the table. Same.dev (now Same.new) is a URL-to-React cloner [S1] and
overlaps little with Kaisen.

| Feature | Kaisen | Lovable | Bolt | v0 | Replit Agent | Base44 | GitHub Spark |
|---|---|---|---|---|---|---|---|
| Starter prompts / examples | yes, 5 starter buttons on New build | (yes), template gallery | (yes) | (yes) | (yes) | (yes) | ? |
| Copy a build (remix / fork / duplicate) | no | yes, with optional history [L1] | ? | yes, fork from a version [V0] | (yes) | ? | (yes), third-party only [GS1] |
| Plan or discuss without changing code | no (builder writes `PLAN.md` and starts) | yes, Chat and Plan modes, Approve to build [L2] | yes, Plan mode, fewer tokens [B2] | ? | yes, Plan mode, billed [RP1] | yes, Discuss mode, 0.3 credits [BA1] | ? |
| Help writing the first prompt | static "Write a good brief" list in the guide | ? | yes, Enhance prompt [B1] | ? | (yes) | ? | ? |
| Version history and restore | yes, last 5 restore points with diffs | yes, every change [L3] | yes [B3] | yes [V0] | yes, includes database [RP2] | yes [BA1] | (yes) |
| Cost shown per run | yes, per run, live, against caps, in dollars | credits | tokens | credits | yes, per checkpoint [RP1] | yes, credits per message [BA1] | ? |
| Live preview beside the chat | no (link opens the app in a new tab) | (yes) | (yes) | yes [V0] | (yes) | yes [BA1] | (yes) |
| Click an element to edit it | no | (yes) | (yes) | yes, Design mode [V0] | (yes) | yes, no credits used [BA1] | ? |
| Model reads attached screenshots | no (files attach, the model sees name and description only) | (yes) | (yes) | yes [V0] | (yes) | ? | ? |
| Export to GitHub / download | yes, both, optional pull request | (yes) | (yes) | (yes) | (yes) | yes, ZIP or GitHub [BA1] | (yes) |
| Fix errors on request | yes, **Fix it** for app errors and failed tests | (yes) | yes, "Attempt fix", docs warn about token burn [B4] | (yes) | (yes) | ? | ? |
| Database for the built app | yes, MySQL/PostgreSQL, bucket, volume via `kaisen.json` | (yes), Supabase | (yes) | (yes) | (yes) | (yes) | yes [GS2] |

Kaisen is already at or above parity on restore points, cost display in real dollars with hard caps, GitHub
output, generated tests, a reviewer step, package checks and build secrets. Two things the others do not offer:
the user's own Anthropic key with dollar caps, and a built app that is an ordinary platform app.

### B.2 Recommendations, easiest first

#### B1. Duplicate a build ("Fork")

- **What:** a **Duplicate** action on the build page. It creates a new build in the same project with the same
  brief and files, no runs, no app, status `idle`. Optionally from a chosen restore point.
- **Who has it:** Lovable (independent copy, optional chat history, a snapshot that never syncs back)
  ([L1](https://docs.lovable.dev/features/projects/remix)); v0 (fork from a specific version)
  ([V0](https://v0.app/docs/design-mode), fork endpoint
  [V0b](https://v0.app/docs/api/v1/reference/chats/fork)).
- **Why it matters here:** restore points go back 5 runs and replace the working copy. A technician who wants to
  try "same app, but on PostgreSQL" or keep a known-good version while experimenting has no option except
  download and paste. Duplicating costs no model spend.
- **Effort:** about 0.5 day. New `POST /kaisen/builds/{id}/duplicate` in `app/routers/kaisen.py` next to
  `create_build` (line 327; reuse its project check and the `KAISEN_MAX_BUILDS_PER_PROJECT` cap); copy
  `KaisenFile` rows (`app/kaisen/models.py:70`), or unpack a `KaisenSnapshot` blob with the code in
  `app/kaisen/snapshots.py`; button in `frontend/src/views/KaisenBuildView.vue`; one test file.
- **Caveats:** do not copy the per-build GitHub token (`github_token_enc`) or build secret values; copy secret
  names only and show them as "needs a value". Decide whether uploads are copied: they count against the
  40 MiB project quota, so either share nothing and say so, or copy with a quota check. Same project only at
  first, so no cross-project access question arises. The copy deploys as a new app with a new hostname and an
  empty database, and uses an app slot.

#### B2. Starter gallery with what each one costs and uses

- **What:** replace the five title-only starter buttons with a small table: name, one line on what it builds,
  storage pills (MySQL, browser only, bucket), the observed cost and run count on Haiku, and **Use this
  brief**. Add the three eval briefs that already exist (Kanban board, Notes with uploads, Small CRM).
- **Who has it:** every competitor leads with examples or a template gallery; Lovable publishes templates by
  category (seen in search results, not read in detail).
- **Why it matters here:** the first question a technician has is "what size of app does this handle, and what
  will it cost me". The data already exists: the User Guide quotes about five cents for Fancy Docs, and the eval
  script records cost per brief. Showing it sets the right expectation before any money is spent.
- **Effort:** 3-4 h, frontend only. `frontend/src/views/KaisenNewView.vue` (`STARTERS` constant at line 26,
  buttons at line 227); brief text for the three extra entries from `scripts/kaisen_eval_briefs.json`; reuse the
  pill and row pattern of `frontend/src/views/TemplatesView.vue` so the two catalogs look alike.
- **Caveats:** state costs as "what we saw on Haiku, not a promise", matching the guide. Live demo links would
  be useful but each one holds an app slot in a 3-app project; leave them out or link at most one. No model
  spend.

#### B3. Confirm the plan before building

- **What:** a checkbox on New build (and a project default): "Show me the plan first". The builder writes
  `PLAN.md`, then asks one question that contains the plan and waits. The user answers "go" or corrects it. The
  build then continues in the same run.
- **Who has it:** Lovable (Plan mode never changes code; Approve switches to building; one credit per plan
  message) ([L2](https://docs.lovable.dev/features/plan-mode)); Bolt (Plan mode, "far fewer tokens" than build
  mode) ([B2](https://support.bolt.new/best-practices/plan-mode)); Base44 (Discuss mode, 0.3 credits per
  message) ([BA1](https://docs.base44.com/Building-your-app/AI-chat-modes)); Replit (Plan mode, billed like
  other agent use) ([RP1](https://docs.replit.com/billing/ai-billing)).
- **Why it matters here:** every run costs money on the user's key, and the guide already says one larger first
  run is cheaper than three follow-ups. A wrong assumption (static site when the user wanted MySQL) is the most
  expensive mistake and is visible in the plan before any code is written.
- **Effort:** 0.5-1 day. The pieces exist: `PLAN.md` is already mandatory (`app/kaisen/prompts/builder.md`
  line 6), `ask_human` already puts the build in `waiting` and resumes on the answer (`app/kaisen/builder.py`
  around line 776, inbox routes in `app/routers/kaisen.py`). Needed: a flag on the build or run + migration, one
  conditional paragraph in the builder prompt, a checkbox in `KaisenNewView.vue`, tests with `FakeModelClient`.
- **Caveats:** adds one model turn and one wait. On Haiku that is a fraction of a cent; it also keeps the prompt
  cache warm only if the user answers within the cache lifetime, so a slow answer re-pays the cached prefix once.
  Off by default so unattended builds and `scripts/kaisen_eval.py` are unchanged (the eval already answers one
  question automatically). This is a prompt change: check it with one or two starter briefs by hand, not a full
  eval run, per the eval spend policy.

#### B4. Brief check before starting (no model), with an optional "Improve brief"

- **What:** step 1, as the user types, show which items from "Write a good brief" are missing: no storage named
  (MySQL / PostgreSQL / browser only), nothing marked out of scope, mentions of email or background jobs that
  Kaisen cannot do. Step 2, optional: an **Improve brief** button that sends the brief to Haiku once and returns
  a rewrite the user can accept or discard.
- **Who has it:** Bolt's Enhance prompt asks questions about the build and returns an editable prompt
  ([B1](https://support.bolt.new/best-practices/prompting-effectively)).
- **Why it matters here:** the brief decides the cost of the build. The limits (one web process, no cron, no
  email) are documented but only found after a run fails on them.
- **Effort:** step 1 is 2-3 h in `KaisenNewView.vue` with keyword rules, no backend. Step 2 is about 0.5 day
  more: one route in `app/routers/kaisen.py`, one request through `AnthropicClient` in `app/kaisen/llm.py`, and
  a `KaisenEvent` row with the cost so it counts toward the caps (`spend_since` in `app/kaisen/builder.py:370`
  sums `KaisenEvent.usd`).
- **Caveats:** step 1 costs nothing. Step 2 costs well under a cent per press on Haiku (8000 characters in,
  about the same out) but must be metered, capped and rate-limited like any call, and the brief must pass the
  existing secret scan before it is sent. Ship step 1 alone first; add step 2 only if users ask.

#### B5. Live preview pane on the build page

- **What:** show the deployed preview in a frame beside the conversation, with phone / tablet / desktop widths
  and a reload button, instead of only a link that opens a new tab.
- **Who has it:** v0 (Preview tab, with Design mode on top of it) ([V0](https://v0.app/docs/design-mode));
  Base44 (preview in the editor) ([BA1](https://docs.base44.com/Getting-Started/app-editor-overview)). Lovable,
  Bolt and Replit are known to have it; not re-checked today.
- **Why it matters here:** the loop is "ask, look, ask again". Switching tabs and reloading after each run is
  friction, and the phone-width check is the one technicians skip.
- **Effort:** 0.5-1 day of frontend (`frontend/src/components/BuildLastDeployment.vue`,
  `frontend/src/views/KaisenBuildView.vue`) plus one backend line. The platform CSP in `app/main.py` (line 290)
  has `default-src 'self'` and no `frame-src`, so framing an app host is blocked today and needs
  `frame-src https://*.<apps domain>` for the SPA.
- **Caveats:** this one is a security decision, not only a layout change. Tenant apps are same-site with the
  platform (the comment at `app/main.py:261` covers this), so the frame must use the `sandbox` attribute and the
  change needs a security-reviewer pass before it ships. An app that sends its own `X-Frame-Options` or
  `frame-ancestors` will show blank; fall back to the link. Do it last of the five. No model spend.

### B.3 Considered but not easy

- **Model reads screenshots (vision).** Designed as uploads P2 (max 3 reference images per run, about 1.5k
  tokens each) and answered "later" by Chas on 2026-11-03. Adds spend per run and a new review of what image
  content reaches the provider.
- **Click an element to edit it.** v0 and Base44 have it ([V0](https://v0.app/docs/design-mode),
  [BA1](https://docs.base44.com/Building-your-app/AI-chat-modes)). Needs a script injected into the preview, a
  mapping from DOM node to source file, and B5 first. Weeks, not days.
- **Browser errors fed back to the builder.** A known gap ("no browser in the loop"). Needs the runtime image to
  inject an error reporter or a headless browser in the worker; either way a runtime change and a re-pin.
- **Public share link for a build** (read-only conversation and files). New unauthenticated surface over content
  that can include a brief with private detail; needs redaction rules and an abuse path. About 2-3 days with
  review.
- **Database state in restore points.** Replit's checkpoints include the database
  ([RP2](https://docs.replit.com/core-concepts/agent/checkpoints-and-rollbacks)); Bolt's restore explicitly
  does not ([B3](https://support.bolt.new/building/using-bolt/rollback-backup)). Kaisen matches Bolt. Tying
  restore points to app backups is possible later but not small.
- **Automatic fix loop without a click.** Bolt's own docs warn that repeated "Attempt fix" burns tokens
  ([B4](https://support.bolt.new/best-practices/maximizing-token-efficiency)). The manual **Fix it** button with
  caps is the right default for users paying per run. Leave as is.
- **Start a build from an existing GitHub repo.** Needs import limits, secret scanning of the import and a
  mapping to `kaisen.json`. About 2-3 days.

---

## Sources

Read on 2026-11-11 unless marked.

Platform:

- [R1] Render deploy hooks: https://render.com/docs/deploy-hooks
- [R2] Render notifications: https://render.com/docs/notifications
- [R3] Render cron jobs: https://render.com/docs/cronjobs
- [RW1] Railway variables (Raw Editor, shared variables): https://docs.railway.com/guides/variables
- [RW2] Railway logs: https://docs.railway.com/guides/logs
- [RW3] Railway app sleeping: https://docs.railway.com/reference/app-sleeping
- [V1] Vercel deploy hooks: https://vercel.com/docs/deploy-hooks
- [N1] Netlify status badges: https://docs.netlify.com/monitor-sites/status-badges/
- [C1] Coolify notifications: https://coolify.io/docs/knowledge-base/notifications
- [C2] Coolify basic auth (search result summary of the docs page):
  https://coolify.io/docs/core/networking/proxy/traefik/basic-auth
- [D1] Dokploy auto deploy: https://docs.dokploy.com/docs/core/auto-deploy
- [D2] Dokploy advanced settings (basic auth): https://docs.dokploy.com/docs/core/applications/advanced
- [K1] Deploy to Koyeb button: https://www.koyeb.com/docs/build-and-deploy/deploy-to-koyeb-button
- [F1] Fly.io autostop/autostart (the URL redirected and the page was not read):
  https://docs.fly.io/launch/autostop-autostart

Not reached: CapRover's app configuration page returned only a redirect stub, so CapRover is not in the table.
Northflank and Zeabur were not checked.

Kaisen (items marked "search" come from search-result summaries of the linked page, not a full read):

- [L1] Lovable remix (search): https://docs.lovable.dev/features/projects/remix
- [L2] Lovable Plan mode: https://docs.lovable.dev/features/plan-mode
- [L3] Lovable version history (search): https://docs.lovable.dev/features/projects/history
- [B1] Bolt prompting, Enhance prompt: https://support.bolt.new/best-practices/prompting-effectively
- [B2] Bolt Plan mode (search): https://support.bolt.new/best-practices/plan-mode
- [B3] Bolt rollback and backup (search): https://support.bolt.new/building/using-bolt/rollback-backup
- [B4] Bolt token efficiency (search): https://support.bolt.new/best-practices/maximizing-token-efficiency
- [V0] v0 Design mode and screenshots (search): https://v0.app/docs/design-mode
- [V0b] v0 fork endpoint (search): https://v0.app/docs/api/v1/reference/chats/fork
- [RP1] Replit AI billing (search): https://docs.replit.com/billing/ai-billing
- [RP2] Replit checkpoints and rollbacks (search):
  https://docs.replit.com/core-concepts/agent/checkpoints-and-rollbacks
- [BA1] Base44 chat modes and editor (search): https://docs.base44.com/Building-your-app/AI-chat-modes and
  https://docs.base44.com/Getting-Started/app-editor-overview
- [GS1] GitHub Spark remix (third-party summary, not confirmed in GitHub's docs):
  https://free.theresanaiforthat.com/ai/github-spark
- [GS2] GitHub Spark concepts (search): https://docs.github.com/en/enterprise-cloud@latest/copilot/concepts/spark
- [FS1] Firebase Studio shutdown dates (third-party, search): https://aiidelist.com/blog/firebase-studio-ai
- [S1] Same.dev review (third-party, search): https://www.banani.co/blog/same-dev-review
