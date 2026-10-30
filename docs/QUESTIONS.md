# Questions & to-dos for Chas

**This file is yours.** Write answers inline, any style; one word is fine. Tick boxes when done.
Claude reads it at the start of every session, carries answers into `docs/ARCHITECTURE.md` /
`docs/requests/`, and moves answered items to **Resolved** at the bottom. The request files have the
background on each item if you want it.

## Open questions

- **Kaisen in PicoPaaS (Preview): three calls I made, tell me if you disagree (2026-10-29).** Design: docs/design/kaisen.md.
  (1) Kaisen settings (Anthropic key, caps, GitHub) belong to the PicoPaaS project, so in a team project any member can
  start builds that spend the team's key; owners edit the settings. (2) The agent loop runs in a separate `kaisen-worker`
  pod (same image, no cluster permissions) so a build can't hurt the API; no new paid resource. (3) Once the built-in
  version works, I delete the standalone test Kaisen and its two preview apps on the jimmy account. I'll ask before
  deleting.
- **Kaisen customers and the 1-app default (2026-10-29).** New PicoPaaS accounts get `DEFAULT_APP_LIMIT` = 1, and Kaisen itself
  uses that slot, so it can't deploy a preview into the customer's personal space. For now Kaisen can deploy into a project
  (3 apps by default). Options: (a) leave it and document "deploy previews into a project"; (b) raise the default for new
  accounts to 2; (c) have Kaisen create its own project. I recommend (a) for now, then (c) once Kaisen has customers.
- **Production hardening (deferred by Chas 2026-10-20, do before any real production use).** The API pod's `picopaas-env`
  secret holds an **account-wide** DO API token and a **full-access** Spaces key, so a code-execution bug in the API reaches
  the whole DigitalOcean account. For production: create a DO token scoped to only what bootstrap/the reconciler need, and a
  Spaces key limited as far as DO allows, then update the GitHub/k8s secrets and re-run bootstrap. Also revisit `main` branch
  protection and moving CI secrets into a protected Environment. Documented in docs/ARCHITECTURE.md (Production hardening).

## To-dos (only you can do these)

- **Kaisen builder v0: what I need from you (2026-10-29).** Design: ~/source/kaisen/docs/BUILDER_V0.md. Setup happens in the app,
  so the only deploy-time secret is `SECRET_KEY`. When the build is ready: (1) ~~create a GitHub repo for Kaisen~~ done: seaburr/kaisen
  (public, empty, remote added); first push follows once the build is green; (2) after the first release run, flip the
  `ghcr.io/seaburr/kaisen` package to Public once; (3) get the Anthropic API key, which you paste into Kaisen's Settings page, not into PicoPaaS or GitHub; (4) optional:
  a target repo plus a fine-grained token (Contents RW, Pull requests RW, that repo only), also entered in Settings. You can skip
  (4) and download a tarball instead.
- **Sign in with Google (design: docs/design/google-signin.md; effort M, not massive).** When you want it built: create a Google Cloud
  project, an OAuth consent screen (External; openid/email/profile; publish to Production), and a Web OAuth client with redirect
  `https://platform.picopaas.xyz/api/auth/google/callback`. Give me the secret names you used and add client id/secret as repo + k8s secrets.
  Also decide: (a) existing password accounts: require a password-confirmed "Link Google" from Account (recommended) or auto-link; (b) restrict by Google Workspace domain? Reply here.

## Resolved

- 2026-10-30: **Bootstrap run approved and done** (run 40982645814, success): KAISEN_RUNTIME_IMAGE pinned to picopaas-kaisen-runtime@sha256:b11f8beb...cf1db47ce; BACKUP_IMAGE re-pinned to the latest backup image (covers the 2026-10-28 MANIFEST change). Weather build deployed into Demo (app 184, 1 of 3 apps, preview 200).
- 2026-10-29: **Kaisen configuration (answered: in-app).** Chas: the customer configures Kaisen in a web form once it is
  deployed (API key, target repo), then it goes to work; output is a GitHub repo OR a tarball download; build the builder first,
  self-testing later. The SPEC v2 section 17 decisions are deferred to M1 (they concern the CI/ruleset path). See ~/source/kaisen/docs/BUILDER_V0.md.
