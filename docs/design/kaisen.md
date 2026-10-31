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
`DB_*`/`DATABASE_URL` refs (`"database"` is `mysql` or `postgres`; `"bucket": true` adds a bucket and `S3_*` refs; `"volume": {"path"}` a persistent volume; build secrets are merged into the env as app secrets). The `deploy_preview` tool then polls the app status, probes the manifest's `health` path (default `/`; 2xx and 3xx count as answering, a redirect is shown as `302 -> /login`; 4xx/5xx and no answer do not; the log line `[kaisen-runtime] bundle sha=<12>` of a different bundle makes the result NOT YET so an old pod's answer is never reported OK; `kube.get_status` stays `deploying` while old pods remain in the rollout) and returns the app's recent logs.
**Logs (built):** the worker makes no kube calls and has no service-account token (K6), so it never reads pods. Instead the
reconciler (API pod, which already serves the user-facing logs view through `KubeProvider.pod_logs`) calls
`app/kaisen/logs.py:snapshot_preview_logs` at the end of each sweep for every preview (status deploying/running/error) whose
build has an active run, and stores the last `KAISEN_PREVIEW_LOG_LINES` (100) lines, each clipped and the whole capped at
`KAISEN_PREVIEW_LOG_BYTES`, in `kaisen_preview_logs` (one row per app, replaced each tick; rows of builds without an active run are
dropped). The snapshot is stripped of control characters and redacted (project secrets, build token, the app's DB password, bundle
URL) before it is stored; the worker reads the row from the database and redacts again, caps the tool result at
`KAISEN_PREVIEW_RESULT_BYTES` (4 KB), and labels it as untrusted app output. No new network path, NetworkPolicy or credential.
Freshness is one reconcile tick, so the text starts with "snapshot taken Ns ago" and a brand-new crash may need a second call.
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

Billing, background workers/cron and email for built apps, CI for generated apps. Built since the first cut (2026-10-30): preview logs (s4), PostgreSQL/bucket/volume previews, build secrets, per-member spend limits (`kaisen_member_limits`; enforced next to the project caps, day resets 00:00 UTC), web access (s11), generated tests (`kaisen.json` `test`, run by `boot.py`, result parsed from the log snapshot by `app/kaisen/testresults.py`; shows only once the runtime image is re-pinned) and the reviewer step (`app/kaisen/reviewing.py`, off by default, one fix round; the builder's `finish` must carry `review_response` or change a file, refused once otherwise; the answer and a files-changed flag are stored in `kaisen_runs.review` JSON and shown on the build page).

## 10. Output is per build (Chas, 2026-10-29)

> The code repo push needs to be moved to the app config since each app gets its own repo. The GitHub key can be part of
> project settings and reused when setting the auth method for a given Kaisen app.

- **Project Kaisen settings** keep: Anthropic key, model, effort, caps, and one **project GitHub token** (secret,
  write-only). The project-level `output_mode`, `github_repo`, `github_branch`, `push_on_finish` and `open_pr` move to the build.
- **Build output** (`kaisen_builds` columns): `output_mode` download|github, `github_repo` (accepts
  `https://github.com/owner/repo[.git]` or `owner/repo`), `github_branch` (blank = `kaisen/<slug>`), `push_on_finish`,
  `open_pr`, `github_auth` project|build, `github_token_enc` (only for `build`).
- API: `GET/PUT /kaisen/builds/{id}/output` (anyone who can run the build; secrets write-only; `clear_github_token`),
  `POST /kaisen/builds/{id}/output/test` -> `{ok, message}` checks push access with the selected token. `POST /kaisen/builds`
  accepts an optional `output` object. Build detail `output` = `{mode, repo, branch, push_on_finish, open_pr, auth,
  token: {set, last4}, project_token_set, can_push}`.
- Validation: github mode needs a repo and a usable token (project token set when auth=project; a build token when
  auth=build). Errors in plain words. Migration copies any existing project-level repo settings onto that project's builds.
- UI: an **Output** card on the build page (edit in place: mode radio, repo URL, branch, auth radio "Use the project's
  GitHub token" / "Use a token for this build only" with the token field and instructions, two checkboxes, Save, Test,
  Push now); optional collapsed "Output" section on New build. Project Kaisen settings show only the GitHub token block.

## 11. Web access (built, Chas 2026-10-30)

Built: per-project `web_access` toggle and domain allow-list, `web_search_20250305` and `web_fetch_20250910` (basic, direct), models in `KAISEN_WEB_MODELS`, `pause_turn` handled, searches metered at `KAISEN_WEB_SEARCH_USD`, audit `kaisen.web_access_update`. The notes below are the original brief.

> Could Kaisen or the LLM do web searches if given a link? ... Yes please.

- Use Anthropic's server-side tools (no MCP): `web_fetch` (only fetches URLs already in the conversation, e.g. a link in
  the brief or feedback) and `web_search`. Pick the tool versions the configured model supports; **verify against the live
  docs first** (the `_20260209` variants are documented for Opus 5.5 / Sonnet 5.5; Haiku 5.5 support unconfirmed).
