# Questions & to-dos for Chas

**This file is yours.** Write answers inline, any style; one word is fine. Tick boxes when done.
Claude reads it at the start of every session, carries answers into `docs/ARCHITECTURE.md` /
`docs/requests/`, and moves answered items to **Resolved** at the bottom. The request files have the
background on each item if you want it.

## Open questions


- **AWS account for P4 testing: ON HOLD (Chas 2026-11-03: "Hold off on AWS for the time being. I will review more in depth and let you know.").** Decisions when you resume (2026-11-02): Checklist: [deployment/aws-account-setup.md](deployment/aws-account-setup.md);
  request [0010](requests/0010-aws-test-environment.md) (PROPOSED). Answers the existing portability question (3) below
  (it supersedes nothing). Please decide: (1) **whose account and credits** (new account: $100-$200 credits for 6 months, needs the
  Paid plan for EKS; or an employer/Activate account); (2) **region** (I recommend us-east-1); (3) **caps** (proposed $5 per
  run, $25 per month); (4) spot nodes ok for non-pentest runs? (5) approve 0010 (static about $0.51/mo; a run is about $0.27/h,
  about $1.10 for 4 h). Then you do checklist Part A and B (about 1-2 h); I take it from the gate.

- **Cloud portability: AWS and GCP first (2026-11-01).** Plan: [PORTABILITY_PLAN.md](PORTABILITY_PLAN.md) (Azure deferred,
  no S3 API). Please answer: (1) AWS per-app bucket credentials: IAM user per app (recommended) or shared prefix-scoped
  keys? (2) GCP: one service account + HMAC key per app (recommended; 100-SA default quota)? (3) whose AWS/GCP accounts
  host throwaway test envs, and the per-run spend cap? (4) AWS before GCP (recommended)? (5) Helm as the self-host
  install format (recommended)? Phases P1-P3 need no new spend and can start once the plan is approved.

- **Wiki.js recipe and the Postgres connection budget (2026-11-01).** Wiki.js 2 only works with an admin override of
  `db_max_connections` = 10 (its pool is config-file only; at 5, saving pages fails). The 1 GB PG node has about 22 usable
  connections in all, so two Wiki.js apps could use most of them. Options: (a) keep it as a template with the admin step
  and approve case by case; (b) build a small wrapper image with a pool of 2 so it fits the default 5. I recommend (a) for
  now and (b) if anyone actually asks for a wiki. Background: docs/RECIPES.md "Wiki.js 2".
  source app (`overwrite_source`) needs that app suspended, but an app stopped by a project or user suspension now sits in a
  suspended project and every restore into it is refused (your rule). So that recovery path only works for an app suspended
  on its own in an active project. Options: (a) leave it (unsuspend the project, suspend the app on its own, restore);
  (b) let the admin restore route skip the project check. I recommend (a): it is rare, admin-only and explicit. (2) Members
  of a suspended project cannot get a file from their bucket: the presigned-download call is a POST, so it counts as a
  write. Backup downloads stay open. Allow GET presigns too, or leave it? I recommend leave it for now.
- **Feedback wave 2026-10-30: calls I made, say if you disagree.** (1) Project names are unique **per owner**
  (case-insensitive), not platform-wide, so nobody learns another user's project names from a 409. (2) Building the
  starter-brief demos spends the Demo project's Anthropic key (Haiku, ~$0.05 each); I'll take demo apps offline between
  builds to stay under Demo's 3-app limit. (3) "Running generated tests" will run the app's own test command inside the
  preview pod at boot (the worker still never executes code), with results fed back through the new log path.
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

- **Re-verify a DB server when only its `flavor` changes? (security-reviewer 2026-11-01, PENTEST K60, LOW)** PATCH checks the connection
  for password/CA changes but not for a flavor change, so a wrong flavor (wrong admin DB) breaks provisioning until fixed.
  Recommend verifying on flavor change too (one line). Admin-only, availability only. OK to add?

## To-dos (only you can do these)

- [ ] **Before turning Kaisen uploads on for your test project (2026-11-03):** (1) ~~managed MySQL `max_allowed_packet`~~ DONE 2026-11-03 (qa-tester, live via a throwaway Adminer app, deleted): `@@global.max_allowed_packet` = 67108864 (64 MiB, well above the 16 MiB needed), MySQL 8.4.11, node `picopaas-mysql-nyc3-2`, host = the `primary` db-server (`private-picopaas-mysql-nyc3-do-user-1298308-0...`); (2) ~~kaisen-imgproc running~~ and (3) ~~node room~~ confirmed by the successful rollout of kaisen-imgproc in deploy 41716416534 (still worth a glance at `kubectl top pod` for peak memory with an ~8 MP image). Remaining for you: turn it on in Admin, Projects, Kaisen uploads.

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