- 2026-10-25: GHCR package `picopaas-example-ghost6` public (it was already public; anonymous pull verified 200). Blogs 98 and 164 cut over to `ghost6:14ac3a1`.
- 2026-10-25: Ghost 6 vs sql_require_primary_key (answered: conform to Ghost). Chas: upstream won't fix; the managed MySQL setting is turned off cluster-wide instead of patching Ghost. See docs/requests/0009.

- 2026-10-25: **Admin CPU/memory ceilings and admin read endpoints (answered: fix it).** Chas: cap the ceilings at what a
  node can hold, require a browser session for the other admin read endpoints (settings, users, audit) as platform logs
  already do, and move the default resources every app gets into Admin settings, with a default and a ceiling for each.
  Queued for engineering (ledger).
- 2026-10-25: **Budget rule vs reality (answered: leave as-is).** No change; Chas was just curious. Floor ~$75.30/mo,
  ~$103.30 with the second node (docs/ARCHITECTURE.md §1).
- 2026-10-25: **Shared database servers vs one per app, pentest F3 (answered: leave as-is).** Accepted for now; added to
  docs/ROADMAP.md backlog to test and enhance later.
- 2026-10-20: **API key scopes (answered and shipped).** Two tiers, `read` (safe methods only) and `full` (default);
  keys can't create or revoke keys (browser session only). See docs/API.md.
- 2026-10-23: **All to-dos done:** GHCR `picopaas-example-wordpress` already public; CLAUDE.md budget rule updated;
  `E2E_USER_TOKEN` / `E2E_ADMIN_TOKEN` added (weekly quota-e2e runs; details in tests/e2e/README.md).
- 2026-10-24: **Object versioning on `picopaas-backups` (answered: no).** Not enabled. Owner deletes and retention prunes free
  space immediately; a leaked backup key could delete backups (accepted risk; key is bucket-scoped).
- 2026-10-24: **Request 0008, second-region backup bucket (answered: not now).** Backups stay in the existing ATL1 `picopaas-backups`
  bucket. A second-region copy is on docs/ROADMAP.md as a future enhancement.
- 2026-10-24: **Backups audit (answered: yes).** Creating, deleting and downloading backups all go to the audit log, including one
  `backup.download_link` row each time a download link is issued. Implemented (`backup.create` for scheduled/final, `backup.pruned`,
  `backup.purged`, `backup.export_deleted`, `backup.download_link`); docs/ADMIN_GUIDE.md. The rest of the pentest follow-ups were handled.
- 2026-10-23: **Persistent volumes (request 0007): APPROVED 2026-10-23.** Enabled on this instance with the defaults (1 GiB users,
  20 GiB admin override, 10 volumes total). `VOLUMES_ENABLED=1` is wired into the bootstrap workflow; takes effect when Chas runs it.
  See `docs/requests/0007-persistent-volumes.md`.
