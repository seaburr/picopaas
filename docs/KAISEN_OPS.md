# Working across PicoPaaS and Kaisen (operations context)

Written 2026-10-30 at Chas's request so future sessions know how the two repositories fit together, how each one
ships, and the traps already found. Read this before touching anything named Kaisen. Design: `docs/design/kaisen.md`
(sections 10-11 are the latest decisions). User-facing how-to: `docs/USER_GUIDE.md` section 16.

## 1. The two repositories

| | **seaburr/picopaas** (this repo) | **seaburr/kaisen** (`~/source/kaisen`, public) |
|---|---|---|
| What it is now | The platform **and** Kaisen (Preview): API, data, UI, worker, runtime image | The **agent kit** (agents/, process/, templates/, LESSONS.md) plus the **retired standalone** Kaisen service (`service/`, `runtime/`) |
| Kaisen code | `app/kaisen/` (builder, llm, deploy, worker, settings, pushing, export, paths, redact, prompts/), `app/routers/kaisen.py`, `frontend/src/views/Kaisen*.vue`, `components/Build*.vue` | `service/kaisen/` is the old standalone app. **Do not add features there.** It is kept only as history and as the source the platform code was ported from |
| Runtime image | `images/kaisen-runtime/` → `ghcr.io/seaburr/picopaas-kaisen-runtime:<sha>` (workflow `kaisen-runtime-image.yml`, smoke-tested in CI) | `runtime/` → `ghcr.io/seaburr/kaisen-runtime` (old, used only by the standalone app) |
| Who commits | **scm-security only** (CLAUDE.md) | Same convention: scm-security commits and pushes |
| Tests | `.venv/bin/python -m pytest -q tests/` (Kaisen: `tests/test_kaisen_*.py`, helpers in `tests/kaisen_helpers.py`) | `cd service && .venv/bin/python -m pytest -q` |

When the user says "Kaisen" they mean the feature inside PicoPaaS unless they say otherwise. Kit changes (agent
definitions, process docs) go to seaburr/kaisen; product changes go here.

## 2. How Kaisen runs in production

- **API + UI** run in the platform pods (2 replicas, 384 MB). They never run model loops.
- **Agent loop** runs in Deployment `kaisen-worker` (`deploy/platform/kaisen-worker.yaml`): same platform image, command
  `python -m app.kaisen.worker`, 1 replica, Recreate, **no service-account token**, only the `picopaas-env` keys it needs
  (`secretKeyRef`, pentest K6), egress limited to DNS, the DB, internet 443 and the Traefik pods. Leader lock: MySQL
  `GET_LOCK('picopaas-kaisen-worker')`. Liveness: a DB-free heartbeat thread touches `/tmp/kaisen-worker-heartbeat` every 30 s, but only while the
  worker's main loop has made progress within `KAISEN_HANG_SECONDS` + 120 s; the probe fails after 120 s, so a wedged worker is restarted
  about 3 min after that. `deploy.yml` waits for its rollout, so a worker that can't start fails the deploy.
- **Image pod** (uploads, shipped OFF): Deployment `kaisen-imgproc` (`deploy/platform/kaisen-imgproc.yaml`, same image, `python -m app.imgproc`, `KAISEN_IMGPROC_ROLE=1`, no secrets, no service-account token, ingress only from `app=picopaas` on 8080, no egress, 320 Mi / 500m). The API reaches it at `KAISEN_IMGPROC_URL` and never decodes an image itself; pod down means uploads answer 503 and nothing else breaks. `deploy.yml` applies it last and non-fatally. Per-project enabling, the kill switch, takedown and the pre-enable checklist are in `ADMIN_GUIDE.md` (Kaisen file uploads); pod checks and rollback in `OPERATIONS.md`. Design: `design/kaisen.md` section 16.
- **Hang path** (`app/kaisen/watchdog.py`, migration `*_kaisen_run_watchdog`; manual interrupt `POST /kaisen/builds/{id}/runs/{run_id}/interrupt` is capped at 3 per build per hour, then 429, because an abandoned model call still bills but is not metered). Every turn start, model response, tool call, deployer step and reviewer turn stamps `kaisen_runs.last_progress_at`. Model calls are bounded (`KAISEN_MODEL_CALL_SECONDS` idle, `KAISEN_MODEL_CALL_MAX_SECONDS` hard), tools by `KAISEN_TOOL_SECONDS`; the worker runs each step on a daemon thread and abandons it when it overruns. `sweep()` finds running runs with no stamp for `KAISEN_HANG_SECONDS` (600) and is called from the worker main loop, with the API pod's reconciler as backstop. A compare-and-swap on `(id, attempt)` bumps `attempt` (fencing: the abandoned step discards its late result), increments `hang_retries`, requeues the run and adds a closed inbox note of kind `hang`. After `KAISEN_HANG_RETRIES` (2) retries the run ends in `error` with `error_reason` `hung`; Resume retries it. Known limits: an abandoned model call may still bill tokens that are not metered; a hung tool thread is abandoned, not killed. `POST /kaisen/builds/{id}/runs/{rid}/interrupt` does the same by hand and is not counted as a retry.
- **Deploys** of built apps go through `routers/spec.deploy_spec()` **as the build's creator into the build's project**
  (same app limit, audit and 403/422 messages as any app). No PicoPaaS API key is involved.