- Project Kaisen settings: **Allow web access** (off by default), optional domain allow-list (`allowed_domains`), `max_uses`
  per request. Owners edit.
- Builder loop: handle `stop_reason == "pause_turn"` (resend to continue); treat `web_search_tool_result` /
  `web_fetch_tool_result` content as untrusted; errors arrive as result blocks, not exceptions.
- Metering: count `usage.server_tool_use` web search requests into events and caps (priced per search, plus tokens).
- Transcript and CONVERSATION.md: show "searched for X" / "read URL Y", never page bodies.
- Security: fetched text is a prompt-injection path; the builder has no secrets and only file tools scoped to its build,
  so off-by-default plus an allow-list is the shape. security-reviewer pass before ship. If the org disabled web tools in
  the Anthropic Console, surface the API error in plain words.

## 12. UX research (2026-10-30)

Method: web search plus doc fetches. Verified against vendor docs where noted; the rest comes from third-party 2026
comparison blogs and is marked (secondary). Search tools found little official changelog detail for Bolt/v0, so treat
those rows as indicative.

### Patterns seen in current tools

| Pattern | Who | Source |
|---|---|---|
| Point-and-click edits on the live preview: select element(s), describe the change; inline text edit (free); draw annotation; pin comments, send to agent later | Lovable | https://docs.lovable.dev/features/visual-edit |
| Automatic checkpoints at milestones and before risky fixes (files, packages, config, conversation, agent memory, dev DB); "Rollback to here", roll forward, rollback then edit forks a branch; checkpoints are also git commits | Replit Agent | https://docs.replit.com/replitai/checkpoints-and-rollbacks |
| Agentic loop that plans, edits several files and runs commands (Apr 2026); multi-agent orchestration (Agent 3) | Bolt, Replit (secondary) | https://www.web3aiblog.com/blog/v0-vs-lovable-vs-bolt-vs-replit-agent-vibecoding-showdown-may-2026 |
| Per-chat git branch with auto-commits and PR merge; token-based pricing (Feb 2026) | v0 (secondary) | https://www.heyuan110.com/posts/ai/2026-07-08-lovable-vs-v0-vs-bolt/ |
| GitHub sync / full project export as the escape hatch | Lovable, Bolt (secondary) | https://dev.to/rupa_tiwari_dd308948d710f/lovable-vs-bolt-vs-v0-which-ai-app-builder-should-you-use-in-2026-15mp |
| Credits/messages shown per action, so cost is visible before sending | Lovable (secondary) | https://dev.to/devtoolpicks/lovable-vs-bolt-vs-replit-vs-v0-in-2026-best-vibe-coding-tool-for-solo-developers-d88 |

Common to all of them (from general knowledge, not re-verified this pass): streaming step list while the agent works,
one-click "fix this error" that pastes the runtime/console error into the next prompt, starter templates, a public
share URL for the preview, and a plan/discuss step before code is written.

### What Kaisen has today

Build page with Live app card, feedback box, run history with redacted transcript, files list + viewer, Download/Push,
inbox for builder questions, output per build. Built since: "Fix it", suggestion chips, starter cards, progress strip, cost hint, PLAN.md. Still missing: undo, diff between runs, element-level edits.

### Recommended enhancements (simple, hobby-cost)