- 2026-10-23: **Audit export via API keys (answered: leave as-is).** A read-scope key owned by an admin can still
  download `/admin/audit/export`; no change (docs/PENTEST.md 2026-10-23 #6).
- 2026-10-22: **Abuse hardening (answered).** Bandwidth cap: implemented as a per-pod outbound cap, default 20 Mbit/s,
  platform-admin override 1 to 160 Mbit/s (`egress_mbit`), documented. Acceptable Use + abuse page: Chas "I trust you on
  the language", so Claude is drafting the terms section and the abuse page.
- 2026-10-22: **FYI: 5 active apps per account, admins included (`MAX_APPS_PER_USER`).** Documented in README, USER_GUIDE
  and ADMIN_GUIDE; bootstrap.md left for Chas to edit.
- 2026-10-22: **PostgreSQL support (answered and shipped).** Chas approved DO Managed PostgreSQL on the cheapest SKU
  (`db-s-1vcpu-1gb`, $15.15/mo, baseline now ≈ $75.30/mo), alongside MySQL rather than replacing it, with the two-stage
  quota trade-off accepted. The spike confirmed what `doadmin` can do. It's live: the `primary-pg` database server is registered, and n8n (app 110) passed
  the acceptance test, which included surviving a restart. The pentest found no cross-tenant data access or privilege
  escalation. Chas accepted F1 (all database servers share one `/24` that tenant egress allows, so a tenant can open TCP
  to the other engine's server, though it can't authenticate) and F2 (trusted `dblink`/`postgres_fdw` let a tenant probe
  the network from the database server). Both are recorded in docs/deployment/runbooks.md (Accepted risks). F3 is still open above. Request 0006 is
  IMPLEMENTED.
- 2026-10-21: **Release-blogger API key (answered).** The key needed is a Ghost **Admin API** credential (`<id>:<secret>`);
  a Custom Integration Admin API key or a **Staff Access Token** both work (JWT signed with the secret, `kid`=id, audience
  `/admin/`). Chas will provide it as GitHub secret **`PICOPAAS_GHOST_BLOGGER_TOKEN`**; `.claude/agents/release-blogger.md`
  now reads that name (plus `GHOST_API_URL` for the blog base URL). **Never print or commit the token.** Still open: the
  blog app (`picopaas-ghost-blog`) must be claimed and `GHOST_API_URL` set (see To-dos).
- 2026-10-20: Pentest-pass follow-ups (Chas): repo made **private**; `/docs`/`/redoc`/`/openapi.json` stay **public** for now;
  `main` left unprotected for now (private repo); DO/Spaces credential scoping **deferred to production** (tracked under Open →
  "Production hardening"). Docs review item 4 (TLS workaround + rotation-repair) verified by Claude: TLS probe shipped on the
  py3.13 toolbox; rotation repairs DB/storage creds but not unreadable secret env vars (docs corrected).
- 2026-10-20: PENTEST #19 step 2 done in deploy/platform/rbac.yaml (deployed d42256a, verified live: new-app create + env-secret
  rewrite both reached running under the narrowed grant): cluster-wide secrets/resourcequotas/limitranges/services removed from
  ClusterRole `picopaas-reconciler`; the SA now has them only via per-namespace RoleBindings (app-*, picopaas-system). Residual:
  the SA keeps `bind` on `picopaas-app-ns` (documented caveat, acceptable).
- 2026-10-20: GitHub issue #9 (vague status_detail) closed (Chas).
- 2026-10-20: Signup wording chosen: "Sign up, and if your account is approved, a PicoPaaS platform admin will reach out." Applied to landing, privacy, dev banner, register, login and pending views.
- 2026-10-20: Role `uberadmin` renamed to `platform_admin` (breaking API change, approved by Chas); UI label "Platform admin"; migration `b4c8e2f6a1d3`.
- 2026-10-20: CLAUDE.md budget rule updated by Chas.
- 2026-10-20: E2E secrets `E2E_USER_TOKEN` / `E2E_ADMIN_TOKEN` added by Chas; first scheduled run triggered 2026-10-20.
- 2026-10-20: Worker node `s-2vcpu-4gb-amd` costs $28/mo (Chas, from the DO bill).
- 2026-10-20: bootstrap.md caps updated by Chas (DB 50MB, bucket 100MB, CPU 100m, RAM 384MB, image 256MB, scratch disk 250MB);
  matches `app/config.py`.
- 2026-10-19: **Own the platform encryption keys.** FERNET_KEY decrypts every secret the platform stores in MySQL; losing it
      loses that data (it happened in the 2026-10-19 cluster rebuild, harmless then). Claude put generated keys in
      repo secrets `PICOPAAS_SECRET_KEY` / `PICOPAAS_FERNET_KEY`, but GitHub secrets are write-only, so nobody has a
      copy. While nothing valuable is encrypted yet: generate your own, save them in your password manager, and
      overwrite both secrets (`python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`
      for FERNET_KEY; any 48+ random chars for SECRET_KEY). Then tell Claude to re-run bootstrap. NOTE: Both updated with the same value however if you do not need both, please delete the unused one so there is less confusion about the purpose of the key and what needs to be generated when deploying.
  **Done:** single key now (FERNET_KEY; SECRET_KEY derived, old key kept for rotation); `PICOPAAS_SECRET_KEY` can be deleted after the next bootstrap succeeds.