- [x] **Security review of Kaisen uploads (2026-11-03).** RESOLVED 2026-11-03 (Chas: "full send"): K79 embedded SVG bitmaps are re-encoded in the image pod (built, K79 FIXED); runtime P1b (nosniff, upload CSP, manifest check) built, needs image republish + re-pin. Details: docs/PENTEST.md K76 to K84. No HIGH/CRIT open; one MEDIUM (SVG CSS string-URL tracking beacon, K76) fixed. (a) K78: done, `sandbox` added to the SVG content CSP. (b) K79: embedded bitmaps inside SVG `<image>` are kept as-is (not re-encoded, not covered by the hash blocklist). Keep (simple) or strip/re-encode them (more code in the image pod)? Before enabling uploads on any project we still need: MySQL `max_allowed_packet` check (16 MiB), peak-RSS measurement on the imgproc pod, backup-size estimate.
- [x] **App health / Settings text labels (2026-11-03, UX sweep P2).** The two pages are linked only by gear/heart-pulse icons;
  proposal: show text labels ("Settings" / "App health") from the sm breakpoint up. Mockup (local, not in the repo):
  `/private/tmp/claude-501/-Users-cberndt-source-picopaas/1c080756-79e3-412e-91b2-bcc6607a5ca0/scratchpad/ux/labels-mockup/`
  (`health_after_1280.png`, `health_after_390.png`, `settings_after_*.png`, plus the before shots). The header buttons on App health
  (Open in Kaisen, Check for new image, Restart, Settings) and on Settings (App health) get icon + text; at 390px they wrap into two
  rows. Per-section Download/Refresh icons stay icon-only. Approve / reject / also label those? **Chas 2026-11-03: "full send, I trust you" = approved as built / proposed.**

- [x] **Projects page (F2, 2026-11-03): built per this mockup and browser-checked; tell me what to change.** Nav: Projects | Apps | Kaisen | Docs. The dropdown with the new-project and gear icons is gone. `/projects` lists your projects (role, members, apps used of the limit, links to Apps/Kaisen) with a "New project" button; click a name to open its settings and members page. On Apps and Kaisen a plain "Apps in [Personal v]" picker shows **only if you have more than one project** (most users see nothing extra). Left out of v1: a per-project "Kaisen key set" column. Mockup (local): `/private/tmp/claude-501/-Users-cberndt-source-picopaas/1c080756-79e3-412e-91b2-bcc6607a5ca0/scratchpad/ux/projects-mockup/` (projects-1280.png, apps-multi-*.png, apps-single-*.png, spec.md). **Chas 2026-11-03: "full send, I trust you" = approved as built / proposed.**

- [x] **Kaisen file uploads (answered 2026-11-03), being built as P1 (switched off).** How I read your answers: (1)(3)(5)(6)(8)(9 amended) and the amended caps: yes as written. (2) SVG: you said no to refusing it, so SVG is **allowed**, but only after strict sanitizing in the secret-less imgproc pod (allowlisted elements/attributes, no script, foreignObject, event handlers or external links), served with nosniff and a script-blocking CSP. security-reviewer will pentest it; tell me if you meant refuse after all. (7)/(10): no separate abuse contact, the Terms keep the existing contact. Uploads are a **per-project switch only a platform admin can turn on**; users see the upload section grayed out ("Not enabled for this project yet"). You turn it on for your test project. (11): yes, uploaded images are used as-is (after re-encoding) in the site; the UI just says they become public once deployed. Your original answers:
  <details>

  - [ ] **Kaisen file uploads: design is in [design/kaisen.md](design/kaisen.md) section 16 (2026-11-03); nothing built, security-reviewer goes first.** Your answers (my recommendation first):
    (1) **P1 scope**: images (png/jpeg/webp) shipped with the site + css/html/txt/md as read-only references, model sees names/sizes/dimensions and your one-line description, not pixels. OK? (yes) Yes.
    (2) **SVG**: refuse (script/XSS risk on `*.picopaas.xyz`); users export PNG or Kaisen draws icons inline as code. OK? (refuse) No.
    (3) **Re-encode every image** (strips GPS/EXIF, caps at 2560 px, kills polyglots) so "as-is" means same picture, not same bytes. OK? (yes) Yes.
    (4) **Vision** (model looks at a screenshot to copy a design): not in P1; P2 for reference images only, max 3 per run, opt-in per file, about 1.5k tokens each. Want it soon? (later)
    (5) **Limits**: 4 MiB in, 20 files and 6 MiB of assets per build, 1 MiB references, 40 MiB per project, 30 uploads/hour/user. Too tight/loose? (keep) That's more than enough for now.
    (6) **Storage in the platform DB** (like bundles), no bucket, no new cost; needs a check of the managed MySQL `max_allowed_packet`. Move to Garage/Spaces only if usage grows past ~1 GB. OK? (yes) Yes.
    (7) **Abuse**: uploads ship OFF behind an admin switch (`kaisen_uploads_enabled`), admin takedown route + search by image hash, nothing but re-encoded images is ever served. Who takes abuse reports, and what contact do the Terms name? (you). Can this not be largely the same as the current ones?
    (8) **Runtime image republish** (nosniff/CSP/manifest check in `boot.py`) needs a bootstrap re-pin from you; P1 is safe without it, OK to do it as P1b? (yes) Yes.
    (9) New dependency **Pillow** (image re-encode in a resource-limited child process). OK? (yes) Yes.
    **Security review (2026-11-03): approve with changes, details in design/kaisen.md 16.8.** Changes to your answers and extra decisions:
    (9) is **amended**: Pillow runs in a separate secret-less `kaisen-imgproc` pod (no service-account token, no DB, no egress), not a child of the API pod, because a codec exploit there could read the API's k8s token and keys. One small extra pod on the existing node, no new paid resource, about +1 day of P1 work. OK? Yes.
    (5)/(6) **amended**: pixel cap 8 MP; plus a per-user (100 MiB) and platform-wide (2 GiB) upload cap so DB growth and backup size are bounded; move blobs to Garage/Spaces at about 500 MiB real use. OK? Yes.
    (10) **Abuse and legal**: image hosting on `*.picopaas.xyz` brings illegal-content risk (reporting/preservation duties may apply). Takedown deletes the bytes and blocklists the hashes; exact-hash only, no perceptual matching. Do you want a legal check before enabling uploads for anyone but you, and which contact goes in the Terms (extends (7))? Until answered: switch stays off. Let's make this a feature I can enable on a project. It should show in the UI for users but be grayed out. I'll enable it only for my testing for now.
    (11) Assets are **public** once deployed (preview URL, public GitHub repo if pushed); private material goes in "reference" (never deployed, but sent to the AI provider when read). Fine to state that in the UI? Well, if they upload an image we can use as-is in the app why not?
    (2) SVG refusal and (3) re-encode: confirmed by review. (8) P1 before P1b: confirmed safe.
  </details>