| # | What | Why | Effort | Likely files |
|---|---|---|---|---|
| 1 | "Fix it" button: when the preview is unhealthy or last deploy failed, one click sends the last deploy log tail (redacted, ~2 KB) as feedback | Most common iteration; removes copy/paste; reuses existing feedback path | S | `frontend/src/views/KaisenBuildView.vue`, `components/BuildRuns.vue`; maybe a small API helper in `app/kaisen/` router |
| 2 | Per-run snapshot + "Restore to before run N": store file hashes/blobs at run end (files are already in DB), restore = write back and redeploy | Cheap undo makes users bolder; Replit's most-cited safety net | M | `app/kaisen/models.py` (+ Alembic), `builder.py`/`worker.py` (snapshot at run end), router, `BuildRuns.vue` |
| 3 | File-level diff for each run (added/changed/deleted list, expandable unified diff) | Lets the user see what feedback actually did without reading transcripts; no LLM cost | S-M | `BuildFiles.vue`, `BuildRuns.vue`, snapshot data from #2 (or per-write events) |
| 4 | Suggested next prompts: 3 chips under the feedback box. Cheapest form is static (e.g. "add tests", "make it mobile friendly", "add a README section") plus the builder's own "next ideas" line from `finish` summary | Blank textarea is the main drop-off; chips cost nothing | S | `KaisenBuildView.vue`; add one sentence to `prompts/builder.md` asking `finish` to end with 2-3 "Next:" ideas, parse in UI |
| 5 | Live progress strip: show current tool call ("writing app.py", "deploying") from existing events, polled | Perceived speed; already have events in DB | S | `KaisenBuildView.vue`, `BuildRuns.vue`, events endpoint |
| 6 | Pre-run cost hint: "last run cost $X; step cap $Y" beside the send button | Users pay with their own key; Lovable-style visible cost | S | `KaisenBuildView.vue` (data already in settings/events) |
| 7 | Starter templates on New build (5 starter briefs from USER_GUIDE s16 as clickable cards that fill the textarea) | Fast first success; static data | S | `KaisenNewView.vue` |
| 8 | Pin-a-note on preview ("click-to-comment" without DOM selection): simple structured feedback form with fields page/what's wrong/what you expected | Approximates visual edits without iframe injection; keeps prompts specific | S | `KaisenBuildView.vue` |
| 9 | Share link for the preview (read-only public URL toggle) | Show-off loop; depends on app visibility settings, so check the security model first | M | app visibility settings, build router |
| 10 | True element-pick visual edit (inject script into preview iframe) | High wow, but needs same-origin or postMessage bridge in generated apps and a security review | L, skip | not now |

### Ranked recommendations (UX)

1. "Fix it" button from deploy logs (S).
2. Static starter-brief cards on New build (S).
3. Suggested-next chips, seeded from `finish` summary (S).
4. Run snapshots + restore (M), then run diff on top of it (S-M).
5. Live progress strip and pre-run cost hint (S each).

## 13. Agent kit state of the art (2026-10-30)