- **Previews** are ordinary apps running `KAISEN_RUNTIME_IMAGE`; `boot.py` downloads the build's files at boot from the
  public, single-use capability URL `GET /kaisen/bundles/{token}.tar.gz` (token passed as the app **secret**
  `KAISEN_BUNDLE_URL`, only the latest bundle stays valid), checks the sha256, installs deps and starts the app.
  `kaisen.json` `{"runtime": "static|python|node", "start", "health", "database": "mysql|postgres", "bucket": true, "volume": {"path"}, "test"}`; a database adds
  `DB_*`/`DATABASE_URL` refs, a bucket `S3_*`. Build secrets (names only to the model) are merged in as app secrets on every deploy. `boot.py` runs `test` before the app (result via the log snapshot).
- **Served headers (P1b):** the runtime sends `X-Content-Type-Options: nosniff` on every response; files under `uploads/` get a fixed image Content-Type and a strict CSP (SVG: `default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox`), and `uploads/` has no directory listing. When the site is served from `public/`, `/uploads/` is mapped to the verified `<project root>/uploads/` (it wins over an agent-written `public/uploads/`, whose files still get the CSP). Server apps (python/node) serve `uploads/` themselves; the builder prompt says so. If the bundle has `.kaisen-uploads.json`, boot checks every upload against it (listed, sha256, size, extension) and refuses to start on a mismatch. Takes effect only after the runtime re-pin.
- **Gates**: env `KAISEN_ENABLED` (default on) **and** admin setting `kaisen_enabled`; `features.kaisen` in `/auth/me`.
- **Logs**: the worker never reads pods. The reconciler (API pod) snapshots a redacted 100-line/8 KB tail of each preview whose build has an active run into table `kaisen_preview_logs` (`app/kaisen/logs.py`, hook in `reconcile_all`); `deploy_preview` reads that row, so output can be one reconcile tick old. The same snapshot carries the test markers.
- **Migrations** (2026-10-30): four new files under `alembic/versions/` named `*_kaisen_preview_logs`, `*_kaisen_web_access_suggestions`, `*_kaisen_member_limits_secrets`, `*_kaisen_reviewer_tests` (single head: the reviewer_tests one; check with `alembic heads`). The API pods apply them on start (`entrypoint.sh` runs `alembic upgrade head` before uvicorn, so a bad migration fails the rollout); app/main.py itself does not run them.

## 3. Shipping changes

1. Dev agents change code/tests/docs, add ledger lines, **don't commit**. Run the Kaisen tests and, before a push, the full
   suite (about 1909 tests, ~6.5 min) plus `cd frontend && npm run typecheck && npm run build`.
2. scm-security commits (gitleaks on every staged chunk; **never** allowlist: change fixture values instead, e.g.
   `not-the-anthropic-key`; `fixture-anthropic-key-1234` tripped `generic-api-key`), pushes, and watches **CI, image,
   deploy** (and `kaisen-runtime-image` when `images/kaisen-runtime/` changed). `deploy` runs after both `image` and `CI`
   and rolls out only when both are green for main's current tip (an older commit never deploys over a newer one), so a
   push reaches prod about 20 minutes later. A manual `deploy` dispatch skips that check.