- 2026-10-19: DOKS "insufficient capacity" explained: a region's API size list shows what has capacity right now. NYC3 lists
  only -amd/-intel variants of the small sizes (no plain s-1vcpu-2gb / s-2vcpu-4gb); ATL1, MKC1 and MEM1 list no small
  sizes at all; RIC1 lists only -intel. Node → `s-1vcpu-2gb-amd` in NYC3 (~$14). The terraform workflow now fails early if the
  size isn't listed. No DO ticket needed.
- 2026-10-19: NYC3 also refused s-2vcpu-4gb → Chas: smallest SKUs everywhere (node `s-1vcpu-2gb` $12, MySQL `db-s-1vcpu-1gb` $15.15); ≈ $44.15/mo. Scale later.
- 2026-10-19: ATL1 refused DOKS at every size (incl. 2 GB) → compute + MySQL move to **NYC3**, back to `s-2vcpu-4gb` (later confirmed as `s-2vcpu-4gb-amd` at $28; see ARCHITECTURE §1); total ≈ $60.30/mo at the time. Spaces buckets stay in ATL1.
- 2026-10-19: ATL1 also out of -amd and -intel 4 GB nodes → Chas chose `s-2vcpu-2gb` ($18) in ATL1; total ≈ $54.30/mo.
- 2026-10-19: ATL1 out of s-2vcpu-4gb capacity → node `s-2vcpu-4gb-amd` (~$28, total ≈ $64.30/mo). MySQL version 8.4 (only one DO offers).
- 2026-10-19: Credentials come from GitHub secrets, not TFC vars: the workflow sets the TFC workspace to local execution (TFC = state only).
- 2026-10-19: Load balancer: yes. Final variant managed MySQL (AMD) + LB ≈ $60.30/mo. History rewrite run by Chas (force-pushed).
- 2026-10-19: Managed MySQL, Premium AMD 1 GiB in ATL1, $19.30/mo (`db-amd-1vcpu-1gb`).
- 2026-10-19: Autoscaler `max_nodes = 2` approved.
- 2026-10-19: Let's Encrypt email → `yz80nomo+lepaas@gmail.com`.
- 2026-10-19: Fake test password in history: rewrite history (force-push OK) instead of `.gitleaksignore`, and add
  guards: `.githooks/pre-commit` gitleaks hook, CI fails if `.gitleaksignore` exists, no-allowlist rule in CLAUDE.md.
- 2026-10-19: All to-dos done (DNS, DO token, Spaces key, TFC workspace, GHCR already public). Chas's hand-made
  bucket `pico-paas-s3-a` (atl1) is adopted as the platform bucket via a Terraform `import` block.
- 2026-10-18: Region ATL1 (NYC3 fallback).
- 2026-10-18: No root images.
- 2026-10-18: Request 0002: credential theft out of scope (option A); DB server + Spaces kept off the public
  internet where possible; k8s API may stay public.
- 2026-10-19: Apps + platform on `raisehellpraisedale.xyz`; seaburr.io no longer used by the platform.
  (Superseded 2026-10-21: migrated to `picopaas.xyz` — see `docs/requests/0004-picopaas-domain.md`.)
- 2026-10-19: Internal-team audience; arbitrary images OK with: signups inert until approved, admin
  suspend/delete for users and apps, audit log, SMTP egress blocked. `hostUsers: false` not adopted.
- 2026-10-19: Terraform state in Terraform Cloud (org `seaburr`), like other projects.
- [x] 2026-10-20: doadmin can't kill app sessions (MySQL 1095 'not owner of thread'); the pod-roll fallback cut a 300MB fill at ~88MB. Now the platform kills sessions as the app's own user first.