- 2026-11-03: **Templates for the new recipes** (Chas, inline): add one-click templates for Vikunja, Umami, ntfy, IT-Tools and Excalidraw; skip Planka for now.
- 2026-11-03: **UX sweep P3s + Terms wording** (Chas, inline): Register keeps both Terms statements (checkbox + sentence); the other P3s (inline Interrupt/429 message, Kaisen header icons and Account time-zone select at 390px, one banner for template "off on this instance" warnings) go ahead. Settings/App health labels stay open pending a mockup.
- 2026-11-03: **Planka license** (Chas, in chat, after reading it): fine for users to run on PicoPaaS themselves (recipe/example, user deploys it), but PicoPaaS must never OFFER Planka as a managed service (no "managed Planka", not marketed as a platform feature).
- 2026-11-02: **K69 unmetered abandoned model calls** (Chas, inline): leave as is (manual Interrupt capped 3/build/hour; up to KAISEN_HANG_RETRIES=2 unmetered calls per run accepted).
- 2026-11-01: **Env value cap (K56)** (Chas, in chat): 8 KiB per value (largest real values are PEMs/JSON keys of 2-3 KB; a cert ref is stored as the ref text).
- 2026-10-31: **K50 restore-over-source and bucket file fetch in suspended projects** (Chas, in chat): leave both as they are (restore over the source only for an app suspended on its own in an active project; members of a suspended project can't fetch bucket files, backup downloads still work).
- 2026-10-30: **K16 web + build secrets** (Chas, in chat): option (a), a build with secrets never gets the web tools, allow-list or not (builder.py; PENTEST K16 closed).
- 2026-10-30: **K39 rollout status for all apps** (Chas, in chat): keep `deploying` until every pod runs the new version for ALL apps, so a release whose pod never gets ready fails after DEPLOY_TIMEOUT_MINUTES and rolls back instead of hiding behind the old pod.
- 2026-10-30: **Deploy gated on CI + ruleset on main** (Chas, in chat, from the factory re-review): deploy.yml waits for green CI and image on the same commit; a ruleset on main blocks force-push and deletion. Also: Dependabot steward handles PicoPaaS #29/#31/#32 after this ship and closes #30 (Node 25).
- 2026-10-30: **Runtime re-pin for static-site tests approved** (Chas, in chat): build static test support into images/kaisen-runtime (eval batch-2 finding P3) and re-pin via bootstrap.yml once green. Also: Dependabot alerts + security updates on both repos (Chas OK), lint wave (ruff, actionlint/shellcheck, ESLint) requested.
- 2026-10-30: **Restore points kept (K31):** last 5 per build is fine (Chas: "If someone is building this much they really need GH wired up"). Card now says it keeps the last N and points to GitHub output (build detail `snapshots_kept`).
- 2026-10-30: **Runtime re-pin approved** (Chas, in chat): run bootstrap.yml with the latest kaisen-runtime digest before the harness eval. Eval project: new project 'Kaisen eval' owned by the E2E test user, Anthropic key = ANT_API_KEY from ~/.picopaas-e2e.env (not Demo). First batch 3 Haiku + 1 Sonnet, report cost before the rest.

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