3. **Runtime image changes** need a re-pin: after `kaisen-runtime-image` publishes, run `bootstrap.yml` with input
   `kaisen_runtime_image_digest=sha256:...` (it writes `KAISEN_RUNTIME_IMAGE` into `picopaas-env`). A bootstrap run
   **needs Chas's approval** (QUESTIONS.md). Leaving `backup_image_digest` empty re-pins the backup image to `:latest` too.
   Current pin (2026-11-03, run 41734937157, Chas approved "full send"): `picopaas-kaisen-runtime@sha256:4a58353a2657f130fd313faf7e2815c6717c0fd9f953786495999c7901d761bf` (P1b upload headers, manifest check, /uploads for public/ sites). Previous: `sha256:0b06ce362c897448c7d4c7e2acd2e75d61c01aa4321cc606c849b0c8fecb77a7` (2026-10-31, run 41192942142). Backup image pinned at `sha256:bc6ebfca524f2b7a149e3c160b8b63d5b9030f1a7e75bb90eb6587fe36bfd694`.
   Dependabot (Mondays) opens base-image PRs for `images/kaisen-runtime/`. A merged one publishes a new image but
   changes nothing live until this re-pin, so batch them and ask Chas for one re-pin at a time.
4. Existing built apps pick up deploy-time changes (e.g. the bundle-URL-as-secret fix) only on their next deploy
   ("Deploy now" / `POST /kaisen/builds/{id}/deploy`; same hostname, ~1 min restart). Ask before redeploying Chas's apps.
5. Security changes to Kaisen get a security-reviewer pass (docs/PENTEST.md has K1-K11 and the final pass). UI changes get
   a ui-ux-designer browser check that **measures page widths against Account/App settings** (a narrow settings page
   slipped through once).

## 4. Accounts and live objects

- `~/.picopaas-e2e.env`: `E2E_ADMIN_TOKEN` = **Chas's own account** (read-mostly; don't spend his Anthropic key or touch his
  builds without asking), `E2E_USER_TOKEN` = test user (app limit 4), Ghost blog creds for release-blogger.
