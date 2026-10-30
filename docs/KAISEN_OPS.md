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
  `GET_LOCK('picopaas-kaisen-worker')`. Liveness: the worker touches `/tmp/kaisen-worker-heartbeat` at least every 60 s;
  the probe fails after 120 s. `deploy.yml` waits for its rollout, so a worker that can't start fails the deploy.
- **Deploys** of built apps go through `routers/spec.deploy_spec()` **as the build's creator into the build's project**
  (same app limit, audit and 403/422 messages as any app). No PicoPaaS API key is involved.
- **Previews** are ordinary apps running `KAISEN_RUNTIME_IMAGE`; `boot.py` downloads the build's files at boot from the
  public, single-use capability URL `GET /kaisen/bundles/{token}.tar.gz` (token passed as the app **secret**
  `KAISEN_BUNDLE_URL`, only the latest bundle stays valid), checks the sha256, installs deps and starts the app.
  `kaisen.json` `{"runtime": "static|python|node", "start", "health", "database": "mysql|postgres", "bucket": true, "volume": {"path"}, "test"}`; a database adds
  `DB_*`/`DATABASE_URL` refs, a bucket `S3_*`. Build secrets (names only to the model) are merged in as app secrets on every deploy. `boot.py` runs `test` before the app (result via the log snapshot).
- **Gates**: env `KAISEN_ENABLED` (default on) **and** admin setting `kaisen_enabled`; `features.kaisen` in `/auth/me`.
- **Logs**: the worker never reads pods. The reconciler (API pod) snapshots a redacted 100-line/8 KB tail of each preview whose build has an active run into table `kaisen_preview_logs` (`app/kaisen/logs.py`, hook in `reconcile_all`); `deploy_preview` reads that row, so output can be one reconcile tick old. The same snapshot carries the test markers.
- **Migrations** (2026-10-30): four new files under `alembic/versions/` named `*_kaisen_preview_logs`, `*_kaisen_web_access_suggestions`, `*_kaisen_member_limits_secrets`, `*_kaisen_reviewer_tests` (single head: the reviewer_tests one; check with `alembic heads`). The API pods apply them on start (`entrypoint.sh` runs `alembic upgrade head` before uvicorn, so a bad migration fails the rollout); app/main.py itself does not run them.

## 3. Shipping changes

1. Dev agents change code/tests/docs, add ledger lines, **don't commit**. Run the Kaisen tests and, before a push, the full
   suite (about 1909 tests, ~6.5 min) plus `cd frontend && npm run typecheck && npm run build`.
2. scm-security commits (gitleaks on every staged chunk; **never** allowlist: change fixture values instead, e.g.
   `not-the-anthropic-key`; `fixture-anthropic-key-1234` tripped `generic-api-key`), pushes, and watches **CI, image,
   deploy** (and `kaisen-runtime-image` when `images/kaisen-runtime/` changed). Note: `deploy` starts on push and is not
   gated on CI.
3. **Runtime image changes** need a re-pin: after `kaisen-runtime-image` publishes, run `bootstrap.yml` with input
   `kaisen_runtime_image_digest=sha256:...` (it writes `KAISEN_RUNTIME_IMAGE` into `picopaas-env`). A bootstrap run
   **needs Chas's approval** (QUESTIONS.md). Leaving `backup_image_digest` empty re-pins the backup image to `:latest` too.
   Current pin: `picopaas-kaisen-runtime@sha256:b11f8beb7f899c348b5044069db05c0f6006d338694550e44cbd223cf1db47ce`.
4. Existing built apps pick up deploy-time changes (e.g. the bundle-URL-as-secret fix) only on their next deploy
   ("Deploy now" / `POST /kaisen/builds/{id}/deploy`; same hostname, ~1 min restart). Ask before redeploying Chas's apps.
5. Security changes to Kaisen get a security-reviewer pass (docs/PENTEST.md has K1-K11 and the final pass). UI changes get
   a ui-ux-designer browser check that **measures page widths against Account/App settings** (a narrow settings page
   slipped through once).

## 4. Accounts, credentials and live objects (no secret values here)

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

## 6. Where we left off (2026-10-30)

Shipped and live: Kaisen in PicoPaaS (per-build output, start on create, CONVERSATION.md, Take offline, inbox dismiss),
final pentest (no HIGH/MEDIUM; open LOWs K9 accepted, P3 no per-user rate limit on Kaisen run/test/push), coverage 94%,
blog posts, user guide how-to with starter briefs, landing page section. Next: Chas's testing feedback (expected
2026-10-30), retire the standalone instance. Built 2026-10-30 (pending push and security review): logs in `deploy_preview`, web access, PostgreSQL/bucket/volume previews, build secrets, member limits, generated tests, reviewer. **Runtime image re-pin pending** (`boot.py` runs generated tests; needs `kaisen-runtime-image` to publish, then a bootstrap run with Chas's approval, s3). Until then the test result is "no result in the log yet" and existing previews are unaffected.

## 7. Eval (manual, never in CI)

`scripts/kaisen_eval.py` builds the briefs in `scripts/kaisen_eval_briefs.json` (the five USER_GUIDE starters plus a kanban board and a notes app with uploads) through the public API, polls each build (answers one `ask_human` with "Decide yourself and note the assumption.", resumes a stop once), runs the briefs' HTTP checks against the preview and prints a markdown table (design s14 columns plus totals). It deletes builds and previews afterwards unless `--keep`. Run it against a **test account's own project** with that account's API token, never Chas's token and never project Demo (id 31) unless Chas asks. It spends real money on that project's Anthropic key (about $0.02-$0.10 per starter on Haiku, more for the two harder briefs), so keep `--max-usd` low (default 1.00; it stops starting new briefs once reached). No CI job runs it.

```bash
export E2E_USER_TOKEN=...   # test-account API key, never echoed
python scripts/kaisen_eval.py --project-id <id> --briefs "Habit tracker,Recipe scaler" --max-usd 0.50 --out /tmp/eval.md
```

The logic is unit-tested with `httpx.MockTransport` in `tests/test_kaisen_eval_script.py`.