Sources checked: Anthropic context engineering post (https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents),
prompt caching docs (https://platform.claude.com/docs/en/docs/build-with-claude/prompt-caching), AGENTS.md
(https://agents.md), spec-driven / multi-agent write-ups (https://www.truefoundry.com/pt/blog/spec-driven-development-ai-agents,
https://habib0x0.github.io/spec-driven-plugin/agents/, https://skills.sh/GanyuanRan/Aegis/subagent-driven-development).
Several SDD pages are plugin marketing, so weigh them lightly.

### Landscape

| Practice | State of the art | Kit / builder today |
|---|---|---|
| Instruction files | AGENTS.md is the cross-tool standard (Linux Foundation, 60k+ repos; nearest file wins; plain Markdown: overview, build/test commands, style, tests, security). Practitioners warn against bloat: ~150-200 standing instructions as a rough ceiling (TrueFoundry, rule of thumb) | Kit has CLAUDE.md per project + agents/*.md (12 roles, 27-70 lines each) + LESSONS.md. No AGENTS.md; builder prompt is 27 lines, fine. Generated apps get README only |
| Spec-driven | requirements -> design -> tasks, then implement per task | Kit: BRIEF.md, SPEC docs, ledger. Builder: brief only, no plan step |
| Role split | Reasoning-tier planner, standard-tier implementer, reasoning-tier reviewer, debugger on rejection; fresh sub-agent per task with spec-review then quality-review | Kit has roles and manager dispatch (max 3). Builder is a single loop, no review pass |
| Context management | Compaction (clear old tool results first), structured notes (NOTES.md/todo), sub-agents returning 1-2k token summaries, just-in-time retrieval | Builder resends full message history each turn (see below). Kit ledger acts as notes |
| Prompt caching | Reads cost 0.1x (0.05x on Sonnet/Opus 5.5; Haiku 5.5 $0.01/MTok); 5 min TTL default, 1 h at 2x write; automatic caching via one top-level `cache_control` follows the growing conversation; min cacheable 512 tokens (Haiku 5.5) / 4096 (Haiku 4.5) | `llm.py` caches only the system block. Tools + system are small, and the growing `messages` are not cached, so every turn pays full input price for the whole history. Largest single cost lever |
| Test-first / verify loop | Tests before code, run in loop, fail output fed back | Builder cannot run code; it must write tests but only `deploy_preview` HTTP 2xx is verified |
| Evals | Small fixed suite of briefs, score by pass/fail + cost | Five starter briefs exist in the guide but are not run automatically |
| Cost controls | Cap per run/day/build, cheaper tier for routine work, turn caps, stop on repeated identical calls | Already present: cap_run/day/build, MAX_TURNS 40, repeat-call detection. Default model is now `claude-sonnet-5-5` (config.KAISEN_DEFAULT_MODEL; Chas 2026-10-30) |

### Recommendations

In-platform builder (`app/kaisen/`):

| # | Change | Value | Cost |
|---|---|---|---|
| B1 | Add automatic caching (top-level `cache_control`, or a breakpoint on the last message block) in `llm.py`, and add a `cache_control` on the last tool definition. Verify via `cache_read_input_tokens` in KaisenEvent. A 40-turn step otherwise re-bills a growing prefix ~40 times | Very high (likely 50-80% input-cost cut on long steps) | S, one function plus test with fake client |
| B2 | Trim old tool results: when sending messages, replace `read_file`/`write_file` results older than N turns with a stub ("file written, 3.1 KB"). File contents are retrievable via list/read tools. Keep the persisted run.messages untouched | High; keeps context small on 384MB/long runs | S-M, `builder.py` message assembly |
| B3 | Re-evaluate default model/effort: change default to the cheapest model that passes the starter-brief eval; leave Opus as opt-in in settings | High for user spend | S config; needs B6 to justify |
| B4 | Add a "plan first" instruction: first turn writes a short `PLAN.md` (checklist), later feedback runs read it and tick items; acts as notes across steps/resumes | Medium; improves resume and iteration coherence, tiny token cost | S, `prompts/builder.md` |
| B5 | Add a self-review step before `finish`: one instruction to re-read kaisen.json, Dockerfile and tests against the brief checklist (no extra sub-agent) | Medium; catches the common kaisen.json/PORT mistakes | S, prompt only |
| B6 | Eval harness: script that runs 3-5 fixed briefs with a fake or real key (manual, not CI), records pass (deploy 2xx), turns and USD to a table in docs | Medium-high as the safety net for B3 and every prompt change | M, new `scripts/kaisen_eval.py` |
| B7 | Optional cheap reviewer pass using the smallest model on the diff before finish | Low-medium; defer until B6 shows quality issues | M |

Agent kit (`~/source/kaisen`):

| # | Change | Value | Cost |
|---|---|---|---|
| K1 | Add an `AGENTS.md` that is the single source (project overview, commands, rules) and make CLAUDE.md a pointer or symlink, per agents.md migration advice | Medium; tool-neutral kit for other agent users | S |
| K2 | Prune/size-check: cap CLAUDE.md and each agent file (target < 60 lines, < ~150 rules total); move examples to docs the agent reads on demand (just-in-time) | Medium; avoids the instruction bloat the SDD critics describe | S |
| K3 | Add a "spec -> tasks" template (`templates/TASKS.md`: requirement, acceptance check, owner role) and require the manager to write it before dispatching a wave | Medium; formalizes the existing BRIEF/ledger practice | S |
| K4 | Per-role model hints in agent frontmatter: planner/reviewer roles (security-reviewer, ui-ux-designer) reasoning tier, workers sonnet/haiku (already sonnet for most; document the policy in WAVE.md) | Medium; budget discipline | S |
| K5 | Reviewer-after-implementer gate in WAVE.md: spec-compliance check by qa-tester before security review, fresh context (sub-agent) | Medium | S |
| K6 | Lessons hygiene: LESSONS.md entries get a date + "evidence" link and are pruned quarterly; add lessons from this research (cache the growing history, trim tool results) | Low-medium | S |
| K7 | Keep PLANNED items (headless orchestrator, /wave skill) parked; not worth hobby tokens now | n/a | none |

### Ranked recommendation list (kit vs builder)

1. Builder B1: cache the growing message history (and last tool) in `llm.py`.
2. Builder B2: stub old tool results in the outgoing messages.
3. Builder B6 then B3: small eval of starter briefs, then pick a cheaper default model.
4. Builder B4 + B5: PLAN.md checklist and pre-finish self-check in `builder.md` (prompt-only).
5. Kit K1 + K2: AGENTS.md as the source, size caps on instruction files.
6. Kit K3-K5: TASKS template, model-tier policy, reviewer-after-implementer gate.

## 14. Demo scorecard (2026-10-30)

Chas's F9 request. The five starter briefs from USER_GUIDE section 16 were built one at a time in project Demo (id 31)
on the project's model (Claude Haiku 5.5, effort medium) by the qa-tester, then checked live with curl, the downloaded
tarball and (for the node/static apps) `node --test`; each build and preview was deleted afterwards. Builds 1/2 and apps
184/185 were not touched. The runtime image is not yet re-pinned, so no generated-test result was ever reported by the
platform (every builder summary says "tests unverified"). Not browser-checked (no headless browser on this machine):
UX scores come from reading the HTML/CSS/JS plus HTTP checks, so treat them as provisional.

| Brief | Cost | Runs | Steps | Time | Deployed | Coverage | Works | Code | kaisen.json | UX |
|---|---|---|---|---|---|---|---|---|---|---|
| Fancy Docs | $0.061 | 2 (1 error, 1 resume) | 11 | 11 min | yes | 5 | 4 | 4 | 5 | 4 |
| Team standup board | $0.017 | 1 | 5 | 2.5 min | yes | 5 | 5 | 4 | 5 | 3 |
| Link shortener | $0.017 | 1 | 8 | 2.7 min | yes | 5 | 5 | 4 | 5 | 3 |
| Habit tracker | $0.016 | 1 | 4 | 1.9 min | yes | 5 | 4 | 4 | 5 | 3 |
| Recipe scaler | $0.030 | 2 (1 stopped, 1 resume) | 10 | 4.5 min | yes | 3 | 4 | 3 | 4 | 4 |

Total spend $0.141 (limit about $2). Scores 1-5. "Works" = live checks: Fancy Docs page create/nest/move/delete,
image upload and fetch, escaped render; standup post/replace/history/validation; shortener custom and random codes,
302 + click count, bad scheme and taken-code errors, 404; habit tracker 14/14 `node --test` pass, assets 200; recipe
scaler logic via node (fractions, metric, unparseable lines unchanged).

Failure notes:
- **Fancy Docs, run 1:** "Model hit max_tokens; step stopped" before any file was written (the builder tried to emit the whole app in
  one turn). Resume succeeded. Fix: prompt asks for at most about 4 files per turn.
- **Recipe scaler, run 1 stopped by the repeat detector.** The brief said static, no server. `deploy_preview` answered
  "TESTS: none declared. Add a small test suite..." for the static runtime, so the model rewrote the app as a Node server to get
  tests run (against the brief). It then got "TESTS: no result in the log yet ... Call deploy_preview again" (image not yet
  re-pinned) three times in a row and the repeat detector stopped it; a resume finished in 18 s. Its own generated test
  was wrong (expected 355 ml for a doubled recipe; actual 710), and the summary admitted it could not run tests.
  Fix: prompt tells the model that "none declared" is fine for static, to change something between retries, and to finish
  after two empty tries. Open suggestion for code (not done, no code changes in this chunk): the "none declared" hint
  should not be sent for `static` runtimes, and the "no result" hint should not say "call again" without the guard.
- PLAN.md was left with unticked deploy/finish items in 4 of 5 builds. Fix: prompt says to tick everything before finish.
- Every demo used ask_human zero times, built `kaisen.json` correctly (runtime, start, health, database, test) and used
  the MySQL TLS recipe without errors; the first deploy with a database was healthy within the run.

Edits made: `app/kaisen/prompts/builder.md` (3 small additions above; `tests/test_kaisen_units.py` and
`tests/test_kaisen_request.py` pass, 39 tests) and `~/source/kaisen/LESSONS.md` (two lessons). Uncommitted.

## 15. Harness for bigger apps (2026-10-30)

Chas asked what Kaisen needs to build increasingly complex applications. Sources: Anthropic, "Effective harnesses for
long-running agents" (https://anthropic.com/engineering/effective-harnesses-for-long-running-agents: initializer +
incremental coding sessions, a feature list that starts all-failing, a progress file, one feature at a time, mark a
feature done only after end-to-end testing); Anthropic context editing docs
(https://platform.claude.com/docs/en/build-with-claude/context-editing: tool-result clearing, compaction, memory);
app.build (https://arxiv.org/html/2509.03310v2: per sub-task minimal context, sandbox run, validator feedback, repair loop).

### What limits Kaisen today

| Limit | Effect on bigger apps |
|---|---|
| Only `write_file` (whole file) | Every change rewrites the whole file: output tokens grow with file size, and the builder hits `max_tokens` (Fancy Docs run 1). Change requests on a 20-file app re-emit hundreds of lines for a one-line fix |
| No search | The builder must `read_file` everything to find where a thing lives; context fills with file bodies |
| Only verification is `deploy_preview` (health 2xx + log tail) plus tests at boot | A feature can be broken while `/` answers 200. The builder cannot hit `/api/...` itself, so "works" is unverified for everything but the home page |
| No feedback before deploy | A Python syntax error or broken `kaisen.json` costs a 1-4 minute deploy round trip to discover |
| One step = 40 turns / 15 min, then the run stops and a human must Resume; Resume replays the full history | Apps that need more than ~40 tool turns stall, and each resume carries an ever larger context |
| No restore point | A bad change request can break a working app with no way back except another run |
| No eval | Prompt/harness changes are judged by hand on 5 demos |

### Plan (ranked; S/M = effort)

| # | Change | Why |
|---|---|---|
| H1 (S) | `edit_file` (exact `old_str` -> `new_str`, must match once) and `search_files` (literal or regex across files, capped hits with line numbers) | Standard coding-agent tools; small edits instead of rewrites, find before read. Biggest token saver for iteration |
| H2 (S) | Write-time checks: `write_file`/`edit_file` results report Python syntax errors (`ast.parse`, never executed), invalid JSON, and `kaisen.json` problems (unknown runtime, missing `start`, bad keys) | Catches the cheap mistakes in the same turn, before a deploy |
| H3 (M) | `http_request` tool: method + path (+ optional body, a few headers) against the build's own preview host only, no redirects followed, capped and redacted body, marked untrusted | End-to-end checks of each feature (the Anthropic harness's "test like a user"). Also given to the reviewer (GET only) |
| H4 (M) | Milestones + auto-continue: for bigger briefs PLAN.md lists features, one at a time, each verified with `http_request`; when a step hits the turn limit and caps allow, the run continues in a fresh context (seed = brief + PLAN.md + file list + last summary) instead of stopping, up to `KAISEN_MAX_STEPS_PER_RUN` | Long builds stop needing a human Resume, and context stays small (context reset with structured notes) |
| H5 (M) | Restore points: snapshot files at each run start; build page "Restore" and per-run diff | Safe iteration on a working app (design s12 #4) |
| H6 (M) | Eval script (`scripts/kaisen_eval.py`): run fixed briefs through the API with the test account, score deploy, endpoint checks, turns, USD | Measures every change above; manual, never in CI (spends money) |
| H7 (later) | Server-side context editing / compaction from the API instead of our own elision, once out of beta for our models | Less code of ours; wait for GA |

Status 2026-10-30: H1-H4 and H6 built (security review K22-K27 in docs/PENTEST.md; K22 regex guard + search deadline fixed).
H5 restore points and a first eval baseline run (needs Chas's go-ahead to spend) are next.

Not planned: a code-execution sandbox in the worker (the worker must never run generated code; the preview pod is the
sandbox), multi-agent planner/implementer splits (cost), browser automation (no headless browser budget).