- `~/.kaisen-test.env`: URL + admin password of the **retired standalone** Kaisen instance (app `kaisen` on the test user,
  https://grand-pony-a4bhrvh.picopaas.xyz) and its two previews. Chas agreed to delete them once the built-in version
  works; **ask before deleting** (QUESTIONS.md).
- Chas's live demo objects (project **Demo**, id 31, limit 3): build 1 "weather" → app 184, build 2 "Fancy Docs" → app 185.
  Project Kaisen settings there: Claude Haiku 5.5, effort medium, caps $2 run / $8 day / $30 build.
- Demo recipe: the one-shot "Fancy Docs" rebuild command (curl + jq, ~5 min, ~$0.05 on Haiku) is in the chat history
  of 2026-10-30 and the brief is the first starter in USER_GUIDE section 16. Take the previous demo app offline first or
  the Demo project's 3-app limit blocks the deploy; a build named like an existing app can't deploy (409).

## 5. Traps already found

- The PicoPaaS API is at the **root** (`/auth/me`, `/apps/spec`, `/kaisen/...`), not `/api/...`.
- Tenant pods can reach `*.picopaas.xyz` only because of NetworkPolicy `allow-egress-ingress` (LB IP is short-circuited to
  Traefik's private pod IPs). Don't remove it.
- zsh `echo "$json"` expands `\n` and corrupts JSON: use `printf '%s'` or Python.
- Headless Chrome screenshots at 390 px crop a wider layout; use device emulation (ui-ux-designer does) before calling
  something a mobile bug.
- MySQL `DATETIME` has no fractions and `UPDATE` returns changed (not matched) rows unless `CLIENT.FOUND_ROWS` is set
  (lease CAS bug class).
- Anthropic: Opus 5.5 / Sonnet 5.5 reject forced `tool_choice` and `thinking: disabled`; verify model support before
  adding server tools (web access, design section 11, is built; all three KAISEN_PRICES models support the web tools).
- Wording Chas set: "Kaisen is in preview", badge "Built with Kaisen · Preview", success text "Key valid. Selected model
  available."; never compare PicoPaaS to Heroku.

## 6. Where we left off (2026-11-03)

Shipped and live: Kaisen in PicoPaaS with per-build output, generated tests, reviewer, web access, PostgreSQL, bucket and
volume previews, build secrets, member limits, and the hang watchdog (automatic retry plus a manual **Interrupt and retry**,
limited to 3 per build per hour). Open: residual unmetered spend from abandoned model calls and auto-retries (see
`QUESTIONS.md`), and the pentest LOWs in `PENTEST.md`. For the current state read "Now / Next" in `LEDGER.md`; for the
runtime image pin see `OPERATIONS.md`.

## 7. Eval (manual, never in CI)

`scripts/kaisen_eval.py` builds the briefs in `scripts/kaisen_eval_briefs.json` (the five USER_GUIDE starters plus a kanban board, a notes app with uploads and the large Small CRM) through the public API, polls each build (answers one `ask_human` with "Decide yourself and note the assumption.", resumes a stop once), runs the briefs' HTTP checks against the preview and prints a markdown table (design s14 columns plus totals). It deletes builds and previews afterwards unless `--keep`. Run it against a **test account's own project** with that account's API token, never Chas's token and never project Demo (id 31) unless Chas asks. It spends real money on that project's Anthropic key (about $0.02-$0.10 per starter on Haiku, more for the two harder briefs), so keep `--max-usd` low (default 1.00; it stops starting new briefs once reached). No CI job runs it.

```bash
export E2E_USER_TOKEN=...   # test-account API key, never echoed
python scripts/kaisen_eval.py --project-id <id> --briefs "Habit tracker,Recipe scaler" --max-usd 0.50 --out /tmp/eval.md
```

Data-collection run (what to use when the eval will not be repeated soon):

```bash
python scripts/kaisen_eval.py --project-id <id> --briefs "habit-tracker,recipe-scaler" --model <model> --effort <effort> \
  --scratch-dir <scratch> --max-usd 2 --out <scratch>/eval.md
```

- `--briefs` takes a comma list of names or slugs, run in the order given.
- `--model` / `--effort`: GETs then PUTs `/projects/{id}/kaisen`, remembers the previous values and restores them in a `finally` (a failed restore is logged and makes the exit code 1). The model is recorded in every row.
- `--data-dir DIR` (default `<scratch-dir>/eval-data/<timestamp>`, `eval-data/` is gitignored): per brief `<slug>/detail.json` (final build detail with run transcripts, tests, review, spend, app), `code.tar.gz` (download route), `run-<id>-diff.json` (runs with `has_diff`; a 404 is tolerated), `checks.json` (request, status, body snippet up to 2 KB, pass/fail, attempts), `poll.jsonl` (t, status, step, last_tool, cost, context_resets per poll); top level `summary.json` and `summary.md`. Data is saved before the build is deleted; `--keep` skips the delete only.
- Metrics come from the transcripts (`[call] name {json}` and `[result] ...` lines, paired in order) and the tarball: tool calls by name, write-check WARNINGs, deploy_preview NOT OK, http_request status spread, repeat-detector / max_tokens / turn-limit stops, context resets, time to first `deploy_preview` OK (poll resolution, so +-`POLL_SECONDS`), per-run durations, PLAN.md ticked/unticked, file count and bytes, tests/README, Dockerfile `USER`, `kaisen.json`. They appear in a second "Behaviour" table in the output and in `summary.json`.
- **Settle** (`--settle-seconds`, default 300): after a build is `done` the post-finish redeploy may still be starting and the test result not final. Before the checks and the saved detail, the script polls build detail until `app.status == "running"` and the test is final (`pass`/`fail`/`timeout`, or nothing declared: `tests` null and no `test` in `kaisen.json`, fetched from the tarball). Settle time and whether it settled go in `row.settle`, the Behaviour table and, if not settled, a "not settled" note; the checks run anyway.
- **Flows**: a brief may define an ordered `flow` (steps like checks, sharing one cookie jar, counted in the Checks column). `{user}`/`{password}` (random `eval-<hex>`) and values captured with `"capture": {"var": "jsonkey"}` are substituted into path, body and `expect_contains`; `"csv"` sends a `text/csv` body; `"fresh": true` drops the cookies first (a new visitor). Flow POSTs are not retried. Briefs carry an optional integer `version` (Kanban board is 2 since the auth endpoints were added to its text; Small CRM, PostgreSQL, is the large eighth brief).
- Feature-verification metrics: distinct `http_request` paths per method, requests that carried a session (Cookie/Authorization header, or any request after a result showing `Set-Cookie`), and "PLAN gate hits" (transcript occurrences of "PLAN.md still has"). Note the http_request result shows only HTTP code, content-type and location, so a builder cannot see a `Set-Cookie`; "Authed reqs" is therefore mostly the Cookie header it sets by hand.
- The token is never written: only API response bodies are saved (never request headers) and any occurrence of the token in a file is replaced by `[redacted]`.

The logic is unit-tested with `httpx.MockTransport` in `tests/test_kaisen_eval_script.py`.

## Evals (manual, run sparingly)
`scripts/kaisen_eval.py` builds real apps on a metered Anthropic key (the eval project 37 uses the test user's key).
Chas (2026-10-31): run it **only after big harness changes or a new model release**, never per change and never in
CI. A full run on Haiku costs about $0.20-0.30; Sonnet several times that. Keep dated data in `eval-data/` (gitignored)
and summarise findings in the ledger and devlog. Default model for new projects is Haiku (Chas, 2026-10-31).

